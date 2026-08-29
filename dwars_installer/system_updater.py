#!/usr/bin/env python3
"""Automatic Home Assistant update orchestrator for DWARS Installer.

Uses the Home Assistant Core REST proxy for update entities and the Supervisor
API only for the final restart/reboot. Upstream GoodWe updates are excluded;
DWARS/DCENT-branded GoodWe artifacts remain eligible and the installer itself
keeps custom_components/goodwe synchronized from the DWARS repository.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OPTIONS_PATH = Path("/data/options.json")
STATE_PATH = Path("/data/system_auto_update_state.json")
CORE_API = "http://supervisor/core/api"
SUPERVISOR_API = "http://supervisor"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
PREFIX = "[DWARS Auto Update 0.4.0]"


def log(message: str) -> None:
    print(f"{PREFIX} {message}", flush=True)


def options() -> dict[str, Any]:
    try:
        value = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def opt_bool(cfg: dict[str, Any], key: str, default: bool) -> bool:
    value = cfg.get(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "ja", "aan"}


def opt_int(cfg: dict[str, Any], key: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(float(cfg.get(key, default))))
    except (TypeError, ValueError):
        return max(minimum, default)


def request_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 60) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    req = Request(url, data=body, headers=headers, method=method)
    with urlopen(req, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def wait_for_core(timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            request_json("GET", f"{CORE_API}/", timeout=15)
            return True
        except Exception:
            time.sleep(15)
    return False


def state_text(state: dict[str, Any]) -> str:
    attrs = state.get("attributes") or {}
    return " ".join(
        str(value or "")
        for value in (
            state.get("entity_id"),
            attrs.get("friendly_name"),
            attrs.get("title"),
            attrs.get("name"),
            attrs.get("release_summary"),
            attrs.get("release_url"),
            attrs.get("installed_version"),
            attrs.get("latest_version"),
        )
    ).lower()


def is_protected_upstream_goodwe(state: dict[str, Any]) -> bool:
    text = state_text(state)
    if "goodwe" not in text:
        return False
    # The custom DWARS agent/repository is allowed; original/HACS GoodWe is not.
    return not any(marker in text for marker in ("dwars", "dcent", "metdezon", "goodwe agent"))


def explicit_exclusions(cfg: dict[str, Any]) -> set[str]:
    raw = str(cfg.get("system_auto_update_exclude_entities", "") or "")
    return {item for item in re.split(r"[,;\s]+", raw) if item}


def update_priority(state: dict[str, Any]) -> tuple[int, str]:
    text = state_text(state)
    entity_id = str(state.get("entity_id") or "")
    # Add-ons/integrations first, then Supervisor, Core, OS last.
    if "home assistant operating system" in text or "home_assistant_operating_system" in entity_id or "os_update" in entity_id:
        priority = 50
    elif "home assistant core" in text or "core_update" in entity_id:
        priority = 40
    elif "supervisor" in text:
        priority = 30
    else:
        priority = 10
    return priority, entity_id


def available_updates(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    states = request_json("GET", f"{CORE_API}/states", timeout=60)
    excluded = explicit_exclusions(cfg)
    updates = []
    for state in states if isinstance(states, list) else []:
        entity_id = str(state.get("entity_id") or "")
        if not entity_id.startswith("update.") or str(state.get("state")).lower() != "on":
            continue
        if entity_id in excluded:
            log(f"Overslaan volgens exclude-lijst: {entity_id}")
            continue
        if is_protected_upstream_goodwe(state):
            log(f"Beschermde originele/upstream GoodWe-update overslaan: {entity_id}")
            continue
        updates.append(state)
    return sorted(updates, key=update_priority)


def install_update(state: dict[str, Any], backup: bool) -> bool:
    entity_id = str(state.get("entity_id") or "")
    attrs = state.get("attributes") or {}
    current = attrs.get("installed_version", "?")
    latest = attrs.get("latest_version", "?")
    log(f"Installeren: {entity_id} ({current} -> {latest})")
    payload: dict[str, Any] = {"entity_id": entity_id}
    # Request a backup whenever the update manager is configured to do so. Some
    # Home Assistant update entities do not expose a human-readable list of
    # supported feature names even though the service accepts this field. A
    # strict integration is retried without ``backup`` below.
    if backup:
        payload["backup"] = True
    try:
        request_json("POST", f"{CORE_API}/services/update/install", payload, timeout=120)
        return True
    except HTTPError as exc:
        # Retry without the optional backup key for integrations with a strict schema.
        if "backup" in payload:
            try:
                request_json("POST", f"{CORE_API}/services/update/install", {"entity_id": entity_id}, timeout=120)
                return True
            except Exception as retry_exc:
                log(f"Update mislukt: {entity_id}: {retry_exc}")
                return False
        log(f"Update mislukt: {entity_id}: HTTP {exc.code}")
    except Exception as exc:
        # Core/OS can drop the connection while successfully replacing itself.
        log(f"Verbinding onderbroken tijdens {entity_id}: {exc}; herstel controleren.")
        return True
    return False


def disable_legacy_update_all_automation(cfg: dict[str, Any]) -> None:
    if not opt_bool(cfg, "disable_legacy_update_all_automation", True):
        return
    try:
        states = request_json("GET", f"{CORE_API}/states", timeout=60)
    except Exception:
        return
    for state in states if isinstance(states, list) else []:
        entity_id = str(state.get("entity_id") or "")
        text = state_text(state)
        if entity_id.startswith("automation.") and "auto update everything" in text:
            try:
                request_json("POST", f"{CORE_API}/services/automation/turn_off", {"entity_id": entity_id, "stop_actions": True}, timeout=30)
                log(f"Legacy update.install entity_id=all automation uitgeschakeld: {entity_id}")
            except Exception as exc:
                log(f"Legacy update-automation kon niet worden uitgeschakeld: {exc}")


def run_cycle(cfg: dict[str, Any]) -> None:
    if not TOKEN:
        log("SUPERVISOR_TOKEN ontbreekt; updatecyclus overgeslagen.")
        return
    if not wait_for_core(opt_int(cfg, "system_auto_update_wait_timeout_sec", 3600, 60)):
        log("Home Assistant Core werd niet bereikbaar; volgende cyclus probeert opnieuw.")
        return

    disable_legacy_update_all_automation(cfg)
    updates = available_updates(cfg)
    if not updates:
        log("Geen toegestane updates beschikbaar.")
        return

    backup = opt_bool(cfg, "system_auto_update_backup", True)
    between = opt_int(cfg, "system_auto_update_between_items_sec", 20, 5)
    wait_timeout = opt_int(cfg, "system_auto_update_wait_timeout_sec", 3600, 60)
    installed: list[str] = []
    system_update = False
    core_update = False

    for state in updates:
        entity_id = str(state.get("entity_id") or "")
        text = state_text(state)
        if install_update(state, backup):
            installed.append(entity_id)
            system_update = system_update or any(k in text for k in ("operating system", "supervisor"))
            core_update = core_update or "home assistant core" in text or "core_update" in entity_id
        # Updates can restart Core or the host. Wait for the API before continuing.
        time.sleep(between)
        if not wait_for_core(wait_timeout):
            log("Core is na update nog niet bereikbaar; cyclus wordt na reboot hervat.")
            break

    STATE_PATH.write_text(json.dumps({"installed": installed, "completed_at": int(time.time())}, indent=2), encoding="utf-8")
    if not installed:
        return

    # Add-on updates restart themselves. Core updates also restart Core. For
    # other updates, explicitly reload Core once; system updates may require a
    # host reboot and are handled last.
    if opt_bool(cfg, "system_auto_update_reboot_after_system_update", True) and system_update:
        log("Systeemupdate geïnstalleerd; host reboot aanvragen.")
        try:
            request_json("POST", f"{SUPERVISOR_API}/host/reboot", {}, timeout=30)
        except Exception as exc:
            log(f"Host reboot request resultaat: {exc}")
    elif opt_bool(cfg, "system_auto_update_restart_core", True) and not core_update:
        log("Updates geïnstalleerd; Home Assistant Core herstarten.")
        try:
            request_json("POST", f"{SUPERVISOR_API}/core/restart", {}, timeout=30)
        except Exception as exc:
            log(f"Core restart request resultaat: {exc}")


def main() -> None:
    cfg = options()
    initial = opt_int(cfg, "system_auto_update_initial_delay_sec", 90, 0)
    interval = opt_int(cfg, "system_auto_update_interval_sec", 21600, 300)
    log(f"Gestart; eerste controle over {initial}s, daarna iedere {interval}s.")
    time.sleep(initial)
    while True:
        try:
            cfg = options()
            if opt_bool(cfg, "system_auto_update_enabled", True):
                run_cycle(cfg)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            log(f"Updatecyclus fout: {exc}")
        except Exception as exc:
            log(f"Onverwachte updatefout: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
