"""Switch platform: on/off control for a gateway used as a smart-plug outlet.

set-gateway-power was reverse engineered from a live phone-side TLS capture
(2026-11-21): a plain signed POST, unlike open/close it is NOT encrypted --
body {esn, powerStatus: 0|1, sign, reqTime}.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, FOLLOW_UP_REFRESHES
from .coordinator import PhilipsCoordinator
from .entity import PhilipsLockEntity
from .homeaccess import CommandError, HomeAccessError
from .homeaccess.tracker import PENDING_TIMEOUT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PhilipsCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(PhilipsPlugSwitch(coordinator, esn)
                       for esn in coordinator.gateway_esns())


class PhilipsPlugSwitch(PhilipsLockEntity, SwitchEntity):
    """A gateway used as a smart-plug outlet.

    Goes through the same coordinator pending-state mechanism as the lock
    entity: the toggle flips in the UI immediately on command, confirmed (or
    corrected) once the realtime gwInf event or a follow-up poll lands.
    """

    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_name = "Power"

    def __init__(self, coordinator: PhilipsCoordinator, esn: str) -> None:
        super().__init__(coordinator, esn)
        self._attr_unique_id = f"{esn}_power"
        self._timers: set[Callable[[], None]] = set()

    @property
    def is_on(self) -> bool | None:
        st = self._lock_state
        if st and st.pending in ("turning_on", "turning_off"):
            return st.pending == "turning_on"
        return st.power == "on" if st and st.power else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_power(False)

    async def _set_power(self, on: bool) -> None:
        verb = "on" if on else "off"
        coordinator = self.coordinator
        coordinator.set_pending(self._esn, "turning_on" if on else "turning_off")
        try:
            await coordinator.client.async_set_gateway_power(self._esn, on)
        except CommandError as e:
            coordinator.set_pending(self._esn, None)
            _LOGGER.warning("set-gateway-power(%s) refused for %s: %s",
                            verb, self._esn, e)
            raise HomeAssistantError(
                f"Philips didn't accept turning {self._name()} {verb}.") from e
        except HomeAccessError as e:
            coordinator.set_pending(self._esn, None)
            raise HomeAssistantError(
                f"Could not turn {self._name()} {verb}: {e}") from e
        if not coordinator.ws_covers_lock(self._esn):
            for delay in FOLLOW_UP_REFRESHES:
                self._later(delay, lambda: self.hass.async_create_task(
                    coordinator.async_request_refresh()))
        self._later(PENDING_TIMEOUT + 1,
                    lambda: coordinator.expire_pending(self._esn))

    def _name(self) -> str:
        lock = self.coordinator.locks.get(self._esn)
        return lock.nickname if lock and lock.nickname else self._esn

    def _later(self, delay: float, action: Callable[[], Any]) -> None:
        """Run `action` on the event loop after `delay` s, unless removed first."""
        @callback
        def _fire(_now: datetime) -> None:
            self._timers.discard(cancel)
            action()

        cancel = async_call_later(self.hass, delay, _fire)
        self._timers.add(cancel)

    async def async_will_remove_from_hass(self) -> None:
        for cancel in self._timers:
            cancel()
        self._timers.clear()
        await super().async_will_remove_from_hass()
