from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import SMA10SEApiClient
from .const import CONF_API_TOKEN, CONF_API_URL, CONF_NAME, DOMAIN, PLATFORMS, VALID_MODES

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_MODE = "set_mode"

SET_MODE_SCHEMA = vol.Schema(
    {
        vol.Required("mode"): vol.In(VALID_MODES),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = SMA10SEApiClient(
        session,
        entry.data[CONF_API_URL],
        entry.data.get(CONF_API_TOKEN, ""),
    )

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{entry.data.get(CONF_NAME, 'SMA 10SE')} status",
        update_method=client.status,
        update_interval=timedelta(seconds=30),
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "name": entry.data.get(CONF_NAME, "SMA 10SE"),
    }

    async def handle_set_mode(call: ServiceCall) -> None:
        mode = call.data["mode"]
        await client.set_mode(mode)
        await coordinator.async_request_refresh()

    # Register once globally; service uses the first configured entry.
    if not hass.services.has_service(DOMAIN, SERVICE_SET_MODE):
        async def service_router(call: ServiceCall) -> None:
            entries = hass.config_entries.async_entries(DOMAIN)
            if not entries:
                raise RuntimeError("No SMA 10SE Browser Control config entry configured")
            first = entries[0]
            data = hass.data[DOMAIN][first.entry_id]
            await data["client"].set_mode(call.data["mode"])
            await data["coordinator"].async_request_refresh()

        hass.services.async_register(DOMAIN, SERVICE_SET_MODE, service_router, schema=SET_MODE_SCHEMA)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
