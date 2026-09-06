"""Config flow to configure Goodwe inverters using their local API."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any

from goodwe import Inverter, InverterError
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_PROTOCOL, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

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


def _normalise_serial(value: Any) -> str:
    """Return a stable serial-number comparison value."""
    return str(value or "").strip("\x00 \t\r\n").upper()


def _entry_value(entry: ConfigEntry, key: str, default: Any = None) -> Any:
    """Read an entry value, respecting options that override entry data."""
    return entry.options.get(key, entry.data.get(key, default))


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
        """Create one config entry for one positively identified inverter.

        User-initiated setup must never repoint an existing config entry.  The
        previous implementation passed ``updates=data`` to
        ``_abort_if_unique_id_configured``.  When a scan selected an existing
        inverter, that could overwrite its stored host before showing
        "already configured".  DHCP recovery still updates an existing entry in
        ``async_step_dhcp`` where that behaviour is intentional.
        """
        serial_number = str(
            getattr(inverter, "serial_number", "") or ""
        ).strip("\x00 \t\r\n")
        if not serial_number:
            _LOGGER.warning("GoodWe at %s connected without a serial number", host)
            return self.async_abort(reason="missing_serial")

        existing_entry = self._entry_for_serial(serial_number)
        if existing_entry is not None:
            existing_host = str(_entry_value(existing_entry, CONF_HOST, "unknown"))
            _LOGGER.info(
                "GoodWe %s at %s is already configured as entry %s at %s",
                serial_number,
                host,
                existing_entry.entry_id,
                existing_host,
            )
            return self.async_abort(
                reason="already_configured_inverter",
                description_placeholders={
                    "serial": serial_number,
                    "existing_host": existing_host,
                    "attempted_host": host,
                },
            )

        await self.async_set_unique_id(serial_number)

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

        # Catch a concurrent flow for the same serial, but do not mutate another
        # inverter entry from a user/manual discovery flow.
        self._abort_if_unique_id_configured()

        title = f"{DEFAULT_NAME} {serial_number}"
        return self.async_create_entry(title=title, data=data)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user.

        The scan only offers inverters that are not configured yet.  Even when
        exactly one candidate is found, a selection screen is shown so the user
        can always choose manual IP entry for a second inverter that does not
        answer GoodWe broadcast discovery.
        """
        if user_input is not None:
            return await self.async_step_manual(user_input)

        self._discovered_inverters = await self._async_discover_unconfigured()

        if self._discovered_inverters:
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
            model_family_input = str(user_input[CONF_MODEL_FAMILY]).strip()
            model_family = (
                None
                if model_family_input.lower() in {"", "auto", "none"}
                else model_family_input
            )

            try:
                _LOGGER.debug(
                    "GoodWe connecting manually to %s protocol=%s family=%s",
                    host,
                    protocol,
                    model_family or "auto",
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

    def _configured_entries(self) -> list[ConfigEntry]:
        """Return every GoodWe config entry, including unloaded entries."""
        return list(self.hass.config_entries.async_entries(DOMAIN))

    def _configured_identity_sets(self) -> tuple[set[str], set[str], set[str]]:
        """Return configured serials, hosts and MAC addresses."""
        serials: set[str] = set()
        hosts: set[str] = set()
        macs: set[str] = set()

        for entry in self._configured_entries():
            serial = _normalise_serial(entry.unique_id)
            if serial:
                serials.add(serial)

            host = str(_entry_value(entry, CONF_HOST, "") or "").strip()
            if host:
                hosts.add(host)

            mac = normalize_mac(_entry_value(entry, CONF_MAC))
            if mac:
                macs.add(mac)

        return serials, hosts, macs

    def _entry_for_serial(self, serial_number: str) -> ConfigEntry | None:
        """Find an existing entry by physical inverter serial number."""
        wanted = _normalise_serial(serial_number)
        for entry in self._configured_entries():
            if _normalise_serial(entry.unique_id) == wanted:
                return entry
        return None

    async def _async_scan_networks(self) -> list[GoodweDiscoveryResult]:
        """Scan the local subnet(s), seeded by already configured inverter IPs."""
        _serials, configured_hosts, _macs = self._configured_identity_sets()
        scopes: list[tuple[str | None, str | None]] = []
        seen_cidrs: set[str] = set()

        for host in sorted(configured_hosts):
            cidr = resolve_network_cidr(None, host)
            if cidr and cidr not in seen_cidrs:
                seen_cidrs.add(cidr)
                scopes.append((cidr, host))

        # Usually all inverters are in one subnet.  Bound the number of scans in
        # case stale entries from many historic networks still exist.
        if not scopes:
            scopes = [(None, None)]
        else:
            scopes = scopes[:4]

        scan_results = await asyncio.gather(
            *(
                async_scan_goodwe_inverters(
                    self.hass,
                    pre_scan_enabled=DEFAULT_PRE_SCAN_ENABLED,
                    network_cidr=cidr,
                    preferred_host=host,
                )
                for cidr, host in scopes
            ),
            return_exceptions=True,
        )

        merged: dict[str, GoodweDiscoveryResult] = {}
        for result_set in scan_results:
            if isinstance(result_set, Exception):
                _LOGGER.warning("GoodWe network scan failed: %s", result_set)
                continue
            for result in result_set:
                current = merged.get(result.host)
                if current is None:
                    merged[result.host] = result
                    continue
                merged[result.host] = GoodweDiscoveryResult(
                    host=result.host,
                    mac=current.mac or result.mac,
                    name=current.name or result.name,
                )

        return list(merged.values())

    async def _async_verify_candidate(
        self, result: GoodweDiscoveryResult
    ) -> GoodweDiscoveryResult | None:
        """Positively identify one scan candidate and read its serial number."""
        preferred_protocol = (
            "TCP" if result.name == "Modbus/TCP candidate" else "UDP"
        )
        try:
            inverter, port, protocol = await asyncio.wait_for(
                async_connect_and_detect_port(
                    host=result.host,
                    protocol=preferred_protocol,
                    timeout=1,
                    retries=1,
                ),
                timeout=6,
            )
        except (InverterError, TimeoutError) as err:
            _LOGGER.debug(
                "Ignoring scan candidate %s; it is not a reachable GoodWe inverter: %s",
                result.host,
                err,
            )
            return None

        serial_number = str(
            getattr(inverter, "serial_number", "") or ""
        ).strip("\x00 \t\r\n")
        if not serial_number:
            _LOGGER.warning(
                "Ignoring GoodWe scan candidate %s because it returned no serial number",
                result.host,
            )
            return None

        return GoodweDiscoveryResult(
            host=result.host,
            mac=result.mac,
            name=result.name,
            serial_number=serial_number,
            model_name=str(getattr(inverter, "model_name", "") or "").strip() or None,
            model_family=type(inverter).__name__,
            port=port,
            protocol=protocol,
        )

    async def _async_discover_unconfigured(self) -> dict[str, GoodweDiscoveryResult]:
        """Scan, identify and return only not-yet-configured inverters."""
        configured_serials, configured_hosts, configured_macs = (
            self._configured_identity_sets()
        )
        results = await self._async_scan_networks()

        # Verify every candidate by physical serial number.  A host address is not
        # an identity: DHCP can reuse an old address and an earlier 0.9.9.32 flow
        # could even have overwritten the stored host before aborting.  Filtering
        # on host here would therefore be capable of hiding the second inverter.
        # The serial-number filter below is authoritative.
        _LOGGER.debug(
            "GoodWe multi-inverter scan: %s raw candidates; configured hosts=%s "
            "configured MACs=%s configured serials=%s",
            len(results),
            sorted(configured_hosts),
            sorted(configured_macs),
            sorted(configured_serials),
        )

        probe_semaphore = asyncio.Semaphore(32)

        async def _bounded_verify(
            result: GoodweDiscoveryResult,
        ) -> GoodweDiscoveryResult | None:
            async with probe_semaphore:
                return await self._async_verify_candidate(result)

        verified_results = await asyncio.gather(
            *(_bounded_verify(result) for result in results),
            return_exceptions=True,
        )

        discovered: dict[str, GoodweDiscoveryResult] = {}
        for verified in verified_results:
            if isinstance(verified, Exception):
                _LOGGER.warning("GoodWe candidate verification failed: %s", verified)
                continue
            if verified is None:
                continue

            serial = _normalise_serial(verified.serial_number)
            if serial in configured_serials:
                _LOGGER.debug(
                    "Skipping already configured GoodWe %s discovered at %s",
                    verified.serial_number,
                    verified.host,
                )
                continue

            # Serial number is the physical-device identity.  It permits multiple
            # config entries while suppressing duplicate responses for one device.
            discovered[serial] = verified

        _LOGGER.info(
            "GoodWe multi-inverter scan found %s not-yet-configured inverter(s): %s",
            len(discovered),
            [item.label for item in discovered.values()],
        )
        return dict(
            sorted(
                discovered.items(),
                key=lambda item: ipaddress.ip_address(item[1].host),
            )
        )

    async def _async_create_from_discovery(
        self, result: GoodweDiscoveryResult
    ) -> ConfigFlowResult:
        """Reconnect to the selected inverter and create its config entry."""
        try:
            inverter, port, protocol = await async_connect_and_detect_port(
                host=result.host,
                protocol=result.protocol or "UDP",
                port=result.port,
                family=result.model_family,
                retries=10,
            )
        except InverterError:
            return self.async_abort(reason="cannot_connect")

        detected_serial = _normalise_serial(
            getattr(inverter, "serial_number", "")
        )
        expected_serial = _normalise_serial(result.serial_number)
        if expected_serial and detected_serial != expected_serial:
            _LOGGER.error(
                "GoodWe identity changed during setup: %s was %s and now answers as %s",
                result.host,
                result.serial_number,
                getattr(inverter, "serial_number", ""),
            )
            return self.async_abort(reason="identity_changed")

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
