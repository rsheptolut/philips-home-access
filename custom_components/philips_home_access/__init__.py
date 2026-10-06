"""The Philips Home Access integration."""
from __future__ import annotations

from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_AREACODE, CONF_STATE_ONLY, DEFAULT_AREACODE, DOMAIN, PLATFORMS
from .coordinator import PhilipsCoordinator
from .entity import device_info_for
from .homeaccess import HomeAccess, Settings
from .homeaccess import state as _state


def _use_ha_state_dir(hass: HomeAssistant) -> None:
    """Keep the library's token/device cache inside HA's config dir (not the
    read-only component dir). Credentials live in the config entry, not here."""
    _state.STATE_DIR = Path(hass.config.path(DOMAIN))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Philips Home Access from a config entry."""
    _use_ha_state_dir(hass)

    settings = Settings(
        identifier=entry.data[CONF_EMAIL],
        credential=entry.data[CONF_PASSWORD],
        areacode=entry.data.get(CONF_AREACODE, DEFAULT_AREACODE),
    )
    client = HomeAccess(settings, session=async_get_clientsession(hass))
    coordinator = PhilipsCoordinator(
        hass, client, state_only=set(entry.options.get(CONF_STATE_ONLY, [])))

    # Logs in + discovers; raises ConfigEntryAuthFailed / ConfigEntryNotReady.
    await coordinator.async_config_entry_first_refresh()
    _register_devices(hass, entry, coordinator)
    _apply_state_only(hass, coordinator)
    await coordinator.async_start_realtime()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Rebuild the entities when the state-only choice changes."""
    await hass.config_entries.async_reload(entry.entry_id)


def _apply_state_only(hass: HomeAssistant, coordinator: PhilipsCoordinator) -> None:
    """Disable whichever of a lock's two faces doesn't apply.

    A state-only lock has a read-only lock sensor instead of a lock entity, and
    vice versa. Disabling (not deleting) the unused one keeps its entity id and
    any customisation should the choice be reversed. Only entities this
    integration disabled are re-enabled -- one the user disabled stays so.
    """
    reg = er.async_get(hass)
    for esn in coordinator.lock_esns():
        state_only = esn in coordinator.state_only
        _set_enabled(hass, reg, "lock", f"{esn}_lock", not state_only)
        _set_enabled(hass, reg, "binary_sensor", f"{esn}_lock_state", state_only)


def _set_enabled(hass: HomeAssistant, reg: er.EntityRegistry, platform: str,
                 unique_id: str, enabled: bool) -> None:
    entity_id = reg.async_get_entity_id(platform, DOMAIN, unique_id)
    if entity_id is None:
        return
    disabled_by = reg.async_get(entity_id).disabled_by
    if enabled and disabled_by is er.RegistryEntryDisabler.INTEGRATION:
        reg.async_update_entity(entity_id, disabled_by=None)
    elif not enabled and disabled_by is None:
        reg.async_update_entity(entity_id,
                                disabled_by=er.RegistryEntryDisabler.INTEGRATION)
        disabled_by = er.RegistryEntryDisabler.INTEGRATION
    # The reload leaves a "restored, unavailable" placeholder for an entity no
    # platform adds any more; it would linger until the next restart.
    st = hass.states.get(entity_id)
    if (disabled_by is er.RegistryEntryDisabler.INTEGRATION and st is not None
            and st.attributes.get("restored")):
        hass.states.async_remove(entity_id)


def _register_devices(hass: HomeAssistant, entry: ConfigEntry,
                      coordinator: PhilipsCoordinator) -> None:
    """Create every device up front, then hang each off its parent.

    An accessory belongs under the lock it is paired to, and a gateway lock
    under its gateway (which has no entities, so nothing else would create it).
    Linking by device id replaces DeviceInfo's deprecated via_device, which
    also needed the parent to exist before the child's entities were added.
    """
    reg = dr.async_get(hass)
    devices = {esn: reg.async_get_or_create(config_entry_id=entry.entry_id,
                                            **device_info_for(esn, lock))
               for esn, lock in coordinator.locks.items()}
    for esn, lock in coordinator.locks.items():
        parent = devices.get(lock.master_sn)
        if parent is not None and devices[esn].via_device_id != parent.id:
            reg.async_update_device(devices[esn].id, via_device_id=parent.id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: PhilipsCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_stop_realtime()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the account's cached session tokens and device list."""
    _use_ha_state_dir(hass)
    await _state.async_clear(entry.data[CONF_EMAIL])
