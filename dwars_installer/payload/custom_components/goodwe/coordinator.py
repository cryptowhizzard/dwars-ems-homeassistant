"""Update coordinator for Goodwe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from goodwe import Inverter, InverterError, RequestFailedException
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    BaseCoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_KEEP_ALIVE,
    CONF_MAC,
    DEFAULT_SCAN_INTERVAL,
)
from .discovery import (
    async_connect_and_detect_port,
    async_find_inverter_by_mac,
    build_updated_entry_data,
    build_updated_entry_options,
    entry_connection_options,
    normalize_mac,
)

_LOGGER = logging.getLogger(__name__)

RECOVERY_COOLDOWN = timedelta(seconds=30)

type GoodweConfigEntry = ConfigEntry[GoodweRuntimeData]


@dataclass
class GoodweRuntimeData:
    """Data class for runtime data."""

    inverter: Inverter
    coordinator: GoodweUpdateCoordinator
    device_info: DeviceInfo


class GoodweUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Gather data for the energy device."""

    config_entry: GoodweConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GoodweConfigEntry,
        inverter: Inverter,
    ) -> None:
        """Initialize update coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.inverter: Inverter = inverter
        self._last_data: dict[str, Any] = {}
        self._polled_entities: dict[BaseCoordinatorEntity, datetime] = {}
        self._last_recovery_attempt: datetime | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the inverter."""
        await self._update_polled_entities()

        try:
            self._last_data = self.data or {}
            return await self.inverter.read_runtime_data()
        except RequestFailedException as ex:
            # UDP communication with inverter is by definition unreliable.
            # It is rather normal in many environments to fail to receive
            # proper response in usual time, so we intentionally ignore isolated
            # failures and report problem with availability only after
            # consecutive streak of 3 failed requests.
            if ex.consecutive_failures_count < 3:
                _LOGGER.debug(
                    "No response received (streak of %d)", ex.consecutive_failures_count
                )
                # Return last known data.
                return self._last_data

            recovered = await self._async_recover_connection()
            if recovered is not None:
                return recovered

            # Inverter does not respond anymore (e.g. it went to sleep mode)
            # and could not be rediscovered by MAC.
            _LOGGER.debug(
                "Inverter not responding (streak of %d)", ex.consecutive_failures_count
            )
            raise UpdateFailed(ex) from ex
        except InverterError as ex:
            recovered = await self._async_recover_connection()
            if recovered is not None:
                return recovered
            raise UpdateFailed(ex) from ex

    async def _async_recover_connection(self) -> dict[str, Any] | None:
        """Recover an unreachable inverter by stored MAC address."""
        now = datetime.now()
        if (
            self._last_recovery_attempt is not None
            and now - self._last_recovery_attempt < RECOVERY_COOLDOWN
        ):
            return None
        self._last_recovery_attempt = now

        mac = normalize_mac(
            self.config_entry.options.get(
                CONF_MAC, self.config_entry.data.get(CONF_MAC)
            )
        )
        if not mac:
            return None

        _LOGGER.info("Trying GoodWe recovery by MAC %s", mac)
        discovered = await async_find_inverter_by_mac(self.hass, mac)
        if discovered is None:
            _LOGGER.debug("GoodWe recovery by MAC %s found no inverter", mac)
            return None

        connection_options = entry_connection_options(
            self.config_entry.data, self.config_entry.options
        )
        try:
            inverter, port, protocol = await async_connect_and_detect_port(
                host=discovered.host,
                **connection_options,
            )
        except InverterError:
            _LOGGER.debug(
                "GoodWe recovery found %s for MAC %s but connection failed",
                discovered.host,
                mac,
                exc_info=True,
            )
            return None

        inverter.set_keep_alive(self.config_entry.options.get(CONF_KEEP_ALIVE, False))
        self.inverter = inverter

        runtime_data = getattr(self.config_entry, "runtime_data", None)
        if runtime_data is not None:
            runtime_data.inverter = inverter

        new_data = build_updated_entry_data(
            self.config_entry.data,
            host=discovered.host,
            port=port,
            protocol=protocol,
            family=type(inverter).__name__,
            mac=discovered.mac or mac,
            discovery_name=discovered.name,
        )
        new_options = build_updated_entry_options(
            dict(self.config_entry.options),
            host=discovered.host,
            port=port,
            protocol=protocol,
            family=type(inverter).__name__,
            mac=discovered.mac or mac,
            discovery_name=discovered.name,
        )

        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data=new_data,
            options=new_options,
        )

        _LOGGER.info(
            "Recovered GoodWe inverter %s at new host %s",
            inverter.serial_number,
            discovered.host,
        )
        return await self.inverter.read_runtime_data()

    async def _update_polled_entities(self) -> None:
        for entity, interval in list(self._polled_entities.items()):
            if interval:
                try:
                    await entity.async_update()
                except InverterError:
                    _LOGGER.debug("Failed to update entity %s", entity.name)

    def sensor_value(self, sensor: str) -> Any:
        """Answer current (or last known) value of the sensor."""
        data = self.data or {}
        val = data.get(sensor)
        return val if val is not None else self._last_data.get(sensor)

    def total_sensor_value(self, sensor: str) -> Any:
        """Answer current value of the 'total' (never 0) sensor."""
        data = self.data or {}
        val = data.get(sensor)
        return val or self._last_data.get(sensor)

    def reset_sensor(self, sensor: str) -> None:
        """Reset sensor value to 0.

        Intended for "daily" cumulative sensors (e.g. PV energy produced today),
        which should be explicitly reset to 0 at midnight if inverter is suspended.
        """
        self._last_data[sensor] = 0
        if self.data is not None:
            self.data[sensor] = 0

    def entity_state_polling(
        self, entity: BaseCoordinatorEntity, interval: int
    ) -> None:
        """Enable/disable polling of entity state."""
        if interval:
            self._polled_entities[entity] = interval
        else:
            self._polled_entities.pop(entity, None)
