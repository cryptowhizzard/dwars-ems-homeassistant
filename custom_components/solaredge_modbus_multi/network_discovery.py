"""Network discovery helpers for DWARS SolarEdge Modbus Multi.

The upstream SolarEdge Modbus Multi integration needs a host/IP address.
These helpers add best-effort IP/MAC handling for installations where the
inverter can move to another DHCP address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import inspect
import logging
import os
import socket
from collections.abc import Iterable
from typing import Any

from pymodbus.client import AsyncModbusTcpClient

_LOGGER = logging.getLogger(__name__)

# SolarEdge Technologies MAC prefixes. DHCP manifest uses the same list.
SOLAREDGE_OUIS = ("002702", "28b77c1", "84d6c5", "9405bba")


def normalize_mac(mac: str | None) -> str | None:
    """Normalize a MAC address to Home Assistant's lowercase colon format."""
    if not mac:
        return None

    clean = "".join(ch for ch in str(mac).lower() if ch in "0123456789abcdef")
    if len(clean) != 12:
        return None

    return ":".join(clean[idx : idx + 2] for idx in range(0, 12, 2))


def mac_without_separators(mac: str | None) -> str | None:
    """Return a normalized MAC address without separators."""
    normalized = normalize_mac(mac)
    if not normalized:
        return None
    return normalized.replace(":", "")


def is_solaredge_mac(mac: str | None) -> bool:
    """Return True if the MAC address matches a known SolarEdge OUI."""
    clean = mac_without_separators(mac)
    return bool(clean and any(clean.startswith(prefix) for prefix in SOLAREDGE_OUIS))


def _read_proc_net_arp() -> dict[str, str]:
    """Read the Linux ARP table as {mac: ip}.

    This works without shelling out to `arp` or `ip`, which may not exist in the
    Home Assistant Core container.
    """
    arp_path = "/proc/net/arp"
    if not os.path.exists(arp_path):
        return {}

    result: dict[str, str] = {}
    try:
        with open(arp_path, encoding="utf-8") as arp_file:
            next(arp_file, None)
            for line in arp_file:
                parts = line.split()
                if len(parts) < 4:
                    continue
                ip_addr = parts[0]
                mac_addr = normalize_mac(parts[3])
                if mac_addr and mac_addr != "00:00:00:00:00:00":
                    result[mac_addr] = ip_addr
    except OSError as err:
        _LOGGER.debug("Unable to read %s: %s", arp_path, err)

    return result


async def _resolve_host(host: str) -> str | None:
    """Resolve a host to an IPv4 address."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, socket.AF_INET)
    except OSError:
        return None

    for info in infos:
        sockaddr = info[4]
        if sockaddr:
            return str(sockaddr[0])
    return None


async def async_tcp_connect(host: str, port: int, timeout: float = 0.8) -> bool:
    """Return True when a TCP connection to host:port can be opened."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError, asyncio.TimeoutError):
        return False

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # pragma: no cover - transport dependent
        pass
    return True


async def async_get_mac_from_host(host: str, port: int = 1502) -> str | None:
    """Best-effort lookup of the MAC address belonging to an IP/host.

    The TCP connection attempt populates the ARP cache on most Linux hosts. We
    then read `/proc/net/arp` to obtain the MAC address.
    """
    ip_addr = await _resolve_host(host)
    if not ip_addr:
        return None

    await async_tcp_connect(ip_addr, port, timeout=0.8)
    await asyncio.sleep(0.1)

    return next((mac for mac, ip in _read_proc_net_arp().items() if ip == ip_addr), None)


async def async_probe_solaredge_modbus(
    host: str,
    port: int = 1502,
    unit_id: int = 1,
    timeout: float = 1.2,
) -> bool:
    """Return True when a host responds like a SunSpec/SolarEdge Modbus device."""
    client = AsyncModbusTcpClient(host=host, port=port, timeout=timeout, retries=0)
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout + 0.5)
        if not client.connected:
            return False

        result = await asyncio.wait_for(
            client.read_holding_registers(address=40000, count=2, slave=unit_id),
            timeout=timeout + 0.5,
        )
        if result is None or result.isError() or not getattr(result, "registers", None):
            return False

        registers = result.registers
        if len(registers) < 2:
            return False

        sunspec_id = (int(registers[0]) << 16) + int(registers[1])
        return sunspec_id == 0x53756E53
    except Exception as err:  # noqa: BLE001 - discovery must never break config flow
        _LOGGER.debug("SolarEdge Modbus probe failed for %s:%s: %s", host, port, err)
        return False
    finally:
        try:
            client.close()
        except Exception:  # pragma: no cover - pymodbus version dependent
            pass


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _adapter_networks_from_homeassistant(hass: Any) -> list[ipaddress.IPv4Network]:
    """Return local IPv4 networks known by Home Assistant."""
    networks: list[ipaddress.IPv4Network] = []
    try:
        from homeassistant.helpers import network as ha_network

        adapters = await _maybe_await(ha_network.async_get_adapters(hass))
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Unable to get Home Assistant network adapters: %s", err)
        adapters = []

    for adapter in adapters or []:
        for ip_info in adapter.get("ipv4", []) or []:
            address = ip_info.get("address")
            prefix = ip_info.get("network_prefix")
            if not address or prefix is None:
                continue
            try:
                interface = ipaddress.ip_interface(f"{address}/{prefix}")
            except ValueError:
                continue
            network = interface.network
            if network.is_loopback or network.is_link_local:
                continue
            # Keep scans bounded. For larger networks, scan the /24 around HA.
            if network.num_addresses > 256:
                network = ipaddress.ip_network(f"{interface.ip}/24", strict=False)
            networks.append(network)

    return networks


async def _fallback_networks() -> list[ipaddress.IPv4Network]:
    """Return a small fallback set of local networks."""
    networks: list[ipaddress.IPv4Network] = []
    try:
        hostname = socket.gethostname()
        infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None, socket.AF_INET)
    except OSError:
        return networks

    for info in infos:
        ip_addr = info[4][0]
        try:
            interface = ipaddress.ip_interface(f"{ip_addr}/24")
        except ValueError:
            continue
        if not interface.ip.is_loopback and not interface.ip.is_link_local:
            networks.append(interface.network)

    return networks


def _iter_hosts(networks: Iterable[ipaddress.IPv4Network]) -> list[str]:
    """Return unique host addresses from networks, limited to /24-sized scans."""
    seen: set[str] = set()
    hosts: list[str] = []
    for network in networks:
        for ip_addr in network.hosts():
            ip_text = str(ip_addr)
            if ip_text not in seen:
                seen.add(ip_text)
                hosts.append(ip_text)
    return hosts


async def async_scan_solaredge_modbus(
    hass: Any,
    port: int = 1502,
    unit_id: int = 1,
    limit: int = 8,
) -> list[dict[str, str | int | None]]:
    """Scan local networks for SolarEdge Modbus devices.

    The scan is intentionally bounded and conservative. It is meant to prefill
    the config flow and to recover changed DHCP addresses, not to inventory a
    large routed network.
    """
    networks = await _adapter_networks_from_homeassistant(hass)
    if not networks:
        networks = await _fallback_networks()

    hosts = _iter_hosts(networks)
    if not hosts:
        return []

    results: list[dict[str, str | int | None]] = []
    semaphore = asyncio.Semaphore(64)

    async def probe(host: str) -> None:
        if len(results) >= limit:
            return
        async with semaphore:
            if len(results) >= limit:
                return
            if not await async_tcp_connect(host, port, timeout=0.35):
                return
            if not await async_probe_solaredge_modbus(host, port, unit_id=unit_id):
                return
            mac = await async_get_mac_from_host(host, port=port)
            results.append({"host": host, "port": port, "mac": mac})

    await asyncio.gather(*(probe(host) for host in hosts))

    # Stable order for deterministic UI defaults.
    results.sort(key=lambda item: ipaddress.ip_address(str(item["host"])))
    return results[:limit]


async def async_find_host_for_mac(
    hass: Any,
    mac: str,
    port: int = 1502,
) -> str | None:
    """Find the current IP address for a MAC address."""
    normalized = normalize_mac(mac)
    if not normalized:
        return None

    arp_entries = _read_proc_net_arp()
    old_ip = arp_entries.get(normalized)
    if old_ip and await async_tcp_connect(old_ip, port, timeout=0.8):
        return old_ip

    await async_scan_solaredge_modbus(hass, port=port, unit_id=1, limit=32)
    arp_entries = _read_proc_net_arp()
    new_ip = arp_entries.get(normalized)
    if new_ip and await async_tcp_connect(new_ip, port, timeout=0.8):
        return new_ip

    return None
