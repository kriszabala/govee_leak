"""Unit tests for the Govee frame/device-list decoder.

These load ``const.py`` and ``protocol.py`` in isolation (without importing the
integration's ``__init__``, which pulls in Home Assistant) so they run under a
plain ``pytest`` with no HA install. The pure-Python protocol layer is the part
most worth covering: a regression here means leaks are silently mis-decoded.
"""
from __future__ import annotations

import base64
import importlib.util
import pathlib
import sys
import types

_DIR = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "govee_leak"
)


def _load_protocol():
    """Load protocol.py with its relative ``from .const import`` satisfied."""
    pkg_name = "_govee_leak_under_test"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(_DIR)]
        sys.modules[pkg_name] = pkg
    for mod in ("const", "protocol"):
        full = f"{pkg_name}.{mod}"
        if full in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(full, _DIR / f"{mod}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{pkg_name}.protocol"]


protocol = _load_protocol()


def make_frame(
    sno: int,
    battery: int,
    leak: bool,
    *,
    b0: int = 0xAA,
    b1: int = 0x04,
    b3: int = 0x02,
) -> str:
    """Build a base64-encoded 20-byte sensor frame for tests."""
    b = bytearray(20)
    b[0] = b0
    b[1] = b1
    b[2] = sno
    b[3] = b3
    b[5] = battery
    b[16] = 0x01 if leak else 0x00
    checksum = 0
    for i in range(19):
        checksum ^= b[i]
    b[19] = checksum
    return base64.b64encode(bytes(b)).decode()


# -- decode_frame --------------------------------------------------------- #


def test_decode_frame_wet():
    r = protocol.decode_frame(make_frame(3, 87, True))
    assert r is not None
    assert r.sno == 3
    assert r.battery == 87
    assert r.leak is True


def test_decode_frame_dry():
    r = protocol.decode_frame(make_frame(0, 100, False))
    assert r is not None
    assert r.sno == 0
    assert r.leak is False


def test_decode_frame_async_event_b0():
    # 0xEE async-event frames are valid too.
    r = protocol.decode_frame(make_frame(5, 50, True, b0=0xEE, b1=0x34))
    assert r is not None
    assert r.leak is True


def test_decode_frame_rejects_wrong_length():
    short = base64.b64encode(bytes(10)).decode()
    assert protocol.decode_frame(short) is None


def test_decode_frame_rejects_bad_b0():
    assert protocol.decode_frame(make_frame(1, 50, True, b0=0x12)) is None


def test_decode_frame_rejects_non_data_command():
    # b1 == 0x35 (alarm-notify) is not a data frame.
    assert protocol.decode_frame(make_frame(1, 50, True, b1=0x35)) is None


def test_decode_frame_rejects_non_per_sensor():
    assert protocol.decode_frame(make_frame(1, 50, True, b3=0x00)) is None


def test_decode_frame_rejects_garbage():
    assert protocol.decode_frame("not base64 @@@") is None


# -- readings_from_message ------------------------------------------------ #


def test_readings_from_message_gateway():
    msg = {
        "sku": "H5044",
        "device": "GW-MAC",
        "op": {"command": [make_frame(2, 80, True), make_frame(4, 75, False)]},
    }
    result = protocol.readings_from_message(msg)
    assert result is not None
    gateway, readings = result
    assert gateway == "GW-MAC"
    assert [r.sno for r in readings] == [2, 4]
    assert readings[0].leak is True
    assert readings[1].leak is False


def test_readings_from_message_skips_non_gateway_sku():
    msg = {"sku": "H5059", "device": "x", "op": {"command": [make_frame(1, 50, True)]}}
    assert protocol.readings_from_message(msg) is None


def test_readings_from_message_no_decodable_frames():
    msg = {"sku": "H5044", "device": "x", "op": {"command": ["garbage"]}}
    assert protocol.readings_from_message(msg) is None


def test_readings_from_message_missing_op():
    assert protocol.readings_from_message({"sku": "H5044", "device": "x"}) is None


# -- parse_leak_sensors --------------------------------------------------- #


def _device(device_id, name, sno, gw="GW", topic="GD/abc", battery=90):
    return {
        "sku": "H5059",
        "device": device_id,
        "deviceName": name,
        "deviceExt": {
            "deviceSettings": {
                "sno": sno,
                "battery": battery,
                "gatewayInfo": {"device": gw, "sku": "H5044", "topic": topic},
            }
        },
    }


def test_parse_leak_sensors_basic():
    sensors = protocol.parse_leak_sensors(
        {"devices": [_device("AA:BB", "Kitchen", 0)]}
    )
    assert len(sensors) == 1
    s = sensors[0]
    assert s.device == "AA:BB"
    assert s.name == "Kitchen"
    assert s.sno == 0
    assert s.gateway_device == "GW"
    assert s.gateway_topic == "GD/abc"
    assert s.battery == 90


def test_parse_leak_sensors_skips_non_leak_sku():
    devices = {"devices": [{"sku": "H5044", "device": "GW", "deviceExt": {}}]}
    assert protocol.parse_leak_sensors(devices) == []


def test_parse_leak_sensors_expands_json_string_settings():
    import json

    dev = _device("CC:DD", "Basement", 7)
    # Some accounts return deviceSettings as a JSON string.
    dev["deviceExt"]["deviceSettings"] = json.dumps(
        dev["deviceExt"]["deviceSettings"]
    )
    sensors = protocol.parse_leak_sensors({"devices": [dev]})
    assert len(sensors) == 1
    assert sensors[0].sno == 7
    assert sensors[0].name == "Basement"


def test_parse_leak_sensors_falls_back_to_generated_name():
    dev = _device("11:22:33:44:55", None, 1)
    dev["deviceName"] = None
    sensors = protocol.parse_leak_sensors({"devices": [dev]})
    assert sensors[0].name == "Govee Leak 44:55"


# --- H5054 (RF sensor via H5040 gateway), polled over HTTP ------------------ #
import json as _json


def _h5054(device="AA:BB:CC:DD:EE:FF:3B:71", battery=100, last_time=1700000000000,
           online=True, gwonline=True):
    return {
        "sku": "H5054",
        "device": device,
        "deviceName": "Utility Room",
        "deviceExt": {
            "deviceSettings": _json.dumps(
                {"battery": battery, "gatewayInfo": {"device": "GW:01", "sku": "H5040"}}
            ),
            "lastDeviceData": _json.dumps(
                {"online": online, "gwonline": gwonline, "lastTime": last_time}
            ),
        },
    }


def test_h5054_is_discovered_as_polled():
    sensors = protocol.parse_leak_sensors({"devices": [_h5054()]})
    assert len(sensors) == 1
    s = sensors[0]
    assert s.sku == "H5054" and s.polled
    assert s.sno == -1 and s.battery == 100
    assert s.gateway_sku == "H5040"


def test_h5059_is_not_polled():
    d = {"sku": "H5059", "device": "X", "deviceExt": {"deviceSettings": _json.dumps({"sno": 2})}}
    (s,) = protocol.parse_leak_sensors({"devices": [d]})
    assert not s.polled and s.sno == 2


def test_polled_statuses():
    st = protocol.polled_statuses({"devices": [_h5054(battery=80, gwonline=False)]})
    s = st["AA:BB:CC:DD:EE:FF:3B:71"]
    assert s.battery == 80 and s.online and not s.gateway_online
    assert s.last_time == 1700000000000


def test_polled_statuses_tolerates_missing_last_device_data():
    d = _h5054()
    del d["deviceExt"]["lastDeviceData"]
    s = protocol.polled_statuses({"devices": [d]})["AA:BB:CC:DD:EE:FF:3B:71"]
    assert s.last_time == 0 and s.online and s.gateway_online


def test_leak_from_warn_messages():
    f = protocol.leak_from_warn_messages
    assert f([]) is False
    assert f(None) is False
    assert f([{"read": True, "message": "Leakage Alert"}]) is False
    assert f([{"read": False, "message": "Leakage Alert: Utility Room"}]) is True
    assert f([{"read": False, "message": "LeakageAlert"}]) is True
    assert f([{"read": False, "message": "Low battery"}]) is False
    # missing "read" is treated as read (conservative, matches homebridge)
    assert f([{"message": "Leakage Alert"}]) is False
