from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


SENSORS = [
    ("Actual mode", "actual_mode", None, None),
    ("Requested mode", "requested_mode", None, None),
    ("Pending mode", "pending_mode", None, None),
    ("Cooldown remaining", "cooldown_remaining_s", SensorDeviceClass.DURATION, UnitOfTime.SECONDS),
    ("Retry remaining", "retry_remaining_s", SensorDeviceClass.DURATION, UnitOfTime.SECONDS),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SMA10SEStatusSensor(entry, data, name, key, device_class, unit) for name, key, device_class, unit in SENSORS])


class SMA10SEStatusSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, data: dict[str, Any], name: str, key: str, device_class: str | None, unit: str | None) -> None:
        super().__init__(data["coordinator"])
        self.entry = entry
        self.key = key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": data["name"],
            "manufacturer": "SMA",
            "model": "Sunny Tripower 10.0 SE",
        }

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        return data.get(self.key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        if self.key not in {"actual_mode", "requested_mode", "pending_mode"}:
            return {}
        return {
            "running": data.get("running"),
            "cooldown_remaining_s": data.get("cooldown_remaining_s"),
            "retry_remaining_s": data.get("retry_remaining_s"),
            "last_error": data.get("last_error"),
            "last_result": data.get("last_result"),
        }
