"""The Goodwe inverter component."""

from __future__ import annotations

import logging
from typing import Any

from goodwe import Inverter, InverterError
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_PROTOCOL, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo

from .const import (
    CONF_AUTO_LOAD_CONTROL,
    CONF_DEFAULT_AREA,
    CONF_DISCOVERY_NAME,
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
    DEFAULT_NETWORK_CIDR,
    DEFAULT_PRE_SCAN_ENABLED,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import GoodweConfigEntry, GoodweRuntimeData, GoodweUpdateCoordinator
from .discovery import (
    async_connect_and_detect_port,
    async_find_inverter_by_host,
    async_find_inverter_by_mac,
    build_updated_entry_data,
    build_updated_entry_options,
    entry_connection_options,
    complete_entry_data,
    normalize_mac,
)
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: GoodweConfigEntry) -> bool:
    """Set up the Goodwe components from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    try:
        inverter, host, port, protocol, mac, discovery_name = await async_connect_entry(
            hass, entry
        )
    except InverterError as err:
        raise ConfigEntryNotReady from err

    keep_alive = entry.options.get(CONF_KEEP_ALIVE, entry.data.get(CONF_KEEP_ALIVE, False))
    inverter.set_keep_alive(keep_alive)

    # If the entry was created manually and no MAC was available yet, try to
    # enrich the entry from the GoodWe broadcast response.
    if not mac:
        discovered = await async_find_inverter_by_host(
            hass,
            host,
            pre_scan_enabled=entry.options.get(
                CONF_PRE_SCAN_ENABLED,
                entry.data.get(CONF_PRE_SCAN_ENABLED, DEFAULT_PRE_SCAN_ENABLED),
            ),
            network_cidr=entry.options.get(
                CONF_NETWORK_CIDR,
                entry.data.get(CONF_NETWORK_CIDR, DEFAULT_NETWORK_CIDR),
            ),
        )
        if discovered and discovered.mac:
            mac = discovered.mac
            discovery_name = discovered.name
            await async_update_network_entry(
                hass,
                entry,
                host=host,
                port=port,
                protocol=protocol,
                family=type(inverter).__name__,
                mac=mac,
                discovery_name=discovery_name,
            )

    device_info = DeviceInfo(
        configuration_url="https://semsplus.goodwe.com/",
        identifiers={(DOMAIN, inverter.serial_number)},
        connections={(CONNECTION_NETWORK_MAC, mac)} if mac else set(),
        name=entry.title,
        manufacturer="GoodWe",
        model=inverter.model_name,
        serial_number=inverter.serial_number,
        sw_version=f"{inverter.firmware} / {inverter.arm_firmware}",
        hw_version=f"{inverter.serial_number[5:8]} {inverter.serial_number[0:5]}",
    )

    # Create update coordinator.
    coordinator = GoodweUpdateCoordinator(hass, entry, inverter)

    # Fetch initial data so we have data when entities subscribe.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = GoodweRuntimeData(
        inverter=inverter,
        coordinator=coordinator,
        device_info=device_info,
    )

    hass.data[DOMAIN][entry.entry_id] = entry.runtime_data

    entry.async_on_unload(entry.add_update_listener(update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await async_assign_device_to_default_area(hass, entry, inverter.serial_number, mac)

    await async_setup_services(hass)

    return True


async def async_connect_entry(
    hass: HomeAssistant, entry: GoodweConfigEntry
) -> tuple[Inverter, str, int, str, str | None, str | None]:
    """Connect to an inverter using entry data and recover by MAC when needed."""
    host = entry.options.get(CONF_HOST, entry.data[CONF_HOST])
    connection_options = entry_connection_options(entry.data, entry.options)
    mac = normalize_mac(entry.options.get(CONF_MAC, entry.data.get(CONF_MAC)))
    discovery_name = entry.options.get(
        CONF_DISCOVERY_NAME, entry.data.get(CONF_DISCOVERY_NAME)
    )

    try:
        inverter, port, protocol = await async_connect_and_detect_port(
            host=host,
            **connection_options,
        )
    except InverterError as original_error:
        if mac:
            _LOGGER.info(
                "GoodWe inverter at %s is unreachable; trying discovery by MAC %s",
                host,
                mac,
            )
            recovered = await async_find_inverter_by_mac(
                hass,
                mac,
                pre_scan_enabled=entry.options.get(
                    CONF_PRE_SCAN_ENABLED,
                    entry.data.get(CONF_PRE_SCAN_ENABLED, DEFAULT_PRE_SCAN_ENABLED),
                ),
                network_cidr=entry.options.get(
                    CONF_NETWORK_CIDR,
                    entry.data.get(CONF_NETWORK_CIDR, DEFAULT_NETWORK_CIDR),
                ),
                preferred_host=host,
            )
            if recovered:
                try:
                    inverter, port, protocol = await async_connect_and_detect_port(
                        host=recovered.host,
                        **connection_options,
                    )
                except InverterError:
                    try:
                        inverter, port, protocol = await async_connect_and_detect_port(
                            host=recovered.host,
                            protocol=connection_options["protocol"],
                            port=None,
                            family=connection_options["family"],
                            comm_addr=connection_options["comm_addr"],
                            timeout=connection_options["timeout"],
                            retries=connection_options["retries"],
                        )
                    except InverterError:
                        _LOGGER.debug(
                            "GoodWe MAC recovery found %s but connection still failed",
                            recovered.host,
                            exc_info=True,
                        )
                        inverter = None
                if inverter is not None:
                    await async_update_network_entry(
                        hass,
                        entry,
                        host=recovered.host,
                        port=port,
                        protocol=protocol,
                        family=type(inverter).__name__,
                        mac=recovered.mac or mac,
                        discovery_name=recovered.name,
                    )
                    return (
                        inverter,
                        recovered.host,
                        port,
                        protocol,
                        recovered.mac or mac,
                        recovered.name,
                    )

        # Last fallback: same host but detect whether the communication port changed.
        try:
            inverter, port, protocol = await async_connect_and_detect_port(
                host=host,
                protocol=connection_options["protocol"],
                port=None,
                family=connection_options["family"],
                comm_addr=connection_options["comm_addr"],
                timeout=connection_options["timeout"],
                retries=connection_options["retries"],
            )
        except InverterError:
            raise original_error

        await async_update_network_entry(
            hass,
            entry,
            host=host,
            port=port,
            protocol=protocol,
            family=type(inverter).__name__,
            mac=mac,
            discovery_name=discovery_name,
        )
        return inverter, host, port, protocol, mac, discovery_name

    await async_update_network_entry(
        hass,
        entry,
        host=host,
        port=port,
        protocol=protocol,
        family=type(inverter).__name__,
        mac=mac,
        discovery_name=discovery_name,
    )
    return inverter, host, port, protocol, mac, discovery_name


async def async_update_network_entry(
    hass: HomeAssistant,
    entry: GoodweConfigEntry,
    *,
    host: str,
    port: int,
    protocol: str,
    family: str,
    mac: str | None,
    discovery_name: str | None = None,
) -> None:
    """Update config entry data/options with current network information."""
    new_data = build_updated_entry_data(
        entry.data,
        host=host,
        port=port,
        protocol=protocol,
        family=family,
        mac=mac,
        discovery_name=discovery_name,
    )
    new_options = build_updated_entry_options(
        dict(entry.options),
        host=host,
        port=port,
        protocol=protocol,
        family=family,
        mac=mac,
        discovery_name=discovery_name,
    )

    if new_data != entry.data or new_options != entry.options:
        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            options=new_options,
        )


async def async_check_port(
    hass: HomeAssistant, entry: GoodweConfigEntry, host: str
) -> Inverter:
    """Check the communication port of the inverter.

    The communication port may change after a firmware update or WiFi/LAN module
    replacement.
    """
    connection_options = entry_connection_options(entry.data, entry.options)
    inverter, port, protocol = await async_connect_and_detect_port(
        host=host,
        protocol=connection_options["protocol"],
        port=None,
        family=connection_options["family"],
        comm_addr=connection_options["comm_addr"],
        timeout=connection_options["timeout"],
        retries=connection_options["retries"],
    )
    family = type(inverter).__name__
    mac = normalize_mac(entry.options.get(CONF_MAC, entry.data.get(CONF_MAC)))
    discovery_name = entry.options.get(
        CONF_DISCOVERY_NAME, entry.data.get(CONF_DISCOVERY_NAME)
    )

    await async_update_network_entry(
        hass,
        entry,
        host=host,
        port=port,
        protocol=protocol,
        family=family,
        mac=mac,
        discovery_name=discovery_name,
    )
    return inverter


async def async_assign_device_to_default_area(
    hass: HomeAssistant,
    entry: GoodweConfigEntry,
    serial_number: str,
    mac: str | None,
) -> None:
    """Assign the inverter device to the configured default area if unassigned."""
    area_name = entry.options.get(
        CONF_DEFAULT_AREA,
        entry.data.get(CONF_DEFAULT_AREA, DEFAULT_AREA_NAME),
    )
    if not area_name:
        return

    area_registry = ar.async_get(hass)
    area = area_registry.async_get_area_by_name(area_name)
    if area is None:
        area = area_registry.async_create(area_name)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(
        identifiers={(DOMAIN, serial_number)},
        connections={(CONNECTION_NETWORK_MAC, mac)} if mac else set(),
    )
    if device is None:
        return

    # Do not overwrite a room/area the user has already chosen manually.
    if device.area_id is None:
        device_registry.async_update_device(device.id, area_id=area.id)


async def async_unload_entry(
    hass: HomeAssistant, config_entry: GoodweConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )

    if unload_ok:
        hass.data[DOMAIN].pop(config_entry.entry_id)

        if not hass.data[DOMAIN]:
            await async_unload_services(hass)

    return unload_ok


async def update_listener(hass: HomeAssistant, config_entry: GoodweConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_migrate_entry(
    hass: HomeAssistant, config_entry: GoodweConfigEntry
) -> bool:
    """Upgrade the schema offline; never rediscover or overwrite a working device.

    DWARS 0.9.9.35 accidentally declared MINOR_VERSION=2 without VERSION=2.
    Both the resulting v1 entries and earlier proper v2 entries must stay usable.
    """
    if config_entry.version > 2:
        return False
    data = complete_entry_data(dict(config_entry.data))
    for key, default in (
        (CONF_DEFAULT_AREA, DEFAULT_AREA_NAME),
        (CONF_AUTO_LOAD_CONTROL, DEFAULT_AUTO_LOAD_CONTROL),
        (CONF_PRE_SCAN_ENABLED, DEFAULT_PRE_SCAN_ENABLED),
        (CONF_NETWORK_CIDR, DEFAULT_NETWORK_CIDR),
    ):
        if data.get(key) is None:
            data[key] = default
    # Null values from old migrations are not intentional user overrides.
    options = {key: value for key, value in config_entry.options.items() if value is not None}
    hass.config_entries.async_update_entry(
        config_entry, data=data, options=options, version=2, minor_version=3,
    )
    return True
