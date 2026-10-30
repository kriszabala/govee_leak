"""Govee H5044-gateway / H5059-leak-sensor frame + device-list decoding.

20-byte frame (confirmed against live capture):
  b0  0xaa polled status, 0xee async event
  b1  0x04 full status dump, 0x34 leak data event, 0x35 alarm-notify (skip)
  b2  sub-device index == deviceSettings.sno (0..9)
  b3  0x02 when this is a per-sensor data frame
  b5  battery percent
  b13 leak state (older firmware only; unreliable, part of a rolling counter)
  b16 leak state mirror: 0x01 wet, 0x00 dry (reliable across firmware versions)
  b19 XOR checksum of bytes 0..18
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass

from .const import GATEWAY_SKUS, LEAK_SKUS, POLLED_LEAK_SKUS

DATA_FRAME_CMDS = {0x04, 0x34}


@dataclass
class SensorReading:
    """A decoded per-sensor data frame."""

    sno: int
    battery: int | None
    leak: bool | None


def decode_frame(b64: str) -> SensorReading | None:
    """Return a SensorReading for per-sensor data frames, else None."""
    try:
        b = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return None
    if len(b) != 20 or b[0] not in (0xAA, 0xEE) or b[1] not in DATA_FRAME_CMDS:
        return None
    if b[3] != 0x02:
        return None
    # b16 is the reliable leak mirror; b13 only matches on older firmware.
    return SensorReading(sno=b[2], battery=b[5], leak=bool(b[16]))


def readings_from_message(msg: dict) -> tuple[str, list[SensorReading]] | None:
    """Return (gateway_device, [SensorReading,...]) from an account-topic message."""
    if msg.get("sku") not in GATEWAY_SKUS:
        return None
    gateway = msg.get("device")
    commands = (msg.get("op") or {}).get("command") or []
    if not gateway or not isinstance(commands, list):
        return None
    readings = [r for c in commands if (r := decode_frame(c)) is not None]
    if not readings:
        return None
    return gateway, readings


@dataclass
class LeakSensor:
    """A physical leak sensor discovered from the account device list."""

    device: str
    name: str
    sno: int
    gateway_device: str
    gateway_sku: str
    gateway_topic: str
    battery: int | None
    sku: str = "H5059"

    @property
    def polled(self) -> bool:
        """True for RF-only sensors (H5054) whose state is fetched over HTTP."""
        return self.sku in POLLED_LEAK_SKUS


def _expand(o):
    return json.loads(o) if isinstance(o, str) else o


def parse_leak_sensors(device_list: dict) -> list[LeakSensor]:
    """Extract leak sensors (H5059 / H5054) from the device-list response."""
    out: list[LeakSensor] = []
    for d in device_list.get("devices", []):
        if d.get("sku") not in LEAK_SKUS:
            continue
        ds = _expand((d.get("deviceExt") or {}).get("deviceSettings")) or {}
        gi = ds.get("gatewayInfo") or {}
        sno = ds.get("sno")
        out.append(
            LeakSensor(
                device=d.get("device"),
                name=d.get("deviceName") or f"Govee Leak {str(d.get('device'))[-5:]}",
                sno=int(sno) if sno is not None else -1,
                gateway_device=gi.get("device", ""),
                gateway_sku=gi.get("sku", ""),
                gateway_topic=gi.get("topic", ""),
                battery=ds.get("battery"),
                sku=d.get("sku"),
            )
        )
    return out


@dataclass
class PolledStatus:
    """online/battery/last-report info for a polled (H5054) sensor."""

    online: bool
    gateway_online: bool
    battery: int | None
    last_time: int


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def polled_statuses(device_list: dict) -> dict[str, PolledStatus]:
    """Map device id -> PolledStatus for every H5054 in the device list.

    The gateway relays each H5054 report into ``lastDeviceData`` (``lastTime``
    advances whenever the sensor transmits: leak, button press, or battery).
    """
    out: dict[str, PolledStatus] = {}
    for d in device_list.get("devices", []):
        if d.get("sku") not in POLLED_LEAK_SKUS:
            continue
        ext = _expand(d.get("deviceExt")) or {}
        ds = _expand(ext.get("deviceSettings")) or {}
        ld = _expand(ext.get("lastDeviceData")) or {}
        out[d.get("device")] = PolledStatus(
            online=bool(ld.get("online", True)),
            gateway_online=bool(ld.get("gwonline", True)),
            battery=_to_int(ds.get("battery")),
            last_time=_to_int(ld.get("lastTime")) or 0,
        )
    return out


def leak_from_warn_messages(messages) -> bool:
    """True if a warnMessage history holds an unread ``LeakageAlert``.

    Same rule homebridge-govee uses for H5054. The alert stays unread (so the
    sensor reads wet) until it is acknowledged in the Govee app or via the
    integration's "Clear leak alert" button.
    """
    if not isinstance(messages, list):
        return False
    return any(
        isinstance(m, dict)
        and not m.get("read", True)
        and re.sub(r"\s+", "", str(m.get("message", "")).lower()).startswith(
            "leakagealert"
        )
        for m in messages
    )
