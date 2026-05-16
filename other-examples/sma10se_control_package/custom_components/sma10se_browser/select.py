from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODE_LABELS, VALID_MODES


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SMA10SEModeSelect(entry, data)])


class SMA10SEModeSelect(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_name = "Battery fallback mode"

    def __init__(self, entry: ConfigEntry, data: dict[str, Any]) -> None:
        super().__init__(data["coordinator"])
        self.entry = entry
        self.client = data["client"]
        self._attr_unique_id = f"{entry.entry_id}_battery_fallback_mode"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": data["name"],
            "manufacturer": "SMA",
            "model": "Sunny Tripower 10.0 SE",
        }

    @property
    def options(self) -> list[str]:
        return list(VALID_MODES)

    @property
    def current_option(self) -> str | None:
        data = self.coordinator.data or {}
        pending = data.get("pending_mode")
        actual = data.get("actual_mode")
        requested = data.get("requested_mode")
        return pending or actual or requested

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "labels": MODE_LABELS,
            "requested_mode": data.get("requested_mode"),
            "actual_mode": data.get("actual_mode"),
            "pending_mode": data.get("pending_mode"),
            "running": data.get("running"),
            "cooldown_remaining_s": data.get("cooldown_remaining_s"),
            "last_error": data.get("last_error"),
        }

    async def async_select_option(self, option: str) -> None:
        await self.client.set_mode(option)
        await self.coordinator.async_request_refresh()
