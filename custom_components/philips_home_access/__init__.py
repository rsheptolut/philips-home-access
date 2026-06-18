"""The Philips Home Access integration."""
from __future__ import annotations

from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_AREACODE, DEFAULT_AREACODE, DOMAIN, PLATFORMS
from .coordinator import PhilipsCoordinator
from .homeaccess import HomeAccess, Settings
from .homeaccess import state as _state


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Philips Home Access from a config entry."""
    # Keep the library's token/device cache inside HA's config dir (not the
    # read-only component dir). Credentials live in the config entry, not here.
    _state.STATE_DIR = Path(hass.config.path(DOMAIN))

    settings = Settings(
        identifier=entry.data[CONF_EMAIL],
        credential=entry.data[CONF_PASSWORD],
        areacode=entry.data.get(CONF_AREACODE, DEFAULT_AREACODE),
    )
    client = HomeAccess(settings, session=async_get_clientsession(hass))
    coordinator = PhilipsCoordinator(hass, client)

    # Logs in + discovers; raises ConfigEntryAuthFailed / ConfigEntryNotReady.
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start_realtime()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: PhilipsCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_stop_realtime()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
