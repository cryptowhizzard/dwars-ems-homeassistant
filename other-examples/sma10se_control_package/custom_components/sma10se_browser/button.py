from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODE_CHARGE, MODE_DISCHARGE, MODE_OFF

BUTTONS = [
    ("Set off / standby", MODE_OFF),
    ("Set charge", MODE_CHARGE),
    ("Set discharge", MODE_DISCHARGE),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SMA10SEModeButton(entry, data, name, mode) for name, mode in BUTTONS])


class SMA10SEModeButton(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, data: dict[str, Any], name: str, mode: str) -> None:
        super().__init__(data["coordinator"])
        self.entry = entry
        self.client = data["client"]
        self.mode = mode
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_button_{mode}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": data["name"],
            "manufacturer": "SMA",
            "model": "Sunny Tripower 10.0 SE",
        }

    async def async_press(self) -> None:
        await self.client.set_mode(self.mode)
        await self.coordinator.async_request_refresh()
