#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import time
import traceback
from typing import Any

import requests


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "ja", "aan")


def log(msg: str):
    print(f"[DWARS Generic] {msg}", flush=True)


API_KEY = os.environ.get("API_KEY", "")
CLIENT_ID = os.environ.get("CLIENT_ID", "")
API_URL = os.environ.get("API_URL", "https://api.metdezon.nl/bms/api/next_action.php")
TELEMETRY_URL = os.environ.get("TELEMETRY_URL", "https://api.metdezon.nl/bms/api/telemetry.php")
INTERVAL = int(os.environ.get("INTERVAL", "60"))
POWER = int(os.environ.get("POWER", "5000"))
DEBUG = env_bool("DEBUG", True)
VERIFY_SSL = env_bool("VERIFY_SSL", True)

AGENT_NAME = os.environ.get("ADDON_NAME", "DWARS Generic EMS Add-on")
AGENT_VERSION = os.environ.get("ADDON_VERSION", "unknown")
AGENT_TYPE = os.environ.get("AGENT_TYPE", "dwars")
BACKUP_YAML_CHECK_ENABLED = env_bool("BACKUP_YAML_CHECK_ENABLED", True)
BACKUP_YAML_PATH = os.environ.get("BACKUP_YAML_PATH", "/config/backup.yaml")
BACKUP_YAML_OVERWRITE = env_bool("BACKUP_YAML_OVERWRITE", False)

HA_URL_ENV = os.environ.get("HA_URL", "http://supervisor/core/api")
HA_CONTROL_ENABLED = env_bool("HA_CONTROL_ENABLED", True)

SOC_ENTITY = os.environ.get("SOC_ENTITY", "")
PV_ENTITY = os.environ.get("PV_ENTITY", "")
GRID_ENTITY = os.environ.get("GRID_ENTITY", "")
BATTERY_POWER_ENTITY = os.environ.get("BATTERY_POWER_ENTITY", "")
INVERTER_MODE_ENTITY = os.environ.get("INVERTER_MODE_ENTITY", "")

MODE_SELECT = os.environ.get("HA_MODE_SELECT", "")
MODE_MAP_JSON = os.environ.get("HA_MODE_MAP_JSON", "")
MODE_IDLE_OPTION = os.environ.get("HA_MODE_IDLE_OPTION", "auto")
MODE_CHARGE_OPTION = os.environ.get("HA_MODE_CHARGE_OPTION", "charge")
MODE_DISCHARGE_OPTION = os.environ.get("HA_MODE_DISCHARGE_OPTION", "discharge")

SERVER_MODES_IDLE_RAW = os.environ.get("HA_SERVER_MODES_IDLE", "1,7")
SERVER_MODES_CHARGE_RAW = os.environ.get("HA_SERVER_MODES_CHARGE", "3")
SERVER_MODES_DISCHARGE_RAW = os.environ.get("HA_SERVER_MODES_DISCHARGE", "4")

POWER_NUMBER = os.environ.get("HA_POWER_NUMBER", "")
CHARGE_POWER_NUMBER = os.environ.get("HA_CHARGE_POWER_NUMBER", "")
DISCHARGE_POWER_NUMBER = os.environ.get("HA_DISCHARGE_POWER_NUMBER", "")
IDLE_POWER_NUMBER = os.environ.get("HA_IDLE_POWER_NUMBER", "")
CHARGE_POWER_VALUE = os.environ.get("HA_CHARGE_POWER_VALUE", "server_power")
DISCHARGE_POWER_VALUE = os.environ.get("HA_DISCHARGE_POWER_VALUE", "server_power")
IDLE_POWER_VALUE = os.environ.get("HA_IDLE_POWER_VALUE", "skip")
SET_POWER_BEFORE_MODE = env_bool("HA_SET_POWER_BEFORE_MODE", True)

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


def truthy_text(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "ja", "aan")


def backup_yaml_content_ok(content: str) -> bool:
    required = (
        "alias: Auto update everything",
        "backup.create_automatic",
        "update.install",
        "entity_id: all",
    )
    return all(marker in content for marker in required)


def ensure_backup_yaml() -> dict[str, Any]:
    """Ensure /config/backup.yaml contains the DWARS auto-update automation."""
    path = str(BACKUP_YAML_PATH or "/config/backup.yaml").strip() or "/config/backup.yaml"
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

            if backup_yaml_content_ok(current):
                status["backup_yaml_ok"] = True
            else:
                if BACKUP_YAML_OVERWRITE:
                    new_content = BACKUP_YAML_CONTENT
                else:
                    separator = "\n\n# DWARS auto update automation\n"
                    new_content = current.rstrip() + separator + BACKUP_YAML_CONTENT
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(new_content)
                changed = True
                status["backup_yaml_ok"] = True
        else:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(BACKUP_YAML_CONTENT)
            changed = True
            status["backup_yaml_ok"] = True

        mtime = int(os.path.getmtime(path)) if os.path.exists(path) else int(time.time())
        status["backup_yaml_updated_at"] = int(time.time()) if changed else mtime
        if changed:
            log(f"backup.yaml ensured at {path}")
    except Exception as exc:
        status["backup_yaml_ok"] = False
        status["backup_yaml_error"] = str(exc)
        log(f"WARN: backup.yaml check failed for {path}: {exc}")

    return status



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


def split_entities(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text or text.lower() in ("none", "null", "skip", "disabled", "false", "off"):
        return []
    return [part.strip() for part in re.split(r"[,;\s]+", text) if part.strip()]


def normalize_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def parse_int_set(raw: str) -> set[int]:
    out: set[int] = set()
    for part in re.split(r"[,;\s]+", str(raw or "")):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except Exception:
            log(f"WARN: invalid mode number {part!r} in mode list {raw!r}")
    return out


def parse_mode_map_json(raw: str) -> dict[int, str]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception as exc:
        log(f"WARN: invalid ha_mode_map_json; ignoring. Error: {exc}")
        return {}
    if not isinstance(data, dict):
        log("WARN: ha_mode_map_json must be a JSON object like {\"3\":\"Charge\"}")
        return {}

    out: dict[int, str] = {}
    for key, value in data.items():
        try:
            mode = int(str(key).strip())
        except Exception:
            log(f"WARN: invalid mode key in ha_mode_map_json: {key!r}")
            continue
        option = str(value or "").strip()
        if option:
            out[mode] = option
    return out


SERVER_MODES_IDLE = parse_int_set(SERVER_MODES_IDLE_RAW)
SERVER_MODES_CHARGE = parse_int_set(SERVER_MODES_CHARGE_RAW)
SERVER_MODES_DISCHARGE = parse_int_set(SERVER_MODES_DISCHARGE_RAW)
MODE_MAP = parse_mode_map_json(MODE_MAP_JSON)


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
        log("ERROR: no Home Assistant token in env. Fill ha_token or enable add-on Home Assistant API access.")
        return None
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def ha_get_state(entity_id: str) -> dict[str, Any] | None:
    if not entity_id:
        return None
    headers = ha_headers()
    if headers is None:
        return None
    try:
        response = requests.get(f"{ha_base_url()}/states/{entity_id}", headers=headers, timeout=5)
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
    headers = ha_headers()
    if headers is None:
        return False
    try:
        response = requests.post(f"{ha_base_url()}/services/{domain}/{service}", headers=headers, json=payload, timeout=8)
        ok = 200 <= response.status_code < 300
        if DEBUG or not ok:
            log(f"HA SERVICE {domain}.{service} {payload} -> {response.status_code} {response.text[:200]}")
        return ok
    except Exception as exc:
        log(f"HA SERVICE {domain}.{service} error: {exc}")
        return False


def ha_get_config_version() -> str | None:
    headers = ha_headers()
    if headers is None:
        return None
    try:
        response = requests.get(f"{ha_base_url()}/config", headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict) and data.get("version"):
                return str(data.get("version"))
        if DEBUG:
            log(f"HA config version -> {response.status_code} {response.text[:200]}")
    except Exception as exc:
        if DEBUG:
            log(f"HA config version error: {exc}")
    return None


def entity_attrs(entity_id: str) -> dict[str, Any]:
    state = ha_get_state(entity_id)
    if not state:
        return {}
    attrs = state.get("attributes") or {}
    return attrs if isinstance(attrs, dict) else {}


def number_entity_max(entity_id: str) -> float | None:
    attrs = entity_attrs(entity_id)
    for key in ("max", "max_value", "native_max_value"):
        val = parse_float(attrs.get(key))
        if val is not None:
            return val
    return None


def number_entity_min(entity_id: str) -> float | None:
    attrs = entity_attrs(entity_id)
    for key in ("min", "min_value", "native_min_value"):
        val = parse_float(attrs.get(key))
        if val is not None:
            return val
    return None


def number_entity_unit(entity_id: str) -> str:
    attrs = entity_attrs(entity_id)
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


def resolve_number_target(entity_id: str, spec: str, server_power: int) -> float | None:
    text = str(spec or "").strip().lower()
    if text in ("", "none", "null", "skip", "disabled", "false", "off"):
        return None
    if text in ("server_power", "server_power_w", "power", "power_w", "watt", "watts"):
        return float(server_power if server_power > 0 else POWER)
    direct = parse_float(text)
    if direct is not None:
        return direct
    if text in ("max", "maximum", "native_max"):
        return number_entity_max(entity_id)
    if text in ("min", "minimum", "native_min"):
        return number_entity_min(entity_id)
    log(f"WARN: unknown power value spec {spec!r}; use server_power/max/min/skip or a number")
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
    if not option or option.lower() in ("none", "null", "skip", "disabled", "false", "off"):
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
                log(f"Mode option {option!r} matched available option {available_text!r}")
                return available_text
        for available in options:
            available_text = str(available).strip()
            if normalize_key(available_text) == requested_key:
                log(f"Mode option {option!r} matched available option {available_text!r}")
                return available_text
        if options:
            log(f"WARN: option {option!r} not in available options for {entity_id}: {options}")
    return option


def ha_select_option(entity_id: str, option: str) -> bool:
    selected_option = normalize_select_option(entity_id, option)
    if not selected_option:
        if DEBUG:
            log(f"Select {entity_id}: option {option!r}; skip")
        return True
    state = ha_get_state(entity_id)
    if state and str(state.get("state", "")).strip() == selected_option:
        if DEBUG:
            log(f"Select {entity_id} already {selected_option!r}; skip")
        return True
    ok = ha_call_service("select", "select_option", {"entity_id": entity_id, "option": selected_option})
    if ok:
        log(f"Select {entity_id} => {selected_option}")
    else:
        log(f"WARN: failed to set select {entity_id} => {selected_option}")
    return ok


def target_for_server_mode(server_mode: int) -> tuple[str, str | None]:
    if server_mode in MODE_MAP:
        return "custom", MODE_MAP[server_mode]
    if server_mode in SERVER_MODES_CHARGE:
        return "charge", MODE_CHARGE_OPTION
    if server_mode in SERVER_MODES_DISCHARGE:
        return "discharge", MODE_DISCHARGE_OPTION
    if server_mode in SERVER_MODES_IDLE:
        return "idle", MODE_IDLE_OPTION
    return "unknown", None


def power_entities_for_kind(kind: str) -> list[str]:
    if kind == "charge":
        raw = CHARGE_POWER_NUMBER or POWER_NUMBER
    elif kind == "discharge":
        raw = DISCHARGE_POWER_NUMBER or POWER_NUMBER
    elif kind == "idle":
        raw = IDLE_POWER_NUMBER or POWER_NUMBER
    else:
        raw = POWER_NUMBER
    return split_entities(raw)


def power_spec_for_kind(kind: str) -> str:
    if kind == "charge":
        return CHARGE_POWER_VALUE
    if kind == "discharge":
        return DISCHARGE_POWER_VALUE
    if kind == "idle":
        return IDLE_POWER_VALUE
    return "skip"


def set_power(kind: str, server_power: int) -> bool:
    entities = power_entities_for_kind(kind)
    spec = power_spec_for_kind(kind)
    if not entities or str(spec or "").strip().lower() in ("", "none", "null", "skip", "disabled", "false", "off"):
        if DEBUG:
            log(f"Power control skip for kind={kind}: entities={entities} spec={spec!r}")
        return True

    ok_all = True
    for entity_id in entities:
        target = resolve_number_target(entity_id, spec, server_power)
        if target is None:
            ok_all = False
            continue
        target = clamp_number_value(entity_id, target)
        ok = ha_set_number_value(entity_id, target)
        ok_all = ok_all and ok
        unit = number_entity_unit(entity_id)
        suffix = f" {unit}" if unit else ""
        if ok:
            log(f"Power {entity_id} => {target:g}{suffix}")
        else:
            log(f"WARN: failed to set power {entity_id} => {target:g}{suffix}")
    return ok_all


def set_mode(option: str) -> bool:
    entities = split_entities(MODE_SELECT)
    if not entities:
        log("WARN: ha_mode_select is empty; cannot set inverter mode")
        return False
    ok_all = True
    for entity_id in entities:
        ok_all = ha_select_option(entity_id, option) and ok_all
    return ok_all


def apply_control(server_mode: int, server_power: int) -> bool:
    if not HA_CONTROL_ENABLED:
        if DEBUG:
            log("HA control disabled; skip")
        return False

    kind, option = target_for_server_mode(server_mode)
    if not option:
        log(f"Unknown server mode {server_mode}; no mode mapping configured")
        return False

    log(
        f"Apply control: server_mode={server_mode} kind={kind} option={option!r} "
        f"server_power={server_power if server_power > 0 else POWER}W mode_select={split_entities(MODE_SELECT)} "
        f"power_entities={power_entities_for_kind(kind)} power_spec={power_spec_for_kind(kind)!r}"
    )

    if SET_POWER_BEFORE_MODE:
        ok_power = set_power(kind, server_power)
        ok_mode = set_mode(option)
    else:
        ok_mode = set_mode(option)
        ok_power = set_power(kind, server_power)
    return ok_mode and ok_power


def read_entity_float(entity_id: str) -> float | None:
    if not entity_id:
        return None
    state = ha_get_state(entity_id)
    if not state:
        return None
    return parse_float(state.get("state"))


def read_telemetry() -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    soc = read_entity_float(SOC_ENTITY)
    if soc is not None:
        telemetry["soc_pct"] = soc
    pv = read_entity_float(PV_ENTITY)
    if pv is not None:
        telemetry["pv_power_w"] = int(round(pv))
    grid = read_entity_float(GRID_ENTITY)
    if grid is not None:
        telemetry["grid_power_w"] = int(round(grid))
    battery_power = read_entity_float(BATTERY_POWER_ENTITY)
    if battery_power is not None:
        telemetry["battery_power_w"] = int(round(battery_power))
    if INVERTER_MODE_ENTITY:
        state = ha_get_state(INVERTER_MODE_ENTITY)
        if state and "state" in state:
            telemetry["inverter_mode"] = str(state.get("state"))
    return telemetry


def fetch_next_action() -> dict[str, Any]:
    if DEBUG:
        log(f"HTTP GET {API_URL} verify_ssl={VERIFY_SSL}")
    response = requests.get(API_URL, headers=HEADERS_EXT, timeout=10, verify=VERIFY_SSL)
    if DEBUG:
        log(f"HTTP {response.status_code} len={len(response.content)}")
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("next_action response is not a JSON object")
    return data


def upload_telemetry(payload: dict[str, Any]) -> None:
    if not TELEMETRY_URL:
        return
    try:
        if DEBUG:
            log(f"POST telemetry {TELEMETRY_URL} -> {payload}")
        response = requests.post(TELEMETRY_URL, headers=HEADERS_EXT, json=payload, timeout=10, verify=VERIFY_SSL)
        if DEBUG:
            log(f"TEL HTTP {response.status_code} {response.text[:200]}")
        response.raise_for_status()
    except Exception as exc:
        log(f"Telemetry upload error: {exc}")


def loop() -> None:
    token_present = bool(get_ha_token())
    log(f"Agent up. debug={DEBUG} verify_ssl={VERIFY_SSL}")
    log(f"Agent metadata: name={AGENT_NAME} version={AGENT_VERSION} type={AGENT_TYPE}")
    log(f"HA_URL={ha_base_url()} token_present={token_present}")
    log(
        "Mode mapping: "
        f"idle_modes={SERVER_MODES_IDLE_RAW}->{MODE_IDLE_OPTION!r} "
        f"charge_modes={SERVER_MODES_CHARGE_RAW}->{MODE_CHARGE_OPTION!r} "
        f"discharge_modes={SERVER_MODES_DISCHARGE_RAW}->{MODE_DISCHARGE_OPTION!r} "
        f"json_override={MODE_MAP}"
    )

    while True:
        try:
            action = fetch_next_action()
            server_mode = parse_int(action.get("mode"), default=-1)
            server_power = parse_int(action.get("power_watt"), default=0)
            reason = str(action.get("reason") or "")
            if DEBUG:
                log(f"server_mode={server_mode} server_power={server_power} reason={reason[:300]}")

            apply_control(server_mode, server_power)

            telemetry = read_telemetry()
            backup_status = ensure_backup_yaml()
            ha_version = ha_get_config_version()

            heartbeat = {
                "client_id": CLIENT_ID or None,
                "reported_at": int(time.time()),
                "agent_name": AGENT_NAME,
                "agent_type": AGENT_TYPE,
                "agent_version": AGENT_VERSION,
                "ha_version": ha_version,
                "backup_yaml_ok": backup_status.get("backup_yaml_ok"),
                "backup_yaml_path": backup_status.get("backup_yaml_path"),
                "backup_yaml_updated_at": backup_status.get("backup_yaml_updated_at"),
                "battery_mode": server_mode,
                "soc": telemetry.get("soc_pct"),
                "pv_power_w": telemetry.get("pv_power_w"),
                "grid_power_w": telemetry.get("grid_power_w"),
                "battery_power_w": telemetry.get("battery_power_w"),
                "inverter_mode": telemetry.get("inverter_mode"),
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
