"""GoodWe discovery and connection helpers.

DWARS additions:
- Live Ethernet IPv4/prefix selection (no guessed /24 or default VPN route).
- A TCP/502 pre-scan before GoodWe discovery: nmap when available, plus a
  dependency-free socket sweep with ARP-resolution time and retry.
- Broadcast scan via WIFIKIT-214028-READ on UDP/48899.
- MAC normalization and ARP enrichment for config entries/device registry.
- Connection helper that can try UDP/TCP ports and return the detected port.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import errno
import ipaddress
import logging
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Any
from xml.etree import ElementTree as ET

from goodwe import Inverter, InverterError, connect
from goodwe.const import GOODWE_TCP_PORT, GOODWE_UDP_PORT
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_PROTOCOL, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_DWARS_MANAGED,
    CONF_AUTO_LOAD_CONTROL,
    CONF_KEEP_ALIVE,
    DEFAULT_SCAN_INTERVAL,
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

from .network_discovery import (
    DiscoveryNetworkError, MAX_SCAN_ADDRESSES, async_detect_scan_scopes, parse_cidr,
)

_LOGGER = logging.getLogger(__name__)

DISCOVERY_VERSION = "0.9.9.34"
_PRE_SCAN_CACHE_TTL = 55.0
_PRE_SCAN_MAX_ADDRESSES = MAX_SCAN_ADDRESSES
_PRE_SCAN_CACHE: dict[tuple[str, str | None], tuple[float, list[str]]] = {}
_PRE_SCAN_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class GoodweDiscoveryResult:
    """A discovered or positively identified GoodWe inverter."""

    host: str
    mac: str | None = None
    name: str | None = None
    serial_number: str | None = None
    model_name: str | None = None
    model_family: str | None = None
    port: int | None = None
    protocol: str | None = None

    @property
    def identity_key(self) -> str:
        """Return the strongest available identity for one scan result."""
        if self.serial_number:
            return self.serial_number.strip("\x00 \t\r\n").upper()
        return self.mac or self.host

    @property
    def label(self) -> str:
        """Return a human-readable selection label."""
        parts = [self.host]
        if self.serial_number:
            parts.append(f"S/N {self.serial_number}")
        if self.model_name:
            parts.append(self.model_name)
        if self.mac:
            parts.append(self.mac)
        if self.name and self.name not in parts:
            parts.append(self.name)
        if self.protocol and self.port:
            parts.append(f"{self.protocol}/{self.port}")
        return " - ".join(parts)


def normalize_mac(mac: Any) -> str | None:
    """Normalize a MAC address for Home Assistant storage.

    Returns ``None`` if the value is missing or malformed.
    """
    if not isinstance(mac, str) or not mac:
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
    """Parse a GoodWe WiFi-kit discovery response.

    The response normally has the form ``<ip>,<mac>,<name>``.
    """
    text = payload.decode("utf-8", errors="ignore").strip("\x00\r\n ")
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
    """Return whether ``value`` is a valid IPv4 address."""
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


def resolve_network_cidr(
    configured_cidr: str | None = None, preferred_host: str | None = None
) -> str | None:
    """Validate only an explicit CIDR. Auto is resolved asynchronously from NICs.

    preferred_host remains accepted for callers from older DWARS versions, but
    an inverter IP is never used to guess the Raspberry's network or mask.
    """
    return parse_cidr(configured_cidr)


def _read_arp_table() -> dict[str, str]:
    """Read the Linux ARP cache as ``IP -> normalized MAC`` mapping."""
    result: dict[str, str] = {}
    try:
        with open("/proc/net/arp", encoding="utf-8") as handle:
            next(handle, None)
            for line in handle:
                fields = line.split()
                if len(fields) < 4:
                    continue
                host = fields[0]
                mac = normalize_mac(fields[3])
                if _looks_like_ipv4(host) and mac and mac != "00:00:00:00:00:00":
                    result[host] = mac
    except OSError as err:
        _LOGGER.debug("Cannot read ARP table: %s", err)
    return result


def _parse_nmap_open_hosts(output: str | bytes | None) -> list[str]:
    """Read completed host records, including partial output after a timeout."""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    hosts: set[str] = set()
    # Process only complete <host> records. A cancelled nmap does not write the
    # closing </nmaprun>, but its already emitted host results are still usable.
    for record in re.findall(r"<host[\s>].*?</host>", output or "", flags=re.DOTALL):
        try:
            host = ET.fromstring(record)
        except ET.ParseError:
            continue
        open_502 = any(
            port.get("protocol") == "tcp" and port.get("portid") == "502"
            and port.find("state") is not None
            and port.find("state").get("state") == "open"
            for port in host.findall("./ports/port")
        )
        if not open_502:
            continue
        for address in host.findall("address"):
            value = address.get("addr", "")
            if address.get("addrtype") == "ipv4" and _looks_like_ipv4(value):
                hosts.add(value)
    return sorted(hosts, key=ipaddress.ip_address)


def _nmap_scan(network_cidr: str, interface: str | None = None) -> list[str] | None:
    """Run the user's TCP/502 discovery, without service/version interrogation.

    Do not force -sS (raw socket privileges) or a 3s host deadline. Nmap can pick
    SYN/connect scanning as appropriate. DNS lookups are disabled. A /24 gets
    120 seconds, not the old 30 seconds. The socket scan remains independent.
    """
    network_cidr = parse_cidr(network_cidr)
    executable = shutil.which("nmap")
    if not executable:
        return None
    command = [executable, "-n", "-Pn", "-T4", "-p", str(GOODWE_TCP_PORT), "--open", "-oX", "-"]
    if interface:
        command.extend(["-e", interface])
    command.append(network_cidr)
    count = ipaddress.IPv4Network(network_cidr).num_addresses
    timeout = max(120, min(600, ((count + 255) // 256) * 30))
    _LOGGER.info("GoodWe discovery: nmap TCP/502 scan of %s (timeout %ss)", network_cidr, timeout)
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as err:
        partial = _parse_nmap_open_hosts(err.output)
        _LOGGER.warning(
            "GoodWe nmap scan of %s timed out; preserving %s open host(s) and doing the TCP fallback",
            network_cidr, len(partial),
        )
        return partial
    except OSError as err:
        _LOGGER.warning("GoodWe nmap unavailable: %s; using TCP fallback", err)
        return None
    if completed.returncode:
        _LOGGER.warning(
            "GoodWe nmap returned %s for %s; using TCP fallback (%s)",
            completed.returncode, network_cidr, completed.stderr.strip()[:300],
        )
    return _parse_nmap_open_hosts(completed.stdout)


def _probe_tcp_502(host: str, source_ip: str | None = None) -> tuple[str, bool]:
    """TCP-connect, including ARP resolution time, with one retry after failure."""
    for attempt in range(2):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(2.0)
            if source_ip:
                sock.bind((source_ip, 0))
            result = sock.connect_ex((host, GOODWE_TCP_PORT))
            if result == 0:
                return host, True
            if result == errno.ECONNREFUSED:
                return host, False  # Host/ARP is alive but port is actually closed.
        except OSError:
            pass
        finally:
            sock.close()
        if attempt == 0:
            time.sleep(0.15)
    return host, False


def _fallback_subnet_scan(network_cidr: str, source_ip: str | None = None) -> list[str]:
    """Scan every usable address, not just existing ARP entries or ICMP replies."""
    network = ipaddress.IPv4Network(parse_cidr(network_cidr))
    hosts = [str(host) for host in network.hosts() if str(host) != source_ip]
    open_hosts: list[str] = []
    with ThreadPoolExecutor(max_workers=min(64, max(1, len(hosts))), thread_name_prefix="goodwe-scan") as pool:
        futures = [pool.submit(_probe_tcp_502, host, source_ip) for host in hosts]
        for future in as_completed(futures):
            host, is_open = future.result()
            if is_open:
                open_hosts.append(host)
    return sorted(set(open_hosts), key=ipaddress.ip_address)


def _pre_scan_network_sync(
    network_cidr: str, source_ip: str | None = None,
    interface: str | None = None, force: bool = False,
) -> list[str]:
    """Pre-scan before UDP discovery. Never cache a negative discovery result."""
    network_cidr = parse_cidr(network_cidr)
    key = (network_cidr, source_ip)
    now = time.monotonic()
    with _PRE_SCAN_CACHE_LOCK:
        cached = _PRE_SCAN_CACHE.get(key)
    if not force and cached and now - cached[0] < _PRE_SCAN_CACHE_TTL:
        return list(cached[1])
    start = time.monotonic()
    nmap_hosts = _nmap_scan(network_cidr, interface)
    _LOGGER.info(
        "GoodWe discovery: %s on %s; TCP probes use 2s and one retry, no ping prerequisite",
        "TCP/ARP sweep after nmap" if nmap_hosts is not None else "nmap not present; native TCP/ARP scan",
        network_cidr,
    )
    socket_hosts = _fallback_subnet_scan(network_cidr, source_ip)
    network = ipaddress.IPv4Network(network_cidr)
    open_hosts = sorted(
        {host for host in set(socket_hosts) | set(nmap_hosts or [])
         if ipaddress.IPv4Address(host) in network and host != source_ip},
        key=ipaddress.ip_address,
    )
    with _PRE_SCAN_CACHE_LOCK:
        for stale in [k for k, v in _PRE_SCAN_CACHE.items() if now - v[0] > _PRE_SCAN_CACHE_TTL]:
            _PRE_SCAN_CACHE.pop(stale, None)
        if open_hosts:
            _PRE_SCAN_CACHE[key] = (time.monotonic(), list(open_hosts))
        else:
            _PRE_SCAN_CACHE.pop(key, None)
    _LOGGER.info(
        "GoodWe discovery: subnet=%s TCP/502 open=%s duration=%.1fs",
        network_cidr, open_hosts, time.monotonic() - start,
    )
    return open_hosts


def _scan_goodwe_broadcast_sync(
    timeout: float, source_ip: str | None = None, broadcast: str = "255.255.255.255"
) -> list[GoodweDiscoveryResult]:
    """Synchronously scan the local broadcast domain for GoodWe inverters."""
    discovered: dict[str, GoodweDiscoveryResult] = {}
    deadline = time.monotonic() + timeout
    next_send = 0.0

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((source_ip or "", 0))
        sock.settimeout(0.20)

        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send:
                try:
                    sock.sendto(
                        GOODWE_DISCOVERY_MESSAGE,
                        (broadcast, GOODWE_DISCOVERY_PORT),
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
            discovered[result.mac or result.host] = result
    finally:
        sock.close()

    return list(discovered.values())


def _scan_goodwe_inverters_sync(
    timeout: float,
    pre_scan_enabled: bool,
    network_cidr: str | None,
    preferred_host: str | None,
    source_ip: str | None = None,
    interface: str | None = None,
    force: bool = False,
) -> list[GoodweDiscoveryResult]:
    """Pre-scan one resolved LAN subnet, then send directed GoodWe broadcasts."""
    open_hosts: list[str] = []
    resolved_cidr = resolve_network_cidr(network_cidr, preferred_host)
    if pre_scan_enabled and resolved_cidr:
        open_hosts = _pre_scan_network_sync(resolved_cidr, source_ip, interface, force)

    broadcast = str(ipaddress.IPv4Network(resolved_cidr).broadcast_address) if resolved_cidr else "255.255.255.255"
    broadcast_results = _scan_goodwe_broadcast_sync(timeout, source_ip, broadcast)
    arp = _read_arp_table()
    discovered_by_host: dict[str, GoodweDiscoveryResult] = {}

    # The host is the primary key within one scan.  Previously broadcast results
    # were keyed by MAC while TCP candidates were sometimes keyed by host.  The
    # same inverter could therefore appear twice when ARP information was
    # incomplete.
    for item in broadcast_results:
        if source_ip and item.host == source_ip:
            continue
        discovered_by_host[item.host] = GoodweDiscoveryResult(
            host=item.host,
            mac=item.mac or arp.get(item.host),
            name=item.name,
        )

    # A WLA dongle with Modbus/TCP may not answer the old UDP discovery packet.
    # Add TCP/502 candidates so the subsequent GoodWe protocol connection can
    # positively identify them and reject unrelated Modbus devices.
    for host in open_hosts:
        if host in discovered_by_host:
            discovered_by_host[host] = replace(discovered_by_host[host], port=GOODWE_TCP_PORT, protocol="TCP")
            continue
        discovered_by_host[host] = GoodweDiscoveryResult(
            host=host, mac=arp.get(host), name="Modbus/TCP candidate",
            port=GOODWE_TCP_PORT, protocol="TCP",
        )

    # Some UDP/8899 Wi-Fi kits do not answer WIFIKIT broadcast discovery,
    # especially when another GoodWe dongle on the same subnet answers first.
    # The subnet pre-scan deliberately populated the ARP table, so include every
    # live ARP host inside the scanned network as a low-priority candidate.  The
    # async protocol verification in config_flow.py positively identifies GoodWe
    # devices and discards routers, printers and unrelated Modbus equipment.
    if pre_scan_enabled and resolved_cidr:
        network = ipaddress.ip_network(resolved_cidr, strict=False)
        for host in sorted(arp, key=ipaddress.ip_address):
            address = ipaddress.ip_address(host)
            if address not in network or address in {
                network.network_address,
                network.broadcast_address,
            }:
                continue
            if host in discovered_by_host or host == source_ip:
                continue
            discovered_by_host[host] = GoodweDiscoveryResult(
                host=host,
                mac=arp.get(host),
                name="ARP candidate",
            )

    return sorted(
        discovered_by_host.values(),
        key=lambda item: ipaddress.ip_address(item.host),
    )


async def async_scan_goodwe_inverters(
    hass: HomeAssistant,
    timeout: float = GOODWE_DISCOVERY_TIMEOUT,
    *,
    pre_scan_enabled: bool = True,
    network_cidr: str | None = None,
    preferred_host: str | None = None,
    force: bool = False,
) -> list[GoodweDiscoveryResult]:
    """Detect live Ethernet scopes, scan them, and merge all candidates by IP.

    Existing inverter addresses never determine the local subnet. In particular
    an old 192.168.178.x entry cannot hide a current DHCP LAN of 192.168.1.x.
    """
    _LOGGER.info("DWARS GoodWe discovery %s: determining live LAN interfaces", DISCOVERY_VERSION)
    scopes = await async_detect_scan_scopes(hass, network_cidr)
    merged: dict[str, GoodweDiscoveryResult] = {}
    # Sequential NICs bound the socket count and Raspberry load to 64 at a time.
    for scope in scopes:
        results = await hass.async_add_executor_job(
            _scan_goodwe_inverters_sync, timeout, pre_scan_enabled,
            scope.cidr, preferred_host, scope.source_ip, scope.interface, force,
        )
        for result in results:
            old = merged.get(result.host)
            if old is None or result.protocol == "TCP":
                merged[result.host] = result
    return sorted(merged.values(), key=lambda item: ipaddress.ip_address(item.host))


async def async_find_inverter_by_mac(
    hass: HomeAssistant,
    mac: str | None,
    timeout: float = GOODWE_DISCOVERY_TIMEOUT,
    *,
    pre_scan_enabled: bool = True,
    network_cidr: str | None = None,
    preferred_host: str | None = None,
) -> GoodweDiscoveryResult | None:
    """Find a GoodWe inverter by its stored MAC address."""
    normalized_mac = normalize_mac(mac)
    if normalized_mac is None:
        return None

    try:
        results = await async_scan_goodwe_inverters(
            hass, timeout, pre_scan_enabled=pre_scan_enabled,
            network_cidr=network_cidr, preferred_host=preferred_host,
        )
    except DiscoveryNetworkError as err:
        # Background recovery must return 'not recovered' to the existing
        # coordinator instead of breaking config-entry setup while DHCP is down.
        _LOGGER.warning("GoodWe MAC recovery: network unavailable: %s", err)
        return None
    for result in results:
        if result.mac == normalized_mac:
            return result

    # UDP-only GoodWe WiFi kits can populate ARP during the subnet pre-scan while
    # neither TCP/502 nor the broadcast reply is available. Match the retained
    # MAC directly against the now-populated kernel ARP table in that case.
    arp = await hass.async_add_executor_job(_read_arp_table)
    for host, arp_mac in arp.items():
        if arp_mac == normalized_mac:
            return GoodweDiscoveryResult(
                host=host, mac=normalized_mac, name="ARP recovery"
            )
    return None


async def async_find_inverter_by_host(
    hass: HomeAssistant, host: str,
    timeout: float = GOODWE_DISCOVERY_TIMEOUT,
    *, pre_scan_enabled: bool = True, network_cidr: str | None = None,
) -> GoodweDiscoveryResult | None:
    """Enrich an already contacted IP from ARP, without rescanning the whole LAN.

    Used after manual connection. Discovery metadata must not turn a successful
    manual setup into another long-running scan or a network-selection failure.
    """
    if not _looks_like_ipv4(host):
        return None
    arp = await hass.async_add_executor_job(_read_arp_table)
    return GoodweDiscoveryResult(host=host, mac=arp.get(host))


def default_port_for_protocol(protocol: str) -> int:
    """Return the default GoodWe port for a protocol."""
    return GOODWE_TCP_PORT if protocol == "TCP" else GOODWE_UDP_PORT


def ports_to_try(protocol: str, configured_port: int | None) -> list[int]:
    """Return the GoodWe communication ports to try."""
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
    """Connect and return inverter, detected port and protocol."""
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

        return inverter, candidate_port, protocol_for_port(candidate_port, protocol)

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
    """Return config entry data updated with current network information."""
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
    """Update options only where options already override network data."""
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


def entry_is_loaded(entry) -> bool:
    """Compare enum values, not their representation across HA versions."""
    state = getattr(entry, "state", "")
    return str(getattr(state, "value", state)).lower() == "loaded"


def entry_is_dwars_managed(entry) -> bool:
    """Recognize new imports and the narrowly identified 0.6.x import format."""
    return bool(entry.data.get(CONF_DWARS_MANAGED)) or (
        getattr(entry, "source", "") == "import"
        and entry.data.get(CONF_AUTO_LOAD_CONTROL) is False
    )


def complete_entry_data(current_data: dict[str, Any]) -> dict[str, Any]:
    """Fill missing/null settings only. Never turn a schema migration into a scan."""
    data = dict(current_data)
    defaults = {
        CONF_PROTOCOL: "UDP", CONF_KEEP_ALIVE: False,
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
        CONF_NETWORK_RETRIES: DEFAULT_NETWORK_RETRIES,
        CONF_NETWORK_TIMEOUT: DEFAULT_NETWORK_TIMEOUT,
        CONF_MODBUS_ID: DEFAULT_MODBUS_ID,
    }
    if data.get(CONF_PORT) == GOODWE_TCP_PORT:
        defaults[CONF_PROTOCOL] = "TCP"
    for key, value in defaults.items():
        if data.get(key) is None:
            data[key] = value
    if data.get(CONF_PORT) is None:
        data[CONF_PORT] = default_port_for_protocol(data[CONF_PROTOCOL])
    if not data.get(CONF_MODEL_FAMILY):
        data[CONF_MODEL_FAMILY] = "none"
    return data


def entry_connection_options(
    entry_data: dict[str, Any], entry_options: dict[str, Any]
) -> dict[str, Any]:
    """Options override data, but legacy null values must not mask defaults."""
    values = complete_entry_data(entry_data)
    values.update({k: v for k, v in entry_options.items() if v is not None})
    values = complete_entry_data(values)
    return {
        "protocol": values[CONF_PROTOCOL],
        "port": values[CONF_PORT],
        "family": values[CONF_MODEL_FAMILY],
        "comm_addr": values[CONF_MODBUS_ID],
        "timeout": values[CONF_NETWORK_TIMEOUT],
        "retries": values[CONF_NETWORK_RETRIES],
    }
