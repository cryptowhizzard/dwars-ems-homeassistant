"""Config flow to configure Goodwe inverters using their local API."""

from __future__ import annotations

import logging
from typing import Any

from goodwe import Inverter, InverterError
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_PROTOCOL, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import (
    CONF_AUTO_LOAD_CONTROL,
    CONF_DEFAULT_AREA,
    CONF_KEEP_ALIVE,
    CONF_MAC,
    CONF_MODBUS_ID,
    CONF_MODEL_FAMILY,
    CONF_NETWORK_CIDR,
    CONF_NETWORK_RETRIES,
    CONF_NETWORK_TIMEOUT,
    CONF_PRE_SCAN_ENABLED,
    DEFAULT_AREA_NAME,
    DEFAULT_AUTO_LOAD_CONTROL,
    DEFAULT_MODBUS_ID,
    DEFAULT_NAME,
    DEFAULT_NETWORK_CIDR,
    DEFAULT_NETWORK_RETRIES,
    DEFAULT_NETWORK_TIMEOUT,
    DEFAULT_PRE_SCAN_ENABLED,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .discovery import (
    GoodweDiscoveryResult,
    async_connect_and_detect_port,
    async_find_inverter_by_host,
    async_scan_goodwe_inverters,
    build_updated_entry_data,
    normalize_mac,
    resolve_network_cidr,
)

PROTOCOL_CHOICES = ["UDP", "TCP"]
DISCOVERED_INVERTER = "discovered_inverter"
MANUAL_DISCOVERY_VALUE = "__manual__"

MANUAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PROTOCOL, default="UDP"): vol.In(PROTOCOL_CHOICES),
        vol.Required(CONF_MODEL_FAMILY, default="none"): str,
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT): int,
        vol.Required(CONF_PROTOCOL): vol.In(PROTOCOL_CHOICES),
        vol.Required(CONF_KEEP_ALIVE): cv.boolean,
        vol.Required(CONF_MODEL_FAMILY): str,
        vol.Optional(CONF_SCAN_INTERVAL): int,
        vol.Optional(CONF_MODBUS_ID): int,
        vol.Optional(CONF_NETWORK_RETRIES): cv.positive_int,
        vol.Optional(CONF_NETWORK_TIMEOUT): cv.positive_int,
        vol.Optional(CONF_DEFAULT_AREA): str,
        vol.Optional(CONF_AUTO_LOAD_CONTROL): cv.boolean,
        vol.Optional(CONF_PRE_SCAN_ENABLED): cv.boolean,
        vol.Optional(CONF_NETWORK_CIDR): str,
    }
)

_LOGGER = logging.getLogger(__name__)


class OptionsFlowHandler(OptionsFlow):
    """Options for the component."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Init object."""
        self.entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        host = self.entry.options.get(CONF_HOST, self.entry.data[CONF_HOST])
        port = self.entry.options.get(CONF_PORT, self.entry.data.get(CONF_PORT))
        protocol = self.entry.options.get(
            CONF_PROTOCOL, self.entry.data.get(CONF_PROTOCOL, "UDP")
        )
        keep_alive = self.entry.options.get(CONF_KEEP_ALIVE, False)
        model_family = self.entry.options.get(
            CONF_MODEL_FAMILY, self.entry.data[CONF_MODEL_FAMILY]
        )
        network_retries = self.entry.options.get(
            CONF_NETWORK_RETRIES, DEFAULT_NETWORK_RETRIES
        )
        network_timeout = self.entry.options.get(
            CONF_NETWORK_TIMEOUT, DEFAULT_NETWORK_TIMEOUT
        )
        modbus_id = self.entry.options.get(CONF_MODBUS_ID, DEFAULT_MODBUS_ID)
        default_area = self.entry.options.get(
            CONF_DEFAULT_AREA,
            self.entry.data.get(CONF_DEFAULT_AREA, DEFAULT_AREA_NAME),
        )
        auto_load_control = self.entry.options.get(
            CONF_AUTO_LOAD_CONTROL,
            self.entry.data.get(CONF_AUTO_LOAD_CONTROL, DEFAULT_AUTO_LOAD_CONTROL),
        )
        pre_scan_enabled = self.entry.options.get(
            CONF_PRE_SCAN_ENABLED,
            self.entry.data.get(CONF_PRE_SCAN_ENABLED, DEFAULT_PRE_SCAN_ENABLED),
        )
        network_cidr = self.entry.options.get(
            CONF_NETWORK_CIDR,
            self.entry.data.get(
                CONF_NETWORK_CIDR,
                resolve_network_cidr(DEFAULT_NETWORK_CIDR, host) or DEFAULT_NETWORK_CIDR,
            ),
        )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA,
                {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_PROTOCOL: protocol,
                    CONF_KEEP_ALIVE: keep_alive,
                    CONF_MODEL_FAMILY: model_family,
                    CONF_SCAN_INTERVAL: self.entry.options.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                    CONF_NETWORK_RETRIES: network_retries,
                    CONF_NETWORK_TIMEOUT: network_timeout,
                    CONF_MODBUS_ID: modbus_id,
                    CONF_DEFAULT_AREA: default_area,
                    CONF_AUTO_LOAD_CONTROL: auto_load_control,
                    CONF_PRE_SCAN_ENABLED: pre_scan_enabled,
                    CONF_NETWORK_CIDR: network_cidr,
                },
            ),
        )


class GoodweFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a Goodwe config flow."""

    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialize the GoodWe flow."""
        self._discovered_inverters: dict[str, GoodweDiscoveryResult] = {}
        self._pending_entry_data: dict[str, Any] | None = None
        self._pending_title: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlowHandler:
        """Get the options flow."""
        return OptionsFlowHandler(config_entry)

    async def async_handle_successful_connection(
        self,
        inverter: Inverter,
        host: str,
        port: int,
        protocol: str,
        mac: str | None = None,
        discovery_name: str | None = None,
    ) -> ConfigFlowResult:
        """Handle a successful connection storing its values on the entry data."""
        await self.async_set_unique_id(inverter.serial_number)

        data = build_updated_entry_data(
            {},
            host=host,
            port=port,
            protocol=protocol,
            family=type(inverter).__name__,
            mac=mac,
            discovery_name=discovery_name,
        )
        data[CONF_DEFAULT_AREA] = DEFAULT_AREA_NAME
        data[CONF_AUTO_LOAD_CONTROL] = DEFAULT_AUTO_LOAD_CONTROL
        data[CONF_PRE_SCAN_ENABLED] = DEFAULT_PRE_SCAN_ENABLED
        data[CONF_NETWORK_CIDR] = (
            resolve_network_cidr(DEFAULT_NETWORK_CIDR, host) or DEFAULT_NETWORK_CIDR
        )

        # If already configured, update host/port/protocol/MAC on discovery and abort.
        self._abort_if_unique_id_configured(updates=data)

        title = f"{DEFAULT_NAME} {inverter.serial_number}"
        return self.async_create_entry(title=title, data=data)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user.

        On the first screen we scan the local broadcast domain. If exactly one
        inverter is discovered, it is added immediately. If multiple are found,
        the user can select one. If none are found, the manual form is shown.
        """
        if user_input is not None:
            return await self.async_step_manual(user_input)

        self._discovered_inverters = await self._async_discover_unconfigured()

        if len(self._discovered_inverters) == 1:
            result = next(iter(self._discovered_inverters.values()))
            return await self._async_create_from_discovery(result)

        if len(self._discovered_inverters) > 1:
            return await self.async_step_select()

        return await self.async_step_manual()

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose from discovered inverters."""
        if user_input is not None:
            selected = user_input[DISCOVERED_INVERTER]
            if selected == MANUAL_DISCOVERY_VALUE:
                return await self.async_step_manual()
            return await self._async_create_from_discovery(
                self._discovered_inverters[selected]
            )

        options = {
            key: result.label for key, result in self._discovered_inverters.items()
        }
        options[MANUAL_DISCOVERY_VALUE] = "Manual IP address"

        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema({vol.Required(DISCOVERED_INVERTER): vol.In(options)}),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual host/IP input."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            protocol = user_input[CONF_PROTOCOL]
            model_family = user_input[CONF_MODEL_FAMILY]

            try:
                _LOGGER.debug(
                    "GoodWe connecting manually to %s protocol=%s family=%s",
                    host,
                    protocol,
                    model_family,
                )
                inverter, port, detected_protocol = await async_connect_and_detect_port(
                    host=host,
                    protocol=protocol,
                    family=model_family,
                    retries=10,
                )
            except InverterError:
                errors[CONF_HOST] = "connection_error"
            else:
                discovery = await async_find_inverter_by_host(self.hass, host)
                mac = discovery.mac if discovery else None
                discovery_name = discovery.name if discovery else None
                return await self.async_handle_successful_connection(
                    inverter,
                    host,
                    port,
                    detected_protocol,
                    mac=mac,
                    discovery_name=discovery_name,
                )

        return self.async_show_form(
            step_id="manual", data_schema=MANUAL_SCHEMA, errors=errors
        )

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Handle DHCP discovery.

        This is used both for initial discovery prompts and for updating the IP
        address of already configured devices that have a registered MAC address.
        """
        host = discovery_info.ip
        mac = normalize_mac(discovery_info.macaddress)
        discovery_name = discovery_info.hostname

        try:
            inverter, port, protocol = await async_connect_and_detect_port(
                host=host,
                protocol="UDP",
                retries=10,
            )
        except InverterError:
            return self.async_abort(reason="cannot_connect")

        await self.async_set_unique_id(inverter.serial_number)
        data = build_updated_entry_data(
            {},
            host=host,
            port=port,
            protocol=protocol,
            family=type(inverter).__name__,
            mac=mac,
            discovery_name=discovery_name,
        )
        data[CONF_DEFAULT_AREA] = DEFAULT_AREA_NAME
        data[CONF_AUTO_LOAD_CONTROL] = DEFAULT_AUTO_LOAD_CONTROL
        data[CONF_PRE_SCAN_ENABLED] = DEFAULT_PRE_SCAN_ENABLED
        data[CONF_NETWORK_CIDR] = (
            resolve_network_cidr(DEFAULT_NETWORK_CIDR, host) or DEFAULT_NETWORK_CIDR
        )

        self._abort_if_unique_id_configured(updates=data)

        self._pending_entry_data = data
        self._pending_title = f"{DEFAULT_NAME} {inverter.serial_number}"
        self.context["title_placeholders"] = {"name": self._pending_title}

        return await self.async_step_dhcp_confirm()

    async def async_step_dhcp_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm DHCP-discovered inverter setup."""
        if self._pending_entry_data is None or self._pending_title is None:
            return self.async_abort(reason="cannot_connect")

        if user_input is not None:
            return self.async_create_entry(
                title=self._pending_title,
                data=self._pending_entry_data,
            )

        return self.async_show_form(
            step_id="dhcp_confirm",
            description_placeholders={
                "host": self._pending_entry_data.get(CONF_HOST, ""),
                "mac": self._pending_entry_data.get(CONF_MAC, "unknown"),
            },
        )

    async def _async_discover_unconfigured(self) -> dict[str, GoodweDiscoveryResult]:
        """Scan and return discovered inverter candidates."""
        results = await async_scan_goodwe_inverters(self.hass)
        discovered: dict[str, GoodweDiscoveryResult] = {}

        for result in results:
            key = result.mac or result.host
            discovered[key] = result

        return discovered

    async def _async_create_from_discovery(
        self, result: GoodweDiscoveryResult
    ) -> ConfigFlowResult:
        """Connect to a discovered inverter and create a config entry."""
        try:
            inverter, port, protocol = await async_connect_and_detect_port(
                host=result.host,
                protocol="UDP",
                retries=10,
            )
        except InverterError:
            return self.async_abort(reason="cannot_connect")

        return await self.async_handle_successful_connection(
            inverter,
            result.host,
            port,
            protocol,
            mac=result.mac,
            discovery_name=result.name,
        )

    @staticmethod
    async def async_detect_inverter_port(
        host: str,
    ) -> tuple[Inverter, int]:
        """Detect the port of the inverter.

        Kept for backwards compatibility with the original integration.
        """
        inverter, port, _protocol = await async_connect_and_detect_port(
            host=host,
            protocol="UDP",
            retries=10,
        )
        return inverter, port
