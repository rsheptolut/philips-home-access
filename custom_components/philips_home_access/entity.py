"""Shared base entity (device grouping + availability)."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PhilipsCoordinator
from .homeaccess import LockState


class PhilipsLockEntity(CoordinatorEntity[PhilipsCoordinator]):
    """Base for all entities of one lock (device = the lock, keyed by esn)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PhilipsCoordinator, esn: str) -> None:
        super().__init__(coordinator)
        self._esn = esn

    @property
    def _lock_state(self) -> LockState | None:
        return self.coordinator.data.get(self._esn)

    @property
    def available(self) -> bool:
        return super().available and self._lock_state is not None

    @property
    def device_info(self) -> DeviceInfo:
        lock = self.coordinator.locks.get(self._esn)
        return DeviceInfo(
            identifiers={(DOMAIN, self._esn)},
            name=lock.nickname if lock and lock.nickname else self._esn,
            manufacturer="Philips",
            model=lock.raw.get("productModel") if lock else None,
            sw_version=lock.raw.get("lockSoftwareVersion") if lock else None,
            serial_number=self._esn,
        )
