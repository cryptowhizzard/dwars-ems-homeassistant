#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import traceback
from typing import Any

import requests


# ========================
# Env helpers
# ========================

def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "ja")


def log(msg: str):
    print(f"[GoodWe] {msg}", flush=True)


# ========================
# Env configuration
# ========================

API_KEY = os.environ.get("API_KEY") or os.environ.get("api_key") or ""
CLIENT_ID = os.environ.get("CLIENT_ID") or os.environ.get("client_id") or ""
API_URL = os.environ.get("API_URL", "https://api.metdezon.nl/bms/api/next_action.php")
TEL_URL = os.environ.get("TELEMETRY_URL", "https://api.metdezon.nl/bms/api/telemetry.php")
INTERVAL = int(os.environ.get("INTERVAL", "60"))
POWER = int(os.environ.get("POWER", "5000"))
VERIFY_SSL = env_bool("VERIFY_SSL", True)
DEBUG = env_bool("DEBUG", False)

# Telemetry entities
SOC_ENTITY = os.environ.get("SOC_ENTITY", "sensor.goodwe_battery_state_of_charge")
MODE_ENTITY = os.environ.get("MODE_ENTITY", "")
PV_ENTITY = os.environ.get("PV_ENTITY", "sensor.goodwe_pv_power")
GRID_ENTITY = os.environ.get("GRID_ENTITY", "sensor.goodwe_active_power")

# Home Assistant API config
DEFAULT_HA_URL = "http://homeassistant:8123"
HA_URL_ENV = os.environ.get("HA_URL", DEFAULT_HA_URL)
DISABLE_HA = env_bool("DISABLE_HA", False)

# GoodWe EMS control through Home Assistant select/number entities.
# Entity settings may contain one entity_id or a comma/semicolon/space/newline separated list.
# This allows one policy action to update multiple GoodWe inverters.
HA_CONTROL_ENABLED = env_bool("HA_CONTROL_ENABLED", True)
EMS_MODE_ENTITY = (
    os.environ.get("HA_EMS_MODE_SELECT")
    or os.environ.get("EMS_MODE_ENTITY")
    or "select.goodwe_ems_mode"
)
EMS_POWER_NUMBER = (
    os.environ.get("HA_EMS_POWER_NUMBER")
    or os.environ.get("EMS_POWER_NUMBER")
    or "number.goodwe_eco_mode_power"
)
EMS_SET_POWER_MODES_RAW = os.environ.get("HA_EMS_SET_POWER_MODES", "3,4")
EMS_SET_POWER_BEFORE_MODE = env_bool("HA_EMS_SET_POWER_BEFORE_MODE", True)

# Value for EMS_POWER_NUMBER:
# - max: use entity max, good for GoodWe eco_mode_power sliders that expose 0..100 %
# - server_power: use API power_watt, good for entities that expect W
# - numeric value: fixed value
EMS_POWER_VALUE_SPEC = os.environ.get("HA_EMS_POWER_VALUE", "max")

# Central server mode -> GoodWe EMS select option.
# Your GoodWe HA entity exposes these internal option values:
# auto, charge_pv, discharge_pv, import_ac, export_ac, conserve, off_grid,
# battery_standby, buy_power, sell_power, charge_battery, discharge_battery.
# The UI may display labels like "Import AC", but select.select_option expects
# the actual option value from the entity attributes.
EMS_MODE_OPTIONS: dict[int, str] = {
    1: os.environ.get("HA_EMS_MODE_0_OPTION", "auto"),
    3: os.environ.get("HA_EMS_MODE_3_OPTION", "import_ac"),
    4: os.environ.get("HA_EMS_MODE_4_OPTION", "export_ac"),
    7: os.environ.get("HA_EMS_MODE_0_OPTION", "auto"),
}

# PV/export curtailment through Home Assistant number + switch entities.
GRID_EXPORT_LIMIT_ENTITIES = (
    os.environ.get("HA_GRID_EXPORT_LIMIT_NUMBER")
    or os.environ.get("GOODWE_GRID_EXPORT_LIMIT_NUMBER")
    or "number.goodwe_net_exportlimiet"
)
GRID_EXPORT_LIMIT_SWITCHES = (
    os.environ.get("HA_GRID_EXPORT_LIMIT_SWITCH")
    or os.environ.get("GOODWE_GRID_EXPORT_LIMIT_SWITCH")
    or "switch.goodwe_grid_export_limit_switch"
)
GRID_EXPORT_LIMIT_OFF_VALUE = os.environ.get("HA_GRID_EXPORT_LIMIT_OFF_VALUE", "0")
GRID_EXPORT_LIMIT_DEFAULT_VALUE = os.environ.get("HA_GRID_EXPORT_LIMIT_DEFAULT_VALUE", "max")
GRID_EXPORT_LIMIT_SWITCH_CURTAIL_STATE = os.environ.get("HA_GRID_EXPORT_LIMIT_SWITCH_CURTAIL_STATE", "on")
GRID_EXPORT_LIMIT_SWITCH_RESTORE_STATE = os.environ.get("HA_GRID_EXPORT_LIMIT_SWITCH_RESTORE_STATE", "off")
PV_CURTAIL_BELOW_EUR_KWH_ENV = os.environ.get("HA_PV_CURTAIL_BELOW_EUR_KWH", "")
PV_CURTAIL_ENABLED = env_bool("HA_PV_CURTAIL_ENABLED", True)

# Charge block / high export protection.
# When the configured sensor goes below the trigger threshold, charge modes are
# blocked for at least the configured duration and until the sensor is above
# the release threshold again.
CHARGE_BLOCK_ENABLED = env_bool("HA_CHARGE_BLOCK_ENABLED", True)
CHARGE_BLOCK_SENSOR = os.environ.get("HA_CHARGE_BLOCK_SENSOR", "sensor.goodwe_active_power_total")
CHARGE_BLOCK_TRIGGER_BELOW_W_RAW = os.environ.get("HA_CHARGE_BLOCK_BELOW_W", "-13000")
CHARGE_BLOCK_RELEASE_ABOVE_W_RAW = os.environ.get("HA_CHARGE_BLOCK_RELEASE_ABOVE_W", "-8000")
CHARGE_BLOCK_DURATION_SEC_RAW = os.environ.get("HA_CHARGE_BLOCK_DURATION_SEC", "300")
CHARGE_BLOCK_MODES_RAW = os.environ.get("HA_CHARGE_BLOCK_MODES", "3")
CHARGE_BLOCK_FALLBACK_OPTION = (
    os.environ.get("HA_CHARGE_BLOCK_FALLBACK_OPTION")
    or os.environ.get("HA_EMS_MODE_0_OPTION")
    or "auto"
)

HEADERS_EXT = {"X-API-Key": API_KEY} if API_KEY else {}


# ========================
# Generic parsers/helpers
# ========================

def parse_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


def parse_int(value: Any, default: int = 0) -> int:
    val = parse_float(value)
    if val is None:
        return default
    return int(round(val))


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


def split_entities(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text or text.lower() in ("none", "skip", "disabled", "false", "off"):
        return []
    return [part.strip() for part in re.split(r"[,;\s]+", text) if part.strip()]


def normalize_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def parse_int_set(raw: str, label: str = "mode list") -> set[int]:
    out: set[int] = set()
    for part in re.split(r"[,;\s]+", str(raw or "")):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except Exception:
            log(f"WARN: invalid mode number in {label}: {part!r}")
    return out


EMS_SET_POWER_MODES = parse_int_set(EMS_SET_POWER_MODES_RAW, "HA_EMS_SET_POWER_MODES")
CHARGE_BLOCK_TRIGGER_BELOW_W = parse_float(CHARGE_BLOCK_TRIGGER_BELOW_W_RAW)
if CHARGE_BLOCK_TRIGGER_BELOW_W is None:
    CHARGE_BLOCK_TRIGGER_BELOW_W = -13000.0

CHARGE_BLOCK_RELEASE_ABOVE_W = parse_float(CHARGE_BLOCK_RELEASE_ABOVE_W_RAW)
if CHARGE_BLOCK_RELEASE_ABOVE_W is None:
    CHARGE_BLOCK_RELEASE_ABOVE_W = -8000.0

CHARGE_BLOCK_DURATION_SEC = max(0, parse_int(CHARGE_BLOCK_DURATION_SEC_RAW, default=300))
CHARGE_BLOCK_MODES = parse_int_set(CHARGE_BLOCK_MODES_RAW, "HA_CHARGE_BLOCK_MODES")
if not CHARGE_BLOCK_MODES:
    CHARGE_BLOCK_MODES = {3}

charge_block_active = False
charge_block_until_ts = 0.0


# ========================
# Home Assistant API
# ========================

def ha_base_url() -> str:
    url = HA_URL_ENV.rstrip("/")
    if not url.endswith("/api"):
        url += "/api"
    return url


def get_ha_token() -> str | None:
    for key in ("HA_TOKEN", "HOMEASSISTANT_TOKEN", "SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        value = os.environ.get(key)
        if value:
            return value
    return None


def ha_headers() -> dict[str, str] | None:
    token = get_ha_token()
    if not token:
        log("ERROR: no Home Assistant token in env (HA_TOKEN/SUPERVISOR_TOKEN/HASSIO_TOKEN).")
        return None
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def ha_get_state(entity_id: str) -> dict[str, Any] | None:
    if DISABLE_HA or not entity_id:
        return None

    headers = ha_headers()
    if headers is None:
        return None

    url = f"{ha_base_url()}/states/{entity_id}"
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            return data if isinstance(data, dict) else None
        if DEBUG:
            log(f"HA GET {entity_id} -> {response.status_code} {response.text[:200]}")
    except Exception as exc:
        if DEBUG:
            log(f"HA GET {entity_id} error: {exc}")
    return None


def ha_call_service(domain: str, service: str, payload: dict[str, Any]) -> bool:
    if DISABLE_HA:
        return False

    headers = ha_headers()
    if headers is None:
        return False

    url = f"{ha_base_url()}/services/{domain}/{service}"
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=8)
        ok = 200 <= response.status_code < 300
        if DEBUG or not ok:
            log(f"HA SERVICE {domain}.{service} {payload} -> {response.status_code} {response.text[:200]}")
        return ok
    except Exception as exc:
        log(f"HA SERVICE {domain}.{service} error: {exc}")
        return False


# ========================
# HA entity helpers
# ========================

def number_entity_attrs(entity_id: str) -> dict[str, Any]:
    state = ha_get_state(entity_id)
    if not state:
        return {}
    attrs = state.get("attributes") or {}
    return attrs if isinstance(attrs, dict) else {}


def number_entity_max(entity_id: str) -> float | None:
    attrs = number_entity_attrs(entity_id)
    for key in ("max", "max_value", "native_max_value"):
        val = parse_float(attrs.get(key))
        if val is not None:
            return val
    return None


def number_entity_min(entity_id: str) -> float | None:
    attrs = number_entity_attrs(entity_id)
    for key in ("min", "min_value", "native_min_value"):
        val = parse_float(attrs.get(key))
        if val is not None:
            return val
    return None


def number_entity_unit(entity_id: str) -> str:
    attrs = number_entity_attrs(entity_id)
    for key in ("unit_of_measurement", "native_unit_of_measurement"):
        unit = attrs.get(key)
        if unit:
            return str(unit)
    return ""


def clamp_number_value(entity_id: str, value: float) -> float:
    original = value
    min_value = number_entity_min(entity_id)
    max_value = number_entity_max(entity_id)

    if min_value is not None and value < min_value:
        value = min_value
    if max_value is not None and value > max_value:
        value = max_value

    if abs(value - original) > 0.001:
        log(f"Number {entity_id}: requested {original:g}, clamped to {value:g}")
    return value


def resolve_number_target(entity_id: str, spec: str, server_power: int | None = None) -> float | None:
    text = str(spec or "").strip().lower()

    if text in ("server_power", "server_power_w", "power", "power_w", "watt", "watts"):
        power = server_power if server_power and server_power > 0 else POWER
        return float(power)

    direct = parse_float(text)
    if direct is not None:
        return direct

    if text in ("max", "maximum", "native_max"):
        return number_entity_max(entity_id)

    if text in ("min", "minimum", "native_min"):
        return number_entity_min(entity_id)

    return None


def ha_set_number_value(entity_id: str, value: float) -> bool:
    current_state = ha_get_state(entity_id)
    if current_state:
        cur = parse_float(current_state.get("state"))
        if cur is not None and abs(cur - value) < 0.001:
            if DEBUG:
                log(f"Number {entity_id} already {cur:g}; skip")
            return True

    return ha_call_service("number", "set_value", {"entity_id": entity_id, "value": value})


def normalize_select_option(entity_id: str, requested_option: str) -> str | None:
    option = str(requested_option or "").strip()
    if not option:
        return None

    state = ha_get_state(entity_id)
    if not state:
        return option

    current = str(state.get("state", "")).strip()
    if current == option or normalize_key(current) == normalize_key(option):
        return current if current else option

    attrs = state.get("attributes") or {}
    options = attrs.get("options") or []
    if isinstance(options, list):
        requested_key = normalize_key(option)

        for available in options:
            available_text = str(available).strip()
            if available_text == option:
                return available_text

        for available in options:
            available_text = str(available).strip()
            if available_text.lower() == option.lower():
                log(f"EMS option '{option}' matched available option '{available_text}'")
                return available_text

        for available in options:
            available_text = str(available).strip()
            if normalize_key(available_text) == requested_key:
                log(f"EMS option '{option}' matched available option '{available_text}'")
                return available_text

        if options:
            log(f"WARN: EMS option '{option}' not in available options for {entity_id}: {options}")

    return option


def ha_select_option(entity_id: str, option: str) -> tuple[bool, str | None]:
    selected_option = normalize_select_option(entity_id, option)
    if not selected_option:
        log(f"WARN: empty EMS option for {entity_id}; skip")
        return False, None

    current_state = ha_get_state(entity_id)
    if current_state and str(current_state.get("state", "")).strip() == selected_option:
        if DEBUG:
            log(f"EMS mode {entity_id} already '{selected_option}'; skip")
        return True, selected_option

    ok = ha_call_service(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": selected_option},
    )
    return ok, selected_option


def parse_switch_target(value: str) -> bool | None:
    text = str(value or "").strip().lower()
    if text in ("", "none", "skip", "disabled"):
        return None
    if text in ("1", "true", "yes", "on", "aan"):
        return True
    if text in ("0", "false", "no", "off", "uit"):
        return False
    log(f"WARN: invalid switch target {value!r}; use on/off/skip")
    return None


def ha_set_switch(entity_id: str, desired_state: str) -> bool:
    desired = parse_switch_target(desired_state)
    if desired is None:
        if DEBUG:
            log(f"Switch {entity_id}: target={desired_state!r}; skip")
        return True

    state = ha_get_state(entity_id)
    wanted_state_text = "on" if desired else "off"
    if state and str(state.get("state", "")).strip().lower() == wanted_state_text:
        if DEBUG:
            log(f"Switch {entity_id} already {wanted_state_text}; skip")
        return True

    service = "turn_on" if desired else "turn_off"
    ok = ha_call_service("switch", service, {"entity_id": entity_id})
    if ok:
        log(f"Switch {entity_id} => {wanted_state_text}")
    else:
        log(f"WARN: failed to set switch {entity_id} => {wanted_state_text}")
    return ok


# ========================
# HA GoodWe EMS control
# ========================

def read_charge_block_sensor() -> float | None:
    """Read the configured charge-block sensor from Home Assistant."""
    entity_id = str(CHARGE_BLOCK_SENSOR or "").strip()
    if not entity_id:
        return None

    state = ha_get_state(entity_id)
    if not state:
        if DEBUG:
            log(f"Charge block: sensor {entity_id} unavailable")
        return None

    value = parse_float(state.get("state"))
    if value is None and DEBUG:
        log(f"Charge block: sensor {entity_id} has non-numeric state {state.get('state')!r}")
    return value


def update_charge_block_state() -> tuple[bool, float | None, str]:
    """Update and return the charge-block latch state.

    The block is triggered when the sensor is below CHARGE_BLOCK_TRIGGER_BELOW_W.
    Once active, it is only released when both conditions are true:
    - the minimum block duration has elapsed
    - the sensor is above CHARGE_BLOCK_RELEASE_ABOVE_W
    """
    global charge_block_active, charge_block_until_ts

    if not CHARGE_BLOCK_ENABLED:
        if charge_block_active:
            log("Charge block disabled by configuration; releasing active block")
        charge_block_active = False
        charge_block_until_ts = 0.0
        return False, None, "disabled"

    sensor_value = read_charge_block_sensor()
    now = time.time()

    if sensor_value is None:
        if charge_block_active:
            remaining = max(0, int(round(charge_block_until_ts - now)))
            if DEBUG:
                log(f"Charge block remains active: sensor unavailable, min_remaining={remaining}s")
            return True, None, "sensor_unavailable_keep_active"
        return False, None, "sensor_unavailable"

    was_active = charge_block_active

    if sensor_value < CHARGE_BLOCK_TRIGGER_BELOW_W:
        charge_block_active = True
        charge_block_until_ts = max(charge_block_until_ts, now + CHARGE_BLOCK_DURATION_SEC)
        if not was_active:
            log(
                "Charge block ACTIVE: "
                f"{CHARGE_BLOCK_SENSOR}={sensor_value:g}W is below {CHARGE_BLOCK_TRIGGER_BELOW_W:g}W; "
                f"charging blocked for at least {CHARGE_BLOCK_DURATION_SEC}s and until above "
                f"{CHARGE_BLOCK_RELEASE_ABOVE_W:g}W"
            )
        elif DEBUG:
            remaining = max(0, int(round(charge_block_until_ts - now)))
            log(
                "Charge block still active: "
                f"{CHARGE_BLOCK_SENSOR}={sensor_value:g}W below trigger, min_remaining={remaining}s"
            )
        return True, sensor_value, "trigger"

    if not charge_block_active:
        return False, sensor_value, "inactive"

    min_duration_done = now >= charge_block_until_ts
    release_threshold_done = sensor_value > CHARGE_BLOCK_RELEASE_ABOVE_W

    if min_duration_done and release_threshold_done:
        charge_block_active = False
        charge_block_until_ts = 0.0
        log(
            "Charge block RELEASED: "
            f"{CHARGE_BLOCK_SENSOR}={sensor_value:g}W is above {CHARGE_BLOCK_RELEASE_ABOVE_W:g}W "
            "and minimum block duration has elapsed"
        )
        return False, sensor_value, "released"

    remaining = max(0, int(round(charge_block_until_ts - now)))
    if DEBUG:
        reasons = []
        if not min_duration_done:
            reasons.append(f"min_remaining={remaining}s")
        if not release_threshold_done:
            reasons.append(f"sensor_not_above_release={sensor_value:g}W<={CHARGE_BLOCK_RELEASE_ABOVE_W:g}W")
        log(f"Charge block remains active: {', '.join(reasons)}")
    return True, sensor_value, "latched"


def set_ems_power(server_mode: int, server_power: int) -> bool:
    if server_mode not in EMS_SET_POWER_MODES:
        return True

    entities = split_entities(EMS_POWER_NUMBER)
    if not entities:
        if DEBUG:
            log(f"EMS power: no HA_EMS_POWER_NUMBER configured; skip for server mode {server_mode}")
        return True

    ok_all = True
    for entity_id in entities:
        target = resolve_number_target(entity_id, EMS_POWER_VALUE_SPEC, server_power)
        if target is None:
            log(f"WARN: could not resolve EMS power target '{EMS_POWER_VALUE_SPEC}' for {entity_id}; skip")
            ok_all = False
            continue
        target = clamp_number_value(entity_id, target)
        ok = ha_set_number_value(entity_id, target)
        ok_all = ok_all and ok
        unit = number_entity_unit(entity_id)
        suffix = f" {unit}" if unit else ""
        if ok:
            log(f"EMS power {entity_id} => {target:g}{suffix}")
        else:
            log(f"WARN: failed to set EMS power {entity_id} => {target:g}{suffix}")

    return ok_all


def set_ems_modes(option: str) -> bool:
    """Set one or more HA select entities to the requested EMS option.

    EMS_MODE_ENTITY may contain a single entity_id or a comma/semicolon/space/newline
    separated list. Each select is normalized independently because different GoodWe
    inverters can expose slightly different option labels/values.
    """
    entities = split_entities(EMS_MODE_ENTITY)
    if not entities:
        log("WARN: HA_EMS_MODE_SELECT is empty; cannot set EMS mode")
        return False

    ok_all = True
    for entity_id in entities:
        ok, selected_option = ha_select_option(entity_id, option)
        ok_all = ok_all and ok
        if ok:
            log(f"EMS mode {entity_id} => {selected_option or option}")
        else:
            log(f"WARN: failed to set EMS mode {entity_id} => {selected_option or option}")

    return ok_all


def apply_battery_control_from_home_assistant(server_mode: int, server_power: int) -> bool:
    if not HA_CONTROL_ENABLED:
        if DEBUG:
            log("HA battery control disabled; skip EMS mode")
        return False

    block_active, block_sensor_value, block_reason = update_charge_block_state()
    charge_request_blocked = block_active and server_mode in CHARGE_BLOCK_MODES

    if charge_request_blocked:
        option = CHARGE_BLOCK_FALLBACK_OPTION
        skip_power = True
        log(
            "Charge block: blocking charge request "
            f"server_mode={server_mode}; sensor={block_sensor_value if block_sensor_value is not None else '?'}W; "
            f"reason={block_reason}; forcing EMS option='{option}'"
        )
    else:
        option = EMS_MODE_OPTIONS.get(server_mode)
        skip_power = False

    if not option:
        log(f"Unknown server mode {server_mode}; no EMS option configured.")
        return False

    select_entities = split_entities(EMS_MODE_ENTITY)
    power_entities = split_entities(EMS_POWER_NUMBER)
    if not select_entities:
        log("WARN: HA_EMS_MODE_SELECT is empty; cannot set EMS mode")
        return False

    power_text = "skipped_by_charge_block" if skip_power else EMS_POWER_VALUE_SPEC
    log(
        f"Set EMS mode via HA: server_mode={server_mode} "
        f"option='{option}' selects={select_entities} power_numbers={power_entities} "
        f"power_value='{power_text}' api_power={server_power if server_power > 0 else POWER}W"
    )

    if skip_power:
        return set_ems_modes(option)

    if EMS_SET_POWER_BEFORE_MODE:
        ok_power = set_ems_power(server_mode, server_power)
        ok_mode = set_ems_modes(option)
    else:
        ok_mode = set_ems_modes(option)
        ok_power = set_ems_power(server_mode, server_power)

    return ok_power and ok_mode

# ========================
# Telemetry
# ========================

def read_from_home_assistant() -> dict[str, Any]:
    out: dict[str, Any] = {}

    soc = ha_get_state(SOC_ENTITY)
    if soc and "state" in soc:
        val = parse_float(soc.get("state"))
        if val is not None:
            out["soc_pct"] = val

    mode_state = ha_get_state(MODE_ENTITY) if MODE_ENTITY else None
    if mode_state and "state" in mode_state:
        val = parse_int(mode_state.get("state"), default=-999)
        if val != -999:
            out["mode"] = val
        else:
            name = normalize_key(mode_state.get("state", ""))
            name_map = {
                "auto": 1,
                "charge": 2,
                "charge_pv": 2,
                "import_ac": 2,
                "discharge": 3,
                "discharge_pv": 3,
                "export_ac": 3,
                "standby": 1,
                "battery_standby": 1,
            }
            mapped = name_map.get(name)
            if mapped is not None:
                out["mode"] = mapped

    pv = ha_get_state(PV_ENTITY)
    if pv and "state" in pv:
        val = parse_float(pv.get("state"))
        if val is not None:
            out["pv_power_w"] = int(round(val))

    grid = ha_get_state(GRID_ENTITY)
    if grid and "state" in grid:
        val = parse_float(grid.get("state"))
        if val is not None:
            out["grid_power_w"] = int(round(val))

    return out


def upload_telemetry(payload: dict):
    if not TEL_URL:
        if DEBUG:
            log("No TELEMETRY_URL configured; skipping telemetry")
        return
    try:
        if DEBUG:
            log(f"POST {TEL_URL} -> {payload}")
        response = requests.post(TEL_URL, headers=HEADERS_EXT, json=payload, timeout=10, verify=VERIFY_SSL)
        if DEBUG:
            log(f"TEL HTTP {response.status_code} {response.text[:200]}")
        response.raise_for_status()
    except Exception as exc:
        log(f"Telemetry upload error: {exc}")


# ========================
# External API / policy
# ========================

def fetch_next_action() -> dict[str, Any]:
    if DEBUG:
        log(f"HTTP GET {API_URL} (verify_ssl={VERIFY_SSL}) …")
    response = requests.get(API_URL, headers=HEADERS_EXT, timeout=10, verify=VERIFY_SSL)
    if DEBUG:
        log(f"HTTP {response.status_code}, len={len(response.content)}")
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("next_action response is not a JSON object")
    return data


def nested_get(data: dict[str, Any], path: list[str]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def action_epex_price(data: dict[str, Any]) -> float | None:
    for value in (
        data.get("epex_price_eur_kwh"),
        nested_get(data, ["epex", "price_eur_kwh"]),
        data.get("price_eur_kwh"),
        data.get("epex_now_eur_kwh"),
    ):
        val = parse_float(value)
        if val is not None:
            return val

    reason = str(data.get("reason") or "")
    match = re.search(r"EPEX\s+now\s+€\s*(-?\d+(?:[\.,]\d+)?)", reason, re.IGNORECASE)
    if match:
        return parse_float(match.group(1))
    return None


def action_pv_curtail_threshold(data: dict[str, Any]) -> float:
    env_threshold = parse_float(PV_CURTAIL_BELOW_EUR_KWH_ENV)
    if env_threshold is not None:
        return env_threshold

    for value in (
        data.get("pv_curtail_below_eur_kwh"),
        nested_get(data, ["epex", "pv_curtail_below_eur_kwh"]),
    ):
        val = parse_float(value)
        if val is not None:
            return val

    return -0.12


def action_pv_curtail_recommended(data: dict[str, Any]) -> bool | None:
    for value in (
        data.get("pv_curtail_recommended"),
        nested_get(data, ["epex", "pv_curtail_recommended"]),
    ):
        val = parse_bool_value(value)
        if val is not None:
            return val
    return None


def decide_pv_curtail(data: dict[str, Any]) -> tuple[bool | None, float | None, float, str]:
    api_decision = action_pv_curtail_recommended(data)
    price_now = action_epex_price(data)
    threshold = action_pv_curtail_threshold(data)

    if api_decision is not None:
        return api_decision, price_now, threshold, "api"

    if price_now is not None:
        return price_now < threshold, price_now, threshold, "price"

    reason = str(data.get("reason") or "")
    if "pv curtailed" in reason.lower():
        return True, price_now, threshold, "reason"

    return None, price_now, threshold, "unknown"


def apply_grid_export_limit_from_action(data: dict[str, Any]):
    if not PV_CURTAIL_ENABLED:
        return

    number_entities = split_entities(GRID_EXPORT_LIMIT_ENTITIES)
    switch_entities = split_entities(GRID_EXPORT_LIMIT_SWITCHES)

    if not number_entities and not switch_entities:
        if DEBUG:
            log("Grid export curtailment enabled, but no number/switch entity configured")
        return

    decision, price_now, threshold, source = decide_pv_curtail(data)
    if decision is None:
        if DEBUG:
            log(f"Grid export limit: no PV curtail decision available (price={price_now}, threshold={threshold}, source={source})")
        return

    action = "curtail" if decision else "restore"
    target_spec = GRID_EXPORT_LIMIT_OFF_VALUE if decision else GRID_EXPORT_LIMIT_DEFAULT_VALUE
    switch_target = GRID_EXPORT_LIMIT_SWITCH_CURTAIL_STATE if decision else GRID_EXPORT_LIMIT_SWITCH_RESTORE_STATE

    log(
        "Grid export limit: "
        f"action={action} number_target={target_spec} switch_target={switch_target} "
        f"decision={decision} price={price_now if price_now is not None else '?'} "
        f"threshold={threshold} source={source}"
    )

    # Set number first. This avoids enabling the switch with an old/unsafe limit.
    for entity_id in number_entities:
        target = resolve_number_target(entity_id, target_spec)
        if target is None:
            log(f"WARN: could not resolve grid export limit target '{target_spec}' for {entity_id}; skip number")
            continue
        target = clamp_number_value(entity_id, target)
        ok = ha_set_number_value(entity_id, target)
        unit = number_entity_unit(entity_id)
        suffix = f" {unit}" if unit else ""
        if ok:
            log(f"Grid export limit number {entity_id} => {target:g}{suffix}")
        else:
            log(f"WARN: failed to set grid export limit number {entity_id} => {target:g}{suffix}")

    # Switch on = export limit active. Switch off = normal export allowed.
    for entity_id in switch_entities:
        ha_set_switch(entity_id, switch_target)


# ========================
# Main loop
# ========================

def loop():
    token_present = bool(get_ha_token())
    log(f"Agent up. verify_ssl={VERIFY_SSL} debug={DEBUG}")
    log(f"HA_URL={ha_base_url()} token_present={token_present} disable_ha={DISABLE_HA}")
    log(
        "HA battery control: "
        f"enabled={HA_CONTROL_ENABLED} ems_select_entities='{EMS_MODE_ENTITY}' "
        f"power_number_entities='{EMS_POWER_NUMBER}' power_value='{EMS_POWER_VALUE_SPEC}' "
        f"power_modes='{EMS_SET_POWER_MODES_RAW}' set_power_before_mode={EMS_SET_POWER_BEFORE_MODE} "
        f"map={EMS_MODE_OPTIONS}"
    )
    log(
        "Grid export curtailment: "
        f"enabled={PV_CURTAIL_ENABLED} number_entities='{GRID_EXPORT_LIMIT_ENTITIES}' "
        f"switch_entities='{GRID_EXPORT_LIMIT_SWITCHES}' off={GRID_EXPORT_LIMIT_OFF_VALUE} "
        f"restore={GRID_EXPORT_LIMIT_DEFAULT_VALUE} switch_curtail={GRID_EXPORT_LIMIT_SWITCH_CURTAIL_STATE} "
        f"switch_restore={GRID_EXPORT_LIMIT_SWITCH_RESTORE_STATE} "
        f"threshold='{PV_CURTAIL_BELOW_EUR_KWH_ENV or 'api/default'}'"
    )
    log(
        "Charge block: "
        f"enabled={CHARGE_BLOCK_ENABLED} sensor='{CHARGE_BLOCK_SENSOR}' "
        f"trigger_below={CHARGE_BLOCK_TRIGGER_BELOW_W:g}W "
        f"release_above={CHARGE_BLOCK_RELEASE_ABOVE_W:g}W "
        f"duration={CHARGE_BLOCK_DURATION_SEC}s modes={sorted(CHARGE_BLOCK_MODES)} "
        f"fallback_option='{CHARGE_BLOCK_FALLBACK_OPTION}'"
    )

    while True:
        try:
            action = fetch_next_action()
            server_mode = parse_int(action.get("mode"), default=-1)
            server_power = parse_int(action.get("power_watt"), default=0)
            reason = str(action.get("reason") or "")
            if DEBUG:
                log(f"server_mode={server_mode}, server_power={server_power}, reason={reason[:300]}")

            # 1) PV/export curtailment through HA number/switch entities.
            apply_grid_export_limit_from_action(action)

            # 2) Battery EMS mode through HA select/number entities.
            apply_battery_control_from_home_assistant(server_mode, server_power)

            # 3) Read telemetry from HA and upload heartbeat.
            telemetry = read_from_home_assistant() if not DISABLE_HA else {}
            if telemetry:
                if "soc_pct" in telemetry:
                    log(f"SOC from HA: {telemetry['soc_pct']}%")
                if "mode" in telemetry:
                    mode_names = {1: "Auto/Standby", 2: "Charge", 3: "Discharge"}
                    mode_value = telemetry["mode"]
                    log(f"Mode from HA: {mode_value} ({mode_names.get(mode_value, 'Unknown')})")
                if "pv_power_w" in telemetry:
                    log(f"PV power from HA: {telemetry['pv_power_w']} W")
                if "grid_power_w" in telemetry:
                    log(f"Grid power from HA: {telemetry['grid_power_w']} W")

            heartbeat = {
                "client_id": CLIENT_ID or None,
                "reported_at": int(time.time()),
                "soc": float(telemetry["soc_pct"]) if telemetry and "soc_pct" in telemetry else None,
                "battery_mode": server_mode,
                "pv_power_w": telemetry.get("pv_power_w") if telemetry else None,
                "grid_power_w": telemetry.get("grid_power_w") if telemetry else None,
            }

            payload = {key: value for key, value in heartbeat.items() if value is not None or key == "battery_mode"}
            upload_telemetry(payload)

        except Exception as exc:
            log(f"ERROR: {exc}")
            if DEBUG:
                traceback.print_exc()

        time.sleep(INTERVAL)


if __name__ == "__main__":
    loop()
