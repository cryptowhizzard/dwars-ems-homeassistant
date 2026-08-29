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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

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

BACKUP_YAML_CHECK_ENABLED = env_bool("BACKUP_YAML_CHECK_ENABLED", False)
BACKUP_YAML_PATH = os.environ.get("BACKUP_YAML_PATH", "/config/backup.yaml")
BACKUP_YAML_OVERWRITE = env_bool("BACKUP_YAML_OVERWRITE", False)

DEFAULT_HA_URL = "http://supervisor/core"
HA_URL_ENV = os.environ.get("HA_URL", DEFAULT_HA_URL) or DEFAULT_HA_URL
DISABLE_HA = env_bool("DISABLE_HA", False)
HA_CONTROL_ENABLED = env_bool("HA_CONTROL_ENABLED", True)
AUTO_ENTITY_DISCOVERY = env_bool("HA_AUTO_ENTITY_DISCOVERY", True)
HA_REGISTRY_DISCOVERY = env_bool("HA_REGISTRY_DISCOVERY", True)
PUBLISH_DIAGNOSTIC_ENTITIES = env_bool("PUBLISH_DIAGNOSTIC_ENTITIES", True)
HA_STORAGE_DIR = Path(os.environ.get("HA_STORAGE_DIR", "/config/.storage"))
SERIAL_NUMBER_ENV = os.environ.get("GOODWE_SERIAL_NUMBER", "").strip()
MAIN_FUSE_PROFILE_RAW = os.environ.get("MAIN_FUSE_PROFILE", "auto")
PHASE_CACHE_PATH = Path(os.environ.get("GOODWE_PHASE_CACHE_PATH", "/data/goodwe_phase_detection.json"))

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

# Fast fuse protection. When the two numeric values are set to ``auto`` the
# main-fuse profile is authoritative. The supported profiles are 1x25, 1x35
# and 3x25_plus. With profile=auto, hardware phase detection selects 1x25 or
# 3x25_plus.
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

BACKUP_YAML_CONTENT = """- alias: DWARS scheduled automatic backup
  description: Create a Home Assistant backup before the independent DWARS update manager runs.
  trigger:
    - platform: time
      at: "03:00:00"
  action:
    - service: backup.create_automatic
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


def _normalize_ha_api_url(url: str) -> str:
    """Return a Home Assistant REST API base URL without a trailing slash."""
    raw = str(url or DEFAULT_HA_URL).strip() or DEFAULT_HA_URL
    parsed = urlsplit(raw)
    path = parsed.path.rstrip("/")
    if not path.endswith("/api"):
        path += "/api"
    return urlunsplit((parsed.scheme or "http", parsed.netloc, path, "", ""))


def ha_base_url() -> str:
    return _normalize_ha_api_url(HA_URL_ENV)


def _is_supervisor_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.hostname or "").lower() == "supervisor" and parsed.path.startswith("/core")


_HA_ACTIVE_CREDENTIAL: tuple[str, str, str] | None = None
_HA_LAST_ERROR: str | None = None
_HA_LAST_STATUS: int | None = None
_HA_LAST_SUCCESS_TS = 0.0
_HA_LAST_AUTH_LOG: tuple[str, str] | None = None
_HA_INVENTORY_OK = False
_LOG_THROTTLE: dict[str, float] = {}


def log_throttled(key: str, message: str, interval: int = 60) -> None:
    now = time.monotonic()
    if now - _LOG_THROTTLE.get(key, -1e12) >= interval:
        _LOG_THROTTLE[key] = now
        log(message)


def _ha_credential_candidates() -> list[tuple[str, str, str]]:
    """Build ordered (source, token, base URL) candidates.

    The Supervisor Core proxy only accepts the token injected into the running
    add-on.  A user supplied long-lived token is useful for a direct Core URL,
    but must never take precedence for http://supervisor/core.
    """
    configured_base = ha_base_url()
    supervisor_base = (
        configured_base
        if _is_supervisor_url(configured_base)
        else "http://supervisor/core/api"
    )
    direct_base = (
        "http://homeassistant:8123/api"
        if _is_supervisor_url(configured_base)
        else configured_base
    )

    supervisor_token = (
        os.environ.get("SUPERVISOR_TOKEN")
        or os.environ.get("HASSIO_TOKEN")
        or ""
    ).strip()
    configured_token = (os.environ.get("HA_USER_TOKEN") or "").strip()
    selected_token = (os.environ.get("HA_TOKEN") or "").strip()
    long_lived_token = (os.environ.get("HOMEASSISTANT_TOKEN") or "").strip()

    candidates: list[tuple[str, str, str]] = []

    def add(source: str, token: str, base: str) -> None:
        if not token:
            return
        item = (source, token, _normalize_ha_api_url(base))
        if not any(existing[1:] == item[1:] for existing in candidates):
            candidates.append(item)

    if _is_supervisor_url(configured_base):
        add("SUPERVISOR_TOKEN", supervisor_token, configured_base)
        add("selected HA_TOKEN", selected_token, configured_base)
        add("configured token via proxy", configured_token, configured_base)
        add("long-lived token via proxy", long_lived_token, configured_base)
        # A Home Assistant long-lived token belongs on the direct Core endpoint.
        add("configured token (direct Core fallback)", configured_token, direct_base)
        add("HOMEASSISTANT_TOKEN (direct Core fallback)", long_lived_token, direct_base)
    else:
        add("configured token", configured_token, configured_base)
        add("HOMEASSISTANT_TOKEN", long_lived_token, configured_base)
        add("selected HA_TOKEN", selected_token, configured_base)
        # An add-on can still recover through the Supervisor proxy when a direct
        # URL/token combination was configured incorrectly.
        add("SUPERVISOR_TOKEN (proxy fallback)", supervisor_token, supervisor_base)

    return candidates


def get_ha_token() -> str | None:
    """Compatibility helper returning the currently preferred token."""
    candidates = _ha_credential_candidates()
    return candidates[0][1] if candidates else None


def ha_headers(token: str | None = None) -> dict[str, str] | None:
    selected = token or get_ha_token()
    if not selected:
        log_throttled("ha_no_token", "ERROR: no Home Assistant token available")
        return None
    return {
        "Authorization": f"Bearer {selected}",
        "Content-Type": "application/json",
    }


def _ordered_ha_credentials() -> list[tuple[str, str, str]]:
    candidates = _ha_credential_candidates()
    if _HA_ACTIVE_CREDENTIAL:
        active = _HA_ACTIVE_CREDENTIAL
        candidates = [active] + [item for item in candidates if item[1:] != active[1:]]
    return candidates


def ha_request(
    method: str,
    path: str,
    *,
    timeout: int = 8,
    payload: dict[str, Any] | None = None,
) -> requests.Response | None:
    """Call the HA REST API and transparently recover from a stale token.

    A 401/403 is retried with the remaining safe token/endpoint combinations.
    The first working combination is cached for subsequent calls.
    """
    global _HA_ACTIVE_CREDENTIAL, _HA_LAST_ERROR, _HA_LAST_STATUS
    global _HA_LAST_SUCCESS_TS, _HA_LAST_AUTH_LOG

    if DISABLE_HA:
        return None
    credentials = _ordered_ha_credentials()
    if not credentials:
        _HA_LAST_ERROR = "no token available"
        log_throttled("ha_no_token", "ERROR: no Home Assistant token available")
        return None

    normalized_path = "/" + str(path or "").lstrip("/")
    last_response: requests.Response | None = None
    errors: list[str] = []

    for source, token, base in credentials:
        headers = ha_headers(token)
        if headers is None:
            continue
        try:
            response = requests.request(
                method.upper(),
                f"{base}{normalized_path}",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            errors.append(f"{source}@{base}: {exc}")
            continue

        last_response = response
        _HA_LAST_STATUS = response.status_code
        if response.status_code in (401, 403):
            errors.append(f"{source}@{base}: HTTP {response.status_code}")
            continue

        _HA_ACTIVE_CREDENTIAL = (source, token, base)
        _HA_LAST_ERROR = None
        _HA_LAST_SUCCESS_TS = time.time()
        if _HA_LAST_AUTH_LOG != (source, base):
            _HA_LAST_AUTH_LOG = (source, base)
            log(f"HA API authenticated via {source} at {base}")
        return response

    if last_response is not None and last_response.status_code in (401, 403):
        _HA_LAST_ERROR = "authentication rejected by all configured HA endpoints"
        log_throttled(
            "ha_auth_failed",
            "ERROR: Home Assistant authentication failed for all safe token/endpoint "
            "combinations; the Supervisor proxy must use the injected SUPERVISOR_TOKEN",
        )
    else:
        _HA_LAST_ERROR = "; ".join(errors[-3:]) or "Home Assistant API unavailable"
        log_throttled("ha_unavailable", f"HA API unavailable: {_HA_LAST_ERROR}")
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
    if response is not None and DEBUG and response.status_code not in (401, 403, 404):
        log(f"HA GET {entity_id} -> {response.status_code} {response.text[:160]}")
    return None


def ha_get_all_states() -> list[dict[str, Any]] | None:
    global _HA_INVENTORY_OK
    if DISABLE_HA:
        return None
    response = ha_request("GET", "states", timeout=10)
    if response is None or response.status_code != 200:
        _HA_INVENTORY_OK = False
        if response is not None and response.status_code not in (401, 403):
            log_throttled(
                "ha_inventory_failed",
                f"HA state inventory failed: HTTP {response.status_code} {response.text[:160]}",
            )
        return None
    try:
        data = response.json()
    except ValueError as exc:
        _HA_INVENTORY_OK = False
        log_throttled("ha_inventory_json", f"HA state inventory JSON error: {exc}")
        return None
    if not isinstance(data, list):
        _HA_INVENTORY_OK = False
        log_throttled("ha_inventory_type", "HA state inventory returned a non-list payload")
        return None
    _HA_INVENTORY_OK = True
    return data


def ha_call_service(domain: str, service: str, payload: dict[str, Any]) -> bool:
    response = ha_request(
        "POST", f"services/{domain}/{service}", timeout=8, payload=payload
    )
    ok = response is not None and 200 <= response.status_code < 300
    if response is not None and (DEBUG or not ok):
        log(
            f"HA SERVICE {domain}.{service} {payload} -> "
            f"{response.status_code} {response.text[:160]}"
        )
    return ok


def ha_set_state(entity_id: str, state: Any, attributes: dict[str, Any]) -> bool:
    response = ha_request(
        "POST",
        f"states/{entity_id}",
        timeout=5,
        payload={"state": state, "attributes": attributes},
    )
    return response is not None and response.status_code in (200, 201)


def ha_get_config_version() -> str | None:
    response = ha_request("GET", "config", timeout=5)
    if response is not None and response.status_code == 200:
        try:
            data = response.json()
        except ValueError:
            return None
        if isinstance(data, dict) and data.get("version"):
            return str(data["version"])
    return None


def _read_ha_storage(name: str) -> dict[str, Any]:
    """Read one Home Assistant .storage record without making it mandatory."""
    if not HA_REGISTRY_DISCOVERY:
        return {}
    path = HA_STORAGE_DIR / name
    for attempt in range(2):
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            data = payload.get("data") if isinstance(payload, dict) else None
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            if attempt == 0:
                time.sleep(0.05)
                continue
            log_throttled(
                f"storage_{name}",
                f"HA registry file {path} could not be read: {exc}; using state-only discovery",
                300,
            )
    return {}


def _registry_rows(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key, [])
    if isinstance(value, dict):
        value = list(value.values())
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _state_available(state: dict[str, Any]) -> bool:
    return str(state.get("state") or "").strip().lower() not in (
        "",
        "unknown",
        "unavailable",
        "none",
        "null",
    )


def _normalize_mac(value: Any) -> str | None:
    raw = re.sub(r"[^0-9A-Fa-f]", "", str(value or ""))
    if len(raw) != 12:
        return None
    return ":".join(raw[index : index + 2] for index in range(0, 12, 2)).upper()


def _record_text_values(record: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in (
        "entity_id",
        "unique_id",
        "translation_key",
        "original_name",
        "name",
        "title",
        "serial_number",
    ):
        value = record.get(key)
        if value is not None:
            values.append(str(value))
    for container_key in ("data", "options"):
        nested = record.get(container_key)
        if isinstance(nested, dict):
            for key in (
                "serial",
                "serial_number",
                "inverter_serial",
                "host",
                "ip",
                "ip_address",
                "mac",
                "mac_address",
            ):
                value = nested.get(key)
                if value is not None:
                    values.append(str(value))
    return values


FUSE_PROFILES: dict[str, dict[str, int]] = {
    "1x25": {
        "phase_count": 1,
        "charge_block_below_w": -3600,
        "charge_block_release_above_w": -2000,
        "grid_export_limit_w": 3000,
    },
    "1x35": {
        "phase_count": 1,
        "charge_block_below_w": -5000,
        "charge_block_release_above_w": -3500,
        "grid_export_limit_w": 3000,
    },
    "3x25_plus": {
        "phase_count": 3,
        "charge_block_below_w": -10000,
        "charge_block_release_above_w": -7500,
        "grid_export_limit_w": 5000,
    },
}

FUSE_PROFILE_ALIASES = {
    "auto": "auto",
    "automatic": "auto",
    "detect": "auto",
    "1x25": "1x25",
    "1_25": "1x25",
    "1phase25": "1x25",
    "single25": "1x25",
    "1x35": "1x35",
    "1_35": "1x35",
    "1phase35": "1x35",
    "single35": "1x35",
    "3x25": "3x25_plus",
    "3x25plus": "3x25_plus",
    "3x25_plus": "3x25_plus",
    "3phase25": "3x25_plus",
    "threephase25": "3x25_plus",
}

# GoodWe product-family evidence.  EUB is the family code present in the
# production serial supplied for the affected three-phase ET G2 inverter.
THREE_PHASE_SERIAL_CODES = {"EUB", "ETU", "BTU", "ETC", "EHU"}


def normalize_fuse_profile(value: Any) -> str:
    key = normalize_key(value).replace("_or_higher", "plus").replace("_of_hoger", "plus")
    compact = re.sub(r"[^a-z0-9]+", "", key)
    return FUSE_PROFILE_ALIASES.get(key, FUSE_PROFILE_ALIASES.get(compact, "auto"))


def serial_family_code(serial: Any) -> str:
    value = normalize_serial(serial)
    if not value:
        return ""
    # Typical format: 9010KEUB253L0104 -> EUB.
    match = re.match(r"^\d{4}[A-Z]?([A-Z]{3})", value)
    return match.group(1) if match else ""


def text_phase_hint(values: Iterable[Any]) -> tuple[int, str, int] | None:
    """Return phase, source label and confidence for model/registry text."""
    text = " ".join(normalize_key(value) for value in values if value is not None)
    if not text:
        return None

    three_patterns = (
        r"(?:^|_)3(?:_|-)?phase(?:_|$)",
        r"(?:^|_)three_phase(?:_|$)",
        r"(?:^|_)3p(?:_|$)",
        r"(?:^|_)(?:et|bt|et_plus|et_g2|et_lv|eh_plus)(?:_|$)",
        r"(?:^|_)three_phase_hybrid(?:_|$)",
    )
    if any(re.search(pattern, text) for pattern in three_patterns):
        return 3, "model_registry", 90

    # Single-phase model names are useful only as weak evidence. A persisted or
    # observed three-phase result is never downgraded by this hint.
    single_patterns = (
        r"(?:^|_)1(?:_|-)?phase(?:_|$)",
        r"(?:^|_)single_phase(?:_|$)",
        r"(?:^|_)(?:es|em|es_uniq|eh_single)(?:_|$)",
    )
    if any(re.search(pattern, text) for pattern in single_patterns):
        return 1, "model_registry", 55
    return None


class EntityMap:
    """Resolved entities and inverter metadata for one GoodWe device."""

    def __init__(self) -> None:
        self.serial = normalize_serial(SERIAL_NUMBER_ENV)
        self.phase_count = 0
        self.phase_source = "unknown"
        self.phase_confidence = 0
        self.main_fuse_profile = "auto"
        self.fuse_profile_source = "auto"
        self.ip_address: str | None = None
        self.mac_address: str | None = None
        self.last_seen: str | None = None
        self.entities: dict[str, str] = {}
        self._inventory: list[dict[str, Any]] = []
        self._state_by_entity: dict[str, dict[str, Any]] = {}
        self._registry_by_entity: dict[str, dict[str, Any]] = {}
        self._goodwe_registry_ids: set[str] = set()
        self._target_registry_ids: set[str] = set()
        self._target_device_ids: set[str] = set()
        self._target_config_entry_ids: set[str] = set()
        self._target_device_rows: list[dict[str, Any]] = []
        self._target_entry_rows: list[dict[str, Any]] = []
        self.discovery_ready = False
        self.registry_available = False
        self.registry_serial_mismatch = False
        self._last_logged_entities: dict[str, str] = {}

    def entity(self, key: str) -> str:
        return self.entities.get(key, "")

    def state(self, entity_id: str) -> dict[str, Any] | None:
        return self._state_by_entity.get(entity_id)

    def _record_mentions_serial(self, record: dict[str, Any], serial: str) -> bool:
        if not serial:
            return False
        for value in _record_text_values(record):
            normalized = normalize_serial(value)
            if normalized == serial or (len(serial) >= 8 and serial in normalized):
                return True
        identifiers = record.get("identifiers")
        if isinstance(identifiers, list):
            for item in identifiers:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    if normalize_key(item[0]) == "goodwe" and normalize_serial(item[1]) == serial:
                        return True
        return False

    def _load_registry_context(self) -> None:
        entity_data = _read_ha_storage("core.entity_registry")
        device_data = _read_ha_storage("core.device_registry")
        entry_data = _read_ha_storage("core.config_entries")
        entity_rows = _registry_rows(entity_data, "entities")
        device_rows = _registry_rows(device_data, "devices")
        entry_rows = _registry_rows(entry_data, "entries")

        self.registry_available = bool(entity_rows or device_rows or entry_rows)
        self._registry_by_entity = {}
        self._goodwe_registry_ids = set()
        self._target_registry_ids = set()
        self._target_device_ids = set()
        self._target_config_entry_ids = set()
        self.registry_serial_mismatch = False

        goodwe_entries = {
            str(row.get("entry_id") or row.get("id") or ""): row
            for row in entry_rows
            if normalize_key(row.get("domain") or row.get("platform")) == "goodwe"
            and str(row.get("entry_id") or row.get("id") or "")
        }
        serial = normalize_serial(self.serial)

        if serial:
            self._target_config_entry_ids.update(
                entry_id
                for entry_id, row in goodwe_entries.items()
                if self._record_mentions_serial(row, serial)
            )

        goodwe_devices: dict[str, dict[str, Any]] = {}
        for row in device_rows:
            device_id = str(row.get("id") or "")
            if not device_id:
                continue
            config_entries = {
                str(value)
                for value in (row.get("config_entries") or [])
                if str(value)
            }
            identifiers = row.get("identifiers") or []
            manufacturer = normalize_key(row.get("manufacturer"))
            is_goodwe = manufacturer == "goodwe" or bool(config_entries & set(goodwe_entries))
            if isinstance(identifiers, list):
                is_goodwe = is_goodwe or any(
                    isinstance(item, (list, tuple))
                    and len(item) >= 2
                    and normalize_key(item[0]) == "goodwe"
                    for item in identifiers
                )
            if not is_goodwe:
                continue
            goodwe_devices[device_id] = row
            if (
                (serial and self._record_mentions_serial(row, serial))
                or bool(config_entries & self._target_config_entry_ids)
            ):
                self._target_device_ids.add(device_id)
                self._target_config_entry_ids.update(config_entries & set(goodwe_entries))

        # If Home Assistant exposes explicit GoodWe serial metadata and none of
        # it matches the configured inverter, do not fall back to arbitrary
        # generic entity IDs.  Explicit entity options can still be used to
        # override this safety stop deliberately.
        if serial and not self._target_device_ids and not self._target_config_entry_ids:
            known_serials: set[str] = set()
            for row in goodwe_devices.values():
                candidate = normalize_serial(row.get("serial_number"))
                if candidate:
                    known_serials.add(candidate)
                for item in row.get("identifiers") or []:
                    if (
                        isinstance(item, (list, tuple))
                        and len(item) >= 2
                        and normalize_key(item[0]) == "goodwe"
                    ):
                        candidate = normalize_serial(item[1])
                        if candidate:
                            known_serials.add(candidate)
            for row in goodwe_entries.values():
                candidate = normalize_serial(row.get("unique_id"))
                if candidate:
                    known_serials.add(candidate)
            if known_serials and serial not in known_serials:
                self.registry_serial_mismatch = True

        # The normal installation contains one GoodWe device.  Generic visible
        # IDs such as number.goodwe_ems_power_limit are safe in that case, but
        # never ignore an explicit serial mismatch: controlling the wrong
        # inverter is worse than leaving an entity unresolved.
        if (
            not self._target_device_ids
            and not self.registry_serial_mismatch
            and len(goodwe_devices) == 1
        ):
            device_id, row = next(iter(goodwe_devices.items()))
            known_serials: set[str] = set()
            candidate = normalize_serial(row.get("serial_number"))
            if candidate:
                known_serials.add(candidate)
            for item in row.get("identifiers") or []:
                if (
                    isinstance(item, (list, tuple))
                    and len(item) >= 2
                    and normalize_key(item[0]) == "goodwe"
                ):
                    candidate = normalize_serial(item[1])
                    if candidate:
                        known_serials.add(candidate)
            if serial and known_serials and serial not in known_serials:
                self.registry_serial_mismatch = True
            else:
                self._target_device_ids.add(device_id)
                self._target_config_entry_ids.update(
                    str(value)
                    for value in (row.get("config_entries") or [])
                    if str(value) in goodwe_entries
                )
        if (
            not self._target_config_entry_ids
            and not self.registry_serial_mismatch
            and len(goodwe_entries) == 1
        ):
            entry_id, row = next(iter(goodwe_entries.items()))
            entry_serial = normalize_serial(row.get("unique_id"))
            if serial and entry_serial and serial != entry_serial:
                self.registry_serial_mismatch = True
            else:
                self._target_config_entry_ids.add(entry_id)

        for row in entity_rows:
            entity_id = str(row.get("entity_id") or "")
            if not entity_id:
                continue
            platform = normalize_key(row.get("platform"))
            config_entry_id = str(row.get("config_entry_id") or "")
            device_id = str(row.get("device_id") or "")
            is_goodwe = platform == "goodwe" or config_entry_id in goodwe_entries
            if not is_goodwe:
                continue
            self._registry_by_entity[entity_id] = row
            self._goodwe_registry_ids.add(entity_id)
            if (
                device_id in self._target_device_ids
                or config_entry_id in self._target_config_entry_ids
                or (serial and self._record_mentions_serial(row, serial))
            ):
                self._target_registry_ids.add(entity_id)

        if (
            not self._target_registry_ids
            and not self.registry_serial_mismatch
            and (len(goodwe_devices) == 1 or len(goodwe_entries) == 1)
        ):
            self._target_registry_ids = set(self._goodwe_registry_ids)

        if self.registry_serial_mismatch:
            log_throttled(
                "goodwe_serial_mismatch",
                f"ERROR: configured GoodWe serial {self.serial} does not match the "
                "GoodWe device found in Home Assistant; automatic control remains disabled",
                300,
            )

        target_devices = [
            row for device_id, row in goodwe_devices.items() if device_id in self._target_device_ids
        ]
        for row in target_devices:
            if not self.serial:
                candidate = normalize_serial(row.get("serial_number"))
                if not candidate:
                    for item in row.get("identifiers") or []:
                        if (
                            isinstance(item, (list, tuple))
                            and len(item) >= 2
                            and normalize_key(item[0]) == "goodwe"
                        ):
                            candidate = normalize_serial(item[1])
                            break
                if candidate:
                    self.serial = candidate
            if not self.mac_address:
                for item in row.get("connections") or []:
                    if (
                        isinstance(item, (list, tuple))
                        and len(item) >= 2
                        and normalize_key(item[0]) in ("mac", "network_mac")
                    ):
                        self.mac_address = _normalize_mac(item[1])
                        if self.mac_address:
                            break

        target_entries = [
            row
            for entry_id, row in goodwe_entries.items()
            if entry_id in self._target_config_entry_ids
        ]
        self._target_device_rows = target_devices
        self._target_entry_rows = target_entries
        for row in target_entries:
            for container_key in ("options", "data"):
                nested = row.get(container_key)
                if not isinstance(nested, dict):
                    continue
                if not self.ip_address:
                    for key in ("host", "ip_address", "ip", "address"):
                        value = str(nested.get(key) or "").strip()
                        if value:
                            self.ip_address = value
                            break
                if not self.mac_address:
                    for key in ("mac", "mac_address"):
                        value = _normalize_mac(nested.get(key))
                        if value:
                            self.mac_address = value
                            break
                if not self.serial:
                    for key in ("serial_number", "serial", "inverter_serial"):
                        value = normalize_serial(nested.get(key))
                        if value:
                            self.serial = value
                            break

    def _goodwe_states(self) -> list[dict[str, Any]]:
        target = [
            state
            for state in self._inventory
            if str(state.get("entity_id") or "") in self._target_registry_ids
        ]
        if target:
            return target
        if self.registry_serial_mismatch:
            return []

        broad: list[dict[str, Any]] = []
        serial = serial_slug(self.serial)
        serial_specific: list[dict[str, Any]] = []
        for state in self._inventory:
            entity_id = str(state.get("entity_id") or "")
            if not entity_id:
                continue
            attrs = state.get("attributes") or {}
            if normalize_key(attrs.get("managed_by")) == "goodwe_agent":
                # Agent-owned diagnostic states must not prove that the physical
                # inverter is online and must never be selected as control entities.
                continue
            eid_key = normalize_key(entity_id)
            friendly = normalize_key(attrs.get("friendly_name", ""))
            registry_match = entity_id in self._goodwe_registry_ids
            attr_platform = normalize_key(
                attrs.get("integration") or attrs.get("platform") or attrs.get("manufacturer")
            )
            if not (
                registry_match
                or "goodwe" in eid_key
                or "goodwe" in friendly
                or attr_platform == "goodwe"
            ):
                continue
            broad.append(state)
            attr_serial = serial_slug(
                attrs.get("serial_number")
                or attrs.get("goodwe_serial")
                or attrs.get("inverter_serial")
            )
            registry = self._registry_by_entity.get(entity_id, {})
            registry_serial = self._record_mentions_serial(registry, self.serial)
            if serial and (
                serial in eid_key
                or serial in friendly
                or attr_serial == serial
                or registry_serial
            ):
                serial_specific.append(state)

        # Preserve serial isolation when evidence exists, but do not discard all
        # generic GoodWe IDs merely because their visible entity_id lacks serial.
        return serial_specific or broad

    def _candidate_fields(self, state: dict[str, Any]) -> list[tuple[str, int]]:
        entity_id = str(state.get("entity_id") or "")
        attrs = state.get("attributes") or {}
        registry = self._registry_by_entity.get(entity_id, {})
        object_id = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
        return [
            (object_id, 160),
            (str(registry.get("translation_key") or ""), 240),
            (str(registry.get("unique_id") or ""), 190),
            (str(registry.get("original_name") or ""), 180),
            (str(registry.get("name") or ""), 170),
            (str(attrs.get("friendly_name") or ""), 150),
        ]

    @staticmethod
    def _alias_score(value: str, alias: str, base: int) -> int:
        field = normalize_key(value)
        wanted = normalize_key(alias)
        if not field or not wanted:
            return 0
        if field == wanted:
            return base + 120
        if field.endswith("_" + wanted) or field.startswith(wanted + "_"):
            return base + 100
        if f"_{wanted}_" in f"_{field}_":
            return base + 65
        return 0

    def _find_by_suffixes(
        self,
        domains: tuple[str, ...],
        suffixes: tuple[str, ...],
        *,
        states: list[dict[str, Any]] | None = None,
        forbidden: tuple[str, ...] = (),
    ) -> str:
        candidates = states if states is not None else self._goodwe_states()
        serial = serial_slug(self.serial)
        ranked: list[tuple[int, str]] = []
        for state in candidates:
            entity_id = str(state.get("entity_id") or "")
            if not entity_id or "." not in entity_id:
                continue
            domain = entity_id.split(".", 1)[0]
            if domain not in domains:
                continue
            candidate_fields = self._candidate_fields(state)
            candidate_text = normalize_key(
                " ".join(value for value, _base in candidate_fields if value)
            )
            if any(
                re.search(pattern, candidate_text)
                for pattern in forbidden
            ):
                continue
            best = 0
            for index, suffix in enumerate(suffixes):
                priority = max(0, 100 - index * 4)
                for value, base in candidate_fields:
                    matched = self._alias_score(value, suffix, base)
                    if matched:
                        best = max(best, matched + priority)
            if not best:
                continue
            if entity_id in self._target_registry_ids:
                best += 500
            elif entity_id in self._goodwe_registry_ids:
                best += 160
            attrs = state.get("attributes") or {}
            haystack = normalize_key(
                " ".join(
                    [
                        entity_id,
                        str(attrs.get("friendly_name") or ""),
                        str(self._registry_by_entity.get(entity_id, {}).get("unique_id") or ""),
                    ]
                )
            )
            if serial and serial in haystack:
                best += 100
            if _state_available(state):
                best += 10
            ranked.append((best, entity_id))
        if not ranked:
            return ""
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return ranked[0][1]

    def _resolve_explicit(
        self,
        raw: str,
        domains: tuple[str, ...],
        suffixes: tuple[str, ...],
        forbidden: tuple[str, ...] = (),
    ) -> str:
        raw = str(raw or "").strip()
        if raw and raw.lower() != "auto":
            return raw
        return self._find_by_suffixes(domains, suffixes, forbidden=forbidden)

    def _metadata_from_states(self) -> None:
        for key, attribute in (
            ("serial", "serial"),
            ("ip", "ip_address"),
            ("mac", "mac_address"),
            ("last_seen", "last_seen"),
        ):
            entity_id = self.entity(key)
            state = self._state_by_entity.get(entity_id)
            if not state or not _state_available(state):
                continue
            value = str(state.get("state") or "").strip()
            if key == "serial" and value:
                self.serial = normalize_serial(value)
            elif key == "ip":
                self.ip_address = value or self.ip_address
            elif key == "mac":
                self.mac_address = _normalize_mac(value) or self.mac_address
            elif key == "last_seen":
                self.last_seen = value or self.last_seen

    def _phase_cache_read(self) -> dict[str, Any]:
        try:
            value = json.loads(PHASE_CACHE_PATH.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return {}
            cached_serial = normalize_serial(value.get("serial"))
            if self.serial and cached_serial and cached_serial != normalize_serial(self.serial):
                return {}
            return value
        except (OSError, ValueError, TypeError):
            return {}

    def _phase_cache_write(self) -> None:
        # Persist only explicit/high-confidence detection. In particular, never
        # make the one-phase absence fallback sticky.
        if self.phase_count not in (1, 3) or self.phase_confidence < 80:
            return
        try:
            PHASE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            PHASE_CACHE_PATH.write_text(
                json.dumps(
                    {
                        "serial": self.serial or None,
                        "phase_count": self.phase_count,
                        "source": self.phase_source,
                        "confidence": self.phase_confidence,
                        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log_throttled("phase_cache_write", f"WARN: phase cache could not be written: {exc}", 300)

    def _set_phase(self, phase: int, source: str, confidence: int) -> None:
        if phase not in (1, 3):
            return
        # Never let weak one-phase evidence downgrade a strong three-phase result.
        if self.phase_count == 3 and self.phase_confidence >= 80 and phase == 1 and confidence < 100:
            return
        self.phase_count = phase
        self.phase_source = source
        self.phase_confidence = confidence
        self._phase_cache_write()

    def _registry_phase_text(self) -> list[Any]:
        values: list[Any] = []
        for row in self._target_device_rows + self._target_entry_rows:
            values.extend(_record_text_values(row))
            values.extend(
                row.get(key)
                for key in (
                    "model",
                    "model_id",
                    "name",
                    "name_by_user",
                    "title",
                    "unique_id",
                    "hw_version",
                    "sw_version",
                )
            )
        for entity_id in self._target_registry_ids or self._goodwe_registry_ids:
            row = self._registry_by_entity.get(entity_id, {})
            values.extend(_record_text_values(row))
            values.append(entity_id)
        return values

    def _has_l2_l3_inventory(self) -> bool:
        patterns = (
            r"(?:^|_)(?:active_power|reactive_power|apparent_power|pgrid|grid_power|meter_power|voltage|current|frequency|power_factor)(?:_total)?_(?:l?[23]|phase_?[23])(?:_|$)",
            r"(?:^|_)(?:l?[23]|phase_?[23])_(?:active_power|voltage|current|power)(?:_|$)",
        )
        # Use registry rows as well as current states. Disabled or temporarily
        # unavailable L2/L3 entities still prove that the inverter is three-phase.
        candidates: list[tuple[str, list[tuple[str, int]]]] = []
        for state in self._goodwe_states():
            candidates.append((str(state.get("entity_id") or ""), self._candidate_fields(state)))
        for entity_id in self._target_registry_ids or self._goodwe_registry_ids:
            row = self._registry_by_entity.get(entity_id, {})
            fields = [
                (entity_id, 100),
                (str(row.get("translation_key") or ""), 100),
                (str(row.get("unique_id") or ""), 100),
                (str(row.get("original_name") or ""), 100),
                (str(row.get("name") or ""), 100),
            ]
            candidates.append((entity_id, fields))
        for _entity_id, fields in candidates:
            for value, _base in fields:
                key = normalize_key(value)
                if any(re.search(pattern, key) for pattern in patterns):
                    return True
        return False

    def _detect_phase_count(self) -> None:
        previous_phase = self.phase_count
        previous_source = self.phase_source
        previous_confidence = self.phase_confidence

        phase_state = self._state_by_entity.get(self.entity("phase"))
        phase = parse_int(phase_state.get("state") if phase_state else None, 0)
        if phase in (1, 3):
            self._set_phase(phase, "phase_entity", 100)
            return

        if self._has_l2_l3_inventory():
            self._set_phase(3, "l2_l3_registry", 100)
            return

        family = serial_family_code(self.serial)
        if family in THREE_PHASE_SERIAL_CODES:
            self._set_phase(3, f"serial_family_{family.lower()}", 95)
            return

        hint = text_phase_hint(self._registry_phase_text())
        if hint is not None:
            hinted_phase, source, confidence = hint
            self._set_phase(hinted_phase, source, confidence)
            if self.phase_count in (1, 3):
                return

        cached = self._phase_cache_read()
        cached_phase = parse_int(cached.get("phase_count"), 0)
        cached_confidence = parse_int(cached.get("confidence"), 0)
        if cached_phase in (1, 3) and cached_confidence >= 80:
            self._set_phase(cached_phase, "persistent_cache", cached_confidence)
            return

        # Preserve a strong in-memory value across temporary registry outages.
        if previous_phase in (1, 3) and previous_confidence >= 80:
            self.phase_count = previous_phase
            self.phase_source = previous_source
            self.phase_confidence = previous_confidence
            return

        # Absence of L2/L3 is not proof of one phase. It is merely the final,
        # conservative fallback until BMS or stronger hardware evidence arrives.
        self.phase_count = 1
        self.phase_source = "fallback_1_phase"
        self.phase_confidence = 10

    def _publish_diagnostics(self) -> None:
        if not PUBLISH_DIAGNOSTIC_ENTITIES or not self.serial:
            return
        prefix = f"sensor.goodwe_{serial_slug(self.serial)}"
        common = {
            "managed_by": "GoodWe Agent",
            "agent_version": AGENT_VERSION,
            "inverter_serial": self.serial,
            "phase_source": self.phase_source,
            "phase_confidence": self.phase_confidence,
            "main_fuse_profile": self.main_fuse_profile,
            "fuse_profile_source": self.fuse_profile_source,
        }
        diagnostics: list[tuple[str, Any, dict[str, Any]]] = [
            (
                f"{prefix}_serial_number",
                self.serial,
                {"friendly_name": f"GoodWe {self.serial} serial number", "icon": "mdi:identifier"},
            ),
            (
                f"{prefix}_inverter_nr_phase",
                self.phase_count or 1,
                {"friendly_name": f"GoodWe {self.serial} inverter phase count", "icon": "mdi:sine-wave"},
            ),
            (
                f"{prefix}_main_fuse_profile",
                self.main_fuse_profile,
                {"friendly_name": f"GoodWe {self.serial} main fuse profile", "icon": "mdi:fuse"},
            ),
        ]
        if self.ip_address:
            diagnostics.append(
                (
                    f"{prefix}_ip_address",
                    self.ip_address,
                    {"friendly_name": f"GoodWe {self.serial} IP address", "icon": "mdi:ip-network"},
                )
            )
        if self.mac_address:
            diagnostics.append(
                (
                    f"{prefix}_mac_address",
                    self.mac_address,
                    {"friendly_name": f"GoodWe {self.serial} MAC address", "icon": "mdi:network-outline"},
                )
            )
        if self.last_seen:
            diagnostics.append(
                (
                    f"{prefix}_last_seen",
                    self.last_seen,
                    {
                        "friendly_name": f"GoodWe {self.serial} last seen",
                        "device_class": "timestamp",
                        "icon": "mdi:clock-check-outline",
                    },
                )
            )
        for entity_id, value, attributes in diagnostics:
            ha_set_state(entity_id, value, {**common, **attributes})

    def _entity_debug_description(self, entity_id: str) -> str:
        state = self._state_by_entity.get(entity_id) or {}
        attrs = state.get("attributes") or {}
        domain = entity_id.split(".", 1)[0] if "." in entity_id else "?"
        extra = ""
        if domain in ("number", "input_number"):
            mode = attrs.get("mode") or "number"
            extra = (
                f" mode={mode} min={attrs.get('min', '?')} max={attrs.get('max', '?')}"
                f" step={attrs.get('step', '?')}"
            )
        return f"{entity_id} ({domain}{extra})"

    def refresh(self) -> None:
        if not AUTO_ENTITY_DISCOVERY and self.entities:
            return
        inventory = ha_get_all_states()
        if inventory is None:
            self.discovery_ready = False
            return
        self._inventory = inventory
        self._state_by_entity = {
            str(state.get("entity_id")): state
            for state in inventory
            if isinstance(state, dict) and state.get("entity_id")
        }
        self._load_registry_context()

        # Serial metadata is normally obtained from the device/config registry.
        # Keep a state-based fallback for installations where /config/.storage is
        # unavailable or registry discovery was disabled.
        if not self.serial:
            serial_entity = self._find_by_suffixes(
                ("sensor",),
                ("inverter_serial_number", "serial_number", "goodwe_serial"),
                states=[
                    state
                    for state in inventory
                    if "goodwe" in normalize_key(state.get("entity_id", ""))
                    or "goodwe" in normalize_key((state.get("attributes") or {}).get("friendly_name", ""))
                ],
            )
            serial_state = self._state_by_entity.get(serial_entity)
            if serial_state and _state_available(serial_state):
                self.serial = normalize_serial(serial_state.get("state"))

        mapping = {
            "soc": (
                SOC_ENTITY_RAW,
                ("sensor",),
                ("battery_state_of_charge", "battery_soc", "battery_soc_1", "soc"),
                (),
            ),
            "mode": (
                MODE_ENTITY_RAW,
                ("sensor", "select"),
                ("battery_mode", "ems_mode"),
                (),
            ),
            "pv": (
                PV_ENTITY_RAW,
                ("sensor",),
                ("pv_power", "total_pv_power", "pv_power_total", "ppv"),
                (),
            ),
            "grid": (
                GRID_ENTITY_RAW,
                ("sensor",),
                (
                    "active_power_total",
                    "meter_active_power_total",
                    "grid_active_power",
                    "grid_power",
                    "active_power",
                    "pgrid",
                ),
                (
                    r"(?:active_power|pgrid|grid_power|meter_power)_(?:l?[123]|phase_?[123])(?:_|$)",
                ),
            ),
            "battery_power": (
                BATTERY_POWER_ENTITY_RAW,
                ("sensor",),
                ("battery_power", "battery_power_1", "pbattery1", "pbattery"),
                (),
            ),
            "phase": (
                PHASE_ENTITY_RAW,
                ("sensor",),
                ("inverter_nr_phase", "inverter_phase_count", "phase_count"),
                (),
            ),
            "serial": (
                SERIAL_ENTITY_RAW,
                ("sensor",),
                ("inverter_serial_number", "serial_number", "goodwe_serial"),
                (),
            ),
            "ip": (
                IP_ENTITY_RAW,
                ("sensor",),
                ("inverter_ip_address", "goodwe_ip_address", "ip_address"),
                (),
            ),
            "mac": (
                MAC_ENTITY_RAW,
                ("sensor",),
                ("inverter_mac_address", "goodwe_mac_address", "mac_address"),
                (),
            ),
            "last_seen": (
                LAST_SEEN_ENTITY_RAW,
                ("sensor",),
                ("inverter_last_seen", "goodwe_last_seen", "last_seen"),
                (),
            ),
            "ems_mode": (
                EMS_MODE_ENTITY_RAW,
                ("select",),
                ("ems_mode", "goodwe_ems_mode"),
                (),
            ),
            "ems_power": (
                EMS_POWER_NUMBER_RAW,
                ("number",),
                ("ems_power_limit", "goodwe_ems_power_limit"),
                (),
            ),
            "dod_holding": (
                DOD_HOLDING_SWITCH_RAW,
                ("switch",),
                ("dod_holding_switch", "dod_holding"),
                (),
            ),
            "backup_supply": (
                BACKUP_SUPPLY_SWITCH_RAW,
                ("switch",),
                ("backup_supply_switch", "backup_supply"),
                (),
            ),
            "dod": (
                DOD_NUMBER_RAW,
                ("number",),
                (
                    "battery_discharge_depth_offline",
                    "depth_of_discharge_backup",
                    "depth_of_discharge_offline",
                    "backup_depth_of_discharge",
                    "backup_dod",
                ),
                (),
            ),
            "dod_on_grid": (
                DOD_ON_GRID_NUMBER_RAW,
                ("number",),
                (
                    "battery_discharge_depth",
                    "depth_of_discharge_on_grid",
                    "on_grid_depth_of_discharge",
                    "ongrid_depth_of_discharge",
                    "on_grid_dod",
                ),
                (r"(?:offline|backup)",),
            ),
            "operation_mode": (
                OPERATION_MODE_SELECT_RAW,
                ("select",),
                ("operation_mode", "inverter_operation_mode"),
                (),
            ),
            "grid_export_limit": (
                GRID_EXPORT_LIMIT_ENTITIES_RAW,
                ("number",),
                ("grid_export_limit", "net_exportlimiet"),
                (),
            ),
            "grid_export_switch": (
                GRID_EXPORT_LIMIT_SWITCHES_RAW,
                ("switch",),
                ("grid_export_limit_switch", "grid_export_switch"),
                (),
            ),
        }
        new_entities: dict[str, str] = {}
        for key, (raw, domains, suffixes, forbidden) in mapping.items():
            resolved = self._resolve_explicit(raw, domains, suffixes, forbidden)
            if resolved:
                new_entities[key] = resolved
        self.entities = new_entities

        self._metadata_from_states()
        self._detect_phase_count()

        # A successful, available state from the selected GoodWe device is the
        # last-seen signal.  Preserve the previous value while the inverter is offline.
        if any(_state_available(state) for state in self._goodwe_states()):
            self.last_seen = datetime.now(timezone.utc).isoformat(timespec="seconds")

        self.discovery_ready = True
        self._publish_diagnostics()

        if DEBUG and self.entities != self._last_logged_entities:
            self._last_logged_entities = dict(self.entities)
            details = ", ".join(
                f"{key}={self._entity_debug_description(entity_id)}"
                for key, entity_id in sorted(self.entities.items())
            )
            log(
                f"Entity discovery: serial={self.serial or '?'} phases={self.phase_count} "
                f"phase_source={self.phase_source} phase_confidence={self.phase_confidence} "
                f"fuse_profile={self.main_fuse_profile}/{self.fuse_profile_source} "
                f"ip={self.ip_address or '?'} "
                f"mac={self.mac_address or '?'} "
                f"registry={'yes' if self.registry_available else 'no'}; {details or 'no entities'}"
            )


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


def _setting_number(settings: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = parse_float(settings.get(key))
        if value is not None:
            return value
    return None


def resolved_fuse_profile() -> tuple[str, str]:
    remote = normalize_fuse_profile(
        REMOTE_INVERTER_SETTINGS.get("main_fuse_profile")
        or REMOTE_INVERTER_SETTINGS.get("fuse_profile")
        or nested_get(REMOTE_INVERTER_SETTINGS, ["main_fuse", "profile"])
    )
    if remote != "auto":
        profile, source = remote, "ems_bms"
    else:
        local = normalize_fuse_profile(MAIN_FUSE_PROFILE_RAW)
        if local != "auto":
            profile, source = local, "local_option"
        else:
            profile = "3x25_plus" if phase_count() == 3 else "1x25"
            source = f"phase_autodetect:{ENTITY_MAP.phase_source}"
    ENTITY_MAP.main_fuse_profile = profile
    ENTITY_MAP.fuse_profile_source = source
    return profile, source


def phase_charge_thresholds() -> tuple[float, float]:
    profile, _source = resolved_fuse_profile()
    defaults = FUSE_PROFILES[profile]

    # An explicitly entered local number is the final operator override.
    local_trigger = parse_float(CHARGE_BLOCK_TRIGGER_RAW)
    local_release = parse_float(CHARGE_BLOCK_RELEASE_RAW)
    remote_trigger = _setting_number(
        REMOTE_INVERTER_SETTINGS,
        "charge_block_below_w",
        "ha_charge_block_below_w",
    )
    remote_release = _setting_number(
        REMOTE_INVERTER_SETTINGS,
        "charge_block_release_above_w",
        "ha_charge_block_release_above_w",
    )
    trigger = local_trigger if local_trigger is not None else (
        remote_trigger if remote_trigger is not None else float(defaults["charge_block_below_w"])
    )
    release = local_release if local_release is not None else (
        remote_release if remote_release is not None else float(defaults["charge_block_release_above_w"])
    )
    return trigger, release


def threshold_source() -> str:
    if parse_float(CHARGE_BLOCK_TRIGGER_RAW) is not None or parse_float(CHARGE_BLOCK_RELEASE_RAW) is not None:
        return "local_numeric_override"
    if _setting_number(REMOTE_INVERTER_SETTINGS, "charge_block_below_w", "ha_charge_block_below_w") is not None or _setting_number(REMOTE_INVERTER_SETTINGS, "charge_block_release_above_w", "ha_charge_block_release_above_w") is not None:
        return "ems_bms_numeric"
    _profile, source = resolved_fuse_profile()
    return source


def phase_export_limit() -> int:
    profile, _source = resolved_fuse_profile()
    return int(FUSE_PROFILES[profile]["grid_export_limit_w"])


# ---------------------------------------------------------------------------
# HA number/select/switch helpers
# ---------------------------------------------------------------------------


def number_entity_attrs(entity_id: str) -> dict[str, Any]:
    state = ha_get_state(entity_id) or ENTITY_MAP.state(entity_id)
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
    step = number_limit(entity_id, ("step", "native_step"))
    original = value
    if minimum is not None:
        value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    # NumberMode.SLIDER and NumberMode.BOX expose the same HA action.  Their
    # min/max/step attributes may differ, so align the target with the entity's
    # native step before calling set_value.
    if step is not None and step > 0:
        base = minimum or 0.0
        value = base + round((value - base) / step) * step
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
        log(f"WARN: {entity_id} is not a number/input_number entity")
        return False
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
    return ha_call_service(
        "select", "select_option", {"entity_id": entity_id, "option": selected}
    )


def ha_set_switch(entity_id: str, desired_state: str) -> bool:
    desired = parse_bool_value(desired_state)
    if desired is None:
        return True
    wanted = "on" if desired else "off"
    current = ha_get_state(entity_id)
    if current and str(current.get("state") or "").lower() == wanted:
        return True
    return ha_call_service(
        "switch", "turn_on" if desired else "turn_off", {"entity_id": entity_id}
    )


def set_numbers(key: str, raw: str, value: float, label: str) -> bool:
    entities = resolved_entities(key, raw)
    if not entities:
        if DEBUG:
            if not _HA_INVENTORY_OK:
                log_throttled(
                    f"unresolved_{key}",
                    f"{label}: not resolved because the HA inventory is unavailable"
                    + (f" ({_HA_LAST_ERROR})" if _HA_LAST_ERROR else ""),
                )
            else:
                log_throttled(f"unsupported_{key}", f"{label}: unsupported/not found")
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
            if not _HA_INVENTORY_OK:
                log_throttled(
                    f"unresolved_{key}",
                    f"{label}: not resolved because the HA inventory is unavailable"
                    + (f" ({_HA_LAST_ERROR})" if _HA_LAST_ERROR else ""),
                )
            else:
                log_throttled(f"unsupported_{key}", f"{label}: unsupported/not found")
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
            if not _HA_INVENTORY_OK:
                log_throttled(
                    f"unresolved_{key}",
                    f"{label}: not resolved because the HA inventory is unavailable"
                    + (f" ({_HA_LAST_ERROR})" if _HA_LAST_ERROR else ""),
                )
            else:
                log_throttled(f"unsupported_{key}", f"{label}: unsupported/not found")
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
    """Return true only for the safe backup-only DWARS automation.

    Legacy versions wrote an ``update.install`` action for ``entity_id: all``.
    That defeats the protected-update policy and is deliberately not accepted
    as valid content anymore.
    """
    return (
        "alias: DWARS scheduled automatic backup" in content
        and "backup.create_automatic" in content
        and "update.install" not in content
        and "entity_id: all" not in content
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
                    else current.rstrip() + "\n\n# DWARS safe backup automation\n" + BACKUP_YAML_CONTENT
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
REMOTE_INVERTER_SETTINGS: dict[str, Any] = {}


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
    # The local add-on checkbox is an explicit site-level override.  A default
    # false value returned by an older BMS record must not disable it.
    return STANDALONE_ENABLED or bool(remote)


def standalone_entity(raw: str, fallback_key: str) -> str:
    if raw and raw.lower() != "auto":
        return split_entities(raw)[0] if split_entities(raw) else ""
    entities = resolved_entities(fallback_key, "auto")
    return entities[0] if entities else ""


def apply_standalone_zero_export(action: dict[str, Any]) -> None:
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
    if not PV_CURTAIL_ENABLED:
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
            "inverter_phase_source": ENTITY_MAP.phase_source,
            "inverter_phase_confidence": ENTITY_MAP.phase_confidence,
            "main_fuse_profile": resolved_fuse_profile()[0],
            "charge_block_below_w": int(round(phase_charge_thresholds()[0])),
            "charge_block_release_above_w": int(round(phase_charge_thresholds()[1])),
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


def apply_remote_phase_setting(settings: dict[str, Any]) -> None:
    """Store BMS settings and apply a phase hint without unsafe downgrades."""
    global REMOTE_INVERTER_SETTINGS
    REMOTE_INVERTER_SETTINGS = dict(settings or {})

    remote_phases = parse_int(settings.get("phase_count"), 0) if settings else 0
    if remote_phases not in (1, 3):
        resolved_fuse_profile()
        return

    # Strong local hardware evidence wins over a conflicting weak BMS value.
    # A BMS value may always promote an unknown/weak fallback to three phases.
    if remote_phases == 3:
        if ENTITY_MAP.phase_count != 3 or ENTITY_MAP.phase_confidence < 80:
            ENTITY_MAP._set_phase(3, "bms", 80)
    elif not (ENTITY_MAP.phase_count == 3 and ENTITY_MAP.phase_confidence >= 80):
        if ENTITY_MAP.phase_source in ("unknown", "fallback_1_phase", "bms"):
            ENTITY_MAP._set_phase(1, "bms", 80)

    resolved_fuse_profile()


def perform_decision_cycle() -> None:
    global last_action, last_server_mode, last_server_power
    action = fetch_next_action()
    last_action = action
    last_server_mode = parse_int(action.get("mode"), 7)
    last_server_power = parse_int(action.get("power_watt"), 0)

    # Server config can enable standalone mode or supply its external sensors.
    settings = inverter_settings_from_action(action)
    apply_remote_phase_setting(settings)
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
        f"serial={ENTITY_MAP.serial or '?'} phases={phase_count()}({ENTITY_MAP.phase_source}) "
        f"fuse={resolved_fuse_profile()[0]} thresholds={phase_charge_thresholds()[0]:g}/{phase_charge_thresholds()[1]:g}W "
        f"source={threshold_source()} SOC={telemetry.get('soc', '?')} grid={telemetry.get('grid_power_w', '?')}W "
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

            # Fetch BMS settings before the first 10-second safety check. This
            # prevents a three-phase site from briefly using one-phase limits
            # immediately after an add-on upgrade/restart.
            if now >= next_decision:
                perform_decision_cycle()
                next_decision = now + DECISION_INTERVAL

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

        except Exception as exc:
            log(f"ERROR: {exc}")
            if DEBUG:
                traceback.print_exc()

        # Small sleep keeps all independent cadences responsive without threads.
        time.sleep(1.0)


if __name__ == "__main__":
    loop()
