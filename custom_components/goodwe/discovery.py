"""GoodWe discovery and connection helpers.

DWARS additions:
- A subnet pre-scan before GoodWe broadcast discovery.  When nmap is available
  the requested ``nmap -Pn -T4 -p 502 --open -sS -sV`` scan is used.  A
  dependency-free threaded TCP probe is the fallback and still fills the ARP
  table for every address in the subnet.
- Broadcast scan via WIFIKIT-214028-READ on UDP/48899.
- MAC normalization and ARP enrichment for config entries/device registry.
- Connection helper that can try UDP/TCP ports and return the detected port.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import ipaddress
import logging
import re
import shutil
import socket
import subprocess
import threading
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

_PRE_SCAN_CACHE_TTL = 55.0
_PRE_SCAN_MAX_ADDRESSES = 4096
_PRE_SCAN_CACHE: dict[str, tuple[float, list[str]]] = {}
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


def _local_ipv4_addresses() -> list[str]:
    """Return plausible non-loopback local IPv4 addresses without shell tools."""
    addresses: set[str] = set()

    try:
        for address in socket.gethostbyname_ex(socket.gethostname())[2]:
            if _looks_like_ipv4(address) and not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass

    # A UDP connect only asks the kernel which source address it would use; no
    # packet needs to reach this destination and Internet access is not required.
    for destination in (("1.1.1.1", 53), ("8.8.8.8", 53)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(destination)
            address = sock.getsockname()[0]
            if _looks_like_ipv4(address) and not address.startswith("127."):
                addresses.add(address)
        except OSError:
            pass
        finally:
            sock.close()

    return sorted(addresses)


def resolve_network_cidr(
    configured_cidr: str | None = None, preferred_host: str | None = None
) -> str | None:
    """Resolve the subnet to pre-scan.

    An explicitly configured CIDR wins.  Otherwise the configured inverter host
    or the local Home Assistant address is converted to a conservative /24.
    Very large networks are deliberately reduced to /24 to avoid accidental
    long-running scans from a typo in the options dialog.
    """
    candidates: list[str] = []
    if configured_cidr and configured_cidr.strip():
        candidates.append(configured_cidr.strip())
    if preferred_host and _looks_like_ipv4(preferred_host):
        candidates.append(f"{preferred_host}/24")
    candidates.extend(f"{address}/24" for address in _local_ipv4_addresses())

    for candidate in candidates:
        try:
            network = ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            _LOGGER.warning("Ignoring invalid GoodWe network CIDR %s", candidate)
            continue
        if network.version != 4:
            continue
        if network.num_addresses > _PRE_SCAN_MAX_ADDRESSES:
            host = preferred_host or next(iter(network.hosts()), None)
            if host is None:
                continue
            network = ipaddress.ip_network(f"{host}/24", strict=False)
            _LOGGER.warning(
                "GoodWe pre-scan network %s is too large; limiting scan to %s",
                candidate,
                network,
            )
        return str(network)
    return None


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


def _nmap_scan(network_cidr: str) -> list[str] | None:
    """Run the requested nmap scan and return hosts with TCP/502 open.

    ``None`` means nmap is unavailable or failed and tells the caller to use the
    dependency-free fallback.  Home Assistant normally runs the integration as
    root, but permission failures for ``-sS`` are handled the same way.
    """
    executable = shutil.which("nmap")
    if not executable:
        return None

    command = [
        executable,
        "-Pn",
        "-T4",
        "-p",
        str(GOODWE_TCP_PORT),
        "--open",
        "-sS",
        "-sV",
        "--max-retries",
        "1",
        "--host-timeout",
        "3s",
        "-oG",
        "-",
        network_cidr,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        _LOGGER.debug("GoodWe nmap pre-scan failed: %s", err)
        return None

    if completed.returncode != 0:
        _LOGGER.debug(
            "GoodWe nmap pre-scan returned %s: %s",
            completed.returncode,
            completed.stderr.strip(),
        )
        return None

    open_hosts: list[str] = []
    for line in completed.stdout.splitlines():
        match = re.match(r"Host:\s+(\d+\.\d+\.\d+\.\d+).*Ports:.*502/open", line)
        if match:
            open_hosts.append(match.group(1))
    return sorted(set(open_hosts), key=ipaddress.ip_address)


def _probe_tcp_502(host: str) -> tuple[str, bool]:
    """Touch one host so the kernel learns ARP and report TCP/502 state."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.25)
    try:
        is_open = sock.connect_ex((host, GOODWE_TCP_PORT)) == 0
    except OSError:
        is_open = False
    finally:
        sock.close()
    return host, is_open


def _fallback_subnet_scan(network_cidr: str) -> list[str]:
    """Populate ARP with a bounded threaded TCP scan and return open hosts."""
    network = ipaddress.ip_network(network_cidr, strict=False)
    hosts = [str(host) for host in network.hosts()]
    open_hosts: list[str] = []

    workers = min(64, max(1, len(hosts)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="goodwe-scan") as pool:
        futures = [pool.submit(_probe_tcp_502, host) for host in hosts]
        for future in as_completed(futures):
            host, is_open = future.result()
            if is_open:
                open_hosts.append(host)

    return sorted(open_hosts, key=ipaddress.ip_address)


def _pre_scan_network_sync(network_cidr: str) -> list[str]:
    """Run a cached subnet pre-scan and return TCP/502 candidates."""
    now = time.monotonic()
    with _PRE_SCAN_CACHE_LOCK:
        cached = _PRE_SCAN_CACHE.get(network_cidr)
    if cached and now - cached[0] < _PRE_SCAN_CACHE_TTL:
        return list(cached[1])

    nmap_hosts = _nmap_scan(network_cidr)

    # Always perform the lightweight socket sweep as well.  Nmap uses its own
    # raw ARP handling on a local Ethernet network and does not reliably fill
    # Linux' /proc/net/arp cache.  The socket sweep is intentionally bounded and
    # makes every live host available as an ARP candidate, including UDP/8899
    # GoodWe Wi-Fi kits that do not expose TCP/502 or answer the broadcast.
    socket_hosts = _fallback_subnet_scan(network_cidr)
    open_hosts = sorted(
        set(socket_hosts) | set(nmap_hosts or []),
        key=ipaddress.ip_address,
    )

    with _PRE_SCAN_CACHE_LOCK:
        _PRE_SCAN_CACHE[network_cidr] = (time.monotonic(), list(open_hosts))

    _LOGGER.debug(
        "GoodWe pre-scan of %s completed; TCP/502 open on %s",
        network_cidr,
        open_hosts,
    )
    return open_hosts


def _scan_goodwe_broadcast_sync(timeout: float) -> list[GoodweDiscoveryResult]:
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
            discovered[result.mac or result.host] = result
    finally:
        sock.close()

    return list(discovered.values())


def _scan_goodwe_inverters_sync(
    timeout: float,
    pre_scan_enabled: bool,
    network_cidr: str | None,
    preferred_host: str | None,
) -> list[GoodweDiscoveryResult]:
    """Pre-scan the subnet, then run the GoodWe broadcast discovery."""
    open_hosts: list[str] = []
    resolved_cidr = resolve_network_cidr(network_cidr, preferred_host)
    if pre_scan_enabled and resolved_cidr:
        open_hosts = _pre_scan_network_sync(resolved_cidr)

    broadcast_results = _scan_goodwe_broadcast_sync(timeout)
    arp = _read_arp_table()
    discovered_by_host: dict[str, GoodweDiscoveryResult] = {}

    # The host is the primary key within one scan.  Previously broadcast results
    # were keyed by MAC while TCP candidates were sometimes keyed by host.  The
    # same inverter could therefore appear twice when ARP information was
    # incomplete.
    for item in broadcast_results:
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
            continue
        discovered_by_host[host] = GoodweDiscoveryResult(
            host=host,
            mac=arp.get(host),
            name="Modbus/TCP candidate",
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
            if host in discovered_by_host:
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
) -> list[GoodweDiscoveryResult]:
    """Scan for GoodWe inverters without blocking Home Assistant's event loop."""
    return await hass.async_add_executor_job(
        _scan_goodwe_inverters_sync,
        timeout,
        pre_scan_enabled,
        network_cidr,
        preferred_host,
    )


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

    for result in await async_scan_goodwe_inverters(
        hass,
        timeout,
        pre_scan_enabled=pre_scan_enabled,
        network_cidr=network_cidr,
        preferred_host=preferred_host,
    ):
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
    hass: HomeAssistant,
    host: str,
    timeout: float = GOODWE_DISCOVERY_TIMEOUT,
    *,
    pre_scan_enabled: bool = True,
    network_cidr: str | None = None,
) -> GoodweDiscoveryResult | None:
    """Find a GoodWe inverter discovery result by host/IP address."""
    for result in await async_scan_goodwe_inverters(
        hass,
        timeout,
        pre_scan_enabled=pre_scan_enabled,
        network_cidr=network_cidr,
        preferred_host=host,
    ):
        if result.host == host:
            return result
    return None


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


def entry_connection_options(
    entry_data: dict[str, Any], entry_options: dict[str, Any]
) -> dict[str, Any]:
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
