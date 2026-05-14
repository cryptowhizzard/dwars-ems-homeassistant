"""GoodWe discovery and connection helpers.

DWARS additions:
- Broadcast scan via WIFIKIT-214028-READ on UDP/48899.
- MAC normalization for config entries and device registry connections.
- Connection helper that can try UDP/TCP ports and return the detected port.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import socket
import time
from typing import Any

from goodwe import Inverter, InverterError, connect
from goodwe.const import GOODWE_TCP_PORT, GOODWE_UDP_PORT
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_PROTOCOL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_DISCOVERY_NAME,
    CONF_MAC,
    CONF_MODBUS_ID,
    CONF_MODEL_FAMILY,
    CONF_NETWORK_RETRIES,
    CONF_NETWORK_TIMEOUT,
    DEFAULT_MODBUS_ID,
    DEFAULT_NETWORK_RETRIES,
    DEFAULT_NETWORK_TIMEOUT,
    GOODWE_DISCOVERY_MESSAGE,
    GOODWE_DISCOVERY_PORT,
    GOODWE_DISCOVERY_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GoodweDiscoveryResult:
    """A discovered GoodWe inverter response."""

    host: str
    mac: str | None = None
    name: str | None = None

    @property
    def label(self) -> str:
        """Return a human readable selection label."""
        parts = [self.host]
        if self.mac:
            parts.append(self.mac)
        if self.name:
            parts.append(self.name)
        return " - ".join(parts)


def normalize_mac(mac: str | None) -> str | None:
    """Normalize a MAC address for Home Assistant storage.

    Returns None if the value is missing or malformed.
    """
    if not mac:
        return None

    cleaned = re.sub(r"[^0-9A-Fa-f]", "", mac)
    if len(cleaned) != 12:
        return None

    try:
        return dr.format_mac(cleaned)
    except ValueError:
        return None


def _parse_discovery_response(
    payload: bytes, remote_host: str | None = None
) -> GoodweDiscoveryResult | None:
    """Parse a GoodWe discovery response.

    Expected response format from the GoodWe WiFi kit is usually:
    "<ip>,<mac>,<name>".
    """
    try:
        text = payload.decode("utf-8", errors="ignore").strip("\x00\r\n ")
    except UnicodeDecodeError:
        return None

    if not text:
        return None

    parts = [part.strip() for part in text.split(",")]
    host = parts[0] if parts else ""
    mac = normalize_mac(parts[1]) if len(parts) > 1 else None
    name = parts[2] if len(parts) > 2 and parts[2] else None

    if not _looks_like_ipv4(host):
        host = remote_host or ""

    if not host:
        return None

    return GoodweDiscoveryResult(host=host, mac=mac, name=name)


def _looks_like_ipv4(value: str) -> bool:
    """Return True if value looks like an IPv4 address."""
    try:
        socket.inet_aton(value)
    except OSError:
        return False
    return value.count(".") == 3


def _scan_goodwe_inverters_sync(timeout: float) -> list[GoodweDiscoveryResult]:
    """Synchronously scan the local broadcast domain for GoodWe inverters."""
    discovered: dict[str, GoodweDiscoveryResult] = {}
    deadline = time.monotonic() + timeout
    next_send = 0.0

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 0))
        sock.settimeout(0.20)

        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send:
                try:
                    sock.sendto(
                        GOODWE_DISCOVERY_MESSAGE,
                        ("255.255.255.255", GOODWE_DISCOVERY_PORT),
                    )
                except OSError as err:
                    _LOGGER.debug("GoodWe discovery broadcast failed: %s", err)
                next_send = now + 0.75

            try:
                payload, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError as err:
                _LOGGER.debug("GoodWe discovery receive failed: %s", err)
                continue

            result = _parse_discovery_response(payload, addr[0])
            if result is None:
                continue

            key = result.mac or result.host
            discovered[key] = result

    finally:
        sock.close()

    return list(discovered.values())


async def async_scan_goodwe_inverters(
    hass: HomeAssistant, timeout: float = GOODWE_DISCOVERY_TIMEOUT
) -> list[GoodweDiscoveryResult]:
    """Scan for GoodWe inverters without blocking the event loop."""
    return await hass.async_add_executor_job(_scan_goodwe_inverters_sync, timeout)


async def async_find_inverter_by_mac(
    hass: HomeAssistant,
    mac: str | None,
    timeout: float = GOODWE_DISCOVERY_TIMEOUT,
) -> GoodweDiscoveryResult | None:
    """Find a GoodWe inverter by stored MAC address."""
    normalized_mac = normalize_mac(mac)
    if normalized_mac is None:
        return None

    for result in await async_scan_goodwe_inverters(hass, timeout):
        if result.mac == normalized_mac:
            return result
    return None


async def async_find_inverter_by_host(
    hass: HomeAssistant,
    host: str,
    timeout: float = GOODWE_DISCOVERY_TIMEOUT,
) -> GoodweDiscoveryResult | None:
    """Find a GoodWe inverter discovery result by host/IP address."""
    for result in await async_scan_goodwe_inverters(hass, timeout):
        if result.host == host:
            return result
    return None


def default_port_for_protocol(protocol: str) -> int:
    """Return the default GoodWe port for a protocol."""
    return GOODWE_TCP_PORT if protocol == "TCP" else GOODWE_UDP_PORT


def ports_to_try(protocol: str, configured_port: int | None) -> list[int]:
    """Return the GoodWe ports to try.

    If the user explicitly configured a port, only that port is tried.
    Without a custom port we try the selected protocol first and the alternative
    protocol second. This helps after firmware or WiFi/LAN module changes.
    """
    if configured_port:
        return [configured_port]

    preferred = default_port_for_protocol(protocol)
    fallback = GOODWE_TCP_PORT if preferred == GOODWE_UDP_PORT else GOODWE_UDP_PORT
    return [preferred, fallback]


def protocol_for_port(port: int, fallback_protocol: str) -> str:
    """Return a protocol label for a GoodWe port."""
    if port == GOODWE_TCP_PORT:
        return "TCP"
    if port == GOODWE_UDP_PORT:
        return "UDP"
    return fallback_protocol


async def async_connect_and_detect_port(
    *,
    host: str,
    protocol: str = "UDP",
    port: int | None = None,
    family: str | None = None,
    comm_addr: int = DEFAULT_MODBUS_ID,
    timeout: int = DEFAULT_NETWORK_TIMEOUT,
    retries: int = DEFAULT_NETWORK_RETRIES,
) -> tuple[Inverter, int, str]:
    """Connect to a GoodWe inverter and return inverter, port and protocol.

    Raises InverterError if no attempted port works.
    """
    failures: list[Exception] = []

    for candidate_port in ports_to_try(protocol, port):
        try:
            inverter = await connect(
                host=host,
                port=candidate_port,
                family=family,
                comm_addr=comm_addr,
                timeout=timeout,
                retries=retries,
            )
        except InverterError as err:
            failures.append(err)
            continue

        detected_protocol = protocol_for_port(candidate_port, protocol)
        return inverter, candidate_port, detected_protocol

    raise InverterError(
        f"Unable to connect to GoodWe inverter at host={host}. Failures={failures}"
    )


def build_updated_entry_data(
    current_data: dict[str, Any],
    *,
    host: str,
    port: int,
    protocol: str,
    family: str,
    mac: str | None,
    discovery_name: str | None = None,
) -> dict[str, Any]:
    """Return config entry data updated with current network/device information."""
    data = dict(current_data)
    data.update(
        {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_PROTOCOL: protocol,
            CONF_MODEL_FAMILY: family,
        }
    )
    if mac:
        data[CONF_MAC] = mac
    if discovery_name:
        data[CONF_DISCOVERY_NAME] = discovery_name
    return data


def build_updated_entry_options(
    current_options: dict[str, Any],
    *,
    host: str,
    port: int,
    protocol: str,
    family: str,
    mac: str | None,
    discovery_name: str | None = None,
) -> dict[str, Any]:
    """Return config entry options updated when options override network data.

    The original integration stores host/port/protocol in options after the
    options dialog is saved. If those options are present, they must be updated
    too, otherwise stale options keep overriding the corrected config entry data.
    """
    options = dict(current_options)
    if CONF_HOST in options:
        options[CONF_HOST] = host
    if CONF_PORT in options:
        options[CONF_PORT] = port
    if CONF_PROTOCOL in options:
        options[CONF_PROTOCOL] = protocol
    if CONF_MODEL_FAMILY in options:
        options[CONF_MODEL_FAMILY] = family
    if mac and CONF_MAC in options:
        options[CONF_MAC] = mac
    if discovery_name and CONF_DISCOVERY_NAME in options:
        options[CONF_DISCOVERY_NAME] = discovery_name
    return options


def entry_connection_options(entry_data: dict[str, Any], entry_options: dict[str, Any]) -> dict[str, Any]:
    """Extract connection options from a GoodWe config entry."""
    protocol = entry_options.get(
        CONF_PROTOCOL, entry_data.get(CONF_PROTOCOL, "UDP")
    )
    return {
        "protocol": protocol,
        "port": entry_options.get(CONF_PORT, entry_data.get(CONF_PORT)),
        "family": entry_options.get(
            CONF_MODEL_FAMILY, entry_data.get(CONF_MODEL_FAMILY)
        ),
        "comm_addr": entry_options.get(CONF_MODBUS_ID, DEFAULT_MODBUS_ID),
        "timeout": entry_options.get(CONF_NETWORK_TIMEOUT, DEFAULT_NETWORK_TIMEOUT),
        "retries": entry_options.get(CONF_NETWORK_RETRIES, DEFAULT_NETWORK_RETRIES),
    }
