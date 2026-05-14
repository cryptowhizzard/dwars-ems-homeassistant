"""Config flow for the SolarEdge Modbus Multi integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

try:
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
except ImportError:  # pragma: no cover - compatibility with older HA releases
    from homeassistant.components.dhcp import DhcpServiceInfo

from .const import (
    CONF_MAC,
    DEFAULT_NAME,
    DOMAIN,
    ConfDefaultFlag,
    ConfDefaultInt,
    ConfDefaultStr,
    ConfName,
)
from .helpers import device_list_from_string, host_valid
from .network_discovery import (
    async_get_mac_from_host,
    async_probe_solaredge_modbus,
    async_scan_solaredge_modbus,
    normalize_mac,
)

_LOGGER = logging.getLogger(__name__)


def generate_config_schema(step_id: str, user_input: dict[str, Any]) -> vol.Schema:
    """Generate config flow or repair schema."""
    schema: dict[vol.Marker, Any] = {}

    if step_id == "user":
        schema |= {vol.Required(CONF_NAME, default=user_input[CONF_NAME]): cv.string}

    if step_id in ["reconfigure", "confirm", "user"]:
        schema |= {
            vol.Required(CONF_HOST, default=user_input[CONF_HOST]): cv.string,
            vol.Required(CONF_PORT, default=user_input[CONF_PORT]): vol.Coerce(int),
            vol.Required(
                f"{ConfName.DEVICE_LIST}",
                default=user_input[ConfName.DEVICE_LIST],
            ): cv.string,
        }

    return vol.Schema(schema)


def _device_list_to_string(value: Any) -> str:
    """Convert a stored device list back to the UI string format."""
    if isinstance(value, str):
        return value
    try:
        return ",".join(str(device) for device in value)
    except TypeError:
        return str(ConfDefaultStr.DEVICE_LIST)


class SolaredgeModbusMultiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SolarEdge Modbus Multi."""

    VERSION = 2
    MINOR_VERSION = 2

    _discovered: dict[str, Any] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow for SolarEdge Modbus Multi."""
        return SolaredgeModbusMultiOptionsFlowHandler()

    def _find_existing_entry(self, data: dict[str, Any], unique_id: str) -> ConfigEntry | None:
        """Find an existing entry by unique ID, MAC or host/port."""
        data_mac = normalize_mac(data.get(CONF_MAC))
        for entry in self._async_current_entries():
            entry_mac = normalize_mac(entry.data.get(CONF_MAC))
            if entry.unique_id == unique_id:
                return entry
            if data_mac and entry_mac and data_mac == entry_mac:
                return entry
            if (
                entry.data.get(CONF_HOST) == data.get(CONF_HOST)
                and entry.data.get(CONF_PORT) == data.get(CONF_PORT)
            ):
                return entry
        return None

    async def _async_validate_and_normalize(
        self,
        user_input: dict[str, Any],
        errors: dict[str, str],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Validate the user input and return normalized data + unique ID."""
        data = dict(user_input)
        data[CONF_HOST] = str(data[CONF_HOST]).lower().strip()
        data[ConfName.DEVICE_LIST] = re.sub(
            r"\s+", "", str(data[ConfName.DEVICE_LIST]), flags=re.UNICODE
        )

        try:
            inverter_list = device_list_from_string(data[ConfName.DEVICE_LIST])
            inverter_count = len(inverter_list)
        except HomeAssistantError as err:
            errors[ConfName.DEVICE_LIST] = f"{err}"
            return None, None

        if not host_valid(data[CONF_HOST]):
            errors[CONF_HOST] = "invalid_host"
            return None, None
        if not 1 <= int(data[CONF_PORT]) <= 65535:
            errors[CONF_PORT] = "invalid_tcp_port"
            return None, None
        if not 1 <= inverter_count <= 32:
            errors[ConfName.DEVICE_LIST] = "invalid_inverter_count"
            return None, None

        data[ConfName.DEVICE_LIST] = inverter_list

        mac = normalize_mac(data.get(CONF_MAC))
        if not mac:
            mac = await async_get_mac_from_host(data[CONF_HOST], int(data[CONF_PORT]))
        if mac:
            data[CONF_MAC] = mac
            unique_id = f"{mac}:{int(data[CONF_PORT])}"
        else:
            unique_id = f"{data[CONF_HOST]}:{int(data[CONF_PORT])}"

        return data, unique_id

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial config flow step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            data, unique_id = await self._async_validate_and_normalize(user_input, errors)
            if data and unique_id:
                await self.async_set_unique_id(unique_id)
                if self._find_existing_entry(data, unique_id):
                    return self.async_abort(reason="already_configured")

                return self.async_create_entry(title=data[CONF_NAME], data=data)
        else:
            user_input = {
                CONF_NAME: DEFAULT_NAME,
                CONF_HOST: "",
                CONF_PORT: ConfDefaultInt.PORT,
                ConfName.DEVICE_LIST: ConfDefaultStr.DEVICE_LIST,
            }

            # Best-effort LAN scan. If exactly one or more SolarEdge Modbus devices
            # are found, prefill the first host. The user can still change it.
            try:
                discovered = await async_scan_solaredge_modbus(
                    self.hass,
                    port=ConfDefaultInt.PORT,
                    unit_id=1,
                    limit=8,
                )
            except Exception as err:  # noqa: BLE001 - discovery should not break setup
                _LOGGER.debug("SolarEdge LAN scan failed: %s", err)
                discovered = []

            if discovered:
                first = discovered[0]
                user_input[CONF_HOST] = first["host"]
                user_input[CONF_PORT] = first.get("port") or ConfDefaultInt.PORT
                if first.get("mac"):
                    self.context.setdefault("title_placeholders", {})["mac"] = first["mac"]

        return self.async_show_form(
            step_id="user",
            data_schema=generate_config_schema("user", user_input),
            errors=errors,
        )

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> FlowResult:
        """Handle DHCP discovery and IP updates."""
        host = getattr(discovery_info, "ip", None)
        mac = normalize_mac(getattr(discovery_info, "macaddress", None))
        if not host or not mac:
            return self.async_abort(reason="not_solaredge")

        port = int(ConfDefaultInt.PORT)
        unique_id = f"{mac}:{port}"
        await self.async_set_unique_id(unique_id)

        data = {
            CONF_NAME: DEFAULT_NAME,
            CONF_HOST: str(host),
            CONF_PORT: port,
            ConfName.DEVICE_LIST: str(ConfDefaultStr.DEVICE_LIST),
            CONF_MAC: mac,
        }

        existing_entry = self._find_existing_entry(data, unique_id)
        if existing_entry is not None:
            if existing_entry.data.get(CONF_HOST) != host:
                self.hass.config_entries.async_update_entry(
                    existing_entry,
                    unique_id=unique_id,
                    data={**existing_entry.data, CONF_HOST: str(host), CONF_MAC: mac},
                )
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(existing_entry.entry_id)
                )
                return self.async_abort(reason="updated_ip")
            return self.async_abort(reason="already_configured")

        # Only offer a new discovered entry if Modbus/SunSpec responds.
        if not await async_probe_solaredge_modbus(str(host), port=port, unit_id=1):
            return self.async_abort(reason="not_solaredge")

        self._discovered = data
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm a discovered SolarEdge Modbus device."""
        errors: dict[str, str] = {}
        defaults = self._discovered or {
            CONF_NAME: DEFAULT_NAME,
            CONF_HOST: "",
            CONF_PORT: ConfDefaultInt.PORT,
            ConfName.DEVICE_LIST: ConfDefaultStr.DEVICE_LIST,
        }

        if user_input is not None:
            candidate = {**defaults, **user_input}
            data, unique_id = await self._async_validate_and_normalize(candidate, errors)
            if data and unique_id:
                await self.async_set_unique_id(unique_id)
                if self._find_existing_entry(data, unique_id):
                    return self.async_abort(reason="already_configured")
                return self.async_create_entry(title=data[CONF_NAME], data=data)

        return self.async_show_form(
            step_id="confirm",
            data_schema=generate_config_schema("confirm", defaults),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the reconfigure flow step."""
        errors: dict[str, str] = {}
        config_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )

        if config_entry is None:
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            data, unique_id = await self._async_validate_and_normalize(user_input, errors)
            if data and unique_id:
                existing_entry = self._find_existing_entry(data, unique_id)
                if existing_entry is not None and existing_entry.entry_id != config_entry.entry_id:
                    errors[CONF_HOST] = "already_configured"
                    errors[CONF_PORT] = "already_configured"
                else:
                    return self.async_update_reload_and_abort(
                        config_entry,
                        unique_id=unique_id,
                        data={**config_entry.data, **data},
                        reason="reconfigure_successful",
                    )
        else:
            user_input = {
                CONF_HOST: config_entry.data.get(CONF_HOST),
                CONF_PORT: config_entry.data.get(CONF_PORT, ConfDefaultInt.PORT),
                ConfName.DEVICE_LIST: _device_list_to_string(
                    config_entry.data.get(ConfName.DEVICE_LIST, ConfDefaultStr.DEVICE_LIST)
                ),
            }

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=generate_config_schema("reconfigure", user_input),
            errors=errors,
        )


class SolaredgeModbusMultiOptionsFlowHandler(OptionsFlow):
    """Handle an options flow for SolarEdge Modbus Multi."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial options flow step."""
        errors = {}

        if user_input is not None:
            if user_input[CONF_SCAN_INTERVAL] < 1:
                errors[CONF_SCAN_INTERVAL] = "invalid_scan_interval"
            elif user_input[CONF_SCAN_INTERVAL] > 86400:
                errors[CONF_SCAN_INTERVAL] = "invalid_scan_interval"
            elif user_input[ConfName.SLEEP_AFTER_WRITE] < 0:
                errors[ConfName.SLEEP_AFTER_WRITE] = "invalid_sleep_interval"
            elif user_input[ConfName.SLEEP_AFTER_WRITE] > 60:
                errors[ConfName.SLEEP_AFTER_WRITE] = "invalid_sleep_interval"
            else:
                if user_input[ConfName.DETECT_BATTERIES] is True:
                    self.init_info = user_input
                    return await self.async_step_battery_options()
                else:
                    if user_input[ConfName.ADV_PWR_CONTROL] is True:
                        self.init_info = user_input
                        return await self.async_step_adv_pwr_ctl()

                    else:
                        return self.async_create_entry(title="", data=user_input)

        else:
            user_input = {
                CONF_SCAN_INTERVAL: self.config_entry.options.get(
                    CONF_SCAN_INTERVAL, ConfDefaultInt.SCAN_INTERVAL
                ),
                ConfName.KEEP_MODBUS_OPEN: self.config_entry.options.get(
                    ConfName.KEEP_MODBUS_OPEN, bool(ConfDefaultFlag.KEEP_MODBUS_OPEN)
                ),
                ConfName.DETECT_METERS: self.config_entry.options.get(
                    ConfName.DETECT_METERS, bool(ConfDefaultFlag.DETECT_METERS)
                ),
                ConfName.DETECT_BATTERIES: self.config_entry.options.get(
                    ConfName.DETECT_BATTERIES, bool(ConfDefaultFlag.DETECT_BATTERIES)
                ),
                ConfName.DETECT_EXTRAS: self.config_entry.options.get(
                    ConfName.DETECT_EXTRAS, bool(ConfDefaultFlag.DETECT_EXTRAS)
                ),
                ConfName.ADV_PWR_CONTROL: self.config_entry.options.get(
                    ConfName.ADV_PWR_CONTROL, bool(ConfDefaultFlag.ADV_PWR_CONTROL)
                ),
                ConfName.SLEEP_AFTER_WRITE: self.config_entry.options.get(
                    ConfName.SLEEP_AFTER_WRITE, ConfDefaultInt.SLEEP_AFTER_WRITE
                ),
            }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=user_input[CONF_SCAN_INTERVAL],
                    ): vol.Coerce(int),
                    vol.Optional(
                        f"{ConfName.KEEP_MODBUS_OPEN}",
                        default=user_input[ConfName.KEEP_MODBUS_OPEN],
                    ): cv.boolean,
                    vol.Optional(
                        f"{ConfName.DETECT_METERS}",
                        default=user_input[ConfName.DETECT_METERS],
                    ): cv.boolean,
                    vol.Optional(
                        f"{ConfName.DETECT_BATTERIES}",
                        default=user_input[ConfName.DETECT_BATTERIES],
                    ): cv.boolean,
                    vol.Optional(
                        f"{ConfName.DETECT_EXTRAS}",
                        default=user_input[ConfName.DETECT_EXTRAS],
                    ): cv.boolean,
                    vol.Optional(
                        f"{ConfName.ADV_PWR_CONTROL}",
                        default=user_input[ConfName.ADV_PWR_CONTROL],
                    ): cv.boolean,
                    vol.Optional(
                        f"{ConfName.SLEEP_AFTER_WRITE}",
                        default=user_input[ConfName.SLEEP_AFTER_WRITE],
                    ): vol.Coerce(int),
                },
            ),
            errors=errors,
        )

    async def async_step_battery_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Battery Options"""
        errors = {}

        if user_input is not None:
            if user_input[ConfName.BATTERY_RATING_ADJUST] < 0:
                errors[ConfName.BATTERY_RATING_ADJUST] = "invalid_percent"
            elif user_input[ConfName.BATTERY_RATING_ADJUST] > 100:
                errors[ConfName.BATTERY_RATING_ADJUST] = "invalid_percent"
            else:
                if self.init_info[ConfName.ADV_PWR_CONTROL] is True:
                    self.init_info = {**self.init_info, **user_input}
                    return await self.async_step_adv_pwr_ctl()

                return self.async_create_entry(
                    title="", data={**self.init_info, **user_input}
                )

        else:
            user_input = {
                ConfName.ALLOW_BATTERY_ENERGY_RESET: self.config_entry.options.get(
                    ConfName.ALLOW_BATTERY_ENERGY_RESET,
                    bool(ConfDefaultFlag.ALLOW_BATTERY_ENERGY_RESET),
                ),
                ConfName.BATTERY_ENERGY_RESET_CYCLES: self.config_entry.options.get(
                    ConfName.BATTERY_ENERGY_RESET_CYCLES,
                    ConfDefaultInt.BATTERY_ENERGY_RESET_CYCLES,
                ),
                ConfName.BATTERY_RATING_ADJUST: self.config_entry.options.get(
                    ConfName.BATTERY_RATING_ADJUST,
                    ConfDefaultInt.BATTERY_RATING_ADJUST,
                ),
            }

        return self.async_show_form(
            step_id="battery_options",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        f"{ConfName.ALLOW_BATTERY_ENERGY_RESET}",
                        default=user_input[ConfName.ALLOW_BATTERY_ENERGY_RESET],
                    ): cv.boolean,
                    vol.Optional(
                        f"{ConfName.BATTERY_ENERGY_RESET_CYCLES}",
                        default=user_input[ConfName.BATTERY_ENERGY_RESET_CYCLES],
                    ): vol.Coerce(int),
                    vol.Optional(
                        f"{ConfName.BATTERY_RATING_ADJUST}",
                        default=user_input[ConfName.BATTERY_RATING_ADJUST],
                    ): vol.Coerce(int),
                }
            ),
            errors=errors,
        )

    async def async_step_adv_pwr_ctl(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Power Control Options"""
        errors = {}

        if user_input is not None:
            return self.async_create_entry(
                title="", data={**self.init_info, **user_input}
            )

        else:
            user_input = {
                ConfName.ADV_STORAGE_CONTROL: self.config_entry.options.get(
                    ConfName.ADV_STORAGE_CONTROL,
                    bool(ConfDefaultFlag.ADV_STORAGE_CONTROL),
                ),
                ConfName.ADV_SITE_LIMIT_CONTROL: self.config_entry.options.get(
                    ConfName.ADV_SITE_LIMIT_CONTROL,
                    bool(ConfDefaultFlag.ADV_SITE_LIMIT_CONTROL),
                ),
            }

        return self.async_show_form(
            step_id="adv_pwr_ctl",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        f"{ConfName.ADV_STORAGE_CONTROL}",
                        default=user_input[ConfName.ADV_STORAGE_CONTROL],
                    ): cv.boolean,
                    vol.Required(
                        f"{ConfName.ADV_SITE_LIMIT_CONTROL}",
                        default=user_input[ConfName.ADV_SITE_LIMIT_CONTROL],
                    ): cv.boolean,
                }
            ),
            errors=errors,
        )
