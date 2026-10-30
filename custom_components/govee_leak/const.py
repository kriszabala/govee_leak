"""Constants for the GoveeLife Water Leak integration."""
from __future__ import annotations

DOMAIN = "govee_leak"

CONF_CODE = "code"

# Default %: at or below this the "Low battery" binary sensor turns on.
DEFAULT_LOW_BATTERY_PCT = 20

# Dispatcher signals (suffixed with entry_id / device id at send time).
SIGNAL_UPDATE = "govee_leak_update"
SIGNAL_AVAILABILITY = "govee_leak_availability"

# H5059 sensors (via H5044/H5043) push 20-byte frames over the account MQTT
# topic. Older H5054 sensors are 433 MHz RF leaves bridged by an H5040 gateway;
# their trips never reach MQTT, so they are polled over HTTP instead.
LEAK_SKU = "H5059"  # kept for backwards compatibility
MQTT_LEAK_SKUS = {"H5059"}
POLLED_LEAK_SKUS = {"H5054"}
LEAK_SKUS = MQTT_LEAK_SKUS | POLLED_LEAK_SKUS
GATEWAY_SKUS = {"H5044", "H5043"}

# How often polled (H5054) sensors are checked. One device-list request per
# tick; warnMessage is only called when a sensor reported something new or is
# currently wet, so steady-state load is ~1 request per interval.
POLL_INTERVAL_SECONDS = 120
