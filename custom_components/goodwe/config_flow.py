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

from .network_discovery import DiscoveryNetworkError

PROTOCOL_CHOICES = ["UDP", "TCP"]
DISCOVERED_INVERTER = "discovered_inverter"
MANUAL_DISCOVERY_VALUE = "__manual__"
RESCAN_DISCOVERY_VALUE = "__rescan__"

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
        self._discovery_task: asyncio.Task | None = None
        self._scan_error: str = ""

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

    async def async_step_import(self, data: dict[str, Any]) -> ConfigFlowResult:
        """DWARS unattended setup. Always identify the physical serial first."""
        if data.get("dwars_discover"):
            discovered = await self._async_discover_unconfigured(include_configured=True)
            candidates = [
                {"host": item.host, "protocol": item.protocol or "UDP",
                 "port": item.port, "model_family": item.model_family,
                 "mac": item.mac, "expected_serial": item.serial_number}
                for item in discovered.values()
            ]
            candidates.extend({"host": host} for host in data.get("hosts", []))
            failures = []
            for candidate in candidates:
                result = await self.hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": "import"}, data=candidate,
                )
                if result.get("type") == "abort" and result.get("reason") not in {
                    "already_configured", "already_configured_inverter", "updated_ip"
                }:
                    failures.append(str(result.get("reason")))
            if failures:
                return self.async_abort(reason="cannot_connect")
            return self.async_abort(reason="dwars_scan_complete")

        host = str(data.get("host") or "").strip()
        try:
            ipaddress.IPv4Address(host)
            inverter, port, protocol = await async_connect_and_detect_port(
                host=host, protocol=data.get("protocol") or "TCP",
                port=data.get("port"), family=data.get("model_family"),
                timeout=2, retries=2,
            )
        except (InverterError, OSError, ValueError, TimeoutError):
            return self.async_abort(reason="cannot_connect")
        serial = _normalise_serial(getattr(inverter, "serial_number", ""))
        if not serial:
            return self.async_abort(reason="missing_serial")
        if data.get("expected_serial") and serial != _normalise_serial(data["expected_serial"]):
            return self.async_abort(reason="identity_changed")
        existing = self._entry_for_serial(serial)
        if existing is not None:
            # Only a positively identified serial can repair its old IP. Preserve
            # manual options/mapping; do not claim another inverter's entry.
            updated = build_updated_entry_data(
                dict(existing.data), host=host, port=port, protocol=protocol,
                family=type(inverter).__name__, mac=data.get("mac"),
            )
            options = dict(existing.options)
            for key, value in ((CONF_HOST, host), (CONF_PORT, port), (CONF_PROTOCOL, protocol)):
                if key in options:
                    options[key] = value
            if dict(existing.data) != updated or dict(existing.options) != options:
                self.hass.config_entries.async_update_entry(existing, data=updated, options=options)
                self.hass.async_create_task(self.hass.config_entries.async_reload(existing.entry_id))
            return self.async_abort(reason="already_configured_inverter")
        result = await self.async_handle_successful_connection(
            inverter, host, port, protocol, mac=data.get("mac"),
        )
        if result.get("type") == "create_entry":
            # The external DWARS agent owns control. Do not start the integration's
            # independent automatic load controller alongside it.
            result["data"][CONF_AUTO_LOAD_CONTROL] = False
            result["options"] = {CONF_AUTO_LOAD_CONTROL: False}
        return result

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

        return await self.async_step_scan()

    async def async_step_scan(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Run discovery in a managed progress task, not in a long HTTP request."""
        if self._discovery_task is None:
            self._scan_error = ""
            self._discovery_task = self.hass.async_create_task(
                self._async_discover_unconfigured()
            )
            # Always register the task with the flow manager, even if eager
            # execution completed it immediately. HA handles cancel/removal.
            return self.async_show_progress(
                step_id="scan", progress_action="scan_network",
                progress_task=self._discovery_task,
            )
        if not self._discovery_task.done():
            return self.async_show_progress(
                step_id="scan", progress_action="scan_network",
                progress_task=self._discovery_task,
            )
        try:
            self._discovered_inverters = self._discovery_task.result()
        except DiscoveryNetworkError as err:
            self._scan_error = str(err)
            _LOGGER.warning("GoodWe network detection failed: %s", err)
        except Exception as err:
            self._scan_error = str(err)
            _LOGGER.exception("GoodWe discovery failed (not a no-devices result)")
        finally:
            self._discovery_task = None
        next_step = "scan_failed" if self._scan_error else (
            "select" if self._discovered_inverters else "no_devices"
        )
        return self.async_show_progress_done(next_step_id=next_step)

    async def async_step_no_devices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """No new serials: offer a fresh scan and manual entry, never auto-select an old unit."""
        return self.async_show_menu(step_id="no_devices", menu_options=["scan", "manual"])

    async def async_step_scan_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Explain network/scan failure separately from a successful empty scan."""
        return self.async_show_menu(
            step_id="scan_failed", menu_options=["scan", "manual"],
            description_placeholders={"error": self._scan_error},
        )

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose from discovered inverters."""
        if user_input is not None:
            selected = user_input[DISCOVERED_INVERTER]
            if selected == MANUAL_DISCOVERY_VALUE:
                return await self.async_step_manual()
            if selected == RESCAN_DISCOVERY_VALUE or selected not in self._discovered_inverters:
                return await self.async_step_scan()
            return await self._async_create_from_discovery(
                self._discovered_inverters[selected]
            )

        options = {
            key: result.label for key, result in self._discovered_inverters.items()
        }
        options[MANUAL_DISCOVERY_VALUE] = "Manual IP address"
        options[RESCAN_DISCOVERY_VALUE] = "Scan again (refresh Ethernet/DHCP)"

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
        """Scan actual Ethernet scopes, independent of historic inverter entries."""
        return await async_scan_goodwe_inverters(
            self.hass, pre_scan_enabled=DEFAULT_PRE_SCAN_ENABLED, force=True,
        )

    async def _async_verify_candidate(
        self, result: GoodweDiscoveryResult
    ) -> GoodweDiscoveryResult | None:
        """Positively identify one scan candidate and read its serial number."""
        preferred_protocol = result.protocol or (
            "TCP" if result.name == "Modbus/TCP candidate" else "UDP"
        )
        # A 6s timeout around BOTH transports could cancel TCP identification
        # before the library finished probing the inverter families. Give each
        # transport its own budget and preserve the observed open TCP/502 hint.
        inverter = None
        for candidate_protocol in (preferred_protocol, "UDP" if preferred_protocol == "TCP" else "TCP"):
            candidate_port = 502 if candidate_protocol == "TCP" else 8899
            try:
                inverter, port, protocol = await asyncio.wait_for(
                    async_connect_and_detect_port(
                        host=result.host, protocol=candidate_protocol, port=candidate_port,
                        timeout=2, retries=1,
                    ),
                    timeout=45,
                )
                break
            except (InverterError, TimeoutError, OSError, ValueError) as err:
                _LOGGER.debug(
                    "GoodWe identification failed: %s %s/%s: %s",
                    result.host, candidate_protocol, candidate_port, err,
                )
        if inverter is None:
            if result.protocol == "TCP" or result.name == "Modbus/TCP candidate":
                _LOGGER.warning(
                    "GoodWe discovery: TCP/502 is open at %s but no GoodWe identity "
                    "could be read via TCP/UDP; not adding an unverified Modbus device",
                    result.host,
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

        _LOGGER.info(
            "GoodWe identified: host=%s serial=%s model=%s protocol=%s/%s",
            result.host, serial_number, getattr(inverter, "model_name", ""), protocol, port,
        )
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

    async def _async_discover_unconfigured(self, *, include_configured: bool = False) -> dict[str, GoodweDiscoveryResult]:
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

        probe_semaphore = asyncio.Semaphore(16)

        async def _bounded_verify(
            result: GoodweDiscoveryResult,
        ) -> GoodweDiscoveryResult | None:
            async with probe_semaphore:
                return await self._async_verify_candidate(result)

        # Probe positive TCP candidates first, then UDP broadcasts, then ARP-only
        # hosts. Identity still comes from the GoodWe serial, never from TCP/502.
        results.sort(key=lambda result: (
            0 if result.protocol == "TCP" else 2 if result.name == "ARP candidate" else 1,
            ipaddress.ip_address(result.host),
        ))
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
            if serial in configured_serials and not include_configured:
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
            "GoodWe multi-inverter scan returned %s inverter(s): %s",
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
