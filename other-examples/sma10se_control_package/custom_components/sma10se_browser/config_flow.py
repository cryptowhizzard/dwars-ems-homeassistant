from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SMA10SEApiClient
from .const import CONF_API_TOKEN, CONF_API_URL, CONF_NAME, DEFAULT_API_URL, DEFAULT_NAME, DOMAIN


class SMA10SEConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            api_url = str(user_input[CONF_API_URL]).rstrip("/")
            api_token = str(user_input.get(CONF_API_TOKEN, "") or "")
            name = str(user_input.get(CONF_NAME, DEFAULT_NAME) or DEFAULT_NAME)

            try:
                session = async_get_clientsession(self.hass)
                client = SMA10SEApiClient(session, api_url, api_token)
                await client.status()
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(api_url)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={CONF_NAME: name, CONF_API_URL: api_url, CONF_API_TOKEN: api_token},
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_API_URL, default=DEFAULT_API_URL): str,
                vol.Optional(CONF_API_TOKEN, default=""): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )
