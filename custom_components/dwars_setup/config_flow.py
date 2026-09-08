"""A singleton setup bridge, activated by the installer using the HA API."""
import voluptuous as vol
from homeassistant import config_entries


class DwarsSetupFlow(config_entries.ConfigFlow, domain="dwars_setup"):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id("dwars_setup")
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="DWARS OneShot", data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
