"""Switch platform: on/off control for a gateway used as a smart-plug outlet.

set-gateway-power was reverse engineered from a live phone-side TLS capture
(2026-11-21): a plain signed POST, unlike open/close it is NOT encrypted --
body {esn, powerStatus: 0|1, sign, reqTime}.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import PhilipsCoordinator
from .entity import PhilipsLockEntity
from .homeaccess import CommandError, HomeAccessError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PhilipsCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(PhilipsPlugSwitch(coordinator, esn)
                       for esn in coordinator.gateway_esns())


class PhilipsPlugSwitch(PhilipsLockEntity, SwitchEntity):
    """A gateway used as a smart-plug outlet."""

    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_name = "Power"

    def __init__(self, coordinator: PhilipsCoordinator, esn: str) -> None:
        super().__init__(coordinator, esn)
        self._attr_unique_id = f"{esn}_power"

    @property
    def is_on(self) -> bool | None:
        st = self._lock_state
        return st.power == "on" if st and st.power else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_power(False)

    async def _set_power(self, on: bool) -> None:
        verb = "on" if on else "off"
        try:
            await self.coordinator.client.async_set_gateway_power(self._esn, on)
        except CommandError as e:
            _LOGGER.warning("set-gateway-power(%s) refused for %s: %s",
                            verb, self._esn, e)
            raise HomeAssistantError(
                f"Philips didn't accept turning {self._name()} {verb}.") from e
        except HomeAccessError as e:
            raise HomeAssistantError(
                f"Could not turn {self._name()} {verb}: {e}") from e
        if not self.coordinator.ws_covers_lock(self._esn):
            await self.coordinator.async_request_refresh()

    def _name(self) -> str:
        lock = self.coordinator.locks.get(self._esn)
        return lock.nickname if lock and lock.nickname else self._esn
