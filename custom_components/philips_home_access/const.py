"""Constants for the Philips Home Access integration."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "philips_home_access"
PLATFORMS = [Platform.LOCK, Platform.BINARY_SENSOR, Platform.SENSOR]

# Config entry data keys
CONF_AREACODE = "areacode"
DEFAULT_AREACODE = "61"

# Safety-net poll interval (realtime WebSocket is the primary update path).
UPDATE_INTERVAL = timedelta(minutes=5)
