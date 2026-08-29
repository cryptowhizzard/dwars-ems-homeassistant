#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DWARS/MetDeZon GoodWe Home Assistant agent.

The agent intentionally controls the inverter through Home Assistant entities.  It
has four independent cadences:

* 10 s: fuse protection / charge-block latch.
* 30 s: standalone zero-export compensation.
* 60 s: BMS/EMS decision, telemetry and mandatory GoodWe defaults.
* 60 s: rediscovery of serial-specific entity IDs and inverter metadata.

The BMS API key is the only customer-specific identifier that is required.  The
API response can supply reserve SoC and the remaining managed defaults.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import time
import traceback
from typing import Any, Iterable
from urllib.parse import urlsplit

import requests


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "ja", "aan")


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(float(str(os.environ.get(name, default)).strip())))
    except (TypeError, ValueError):
        return max(minimum, default)


def log(message: str) -> None:
    print(f"[GoodWe] {message}", flush=True)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("API_KEY") or os.environ.get("api_key") or ""
CLIENT_ID = os.environ.get("CLIENT_ID") or os.environ.get("client_id") or ""
API_URL = os.environ.get(
    "API_URL", "https://api.metdezon.nl/bms/api/next_action.php"
)
TEL_URL = os.environ.get(
    "TELEMETRY_URL", "https://api.metdezon.nl/bms/api/telemetry.php"
)
VERIFY_SSL = env_bool("VERIFY_SSL", True)

DECISION_INTERVAL = env_int("INTERVAL", 60, 10)
SAFETY_INTERVAL = env_int("SAFETY_INTERVAL", 10, 2)
STANDALONE_INTERVAL = env_int("STANDALONE_INTERVAL", 30, 5)
DEFAULTS_INTERVAL = env_int("DEFAULTS_INTERVAL", 60, 10)
ENTITY_DISCOVERY_INTERVAL = env_int("ENTITY_DISCOVERY_INTERVAL", 60, 10)
POWER = env_int("POWER", 5000, 0)
DEBUG = env_bool("DEBUG", False)

AGENT_NAME = os.environ.get("ADDON_NAME", "GoodWe Agent")
AGENT_VERSION = os.environ.get("ADDON_VERSION", "unknown")
AGENT_TYPE = os.environ.get("AGENT_TYPE", "goodwe")

BACKUP_YAML_CHECK_ENABLED = env_bool("BACKUP_YAML_CHECK_ENABLED", True)
BACKUP_YAML_PATH = os.environ.get("BACKUP_YAML_PATH", "/config/backup.yaml")
BACKUP_YAML_OVERWRITE = env_bool("BACKUP_YAML_OVERWRITE", False)

DEFAULT_HA_URL = "http://supervisor/core"
HA_URL_ENV = os.environ.get("HA_URL", DEFAULT_HA_URL) or DEFAULT_HA_URL
DISABLE_HA = env_bool("DISABLE_HA", False)
HA_CONTROL_ENABLED = env_bool("HA_CONTROL_ENABLED", True)
AUTO_ENTITY_DISCOVERY = env_bool("HA_AUTO_ENTITY_DISCOVERY", True)
SERIAL_NUMBER_ENV = os.environ.get("GOODWE_SERIAL_NUMBER", "").strip()

# Telemetry. "auto" means: select the entity belonging to the detected serial.
SOC_ENTITY_RAW = os.environ.get("SOC_ENTITY", "auto")
MODE_ENTITY_RAW = os.environ.get("MODE_ENTITY", "")
PV_ENTITY_RAW = os.environ.get("PV_ENTITY", "auto")
GRID_ENTITY_RAW = os.environ.get("GRID_ENTITY", "auto")
BATTERY_POWER_ENTITY_RAW = os.environ.get("BATTERY_POWER_ENTITY", "auto")
PHASE_ENTITY_RAW = os.environ.get("GOODWE_PHASE_ENTITY", "auto")
SERIAL_ENTITY_RAW = os.environ.get("GOODWE_SERIAL_ENTITY", "auto")
IP_ENTITY_RAW = os.environ.get("GOODWE_IP_ENTITY", "auto")
MAC_ENTITY_RAW = os.environ.get("GOODWE_MAC_ENTITY", "auto")
LAST_SEEN_ENTITY_RAW = os.environ.get("GOODWE_LAST_SEEN_ENTITY", "auto")

# GoodWe EMS entities.
EMS_MODE_ENTITY_RAW = (
    os.environ.get("HA_EMS_MODE_SELECT")
    or os.environ.get("EMS_MODE_ENTITY")
    or "auto"
)
EMS_POWER_NUMBER_RAW = (
    os.environ.get("HA_EMS_POWER_NUMBER")
    or os.environ.get("EMS_POWER_NUMBER")
    or "auto"
)
EMS_SET_POWER_MODES_RAW = os.environ.get("HA_EMS_SET_POWER_MODES", "3,4")
EMS_SET_POWER_BEFORE_MODE = env_bool("HA_EMS_SET_POWER_BEFORE_MODE", True)
EMS_POWER_VALUE_SPEC = os.environ.get("HA_EMS_POWER_VALUE", "server_power")

# Mode 1 is an explicit battery hold/standby mode.  Legacy import_ac/export_ac
# values are normalized later so upgraded installations do not keep using them.
EMS_MODE_OPTIONS: dict[int, str] = {
    0: os.environ.get("HA_EMS_MODE_0_OPTION", "auto"),
    1: os.environ.get("HA_EMS_MODE_1_OPTION", "battery_standby"),
    3: os.environ.get("HA_EMS_MODE_3_OPTION", "charge_battery"),
    4: os.environ.get("HA_EMS_MODE_4_OPTION", "discharge_battery"),
    7: os.environ.get("HA_EMS_MODE_7_OPTION", "auto"),
}

# Managed defaults.  The BMS reserve SoC always takes precedence for the two
# DOD values.  With OVERRIDE_DEFAULT_VALUES enabled, the configured values are
# used; otherwise the safe DWARS defaults are enforced.
OVERRIDE_DEFAULT_VALUES = env_bool("OVERRIDE_DEFAULT_VALUES", False)
DEFAULT_DOD = env_int("GOODWE_DEFAULT_DOD", 90, 0)
DEFAULT_DOD_ON_GRID = env_int("GOODWE_DEFAULT_DOD_ON_GRID", 90, 0)
DEFAULT_DOD_HOLDING = os.environ.get("GOODWE_DEFAULT_DOD_HOLDING", "off")
DEFAULT_BACKUP_SUPPLY = os.environ.get("GOODWE_DEFAULT_BACKUP_SUPPLY", "on")
DEFAULT_OPERATION_MODE = os.environ.get("GOODWE_DEFAULT_OPERATION_MODE", "general")

DOD_HOLDING_SWITCH_RAW = os.environ.get("HA_DOD_HOLDING_SWITCH", "auto")
BACKUP_SUPPLY_SWITCH_RAW = os.environ.get("HA_BACKUP_SUPPLY_SWITCH", "auto")
DOD_NUMBER_RAW = os.environ.get("HA_DOD_NUMBER", "auto")
DOD_ON_GRID_NUMBER_RAW = os.environ.get("HA_DOD_ON_GRID_NUMBER", "auto")
OPERATION_MODE_SELECT_RAW = os.environ.get("HA_OPERATION_MODE_SELECT", "auto")

# Grid export limit.  "auto" resolves to 3000 W on one phase and 5000 W on
# three phases.  Switch restore defaults to ON as requested.
GRID_EXPORT_LIMIT_ENTITIES_RAW = (
    os.environ.get("HA_GRID_EXPORT_LIMIT_NUMBER")
    or os.environ.get("GOODWE_GRID_EXPORT_LIMIT_NUMBER")
    or "auto"
)
GRID_EXPORT_LIMIT_SWITCHES_RAW = (
    os.environ.get("HA_GRID_EXPORT_LIMIT_SWITCH")
    or os.environ.get("GOODWE_GRID_EXPORT_LIMIT_SWITCH")
    or "auto"
)
GRID_EXPORT_LIMIT_OFF_VALUE = os.environ.get("HA_GRID_EXPORT_LIMIT_OFF_VALUE", "0")
GRID_EXPORT_LIMIT_DEFAULT_VALUE = os.environ.get(
    "HA_GRID_EXPORT_LIMIT_DEFAULT_VALUE", "auto"
)
GRID_EXPORT_LIMIT_SWITCH_CURTAIL_STATE = os.environ.get(
    "HA_GRID_EXPORT_LIMIT_SWITCH_CURTAIL_STATE", "on"
)
GRID_EXPORT_LIMIT_SWITCH_RESTORE_STATE = os.environ.get(
    "HA_GRID_EXPORT_LIMIT_SWITCH_RESTORE_STATE", "on"
)
PV_CURTAIL_BELOW_EUR_KWH_ENV = os.environ.get(
    "HA_PV_CURTAIL_BELOW_EUR_KWH", ""
)
PV_CURTAIL_ENABLED = env_bool("HA_PV_CURTAIL_ENABLED", True)

# Fast fuse protection.  Thresholds use "auto" by default:
# one phase -3500/-2000 W; three phase -8000/-5000 W.
CHARGE_BLOCK_ENABLED = env_bool("HA_CHARGE_BLOCK_ENABLED", True)
CHARGE_BLOCK_SENSOR_RAW = os.environ.get("HA_CHARGE_BLOCK_SENSOR", "auto")
CHARGE_BLOCK_TRIGGER_RAW = os.environ.get("HA_CHARGE_BLOCK_BELOW_W", "auto")
CHARGE_BLOCK_RELEASE_RAW = os.environ.get(
    "HA_CHARGE_BLOCK_RELEASE_ABOVE_W", "auto"
)
CHARGE_BLOCK_DURATION_SEC = env_int("HA_CHARGE_BLOCK_DURATION_SEC", 300, 0)
CHARGE_BLOCK_MODES_RAW = os.environ.get("HA_CHARGE_BLOCK_MODES", "3")
CHARGE_BLOCK_FALLBACK_OPTION = os.environ.get(
    "HA_CHARGE_BLOCK_FALLBACK_OPTION", "auto"
)

# Standalone inverter with PV on another inverter/meter.
STANDALONE_ENABLED = env_bool("STANDALONE_ENABLED", False)
STANDALONE_PV_ENTITY_RAW = os.environ.get("STANDALONE_PV_ENTITY", "")
STANDALONE_GRID_ENTITY_RAW = os.environ.get("STANDALONE_GRID_ENTITY", "auto")
STANDALONE_DEADBAND_W = env_int("STANDALONE_DEADBAND_W", 150, 0)
STANDALONE_MAX_CHARGE_W = env_int("STANDALONE_MAX_CHARGE_W", 0, 0)

HEADERS_EXT = {"X-API-Key": API_KEY} if API_KEY else {}

BACKUP_YAML_CONTENT = """- alias: Auto update everything
  description: Automatically install updates
  trigger:
    - platform: time
      at: "03:00:00"

  action:
    - service: backup.create_automatic

    - delay: "00:02:00"

    - service: update.install
      target:
        entity_id: all

  mode: single
"""


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def parse_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text or text.lower() in ("unknown", "unavailable", "none", "null"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: Any, default: int = 0) -> int:
    parsed = parse_float(value)
    return default if parsed is None else int(round(parsed))


def parse_bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "ja", "aan"):
        return True
    if text in ("0", "false", "no", "off", "nee", "uit"):
        return False
    return None


def split_entities(raw: str | Iterable[str]) -> list[str]:
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw or "").strip()
    if not text or text.lower() in ("none", "skip", "disabled", "false", "off"):
        return []
    return [part.strip() for part in re.split(r"[,;\s]+", text) if part.strip()]


def normalize_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def normalize_serial(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "").strip()).upper()


def serial_slug(value: Any) -> str:
    return normalize_key(normalize_serial(value))


def parse_int_set(raw: str, label: str) -> set[int]:
    out: set[int] = set()
    for part in re.split(r"[,;\s]+", str(raw or "")):
        if not part.strip():
            continue
        try:
            out.add(int(part))
        except ValueError:
            log(f"WARN: invalid integer in {label}: {part!r}")
    return out


def nested_get(data: dict[str, Any], path: list[str]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def normalize_ems_option(option: str) -> str:
    key = normalize_key(option)
    aliases = {
        "import_ac": "charge_battery",
        "buy_power": "charge_battery",
        "charge_ac": "charge_battery",
        "export_ac": "discharge_battery",
        "sell_power": "discharge_battery",
        "standby": "battery_standby",
    }
    return aliases.get(key, key)


EMS_SET_POWER_MODES = parse_int_set(EMS_SET_POWER_MODES_RAW, "HA_EMS_SET_POWER_MODES") or {3, 4}
CHARGE_BLOCK_MODES = parse_int_set(CHARGE_BLOCK_MODES_RAW, "HA_CHARGE_BLOCK_MODES") or {3}
EMS_MODE_OPTIONS = {mode: normalize_ems_option(option) for mode, option in EMS_MODE_OPTIONS.items()}


# ---------------------------------------------------------------------------
# HA API and entity discovery
# ---------------------------------------------------------------------------


def ha_base_url() -> str:
    url = (HA_URL_ENV or DEFAULT_HA_URL).strip().rstrip("/")
    if not url.endswith("/api"):
        url += "/api"
    return url


def ha_uses_supervisor_proxy() -> bool:
    parsed = urlsplit(ha_base_url())
    return (parsed.hostname or "").lower() in {"supervisor", "hassio"}


def _first_token(*names: str) -> str:
    for name in names:
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def ha_token_candidates() -> list[tuple[str, str]]:
    """Return unique bearer tokens in the correct order for the HA URL.

    The internal Supervisor Core proxy explicitly requires SUPERVISOR_TOKEN.
    A user-supplied long-lived access token remains useful for a direct HA URL
    and as a fallback when an installation has a non-standard proxy setup.
    """

    supervisor = _first_token(
        "SUPERVISOR_TOKEN", "SUPERVISOR_ACCESS_TOKEN", "HASSIO_TOKEN"
    )
    configured = _first_token(
        "CONFIGURED_HA_TOKEN", "HA_TOKEN", "HOMEASSISTANT_TOKEN"
    )
    ordered = (
        [("SUPERVISOR_TOKEN", supervisor), ("configured HA token", configured)]
        if ha_uses_supervisor_proxy()
        else [("configured HA token", configured), ("SUPERVISOR_TOKEN", supervisor)]
    )
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, token in ordered:
        if token and token not in seen:
            result.append((source, token))
            seen.add(token)
    return result


_HA_SESSION = requests.Session()
_HA_SELECTED_TOKEN: tuple[str, str] | None = None
_HA_LAST_AUTH_ERROR = ""
_HA_LAST_AUTH_SOURCE = ""
_DEVICE_METADATA_CACHE: dict[str, dict[str, Any]] = {}


def ha_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": f"DWARS-GoodWe-Agent/{AGENT_VERSION}",
    }


def ha_request(
    method: str,
    path: str,
    *,
    timeout: int = 8,
    json_payload: dict[str, Any] | None = None,
) -> requests.Response | None:
    """Call Home Assistant and automatically recover from a stale token.

    Only 401/403 responses are retried with another token. Other HTTP statuses
    prove authentication worked and are returned to the caller unchanged.
    """

    global _HA_SELECTED_TOKEN, _HA_LAST_AUTH_ERROR, _HA_LAST_AUTH_SOURCE
    if DISABLE_HA:
        return None

    candidates = ha_token_candidates()
    if not candidates:
        signature = f"no-token:{ha_base_url()}"
        if signature != _HA_LAST_AUTH_ERROR:
            log(
                "ERROR: no Home Assistant bearer token available; enable "
                "homeassistant_api or configure a valid long-lived access token"
            )
            _HA_LAST_AUTH_ERROR = signature
        return None

    if _HA_SELECTED_TOKEN in candidates:
        candidates = [_HA_SELECTED_TOKEN] + [
            candidate for candidate in candidates if candidate != _HA_SELECTED_TOKEN
        ]

    auth_failures: list[str] = []
    last_response: requests.Response | None = None
    for source, token in candidates:
        try:
            response = _HA_SESSION.request(
                method,
                f"{ha_base_url()}/{path.lstrip('/')}",
                headers=ha_headers(token),
                json=json_payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            if DEBUG:
                log(f"HA {method.upper()} {path} error via {source}: {exc}")
            # A second token cannot fix a transport error to the same endpoint.
            return None

        last_response = response
        if response.status_code in (401, 403):
            auth_failures.append(f"{source}={response.status_code}")
            if _HA_SELECTED_TOKEN == (source, token):
                _HA_SELECTED_TOKEN = None
            continue

        _HA_SELECTED_TOKEN = (source, token)
        _HA_LAST_AUTH_ERROR = ""
        if source != _HA_LAST_AUTH_SOURCE:
            log(f"HA authentication OK via {source}")
            _HA_LAST_AUTH_SOURCE = source
        return response

    signature = f"{ha_base_url()}|{'|'.join(auth_failures)}"
    if signature != _HA_LAST_AUTH_ERROR:
        log(
            "ERROR: Home Assistant authentication failed at "
            f"{ha_base_url()} ({', '.join(auth_failures) or 'no response'}). "
            "For http://supervisor/core leave ha_token empty or ensure the "
            "injected SUPERVISOR_TOKEN is available."
        )
        _HA_LAST_AUTH_ERROR = signature
    return last_response


def ha_get_state(entity_id: str) -> dict[str, Any] | None:
    if DISABLE_HA or not entity_id:
        return None
    response = ha_request("GET", f"states/{entity_id}", timeout=5)
    if response is not None and response.status_code == 200:
        try:
            data = response.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    if DEBUG and response is not None:
        log(f"HA GET {entity_id} -> {response.status_code} {response.text[:160]}")
    return None


def ha_get_all_states() -> list[dict[str, Any]]:
    response = ha_request("GET", "states", timeout=10)
    if response is None:
        return []
    if response.status_code != 200:
        if DEBUG:
            log(f"HA state inventory -> {response.status_code} {response.text[:160]}")
        return []
    try:
        data = response.json()
    except ValueError as exc:
        if DEBUG:
            log(f"HA state inventory JSON error: {exc}")
        return []
    return data if isinstance(data, list) else []


def ha_call_service(domain: str, service: str, payload: dict[str, Any]) -> bool:
    response = ha_request(
        "POST", f"services/{domain}/{service}", timeout=8, json_payload=payload
    )
    if response is None:
        return False
    ok = 200 <= response.status_code < 300
    if DEBUG or not ok:
        log(
            f"HA SERVICE {domain}.{service} {payload} -> "
            f"{response.status_code} {response.text[:160]}"
        )
    return ok


def ha_get_config_version() -> str | None:
    response = ha_request("GET", "config", timeout=5)
    if response is None or response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return str(data["version"]) if isinstance(data, dict) and data.get("version") else None


def ha_render_template(template: str) -> str | None:
    response = ha_request(
        "POST", "template", timeout=8, json_payload={"template": template}
    )
    if response is None or response.status_code != 200:
        return None
    return response.text.strip()


def ha_entity_device_metadata(entity_id: str) -> dict[str, Any]:
    """Read device-registry metadata for an entity through HA's template API.

    REST state objects do not contain device_id or the GoodWe unique_id. Device
    metadata is therefore used to match entities to the configured inverter
    serial when more than one GoodWe inverter exists or entity IDs were renamed.
    """

    if not entity_id:
        return {}
    cached = _DEVICE_METADATA_CACHE.get(entity_id)
    if cached is not None:
        return cached

    literal = json.dumps(entity_id)
    template = (
        "{% set e = "
        + literal
        + " %}{{ {"
        + "'device_id': device_id(e), "
        + "'serial_number': device_attr(e, 'serial_number'), "
        + "'manufacturer': device_attr(e, 'manufacturer'), "
        + "'model': device_attr(e, 'model'), "
        + "'name': device_attr(e, 'name'), "
        + "'connections': (device_attr(e, 'connections') | string), "
        + "'configuration_url': device_attr(e, 'configuration_url')"
        + "} | to_json }}"
    )
    rendered = ha_render_template(template)
    metadata: dict[str, Any] = {}
    if rendered:
        try:
            parsed = json.loads(rendered)
            if isinstance(parsed, dict):
                metadata = parsed
        except ValueError:
            if DEBUG:
                log(f"HA device metadata JSON error for {entity_id}: {rendered[:160]}")
    _DEVICE_METADATA_CACHE[entity_id] = metadata
    return metadata


class EntityMap:
    """Resolved entities and inverter metadata for one GoodWe device."""

    def __init__(self) -> None:
        self.serial = normalize_serial(SERIAL_NUMBER_ENV)
        self.phase_count = 0
        self.ip_address: str | None = None
        self.mac_address: str | None = None
        self.last_seen: str | None = None
        self.entities: dict[str, str] = {}
        self._inventory: list[dict[str, Any]] = []
        self._has_refreshed = False
        self._last_discovery_signature = ""
        self._forced_auto_warning_logged = False

    def entity(self, key: str) -> str:
        return self.entities.get(key, "")

    @staticmethod
    def _entity_domain(entity_id: str) -> str:
        return entity_id.split(".", 1)[0] if "." in entity_id else ""

    @staticmethod
    def _entity_object_id(entity_id: str) -> str:
        return entity_id.split(".", 1)[1] if "." in entity_id else entity_id

    @staticmethod
    def _state_available(state: dict[str, Any]) -> bool:
        return str(state.get("state") or "").lower() not in (
            "unknown",
            "unavailable",
            "none",
            "null",
            "",
        )

    @staticmethod
    def _state_search_text(state: dict[str, Any]) -> tuple[str, str, str]:
        entity_id = str(state.get("entity_id") or "")
        attrs = state.get("attributes") or {}
        object_key = normalize_key(EntityMap._entity_object_id(entity_id))
        friendly = normalize_key(attrs.get("friendly_name", ""))
        combined = normalize_key(f"{entity_id} {attrs.get('friendly_name', '')}")
        return object_key, friendly, combined

    @staticmethod
    def _looks_goodwe(state: dict[str, Any], metadata: dict[str, Any] | None = None) -> bool:
        _object_key, _friendly, combined = EntityMap._state_search_text(state)
        if "goodwe" in combined:
            return True
        attrs = state.get("attributes") or {}
        attr_serial = attrs.get("serial_number") or attrs.get("goodwe_serial")
        if attr_serial:
            return True
        metadata = metadata or {}
        manufacturer = normalize_key(metadata.get("manufacturer"))
        return "goodwe" in manufacturer

    def _auto_requested(self) -> bool:
        raw_values = (
            SOC_ENTITY_RAW,
            PV_ENTITY_RAW,
            GRID_ENTITY_RAW,
            BATTERY_POWER_ENTITY_RAW,
            PHASE_ENTITY_RAW,
            SERIAL_ENTITY_RAW,
            IP_ENTITY_RAW,
            MAC_ENTITY_RAW,
            LAST_SEEN_ENTITY_RAW,
            EMS_MODE_ENTITY_RAW,
            EMS_POWER_NUMBER_RAW,
            DOD_HOLDING_SWITCH_RAW,
            BACKUP_SUPPLY_SWITCH_RAW,
            DOD_NUMBER_RAW,
            DOD_ON_GRID_NUMBER_RAW,
            OPERATION_MODE_SELECT_RAW,
            GRID_EXPORT_LIMIT_ENTITIES_RAW,
            GRID_EXPORT_LIMIT_SWITCHES_RAW,
            CHARGE_BLOCK_SENSOR_RAW,
            STANDALONE_GRID_ENTITY_RAW,
        )
        return any(str(value or "").strip().lower() == "auto" for value in raw_values)

    def _goodwe_states(self) -> list[dict[str, Any]]:
        """Return probable GoodWe states without requiring serial in entity_id.

        HA entity IDs such as number.goodwe_ems_power_limit normally do not carry
        the inverter serial. The previous hard serial filter removed every valid
        control entity when goodwe_serial_number was configured.
        """

        result: list[dict[str, Any]] = []
        for state in self._inventory:
            entity_id = str(state.get("entity_id") or "")
            if not entity_id:
                continue
            object_key, friendly, combined = self._state_search_text(state)
            attrs = state.get("attributes") or {}
            attr_serial = normalize_serial(
                attrs.get("serial_number") or attrs.get("goodwe_serial")
            )
            serial = normalize_serial(self.serial)
            if (
                "goodwe" in combined
                or (serial and serial_slug(serial) in combined)
                or (serial and attr_serial == serial)
                or object_key.startswith("goodwe_")
                or friendly.startswith("goodwe_")
            ):
                result.append(state)
        return result

    def _candidate_device_score(
        self, entity_id: str, state: dict[str, Any]
    ) -> tuple[int, bool]:
        """Return extra score and whether a known serial mismatches."""

        metadata = ha_entity_device_metadata(entity_id)
        score = 0
        mismatch = False
        metadata_serial = normalize_serial(metadata.get("serial_number"))
        wanted_serial = normalize_serial(self.serial)
        if wanted_serial and metadata_serial:
            if metadata_serial == wanted_serial:
                score += 2500
            else:
                mismatch = True
        if "goodwe" in normalize_key(metadata.get("manufacturer")):
            score += 300
        if self._looks_goodwe(state, metadata):
            score += 150
        return score, mismatch

    def _find_by_suffixes(
        self,
        domains: tuple[str, ...],
        suffixes: tuple[str, ...],
        *,
        states: list[dict[str, Any]] | None = None,
    ) -> str:
        candidates = states if states is not None else self._inventory
        wanted_serial_slug = serial_slug(self.serial)
        ranked: list[tuple[int, str, dict[str, Any]]] = []

        for state in candidates:
            entity_id = str(state.get("entity_id") or "")
            if not entity_id or self._entity_domain(entity_id) not in domains:
                continue
            object_key, friendly, combined = self._state_search_text(state)
            best_match_score = 0
            for index, suffix in enumerate(suffixes):
                suffix_key = normalize_key(suffix)
                if not suffix_key:
                    continue
                specificity = max(0, 100 - index)
                if object_key == suffix_key:
                    match_score = 1500 + specificity
                elif object_key == f"goodwe_{suffix_key}":
                    match_score = 1450 + specificity
                elif object_key.endswith(f"_{suffix_key}"):
                    match_score = 1250 + specificity
                elif object_key.endswith(suffix_key):
                    match_score = 1150 + specificity
                elif friendly == suffix_key or friendly == f"goodwe_{suffix_key}":
                    match_score = 1050 + specificity
                elif friendly.endswith(f"_{suffix_key}") or friendly.endswith(suffix_key):
                    match_score = 900 + specificity
                elif suffix_key in object_key:
                    match_score = 650 + specificity
                elif suffix_key in friendly:
                    match_score = 500 + specificity
                else:
                    continue
                best_match_score = max(best_match_score, match_score)

            if not best_match_score:
                continue

            if "goodwe" in combined:
                best_match_score += 300
            if wanted_serial_slug and wanted_serial_slug in combined:
                best_match_score += 900
            if self._state_available(state):
                best_match_score += 20
            if self._entity_domain(entity_id).startswith("input_"):
                # Prefer the integration's native entity over an optional helper.
                best_match_score -= 25
            ranked.append((best_match_score, entity_id, state))

        if not ranked:
            return ""

        evaluated: list[tuple[int, str]] = []
        for score, entity_id, state in ranked:
            extra, mismatch = self._candidate_device_score(entity_id, state)
            if mismatch:
                continue
            evaluated.append((score + extra, entity_id))

        if not evaluated:
            return ""
        evaluated.sort(key=lambda item: (-item[0], item[1]))
        if DEBUG and len(evaluated) > 1 and evaluated[0][0] == evaluated[1][0]:
            log(
                f"WARN: ambiguous HA entity candidates {evaluated[:4]}; "
                f"selected {evaluated[0][1]}"
            )
        return evaluated[0][1]

    def _resolve_explicit(
        self,
        raw: str,
        domains: tuple[str, ...],
        suffixes: tuple[str, ...],
    ) -> str:
        raw = str(raw or "").strip()
        if raw and raw.lower() != "auto":
            return raw
        return self._find_by_suffixes(domains, suffixes)

    def _discover_serial_from_device_registry(self) -> None:
        if self.serial:
            return
        for state in self._goodwe_states():
            entity_id = str(state.get("entity_id") or "")
            metadata = ha_entity_device_metadata(entity_id)
            serial = normalize_serial(metadata.get("serial_number"))
            if serial:
                self.serial = serial
                return

    @staticmethod
    def _extract_mac(value: Any) -> str | None:
        match = re.search(
            r"(?i)(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}", str(value or "")
        )
        return match.group(0).lower().replace("-", ":") if match else None

    @staticmethod
    def _extract_ip(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            host = urlsplit(text).hostname if "://" in text else text
            if host:
                ipaddress.ip_address(host)
                return host
        except ValueError:
            return None
        return None

    def refresh(self) -> None:
        auto_required = self._auto_requested()
        # Even with periodic discovery disabled, perform one initial inventory
        # pass so explicitly configured entities and diagnostics are populated.
        if not AUTO_ENTITY_DISCOVERY and not auto_required and self._has_refreshed:
            return
        if not AUTO_ENTITY_DISCOVERY and auto_required and not self._forced_auto_warning_logged:
            log(
                "WARN: ha_auto_entity_discovery is off while one or more entity "
                "options are 'auto'; discovery remains enabled for those fields"
            )
            self._forced_auto_warning_logged = True

        inventory = ha_get_all_states()
        if not inventory:
            return
        self._inventory = inventory
        self._has_refreshed = True
        _DEVICE_METADATA_CACHE.clear()

        # Serial sensor first; device-registry serial is the fallback.
        if not self.serial:
            serial_entity = self._find_by_suffixes(
                ("sensor",),
                ("inverter_serial_number", "serial_number", "inverter_serial"),
                states=inventory,
            )
            if serial_entity:
                serial_state = next(
                    (s for s in inventory if s.get("entity_id") == serial_entity), None
                )
                if serial_state and self._state_available(serial_state):
                    self.serial = normalize_serial(serial_state.get("state"))
            self._discover_serial_from_device_registry()

        mapping = {
            "soc": (
                SOC_ENTITY_RAW,
                ("sensor",),
                ("battery_state_of_charge", "battery_soc", "state_of_charge"),
            ),
            "mode": (
                MODE_ENTITY_RAW,
                ("sensor", "select", "input_select"),
                ("battery_mode", "ems_mode"),
            ),
            "pv": (
                PV_ENTITY_RAW,
                ("sensor",),
                ("pv_power", "ppv", "pv_power_total", "total_pv_power"),
            ),
            "grid": (
                GRID_ENTITY_RAW,
                ("sensor",),
                (
                    "active_power_total",
                    "meter_active_power",
                    "grid_power",
                    "active_power",
                    "pgrid",
                ),
            ),
            "battery_power": (
                BATTERY_POWER_ENTITY_RAW,
                ("sensor",),
                ("battery_power", "pbattery1", "pbattery"),
            ),
            "phase": (
                PHASE_ENTITY_RAW,
                ("sensor",),
                ("inverter_nr_phase", "inverter_phase_count", "phase_count"),
            ),
            "serial": (
                SERIAL_ENTITY_RAW,
                ("sensor",),
                ("inverter_serial_number", "serial_number", "inverter_serial"),
            ),
            "ip": (
                IP_ENTITY_RAW,
                ("sensor",),
                ("inverter_ip_address", "ip_address", "inverter_ip"),
            ),
            "mac": (
                MAC_ENTITY_RAW,
                ("sensor",),
                ("inverter_mac_address", "mac_address", "inverter_mac"),
            ),
            "last_seen": (
                LAST_SEEN_ENTITY_RAW,
                ("sensor",),
                ("inverter_last_seen", "last_seen_on", "last_seen"),
            ),
            "ems_mode": (
                EMS_MODE_ENTITY_RAW,
                ("select", "input_select"),
                ("ems_mode", "inverter_ems_mode"),
            ),
            # HA NumberEntity mode=slider and mode=box are the same domain/API.
            "ems_power": (
                EMS_POWER_NUMBER_RAW,
                ("number", "input_number"),
                ("ems_power_limit", "inverter_ems_power_limit", "eco_mode_power"),
            ),
            "dod_holding": (
                DOD_HOLDING_SWITCH_RAW,
                ("switch", "input_boolean"),
                ("dod_holding_switch", "dod_holding"),
            ),
            "backup_supply": (
                BACKUP_SUPPLY_SWITCH_RAW,
                ("switch", "input_boolean"),
                ("backup_supply_switch", "backup_supply", "backup_output"),
            ),
            "dod": (
                DOD_NUMBER_RAW,
                ("number", "input_number"),
                (
                    "battery_discharge_depth_offline",
                    "depth_of_discharge_backup",
                    "depth_of_discharge_off_grid",
                    "depth_of_discharge_offline",
                ),
            ),
            "dod_on_grid": (
                DOD_ON_GRID_NUMBER_RAW,
                ("number", "input_number"),
                (
                    "battery_discharge_depth",
                    "depth_of_discharge_on_grid",
                    "on_grid_depth_of_discharge",
                    "ongrid_battery_dod",
                ),
            ),
            "operation_mode": (
                OPERATION_MODE_SELECT_RAW,
                ("select", "input_select"),
                ("operation_mode", "inverter_operation_mode"),
            ),
            "grid_export_limit": (
                GRID_EXPORT_LIMIT_ENTITIES_RAW,
                ("number", "input_number"),
                ("grid_export_limit", "net_exportlimiet", "export_limit"),
            ),
            "grid_export_switch": (
                GRID_EXPORT_LIMIT_SWITCHES_RAW,
                ("switch", "input_boolean"),
                ("grid_export_limit_switch", "export_limit_switch"),
            ),
        }

        new_entities: dict[str, str] = {}
        for key, (raw, domains, suffixes) in mapping.items():
            resolved = self._resolve_explicit(raw, domains, suffixes)
            if resolved:
                new_entities[key] = resolved
        self.entities = new_entities

        # Metadata entity states, when a custom GoodWe integration exposes them.
        for key in ("serial", "ip", "mac", "last_seen"):
            entity_id = self.entity(key)
            state = next((s for s in inventory if s.get("entity_id") == entity_id), None)
            if not state or not self._state_available(state):
                continue
            value = str(state.get("state") or "").strip()
            if key == "serial" and value:
                self.serial = normalize_serial(value)
            elif key == "ip":
                self.ip_address = self._extract_ip(value) or self.ip_address
            elif key == "mac":
                self.mac_address = self._extract_mac(value) or self.mac_address
            elif key == "last_seen":
                self.last_seen = value or self.last_seen

        # Standard GoodWe integration stores serial and MAC on the device, not in
        # state attributes. Read those through the template API from any resolved
        # native entity.
        for entity_id in self.entities.values():
            if not entity_id or "," in entity_id or " " in entity_id:
                continue
            metadata = ha_entity_device_metadata(entity_id)
            metadata_serial = normalize_serial(metadata.get("serial_number"))
            if metadata_serial and not self.serial:
                self.serial = metadata_serial
            if not self.mac_address:
                self.mac_address = self._extract_mac(metadata.get("connections"))
            if not self.ip_address:
                self.ip_address = self._extract_ip(metadata.get("configuration_url"))
            if self.serial and self.mac_address and self.ip_address:
                break

        phase_state = ha_get_state(self.entity("phase")) if self.entity("phase") else None
        phase = parse_int(phase_state.get("state") if phase_state else None, 0)
        if phase in (1, 3):
            self.phase_count = phase
        else:
            # Active Power L2/L3 (or equivalent pgrid/grid-power names) proves a
            # three-phase inverter. Slider/box attributes are irrelevant here.
            has_l2_l3 = False
            for state in self._goodwe_states():
                object_key, friendly, _combined = self._state_search_text(state)
                key = f"{object_key} {friendly}"
                if re.search(
                    r"(?:active_power|meter_active_power|grid_power|pgrid)(?:_l)?[23](?:$|_)",
                    key,
                ) and self._state_available(state):
                    has_l2_l3 = True
                    break
            self.phase_count = 3 if has_l2_l3 else 1

        # Last state update is a useful fallback when no dedicated last-seen
        # sensor exists. It remains available after an inverter goes offline.
        timestamps = [
            str(state.get("last_updated") or state.get("last_changed") or "")
            for state in self._goodwe_states()
            if state.get("last_updated") or state.get("last_changed")
        ]
        if timestamps and not self.last_seen:
            self.last_seen = max(timestamps)

        presentation: dict[str, str] = {}
        for key in ("ems_power", "dod", "dod_on_grid", "grid_export_limit"):
            entity_id = self.entity(key)
            state = next((s for s in inventory if s.get("entity_id") == entity_id), None)
            attrs = state.get("attributes") if state else {}
            if entity_id:
                presentation[key] = str((attrs or {}).get("mode") or "number")

        signature = repr(
            (
                self.serial,
                self.phase_count,
                self.ip_address,
                self.mac_address,
                sorted(self.entities.items()),
                sorted(presentation.items()),
            )
        )
        if DEBUG and signature != self._last_discovery_signature:
            log(
                f"Entity discovery: serial={self.serial or '?'} phases={self.phase_count} "
                f"ip={self.ip_address or '?'} mac={self.mac_address or '?'} "
                f"entities={self.entities} number_ui={presentation}"
            )
            self._last_discovery_signature = signature


ENTITY_MAP = EntityMap()


def resolved_entities(key: str, raw: str = "") -> list[str]:
    # An explicitly configured entity is an operator override and must win over
    # automatic discovery. This is especially important when the fuse sensor is
    # a site meter rather than the GoodWe grid-power sensor.
    if raw and raw.lower() != "auto":
        return split_entities(raw)
    resolved = ENTITY_MAP.entity(key)
    if resolved:
        return split_entities(resolved)
    return []


def phase_count() -> int:
    return ENTITY_MAP.phase_count if ENTITY_MAP.phase_count in (1, 3) else 1


def phase_charge_thresholds() -> tuple[float, float]:
    trigger = parse_float(CHARGE_BLOCK_TRIGGER_RAW)
    release = parse_float(CHARGE_BLOCK_RELEASE_RAW)
    if trigger is None:
        trigger = -8000.0 if phase_count() == 3 else -3500.0
    if release is None:
        release = -5000.0 if phase_count() == 3 else -2000.0
    return trigger, release


def phase_export_limit() -> int:
    return 5000 if phase_count() == 3 else 3000


# ---------------------------------------------------------------------------
# HA number/select/switch helpers
# ---------------------------------------------------------------------------


def number_entity_attrs(entity_id: str) -> dict[str, Any]:
    state = ha_get_state(entity_id)
    attrs = state.get("attributes") if state else None
    return attrs if isinstance(attrs, dict) else {}


def number_limit(entity_id: str, keys: tuple[str, ...]) -> float | None:
    attrs = number_entity_attrs(entity_id)
    for key in keys:
        value = parse_float(attrs.get(key))
        if value is not None:
            return value
    return None


def clamp_number_value(entity_id: str, value: float) -> float:
    minimum = number_limit(entity_id, ("min", "min_value", "native_min_value"))
    maximum = number_limit(entity_id, ("max", "max_value", "native_max_value"))
    original = value
    if minimum is not None:
        value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    if DEBUG and abs(value - original) > 0.001:
        log(f"Number {entity_id}: clamped {original:g} to {value:g}")
    return value


def ha_set_number_value(entity_id: str, value: float, tolerance: float = 0.01) -> bool:
    current = ha_get_state(entity_id)
    current_value = parse_float(current.get("state") if current else None)
    if current_value is not None and abs(current_value - value) <= tolerance:
        return True
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    if domain not in ("number", "input_number"):
        log(f"WARN: {entity_id} is not a writable number/input_number entity")
        return False
    # Home Assistant number mode 'slider' and 'box' are UI presentations only;
    # both are written using the domain's set_value action.
    return ha_call_service(domain, "set_value", {"entity_id": entity_id, "value": value})


def normalize_select_option(entity_id: str, requested_option: str) -> str | None:
    option = normalize_ems_option(requested_option)
    if not option:
        return None
    state = ha_get_state(entity_id)
    if not state:
        return option
    current = str(state.get("state") or "").strip()
    attrs = state.get("attributes") or {}
    options = attrs.get("options") or []
    requested_key = normalize_key(option)
    aliases = {normalize_key(item): str(item) for item in options if str(item).strip()}
    if requested_key in aliases:
        return aliases[requested_key]
    if normalize_key(current) == requested_key:
        return current
    # Some integrations still expose legacy labels. Prefer their canonical
    # option only when charge_battery/discharge_battery is not available.
    reverse_aliases = {
        "charge_battery": ("import_ac", "buy_power"),
        "discharge_battery": ("export_ac", "sell_power"),
        "battery_standby": ("standby",),
    }
    for alias in reverse_aliases.get(requested_key, ()):
        if alias in aliases:
            return aliases[alias]
    if options:
        log(f"WARN: option '{option}' not exposed by {entity_id}: {options}")
    return option


def ha_select_option(entity_id: str, option: str) -> bool:
    selected = normalize_select_option(entity_id, option)
    if not selected:
        return False
    current = ha_get_state(entity_id)
    if current and normalize_key(current.get("state")) == normalize_key(selected):
        return True
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    if domain not in ("select", "input_select"):
        log(f"WARN: {entity_id} is not a writable select/input_select entity")
        return False
    return ha_call_service(
        domain, "select_option", {"entity_id": entity_id, "option": selected}
    )


def ha_set_switch(entity_id: str, desired_state: str) -> bool:
    desired = parse_bool_value(desired_state)
    if desired is None:
        return True
    wanted = "on" if desired else "off"
    current = ha_get_state(entity_id)
    if current and str(current.get("state") or "").lower() == wanted:
        return True
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    if domain not in ("switch", "input_boolean"):
        log(f"WARN: {entity_id} is not a writable switch/input_boolean entity")
        return False
    return ha_call_service(
        domain, "turn_on" if desired else "turn_off", {"entity_id": entity_id}
    )


def set_numbers(key: str, raw: str, value: float, label: str) -> bool:
    entities = resolved_entities(key, raw)
    if not entities:
        if DEBUG:
            log(f"{label}: unsupported/not found")
        return True
    ok_all = True
    for entity_id in entities:
        target = clamp_number_value(entity_id, value)
        ok = ha_set_number_value(entity_id, target)
        ok_all = ok_all and ok
        if not ok:
            log(f"WARN: failed {label} {entity_id} => {target:g}")
    return ok_all


def set_switches(key: str, raw: str, desired: str, label: str) -> bool:
    entities = resolved_entities(key, raw)
    if not entities:
        if DEBUG:
            log(f"{label}: unsupported/not found")
        return True
    ok_all = True
    for entity_id in entities:
        ok = ha_set_switch(entity_id, desired)
        ok_all = ok_all and ok
        if not ok:
            log(f"WARN: failed {label} {entity_id} => {desired}")
    return ok_all


def set_selects(key: str, raw: str, option: str, label: str) -> bool:
    entities = resolved_entities(key, raw)
    if not entities:
        if DEBUG:
            log(f"{label}: unsupported/not found")
        return True
    ok_all = True
    for entity_id in entities:
        ok = ha_select_option(entity_id, option)
        ok_all = ok_all and ok
        if not ok:
            log(f"WARN: failed {label} {entity_id} => {option}")
    return ok_all


# ---------------------------------------------------------------------------
# Backup automation
# ---------------------------------------------------------------------------


def backup_yaml_content_ok(content: str) -> bool:
    return all(
        marker in content
        for marker in (
            "alias: Auto update everything",
            "backup.create_automatic",
            "update.install",
            "entity_id: all",
        )
    )


def ensure_backup_yaml() -> dict[str, Any]:
    path = str(BACKUP_YAML_PATH or "/config/backup.yaml").strip()
    status: dict[str, Any] = {
        "backup_yaml_path": path,
        "backup_yaml_ok": None,
        "backup_yaml_updated_at": None,
    }
    if not BACKUP_YAML_CHECK_ENABLED:
        return status
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        changed = False
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                current = handle.read()
            if not backup_yaml_content_ok(current):
                content = (
                    BACKUP_YAML_CONTENT
                    if BACKUP_YAML_OVERWRITE
                    else current.rstrip() + "\n\n# DWARS auto update automation\n" + BACKUP_YAML_CONTENT
                )
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(content)
                changed = True
        else:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(BACKUP_YAML_CONTENT)
            changed = True
        status["backup_yaml_ok"] = True
        status["backup_yaml_updated_at"] = int(
            time.time() if changed else os.path.getmtime(path)
        )
    except Exception as exc:
        status["backup_yaml_ok"] = False
        status["backup_yaml_error"] = str(exc)
        log(f"WARN: backup.yaml check failed: {exc}")
    return status


# ---------------------------------------------------------------------------
# Mandatory inverter defaults and BMS override
# ---------------------------------------------------------------------------


def inverter_settings_from_action(action: dict[str, Any]) -> dict[str, Any]:
    settings = action.get("inverter_settings")
    if not isinstance(settings, dict):
        settings = nested_get(action, ["agent_config", "inverter"])
    return settings if isinstance(settings, dict) else {}


def action_reserve_soc(action: dict[str, Any]) -> float | None:
    settings = inverter_settings_from_action(action)
    for value in (
        settings.get("reserve_soc_pct"),
        action.get("reserve_soc_pct"),
        action.get("battery_reserve_pct"),
    ):
        parsed = parse_float(value)
        if parsed is not None:
            return min(100.0, max(0.0, parsed))
    return None


def apply_managed_defaults(action: dict[str, Any]) -> None:
    if not HA_CONTROL_ENABLED or DISABLE_HA:
        return
    settings = inverter_settings_from_action(action)
    reserve_soc = action_reserve_soc(action)

    # BMS/EMS reserve always overrides DOD: reserve 30% -> DOD 70%.
    if reserve_soc is not None:
        dod = max(0.0, min(100.0, 100.0 - reserve_soc))
        dod_on_grid = dod
    else:
        dod = parse_float(settings.get("depth_of_discharge_pct"))
        dod_on_grid = parse_float(settings.get("depth_of_discharge_on_grid_pct"))
        if dod is None:
            dod = float(DEFAULT_DOD)
        if dod_on_grid is None:
            dod_on_grid = float(DEFAULT_DOD_ON_GRID)

    if OVERRIDE_DEFAULT_VALUES:
        # Local configured defaults are intentional; reserve remains authoritative.
        if reserve_soc is None:
            dod = float(DEFAULT_DOD)
            dod_on_grid = float(DEFAULT_DOD_ON_GRID)
        dod_holding = DEFAULT_DOD_HOLDING
        backup_supply = DEFAULT_BACKUP_SUPPLY
        operation_mode = DEFAULT_OPERATION_MODE
    else:
        dod_holding = str(settings.get("dod_holding", "off"))
        backup_supply = str(settings.get("backup_supply", "on"))
        operation_mode = str(settings.get("operation_mode", "general"))

    set_switches("dod_holding", DOD_HOLDING_SWITCH_RAW, dod_holding, "DOD holding")
    set_switches("backup_supply", BACKUP_SUPPLY_SWITCH_RAW, backup_supply, "backup supply")
    set_numbers("dod", DOD_NUMBER_RAW, dod, "depth of discharge")
    set_numbers("dod_on_grid", DOD_ON_GRID_NUMBER_RAW, dod_on_grid, "depth of discharge on-grid")
    set_selects(
        "operation_mode",
        OPERATION_MODE_SELECT_RAW,
        normalize_key(operation_mode) or "general",
        "operation mode",
    )


# ---------------------------------------------------------------------------
# EMS mode and fuse protection
# ---------------------------------------------------------------------------

charge_block_active = False
charge_block_until_ts = 0.0
last_server_mode = 7
last_server_power = 0
last_action: dict[str, Any] = {}


def read_entity_number(entity_id: str) -> float | None:
    state = ha_get_state(entity_id)
    return parse_float(state.get("state") if state else None)


def charge_block_sensor_entity() -> str:
    entities = resolved_entities("grid", CHARGE_BLOCK_SENSOR_RAW)
    return entities[0] if entities else ""


def update_charge_block_state() -> tuple[bool, float | None, str]:
    global charge_block_active, charge_block_until_ts
    if not CHARGE_BLOCK_ENABLED:
        charge_block_active = False
        charge_block_until_ts = 0.0
        return False, None, "disabled"

    entity_id = charge_block_sensor_entity()
    value = read_entity_number(entity_id) if entity_id else None
    trigger, release = phase_charge_thresholds()
    now = time.monotonic()

    if value is None:
        return charge_block_active, None, "sensor_unavailable"

    if value < trigger:
        if not charge_block_active:
            log(
                f"Charge block ACTIVE: {entity_id}={value:g} W, threshold={trigger:g} W"
            )
        charge_block_active = True
        charge_block_until_ts = max(charge_block_until_ts, now + CHARGE_BLOCK_DURATION_SEC)
        return True, value, "trigger"

    if charge_block_active and now >= charge_block_until_ts and value > release:
        charge_block_active = False
        charge_block_until_ts = 0.0
        log(
            f"Charge block RELEASED: {entity_id}={value:g} W, release={release:g} W"
        )
        return False, value, "released"

    return charge_block_active, value, "latched" if charge_block_active else "inactive"


def set_ems_power(server_mode: int, server_power: int, force: bool = False) -> bool:
    if not force and server_mode not in EMS_SET_POWER_MODES:
        return True
    entities = resolved_entities("ems_power", EMS_POWER_NUMBER_RAW)
    if not entities:
        return False
    power = float(server_power if server_power > 0 else POWER)
    ok_all = True
    for entity_id in entities:
        target = power
        spec = normalize_key(EMS_POWER_VALUE_SPEC)
        if spec in ("max", "maximum", "native_max"):
            target = number_limit(entity_id, ("max", "max_value", "native_max_value")) or power
        elif parse_float(EMS_POWER_VALUE_SPEC) is not None:
            target = float(parse_float(EMS_POWER_VALUE_SPEC) or power)
        target = clamp_number_value(entity_id, target)
        ok_all = ha_set_number_value(entity_id, target) and ok_all
    return ok_all


def set_ems_modes(option: str) -> bool:
    return set_selects("ems_mode", EMS_MODE_ENTITY_RAW, normalize_ems_option(option), "EMS mode")


def apply_battery_control(server_mode: int, server_power: int) -> bool:
    if not HA_CONTROL_ENABLED:
        return False
    block, value, reason = update_charge_block_state()
    if block and server_mode in CHARGE_BLOCK_MODES:
        log(
            f"Charge request blocked: mode={server_mode}, grid={value}, reason={reason}; "
            f"forcing {CHARGE_BLOCK_FALLBACK_OPTION}"
        )
        return set_ems_modes(CHARGE_BLOCK_FALLBACK_OPTION)

    option = EMS_MODE_OPTIONS.get(server_mode)
    if not option:
        log(f"WARN: unknown server mode {server_mode}")
        return False
    if EMS_SET_POWER_BEFORE_MODE:
        return set_ems_power(server_mode, server_power) and set_ems_modes(option)
    return set_ems_modes(option) and set_ems_power(server_mode, server_power)


def enforce_fast_safety() -> None:
    block, value, reason = update_charge_block_state()
    if block and last_server_mode in CHARGE_BLOCK_MODES:
        if DEBUG:
            log(f"10 s safety loop: block active ({value}, {reason})")
        set_ems_modes(CHARGE_BLOCK_FALLBACK_OPTION)


# ---------------------------------------------------------------------------
# Standalone zero-export compensation
# ---------------------------------------------------------------------------


def standalone_settings_from_action(action: dict[str, Any]) -> dict[str, Any]:
    settings = inverter_settings_from_action(action)
    nested = settings.get("standalone")
    nested = nested if isinstance(nested, dict) else {}
    return {
        "enabled": settings.get("standalone_enabled", nested.get("enabled")),
        "pv_entity": settings.get(
            "standalone_pv_entity", nested.get("external_pv_entity")
        ),
        "grid_entity": settings.get(
            "standalone_grid_entity", nested.get("external_grid_entity")
        ),
        "max_charge_w": settings.get(
            "standalone_max_charge_w", nested.get("max_charge_w")
        ),
    }


def standalone_enabled_from_action(action: dict[str, Any]) -> bool:
    remote = parse_bool_value(standalone_settings_from_action(action).get("enabled"))
    # Standalone is a physical site property. A local explicit true must win over
    # the BMS default false; the server can still enable it remotely as well.
    return STANDALONE_ENABLED or remote is True


def standalone_entity(raw: str, fallback_key: str) -> str:
    if raw and raw.lower() != "auto":
        return split_entities(raw)[0] if split_entities(raw) else ""
    entities = resolved_entities(fallback_key, "auto")
    return entities[0] if entities else ""


def apply_standalone_zero_export(action: dict[str, Any]) -> None:
    if not HA_CONTROL_ENABLED or DISABLE_HA:
        return
    if not standalone_enabled_from_action(action) or last_server_mode not in (0, 7):
        return
    standalone = standalone_settings_from_action(action)
    grid_entity = str(standalone.get("grid_entity") or "").strip()
    if not grid_entity:
        grid_entity = standalone_entity(STANDALONE_GRID_ENTITY_RAW, "grid")
    grid_w = read_entity_number(grid_entity) if grid_entity else None
    if grid_w is None:
        return

    # Existing EMS convention: negative grid power means export.
    export_w = max(0, int(round(-grid_w)))
    if export_w <= STANDALONE_DEADBAND_W:
        set_ems_modes(EMS_MODE_OPTIONS.get(7, "auto"))
        return

    remote_max = parse_int(standalone.get("max_charge_w"), 0)
    cap = remote_max or STANDALONE_MAX_CHARGE_W or POWER or phase_export_limit()
    target = min(export_w, cap) if cap > 0 else export_w
    block, _value, _reason = update_charge_block_state()
    if block:
        set_ems_modes(CHARGE_BLOCK_FALLBACK_OPTION)
        return

    set_ems_power(3, target, force=True)
    set_ems_modes("charge_battery")
    log(
        f"Standalone zero-export: grid={grid_w:g} W, charging={target} W "
        f"(updated every {STANDALONE_INTERVAL}s)"
    )


# ---------------------------------------------------------------------------
# Grid export curtailment
# ---------------------------------------------------------------------------


def action_epex_price(data: dict[str, Any]) -> float | None:
    for value in (
        data.get("epex_price_eur_kwh"),
        nested_get(data, ["epex", "price_eur_kwh"]),
        data.get("price_eur_kwh"),
    ):
        parsed = parse_float(value)
        if parsed is not None:
            return parsed
    reason = str(data.get("reason") or "")
    match = re.search(r"EPEX\s+now\s+€\s*(-?\d+(?:[\.,]\d+)?)", reason, re.I)
    return parse_float(match.group(1)) if match else None


def action_pv_curtail_threshold(data: dict[str, Any]) -> float:
    env_value = parse_float(PV_CURTAIL_BELOW_EUR_KWH_ENV)
    if env_value is not None:
        return env_value
    for value in (
        data.get("pv_curtail_below_eur_kwh"),
        nested_get(data, ["epex", "pv_curtail_below_eur_kwh"]),
    ):
        parsed = parse_float(value)
        if parsed is not None:
            return parsed
    return -0.12


def decide_pv_curtail(data: dict[str, Any]) -> bool | None:
    explicit = parse_bool_value(
        data.get("pv_curtail_recommended", nested_get(data, ["epex", "pv_curtail_recommended"]))
    )
    if explicit is not None:
        return explicit
    price = action_epex_price(data)
    return price < action_pv_curtail_threshold(data) if price is not None else None


def apply_grid_export_limit(data: dict[str, Any]) -> None:
    if not HA_CONTROL_ENABLED or DISABLE_HA or not PV_CURTAIL_ENABLED:
        return
    decision = decide_pv_curtail(data)
    # Zonder actuele prijs is de veilige normale toestand de fase-afhankelijke
    # exportlimiet met de restore-switch aan; laat een oude curtail-status niet staan.
    if decision is None:
        decision = False
    number_entities = resolved_entities("grid_export_limit", GRID_EXPORT_LIMIT_ENTITIES_RAW)
    switch_entities = resolved_entities("grid_export_switch", GRID_EXPORT_LIMIT_SWITCHES_RAW)
    target_spec = GRID_EXPORT_LIMIT_OFF_VALUE if decision else GRID_EXPORT_LIMIT_DEFAULT_VALUE
    target = parse_float(target_spec)
    if target is None:
        target = float(phase_export_limit())
    for entity_id in number_entities:
        ha_set_number_value(entity_id, clamp_number_value(entity_id, target))
    switch_target = (
        GRID_EXPORT_LIMIT_SWITCH_CURTAIL_STATE
        if decision
        else GRID_EXPORT_LIMIT_SWITCH_RESTORE_STATE
    )
    for entity_id in switch_entities:
        ha_set_switch(entity_id, switch_target)


# ---------------------------------------------------------------------------
# Telemetry/API
# ---------------------------------------------------------------------------


def fetch_next_action() -> dict[str, Any]:
    response = requests.get(
        API_URL, headers=HEADERS_EXT, timeout=12, verify=VERIFY_SSL
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("next_action response is not a JSON object")
    return data


def read_optional_number(entity_id: str) -> int | None:
    value = read_entity_number(entity_id) if entity_id else None
    return int(round(value)) if value is not None else None


def read_telemetry() -> dict[str, Any]:
    out: dict[str, Any] = {}
    soc = read_entity_number(ENTITY_MAP.entity("soc"))
    if soc is not None:
        out["soc"] = soc
    for key, entity_key in (
        ("pv_power_w", "pv"),
        ("grid_power_w", "grid"),
        ("battery_power_w", "battery_power"),
    ):
        value = read_optional_number(ENTITY_MAP.entity(entity_key))
        if value is not None:
            out[key] = value

    standalone = standalone_settings_from_action(last_action)
    standalone_pv_entity = str(standalone.get("pv_entity") or "").strip()
    if not standalone_pv_entity:
        standalone_pv_entity = STANDALONE_PV_ENTITY_RAW
    external_pv = read_optional_number(standalone_pv_entity) if standalone_pv_entity else None
    if external_pv is not None:
        out["external_pv_power_w"] = external_pv
        if "pv_power_w" not in out or standalone_enabled_from_action(last_action):
            out["pv_power_w"] = external_pv

    out.update(
        {
            "inverter_serial": ENTITY_MAP.serial or None,
            # Never report the Raspberry/add-on address as the inverter address.
            # The integration's persistent network metadata is authoritative.
            "inverter_ip": ENTITY_MAP.ip_address or None,
            "inverter_mac": ENTITY_MAP.mac_address or None,
            "inverter_phases": phase_count(),
            "inverter_last_seen_at": ENTITY_MAP.last_seen or None,
        }
    )
    return {key: value for key, value in out.items() if value is not None}


def local_ip_address() -> str | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("1.1.1.1", 53))
            ip = sock.getsockname()[0]
            ipaddress.ip_address(ip)
            return ip
        finally:
            sock.close()
    except OSError:
        return None


def upload_telemetry(payload: dict[str, Any]) -> None:
    if not TEL_URL:
        return
    try:
        response = requests.post(
            TEL_URL, headers=HEADERS_EXT, json=payload, timeout=12, verify=VERIFY_SSL
        )
        response.raise_for_status()
    except Exception as exc:
        log(f"Telemetry upload error: {exc}")


def perform_decision_cycle() -> None:
    global last_action, last_server_mode, last_server_power
    action = fetch_next_action()
    last_action = action
    last_server_mode = parse_int(action.get("mode"), 7)
    last_server_power = parse_int(action.get("power_watt"), 0)

    # Server config can enable standalone mode or supply its external sensors.
    settings = inverter_settings_from_action(action)
    remote_serial = normalize_serial(
        settings.get("serial_number")
        or settings.get("inverter_serial")
        or action.get("inverter_serial")
        or action.get("serial_number")
    )
    if remote_serial and remote_serial != ENTITY_MAP.serial:
        ENTITY_MAP.serial = remote_serial
        ENTITY_MAP.refresh()
    remote_phases = parse_int(settings.get("phase_count"), 0) if settings else 0
    if remote_phases in (1, 3):
        # Een expliciete BMS-override of eerder door de agent gerapporteerde fase
        # is leidend wanneer HA de fase nog niet heeft kunnen detecteren.
        ENTITY_MAP.phase_count = remote_phases
    if settings and DEBUG:
        log(f"BMS inverter settings: {settings}")

    apply_managed_defaults(action)
    apply_grid_export_limit(action)
    apply_battery_control(last_server_mode, last_server_power)

    telemetry = read_telemetry()
    backup = ensure_backup_yaml()
    heartbeat: dict[str, Any] = {
        "client_id": CLIENT_ID or None,
        "reported_at": int(time.time()),
        "agent_name": AGENT_NAME,
        "agent_type": AGENT_TYPE,
        "agent_version": AGENT_VERSION,
        "ha_version": ha_get_config_version(),
        "backup_yaml_ok": backup.get("backup_yaml_ok"),
        "backup_yaml_path": backup.get("backup_yaml_path"),
        "backup_yaml_updated_at": backup.get("backup_yaml_updated_at"),
        "battery_mode": last_server_mode,
        **telemetry,
    }
    upload_telemetry(
        {key: value for key, value in heartbeat.items() if value is not None}
    )
    log(
        f"Decision mode={last_server_mode} power={last_server_power}W; "
        f"serial={ENTITY_MAP.serial or '?'} phases={phase_count()} "
        f"SOC={telemetry.get('soc', '?')} grid={telemetry.get('grid_power_w', '?')}W "
        f"PV={telemetry.get('pv_power_w', '?')}W battery={telemetry.get('battery_power_w', '?')}W"
    )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def loop() -> None:
    log(
        f"Agent up version={AGENT_VERSION}; HA={ha_base_url()}; decision={DECISION_INTERVAL}s, "
        f"safety={SAFETY_INTERVAL}s, standalone={STANDALONE_INTERVAL}s, defaults={DEFAULTS_INTERVAL}s"
    )
    log(
        f"Recommended defaults: DOD=90%, DOD on-grid=90%, DOD holding=off, "
        f"backup supply=on, operation=general, mode1=battery_standby, mode3=charge_battery"
    )
    log(
        f"HA auth mode={'supervisor_proxy' if ha_uses_supervisor_proxy() else 'direct'}; "
        f"available_tokens={[source for source, _token in ha_token_candidates()]}"
    )
    if not HA_CONTROL_ENABLED:
        log("WARN: ha_control_enabled=false; entity detection works but inverter writes are disabled")

    now = time.monotonic()
    next_discovery = now
    next_safety = now
    next_standalone = now
    next_defaults = now
    next_decision = now

    while True:
        now = time.monotonic()
        try:
            if now >= next_discovery:
                ENTITY_MAP.refresh()
                next_discovery = now + ENTITY_DISCOVERY_INTERVAL

            if now >= next_safety:
                enforce_fast_safety()
                next_safety = now + SAFETY_INTERVAL

            if now >= next_standalone:
                apply_standalone_zero_export(last_action)
                next_standalone = now + STANDALONE_INTERVAL

            if now >= next_defaults:
                # Hardwareveiligheidsdefaults mogen niet afhankelijk zijn van de
                # bereikbaarheid van de BMS. Met een lege action gelden de lokale
                # veilige defaults; zodra de API weer antwoordt blijft reserve-SoC
                # leidend voor beide DOD-waarden.
                apply_managed_defaults(last_action)
                apply_grid_export_limit(last_action)
                next_defaults = now + DEFAULTS_INTERVAL

            if now >= next_decision:
                perform_decision_cycle()
                next_decision = now + DECISION_INTERVAL

        except Exception as exc:
            log(f"ERROR: {exc}")
            if DEBUG:
                traceback.print_exc()

        # Small sleep keeps all independent cadences responsive without threads.
        time.sleep(1.0)


if __name__ == "__main__":
    loop()
