"""Sensor platform: battery level, and a gateway/plug's Wi-Fi diagnostics."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import PhilipsCoordinator
from .entity import PhilipsLockEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PhilipsCoordinator = hass.data[DOMAIN][entry.entry_id]
    gateways = coordinator.gateway_esns()
    # Every tracked esn, accessories included: the door sensor runs on its own
    # battery, separate from the lock's, and both are worth surfacing.
    # (A gateway has no battery of its own.)
    entities: list[SensorEntity] = [PhilipsBatterySensor(coordinator, esn)
                                    for esn in coordinator.data
                                    if esn not in gateways]
    entities += [PhilipsWifiSignalSensor(coordinator, esn) for esn in gateways]
    entities += [PhilipsIpAddressSensor(coordinator, esn) for esn in gateways]
    async_add_entities(entities)


class PhilipsBatterySensor(PhilipsLockEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Battery"

    def __init__(self, coordinator: PhilipsCoordinator, esn: str) -> None:
        super().__init__(coordinator, esn)
        self._attr_unique_id = f"{esn}_battery"

    @property
    def native_value(self) -> int | None:
        st = self._lock_state
        return st.battery if st else None


class PhilipsWifiSignalSensor(PhilipsLockEntity, SensorEntity):
    """A gateway/plug's Wi-Fi signal strength, from gwInf reports only."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Wi-Fi signal"

    def __init__(self, coordinator: PhilipsCoordinator, esn: str) -> None:
        super().__init__(coordinator, esn)
        self._attr_unique_id = f"{esn}_wifi_signal"

    @property
    def native_value(self) -> int | None:
        st = self._lock_state
        return st.wifi_signal if st else None


class PhilipsIpAddressSensor(PhilipsLockEntity, SensorEntity):
    """A gateway/plug's current LAN IP, from gwInf reports only."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "IP address"

    def __init__(self, coordinator: PhilipsCoordinator, esn: str) -> None:
        super().__init__(coordinator, esn)
        self._attr_unique_id = f"{esn}_ip"

    @property
    def native_value(self) -> str | None:
        st = self._lock_state
        return st.ip if st else None
