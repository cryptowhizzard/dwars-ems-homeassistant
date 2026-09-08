"""Select LAN scan scopes from actual interfaces, never from guessed /24s.

HA's network helper supplies interface preferences and IPv4 prefixes. A live
Linux interface snapshot refreshes DHCP addresses that may have changed since
Core startup. No Supervisor token, Internet connection, or new dependency is
needed. All OS reads run in Home Assistant's executor.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import logging
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
from typing import Any

_LOGGER = logging.getLogger(__name__)
MAX_SCAN_ADDRESSES = 4096
_VIRTUAL = re.compile(
    r"^(lo\d*$|docker|hassio|veth|br-|virbr|tailscale|tun|tap|wg|zt|vmnet|dummy)",
    re.IGNORECASE,
)
_ETHERNET = re.compile(r"^(eth\d|end\d|en\d|en[opsx])", re.IGNORECASE)
_WIRELESS = re.compile(r"^(wlan|wlp|wlx|wlo|wifi)", re.IGNORECASE)


class DiscoveryNetworkError(RuntimeError):
    """A scan scope could not be determined safely; not a 'no devices' result."""


@dataclass(frozen=True)
class ScanScope:
    """A network and the local interface/source address that reaches it."""

    cidr: str
    interface: str | None = None
    source_ip: str | None = None
    source: str = "configured"

    @property
    def broadcast(self) -> str:
        return str(ipaddress.IPv4Network(self.cidr).broadcast_address)


def _kind(name: str, adapter: dict[str, Any]) -> str:
    kind = str(adapter.get("type", "")).lower()
    if kind in {"wireless", "wifi", "wlan"} or _WIRELESS.match(name):
        return "wireless"
    if kind == "ethernet" or _ETHERNET.match(name):
        return "ethernet"
    return "other"


def _address_allowed(address: ipaddress.IPv4Address) -> bool:
    return not (
        address.is_loopback or address.is_link_local or address.is_unspecified
        or address.is_multicast or address == ipaddress.IPv4Address("255.255.255.255")
        # The Supervisor bridge is not the customer's LAN. Core normally uses
        # host networking, but do not scan this bridge if installed otherwise.
        or address in ipaddress.IPv4Network("172.30.32.0/23")
    )


def parse_cidr(value: str | None) -> str | None:
    """Validate an explicit scope, never silently shrink a network to /24."""
    if value is None or str(value).strip().lower() in {"", "auto"}:
        return None
    try:
        net = ipaddress.ip_network(str(value).strip(), strict=False)
    except ValueError as err:
        raise DiscoveryNetworkError(f"Invalid GoodWe scan CIDR: {value}") from err
    if net.version != 4:
        raise DiscoveryNetworkError("GoodWe discovery requires an IPv4 network")
    if net.num_addresses > MAX_SCAN_ADDRESSES:
        raise DiscoveryNetworkError(
            f"GoodWe network {net} contains {net.num_addresses} addresses; "
            f"the scan limit is {MAX_SCAN_ADDRESSES}. Specify a smaller scan CIDR; "
            "the network has NOT been silently reduced to /24."
        )
    if not _address_allowed(net.network_address) or net.is_loopback:
        raise DiscoveryNetworkError(f"Not a usable GoodWe LAN scope: {net}")
    return str(net)


def scopes_from_adapters(
    adapters: list[dict[str, Any]], *, source: str = "Home Assistant network"
) -> list[ScanScope]:
    """Prefer all connected Ethernet NICs, Wi-Fi only if no Ethernet is usable."""
    groups: dict[str, list[ScanScope]] = {"ethernet": [], "wireless": [], "other": []}
    too_large: list[str] = []
    for adapter in adapters:
        name = str(adapter.get("name", adapter.get("interface", "")))
        if not name or _VIRTUAL.match(name) or adapter.get("virtual", False):
            continue
        if adapter.get("enabled") is False or adapter.get("connected") is False:
            continue
        for info in adapter.get("ipv4", []):
            try:
                address = ipaddress.IPv4Address(info["address"])
                prefix = int(info["network_prefix"])
                if not 1 <= prefix <= 32 or not _address_allowed(address):
                    continue
                net = ipaddress.IPv4Network(f"{address}/{prefix}", strict=False)
            except (KeyError, ValueError, TypeError):
                continue
            if net.num_addresses > MAX_SCAN_ADDRESSES:
                too_large.append(f"{name}={address}/{prefix}")
                continue
            groups[_kind(name, adapter)].append(
                ScanScope(str(net), name, str(address), source)
            )
    if too_large:
        _LOGGER.warning(
            "GoodWe discovery: interfaces exceeding the %s-address scan limit: %s. "
            "No /24 is guessed. Configure an explicit smaller scan CIDR if needed.",
            MAX_SCAN_ADDRESSES, ", ".join(too_large),
        )
    chosen = groups["ethernet"] or groups["wireless"] or groups["other"]
    if not chosen and too_large:
        raise DiscoveryNetworkError(
            f"LAN is too large for automatic scanning ({', '.join(too_large)}). "
            f"Limit: {MAX_SCAN_ADDRESSES} addresses; select a smaller CIDR."
        )
    # One subnet can appear on multiple adapters. Prefer its first local NIC.
    return list({scope.cidr: scope for scope in reversed(chosen)}.values())[::-1]


def _read_live_adapters_sync() -> list[dict[str, Any]] | None:
    """Read live DHCP addresses/prefixes on Linux; None means unavailable.

    iproute2 gives all IPv4 addresses, including secondary addresses. The ioctl
    fallback does not require iproute2 and handles the primary IPv4 of a Pi NIC.
    """
    ip_binary = shutil.which("ip")
    if ip_binary:
        try:
            reply = subprocess.run(
                [ip_binary, "-j", "-4", "address", "show"],
                capture_output=True, text=True, check=False, timeout=5,
            )
            rows = json.loads(reply.stdout) if reply.returncode == 0 else None
            if isinstance(rows, list):
                result = []
                for row in rows:
                    name = str(row.get("ifname", "")).split("@", 1)[0]
                    flags = row.get("flags", [])
                    link_kind = row.get("linkinfo", {}).get("info_kind", "")
                    result.append({
                        "name": name,
                        "enabled": "UP" in flags,
                        "connected": row.get("operstate") not in {"DOWN", "LOWERLAYERDOWN"},
                        "virtual": link_kind in {"veth", "wireguard", "tun", "dummy"},
                        "type": "wireless" if Path(f"/sys/class/net/{name}/wireless").exists()
                            else "ethernet" if row.get("link_type") == "ether" else "other",
                        "ipv4": [
                            {"address": addr["local"], "network_prefix": addr["prefixlen"]}
                            for addr in row.get("addr_info", [])
                            if addr.get("family") == "inet" and "local" in addr
                            and "prefixlen" in addr
                        ],
                    })
                return result
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError) as err:
            _LOGGER.debug("GoodWe live iproute2 interface read unavailable: %s", err)
    try:
        import fcntl  # Linux only; imported in executor, not an add-on dependency.

        adapters = []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for _index, name in socket.if_nameindex():
                request = struct.pack("256s", name.encode("utf-8")[:15])
                adapter: dict[str, Any] = {"name": name, "ipv4": [], "enabled": False}
                try:
                    raw_flags = fcntl.ioctl(sock.fileno(), 0x8913, request)  # SIOCGIFFLAGS
                    flags = struct.unpack_from("H", raw_flags, 16)[0]
                    adapter["enabled"] = bool(flags & 0x1) and not bool(flags & 0x8)
                    adapter["connected"] = bool(flags & 0x40)  # IFF_RUNNING
                    addr = fcntl.ioctl(sock.fileno(), 0x8915, request)[20:24]
                    mask = fcntl.ioctl(sock.fileno(), 0x891B, request)[20:24]
                    address, netmask = socket.inet_ntoa(addr), socket.inet_ntoa(mask)
                    prefix = ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
                    adapter["ipv4"] = [{"address": address, "network_prefix": prefix}]
                except OSError:
                    pass  # Include a down/no-address NIC so stale HA data is not reused.
                adapters.append(adapter)
        return adapters
    except (ImportError, AttributeError, OSError) as err:
        _LOGGER.debug("GoodWe live ioctl interface read unavailable: %s", err)
        return None


async def async_detect_scan_scopes(hass: Any, configured_cidr: str | None = None) -> list[ScanScope]:
    """Read current local scopes; an explicit CIDR is additional, not a LAN guess."""
    from homeassistant.components import network

    try:
        adapters = list(await network.async_get_adapters(hass))
    except Exception as err:  # Not cancellation; still try live OS interface data.
        _LOGGER.warning("GoodWe Home Assistant adapter read failed: %s", err)
        adapters = []
    live = await hass.async_add_executor_job(_read_live_adapters_sync)
    if live is not None:
        # Live kernel interfaces are authoritative for address/prefix and link
        # status. HA's cached hostname/default-route address cannot replace them.
        preferences = {item.get("name"): item for item in adapters}
        adapters = [dict(preferences.get(item["name"], {}), **item) for item in live]
        source = "Home Assistant/Linux live IPv4"
    else:
        source = "Home Assistant network"
    explicit = parse_cidr(configured_cidr)
    try:
        scopes = scopes_from_adapters(adapters, source=source)
    except DiscoveryNetworkError:
        if not explicit:
            raise
        scopes = []
    if explicit and not any(
        ipaddress.IPv4Network(explicit).subnet_of(ipaddress.IPv4Network(s.cidr))
        for s in scopes
    ):
        scopes.append(ScanScope(explicit))
    if not scopes:
        raise DiscoveryNetworkError(
            "No connected IPv4 LAN interface with a usable prefix was found. "
            "Check Home Assistant Ethernet/DHCP and host networking; "
            "no hostname, VPN, Docker address or old inverter /24 is used as a substitute."
        )
    for scope in scopes:
        _LOGGER.info(
            "GoodWe discovery: interface=%s IPv4=%s subnet=%s source=%s",
            scope.interface or "routing", scope.source_ip or "auto",
            scope.cidr, scope.source,
        )
    return scopes
