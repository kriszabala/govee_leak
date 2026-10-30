"""Runtime state holder for the GoveeLife Water Leak integration."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .api import GoveeAuthError, GoveeCloud, GoveeCreds
from .const import POLL_INTERVAL_SECONDS, SIGNAL_AVAILABILITY, SIGNAL_UPDATE
from .iot import GoveeIotClient
from .protocol import (
    LeakSensor,
    SensorReading,
    leak_from_warn_messages,
    parse_leak_sensors,
    polled_statuses,
)

_LOGGER = logging.getLogger(__name__)

# Govee's IoT cert/token are refreshed periodically by the app; re-auth daily
# so a stale cert can't silently kill the stream.
REAUTH_INTERVAL = timedelta(hours=12)

# Watchdog: how often to check the stream and send a liveness poll.
WATCHDOG_INTERVAL = timedelta(minutes=5)
# If no inbound traffic for this long (even our own polls go unanswered) the
# socket is considered dead and we force a full reconnect.
STALE_AFTER = timedelta(minutes=15)

# H5054 sensors (RF via H5040 gateway) are polled over HTTP on this cadence.
POLL_INTERVAL = timedelta(seconds=POLL_INTERVAL_SECONDS)


@dataclass
class SensorState:
    """Latest known state for one leak sensor."""

    sensor: LeakSensor
    leak: bool | None = None
    battery: int | None = None
    available: bool = False


@dataclass
class _SensorData:
    sensors: list[LeakSensor] = field(default_factory=list)


class GoveeLeakRuntime:
    """Owns the cloud auth, device list, and live IoT stream for one entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        email: str,
        password: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._cloud = GoveeCloud(email, password)
        self._iot: GoveeIotClient | None = None
        self._creds: GoveeCreds | None = None
        self._cancel_reauth = None
        self._cancel_watchdog = None
        self._cancel_poll = None
        # Serialises HTTP polls and "clear leak" so they can't interleave.
        self._poll_lock = asyncio.Lock()
        # device id -> last lastTime seen for polled (H5054) sensors
        self._last_report: dict[str, int] = {}
        # device id -> SensorState
        self.states: dict[str, SensorState] = {}
        # (gateway_device, sno) -> device id
        self._route: dict[tuple[str, int], str] = {}

    @property
    def sensors(self) -> list[LeakSensor]:
        return [s.sensor for s in self.states.values()]

    @property
    def _has_mqtt_sensors(self) -> bool:
        return any(not s.sensor.polled for s in self.states.values())

    @property
    def _has_polled_sensors(self) -> bool:
        return any(s.sensor.polled for s in self.states.values())

    async def async_setup(self) -> None:
        """Authenticate, load devices, and start the IoT stream."""
        creds = await self.hass.async_add_executor_job(self._cloud.authenticate, None)
        self._creds = creds
        device_list = await self.hass.async_add_executor_job(
            self._cloud.device_list, creds.token
        )
        for sensor in parse_leak_sensors(device_list):
            self.states[sensor.device] = SensorState(
                sensor=sensor, battery=sensor.battery
            )
            if not sensor.polled:
                self._route[(sensor.gateway_device, sensor.sno)] = sensor.device

        _LOGGER.info(
            "Discovered %d Govee leak sensors (%d polled over HTTP)",
            len(self.states),
            sum(1 for s in self.states.values() if s.sensor.polled),
        )
        await self._async_start_iot(creds)
        if self._has_polled_sensors:
            # Seed state right away so H5054 sensors don't sit at "unknown".
            await self._async_poll_http(device_list=device_list)
            self._cancel_poll = async_track_time_interval(
                self.hass, self._async_poll_http, POLL_INTERVAL
            )
        self._cancel_reauth = async_track_time_interval(
            self.hass, self._async_reauth, REAUTH_INTERVAL
        )
        self._cancel_watchdog = async_track_time_interval(
            self.hass, self._async_watchdog, WATCHDOG_INTERVAL
        )

    async def _async_start_iot(self, creds: GoveeCreds) -> None:
        if not self._has_mqtt_sensors:
            # Only polled (H5054) sensors: nothing arrives on MQTT, and an idle
            # stream would trip the watchdog into re-authenticating every
            # STALE_AFTER. Skip it.
            return
        poll_topics = sorted(
            {
                s.sensor.gateway_topic
                for s in self.states.values()
                if s.sensor.gateway_topic
            }
        )
        self._iot = GoveeIotClient(
            creds,
            on_readings=self._handle_readings_threadsafe,
            on_connection=self._handle_connection_threadsafe,
            poll_topics=poll_topics,
        )
        # Building the SSL context (load_default_certs / load_cert_chain) is
        # blocking, so start the client in the executor.
        await self.hass.async_add_executor_job(self._iot.start)

    async def _async_reauth(self, _now=None) -> None:
        """Refresh credentials and restart the IoT connection."""
        await self._async_restart_iot(reauth=True)

    async def _async_restart_iot(self, *, reauth: bool) -> None:
        """Tear down and restart the IoT stream, optionally re-authenticating."""
        if reauth or self._creds is None:
            try:
                creds = await self.hass.async_add_executor_job(
                    self._cloud.authenticate, None
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Govee re-auth failed: %s", err)
                self._mark_all_unavailable()
                return
            self._creds = creds
        if self._iot is not None:
            await self.hass.async_add_executor_job(self._iot.stop)
            self._iot = None
        try:
            await self._async_start_iot(self._creds)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Govee IoT restart failed: %s", err)
            self._mark_all_unavailable()
            return
        _LOGGER.debug("Govee IoT stream restarted (reauth=%s)", reauth)

    async def _async_watchdog(self, _now) -> None:
        """Detect a silently dead stream and recover it.

        paho auto-reconnects on a clean disconnect, but a half-open socket can
        stall without notice. Each tick we send a liveness poll; if nothing has
        come back for STALE_AFTER we force a full reconnect (with re-auth, in
        case the cert/token expired).
        """
        iot = self._iot
        if iot is None:
            return
        age = iot.last_activity_age()
        if age > STALE_AFTER.total_seconds():
            _LOGGER.warning(
                "No Govee IoT traffic for %.0fs; forcing reconnect", age
            )
            await self._async_restart_iot(reauth=True)
            return
        # Stream looks alive; nudge the gateway so states stay fresh.
        await self.hass.async_add_executor_job(iot.poll_now)

    async def async_poll_now(self) -> None:
        """Manually request a fresh status dump from the gateway(s)."""
        if self._has_polled_sensors:
            # Force a warnMessage read for every polled sensor.
            self._last_report.clear()
            await self._async_poll_http()
        iot = self._iot
        if iot is None:
            return
        published = await self.hass.async_add_executor_job(iot.poll_now)
        if not published:
            _LOGGER.debug("Refresh requested while disconnected; reconnecting")
            await self._async_restart_iot(reauth=False)

    # -- HTTP polling for H5054 (RF sensor via H5040 gateway) ------------- #
    async def _async_poll_http(self, _now=None, *, device_list: dict | None = None) -> None:
        """Refresh online/battery/leak for polled sensors.

        One device-list request per tick. warnMessage is only fetched for a
        sensor when its lastTime advanced (it transmitted something) or it is
        currently wet/unknown (to notice the alert being read in the app).
        """
        if self._creds is None:
            return
        async with self._poll_lock:
            token = self._creds.token
            try:
                if device_list is None:
                    device_list = await self.hass.async_add_executor_job(
                        self._cloud.device_list, token
                    )
                statuses = polled_statuses(device_list)
                for device, state in self.states.items():
                    if not state.sensor.polled:
                        continue
                    st = statuses.get(device)
                    if st is None:
                        _LOGGER.debug("Polled sensor %s missing from device list", device)
                        continue
                    prev = self._last_report.get(device, 0)
                    if st.last_time > prev or state.leak is not False:
                        msgs = await self.hass.async_add_executor_job(
                            self._cloud.warn_messages, token, device, state.sensor.sku
                        )
                        _LOGGER.debug(
                            "warnMessage %s: %d entries, first=%s",
                            device,
                            len(msgs),
                            msgs[:3],
                        )
                        state.leak = leak_from_warn_messages(msgs)
                        self._last_report[device] = st.last_time
                    if st.battery is not None:
                        state.battery = st.battery
                    state.available = st.online and st.gateway_online
                    _LOGGER.debug(
                        "Polled %s: leak=%s battery=%s online=%s gw_online=%s lastTime=%s",
                        device,
                        state.leak,
                        state.battery,
                        st.online,
                        st.gateway_online,
                        st.last_time,
                    )
                    async_dispatcher_send(self.hass, f"{SIGNAL_UPDATE}_{device}")
            except GoveeAuthError as err:
                _LOGGER.info("Govee token rejected during poll (%s); re-authenticating", err)
                self.hass.async_create_task(self._async_restart_iot(reauth=True))
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Govee H5054 poll failed: %s", err)

    async def async_clear_leak(self, device: str) -> None:
        """Mark a polled sensor's leak alerts read (same as the app's "Read")."""
        state = self.states.get(device)
        if state is None or not state.sensor.polled or self._creds is None:
            return
        async with self._poll_lock:
            await self.hass.async_add_executor_job(
                self._cloud.warn_lifted, self._creds.token, device, state.sensor.sku
            )
            state.leak = False
            # Re-read on the next tick to confirm (a still-wet sensor re-alerts).
            self._last_report.pop(device, None)
            async_dispatcher_send(self.hass, f"{SIGNAL_UPDATE}_{device}")

    # -- threadsafe bridges (called from the paho thread) ----------------- #
    def _handle_readings_threadsafe(
        self, gateway: str, readings: list[SensorReading]
    ) -> None:
        self.hass.loop.call_soon_threadsafe(self._apply_readings, gateway, readings)

    def _handle_connection_threadsafe(self, connected: bool) -> None:
        self.hass.loop.call_soon_threadsafe(self._apply_connection, connected)

    @callback
    def _apply_readings(self, gateway: str, readings: list[SensorReading]) -> None:
        for reading in readings:
            device = self._route.get((gateway, reading.sno))
            if device is None:
                _LOGGER.debug(
                    "Reading for unknown sensor gw=%s sno=%s", gateway, reading.sno
                )
                continue
            state = self.states[device]
            if reading.leak is not None:
                state.leak = reading.leak
            if reading.battery is not None:
                state.battery = reading.battery
            state.available = True
            async_dispatcher_send(self.hass, f"{SIGNAL_UPDATE}_{device}")

    @callback
    def _apply_connection(self, connected: bool) -> None:
        for device, state in self.states.items():
            if state.sensor.polled:
                # Availability of polled sensors comes from the HTTP poll.
                continue
            state.available = connected
            async_dispatcher_send(self.hass, f"{SIGNAL_AVAILABILITY}_{device}")

    def _mark_all_unavailable(self) -> None:
        """Flag every MQTT sensor unavailable (called from the HA loop)."""
        self._handle_connection_threadsafe(False)

    async def async_shutdown(self) -> None:
        if self._cancel_reauth is not None:
            self._cancel_reauth()
            self._cancel_reauth = None
        if self._cancel_watchdog is not None:
            self._cancel_watchdog()
            self._cancel_watchdog = None
        if self._cancel_poll is not None:
            self._cancel_poll()
            self._cancel_poll = None
        if self._iot is not None:
            await self.hass.async_add_executor_job(self._iot.stop)
            self._iot = None
