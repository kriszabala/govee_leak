"""Base entity for the GoveeLife Water Leak integration."""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_AVAILABILITY, SIGNAL_UPDATE
from .runtime import GoveeLeakRuntime, SensorState


class GoveeLeakEntity(Entity):
    """Common base wiring device info, availability, and dispatcher updates."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, runtime: GoveeLeakRuntime, device: str) -> None:
        self._runtime = runtime
        self._device = device
        self._state: SensorState = runtime.states[device]
        sensor = self._state.sensor
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device)},
            name=sensor.name,
            manufacturer="Govee",
            model=sensor.sku,
            via_device=(DOMAIN, sensor.gateway_device) if sensor.gateway_device else None,
        )

    @property
    def available(self) -> bool:
        return self._state.available

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_UPDATE}_{self._device}",
                self._handle_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_AVAILABILITY}_{self._device}",
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
