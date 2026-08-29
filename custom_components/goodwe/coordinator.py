"""Update coordinator for GoodWe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from goodwe import Inverter, InverterError, RequestFailedException
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    BaseCoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_KEEP_ALIVE,
    CONF_MAC,
    CONF_NETWORK_CIDR,
    CONF_PRE_SCAN_ENABLED,
    DEFAULT_NETWORK_CIDR,
    DEFAULT_PRE_SCAN_ENABLED,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
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
METADATA_SAVE_INTERVAL = timedelta(seconds=60)


type GoodweConfigEntry = ConfigEntry[GoodweRuntimeData]


@dataclass
class GoodweRuntimeData:
    """Runtime data for one GoodWe inverter."""

    inverter: Inverter
    coordinator: GoodweUpdateCoordinator
    device_info: DeviceInfo


class GoodweUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Gather runtime data and retain network metadata for diagnostics/recovery."""

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

        self.last_seen: datetime | None = None
        self.last_known_host: str = self.current_host
        self.last_known_mac: str | None = self.current_mac
        self.phase_count: int = self._detect_phase_count({})

        self._metadata_loaded = False
        self._metadata_store = Store[dict[str, Any]](
            hass, 1, f"{DOMAIN}.{entry.entry_id}.network_metadata"
        )
        self._last_metadata_save: datetime | None = None

    @property
    def serial_number(self) -> str:
        """Return the inverter serial number."""
        return str(self.inverter.serial_number or "")

    @property
    def current_host(self) -> str:
        """Return the currently configured inverter host."""
        return str(
            self.config_entry.options.get(
                CONF_HOST, self.config_entry.data.get(CONF_HOST, "")
            )
        )

    @property
    def current_mac(self) -> str | None:
        """Return the normalized stored MAC address."""
        return normalize_mac(
            self.config_entry.options.get(
                CONF_MAC, self.config_entry.data.get(CONF_MAC)
            )
        )

    @property
    def pre_scan_enabled(self) -> bool:
        """Return whether subnet pre-scanning is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_PRE_SCAN_ENABLED,
                self.config_entry.data.get(
                    CONF_PRE_SCAN_ENABLED, DEFAULT_PRE_SCAN_ENABLED
                ),
            )
        )

    @property
    def network_cidr(self) -> str:
        """Return the configured pre-scan network."""
        return str(
            self.config_entry.options.get(
                CONF_NETWORK_CIDR,
                self.config_entry.data.get(CONF_NETWORK_CIDR, DEFAULT_NETWORK_CIDR),
            )
            or ""
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the inverter."""
        await self._async_load_metadata()
        await self._update_polled_entities()

        try:
            self._last_data = self.data or {}
            data = await self.inverter.read_runtime_data()
            await self._async_record_success(data)
            return data
        except RequestFailedException as ex:
            # UDP communication is unreliable.  Keep the previous values for two
            # isolated misses, but do not advance last_seen unless a real response
            # was received.
            if ex.consecutive_failures_count < 3:
                _LOGGER.debug(
                    "No response received (streak of %d)", ex.consecutive_failures_count
                )
                return self._last_data

            recovered = await self._async_recover_connection()
            if recovered is not None:
                await self._async_record_success(recovered)
                return recovered

            _LOGGER.debug(
                "Inverter not responding (streak of %d)", ex.consecutive_failures_count
            )
            raise UpdateFailed(ex) from ex
        except InverterError as ex:
            recovered = await self._async_recover_connection()
            if recovered is not None:
                await self._async_record_success(recovered)
                return recovered
            raise UpdateFailed(ex) from ex

    async def _async_load_metadata(self) -> None:
        """Load persistent last-seen/network metadata once."""
        if self._metadata_loaded:
            return
        self._metadata_loaded = True
        stored = await self._metadata_store.async_load() or {}

        raw_last_seen = stored.get("last_seen")
        if isinstance(raw_last_seen, str):
            try:
                parsed = datetime.fromisoformat(raw_last_seen)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                self.last_seen = parsed
            except ValueError:
                pass

        if stored.get("last_known_host"):
            self.last_known_host = str(stored["last_known_host"])
        if normalize_mac(stored.get("last_known_mac")):
            self.last_known_mac = normalize_mac(stored.get("last_known_mac"))
        if stored.get("phase_count") in (1, 3):
            self.phase_count = int(stored["phase_count"])

    async def _async_record_success(self, data: dict[str, Any]) -> None:
        """Update last-seen, host, MAC and phase metadata after a real response."""
        now = datetime.now(timezone.utc)
        self.last_seen = now
        self.last_known_host = self.current_host or self.last_known_host
        self.last_known_mac = self.current_mac or self.last_known_mac
        self.phase_count = self._detect_phase_count(data)

        if (
            self._last_metadata_save is None
            or now - self._last_metadata_save >= METADATA_SAVE_INTERVAL
        ):
            await self._metadata_store.async_save(
                {
                    "last_seen": now.isoformat(),
                    "last_known_host": self.last_known_host,
                    "last_known_mac": self.last_known_mac,
                    "phase_count": self.phase_count,
                    "serial_number": self.serial_number,
                }
            )
            self._last_metadata_save = now

    def _detect_phase_count(self, data: dict[str, Any]) -> int:
        """Detect one/three phase from inverter identification and sensor layout."""
        output_type = getattr(self.inverter, "ac_output_type", None)
        if output_type == 0:
            return 1
        if output_type in (1, 2):
            return 3

        # Older families do not always expose ac_output_type.  The presence of
        # L2/L3 AC sensors is a reliable fallback even while their current value
        # happens to be zero.
        ids: set[str] = set(data)
        try:
            ids.update(str(sensor.id_).lower() for sensor in self.inverter.sensors())
        except (AttributeError, TypeError):
            pass
        ids = {item.lower() for item in ids}
        three_phase_markers = (
            "active_power_l2",
            "active_power_l3",
            "active_power2",
            "active_power3",
            "pgrid2",
            "pgrid3",
            "vgrid2",
            "vgrid3",
            "igrid2",
            "igrid3",
            "vac2",
            "vac3",
            "iac2",
            "iac3",
        )
        if any(
            marker == sensor_id or marker in sensor_id
            for sensor_id in ids
            for marker in three_phase_markers
        ):
            return 3
        return 1

    async def _async_recover_connection(self) -> dict[str, Any] | None:
        """Recover an unreachable inverter by its stored MAC address."""
        now = datetime.now(timezone.utc)
        if (
            self._last_recovery_attempt is not None
            and now - self._last_recovery_attempt < RECOVERY_COOLDOWN
        ):
            return None
        self._last_recovery_attempt = now

        mac = self.current_mac or self.last_known_mac
        if not mac:
            return None

        _LOGGER.info("Trying GoodWe recovery by MAC %s", mac)
        discovered = await async_find_inverter_by_mac(
            self.hass,
            mac,
            pre_scan_enabled=self.pre_scan_enabled,
            network_cidr=self.network_cidr,
            preferred_host=self.last_known_host or self.current_host,
        )
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
            # The stored explicit port may be stale.  Retry both standard ports.
            try:
                inverter, port, protocol = await async_connect_and_detect_port(
                    host=discovered.host,
                    protocol=connection_options["protocol"],
                    port=None,
                    family=connection_options["family"],
                    comm_addr=connection_options["comm_addr"],
                    timeout=connection_options["timeout"],
                    retries=connection_options["retries"],
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

        self.last_known_host = discovered.host
        self.last_known_mac = discovered.mac or mac
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
        """Return current or last known sensor value."""
        data = self.data or {}
        value = data.get(sensor)
        return value if value is not None else self._last_data.get(sensor)

    def total_sensor_value(self, sensor: str) -> Any:
        """Return current value of a cumulative sensor, preserving non-zero last value."""
        data = self.data or {}
        value = data.get(sensor)
        return value or self._last_data.get(sensor)

    def reset_sensor(self, sensor: str) -> None:
        """Reset a daily cumulative sensor to zero."""
        self._last_data[sensor] = 0
        if self.data is not None:
            self.data[sensor] = 0

    def entity_state_polling(
        self, entity: BaseCoordinatorEntity, interval: int
    ) -> None:
        """Enable or disable polling of an entity state."""
        if interval:
            self._polled_entities[entity] = interval
        else:
            self._polled_entities.pop(entity, None)
