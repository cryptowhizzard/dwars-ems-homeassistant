#!/usr/bin/env python3
"""DWARS fleet-safe automatic updater for Home Assistant OS installations.

The updater runs inside the DWARS Installer add-on and uses the documented
Supervisor and Home Assistant Core APIs. It deliberately creates one full
backup per run and never asks individual update entities to create their own
backup. Progress is persisted under /data so a Core restart, Supervisor
restart, add-on self-update, or host reboot can be resumed safely.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import fnmatch
import hashlib
import json
import os
import socket
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = "0.5.1"
STATE_VERSION = 2
DEFAULT_STATE_PATH = "/data/dwars_auto_update_state.json"
DEFAULT_LOCK_PATH = "/data/dwars_maintenance.lock"
DEFAULT_OPTIONS_PATH = "/data/options.json"
DEFAULT_SUPERVISOR_URL = "http://supervisor"
STATUS_ENTITY = "sensor.dwars_auto_update_status"
NOTIFICATION_ID = "dwars_auto_update_result"

STAGES = (
    "preflight",
    "backup",
    "addons",
    "entities",
    "restart_after_entities",
    "retry",
    "plugins",
    "supervisor",
    "os",
    "os_wait_reboot",
    "core",
    "verify",
)


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class PauseRun(RuntimeError):
    """Pause an active run without marking it failed (normally for reboot)."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def log(message: str) -> None:
    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    print(f"[DWARS Auto Updater] {stamp} {message}", flush=True)


def to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "ja", "aan"}:
        return True
    if text in {"0", "false", "no", "off", "nee", "uit", ""}:
        return False
    return default


def to_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload and payload.get("data") is not None:
        return payload["data"]
    return payload


def compact_exception(exc: BaseException) -> str:
    text = str(exc).replace("\n", " ").strip()
    return text[:700] if text else exc.__class__.__name__


def api_error_may_be_interrupted_update(exc: "ApiError") -> bool:
    """Return true only when an update may have continued after disconnect.

    Explicit client errors (400-499) mean Supervisor rejected the request and
    must never be interpreted as a successful/staged update. A transport error
    or a gateway/service restart (502/503/504) can happen while Core,
    Supervisor or the add-on container is being replaced, so those are
    verified through the relevant info endpoint.
    """
    return exc.status is None or exc.status in {502, 503, 504}


class Options:
    """Read add-on options while tolerating legacy nested option maps."""

    def __init__(self, path: str = DEFAULT_OPTIONS_PATH) -> None:
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"Options konden niet worden gelezen ({exc}); veilige defaults worden gebruikt.")
            return {}
        if not isinstance(raw, dict):
            return {}

        # Older broken builds could store the complete option map below
        # "options" or even as the value of a string option. Prefer the map
        # that actually contains known DWARS updater keys.
        candidates: list[dict[str, Any]] = [raw]
        nested = raw.get("options")
        if isinstance(nested, dict):
            candidates.insert(0, nested)
        for value in raw.values():
            if isinstance(value, dict) and any(
                key in value
                for key in (
                    "auto_full_system_update",
                    "github_repo_zip_url",
                    "inverter_type",
                )
            ):
                candidates.insert(0, value)
        return candidates[0]

    def get(self, key: str, default: Any = None) -> Any:
        value = self.data.get(key, default)
        if isinstance(value, (dict, list)):
            return default
        return value

    def bool(self, key: str, default: bool = False) -> bool:
        return to_bool(self.get(key, default), default)

    def integer(
        self,
        key: str,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        return to_int(self.get(key, default), default, minimum, maximum)

    def text(self, key: str, default: str = "") -> str:
        value = self.get(key, default)
        return str(value).strip() if value is not None else default


class JsonState:
    def __init__(self, path: str = DEFAULT_STATE_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"state_version": STATE_VERSION}
        if not isinstance(value, dict):
            return {"state_version": STATE_VERSION}
        if value.get("state_version") != STATE_VERSION:
            # Keep useful scheduling history, but do not resume an incompatible
            # in-flight state machine.
            return {
                "state_version": STATE_VERSION,
                "last_completed_at": value.get("last_completed_at"),
                "last_result": value.get("last_result"),
            }
        return value

    def save(self, value: dict[str, Any]) -> None:
        value["state_version"] = STATE_VERSION
        value["updated_at"] = iso_now()
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, self.path)


def resolve_supervisor_token() -> tuple[str, str]:
    """Resolve Supervisor auth across current, legacy and S6 environments."""
    for key in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value, key

    for filename in (
        "/run/s6/container_environment/SUPERVISOR_TOKEN",
        "/run/s6/container_environment/HASSIO_TOKEN",
        "/var/run/s6/container_environment/SUPERVISOR_TOKEN",
        "/var/run/s6/container_environment/HASSIO_TOKEN",
    ):
        try:
            value = Path(filename).read_text(encoding="utf-8", errors="ignore").replace("\x00", "").strip()
        except OSError:
            continue
        if value:
            return value, filename
    return "", ""


def wait_for_supervisor_token(poll_seconds: int = 60) -> tuple[str, str]:
    """Daemon-safe auth wait: never crash merely because token injection is late/missing."""
    warned = False
    while True:
        token, source = resolve_supervisor_token()
        if token:
            if warned:
                print(f"[DWARS AutoUpdater] Supervisor API-auth is beschikbaar via {source}; token wordt niet gelogd.", flush=True)
            return token, source
        if not warned:
            print(
                "[DWARS AutoUpdater] Supervisor API-token ontbreekt. "
                "Ik blijf draaien en probeer SUPERVISOR_TOKEN, legacy HASSIO_TOKEN en S6 environment-files opnieuw.",
                flush=True,
            )
            warned = True
        time.sleep(max(5, poll_seconds))


class ApiClient:
    def __init__(self, token: str, supervisor_url: str = DEFAULT_SUPERVISOR_URL) -> None:
        if not token:
            raise RuntimeError("SUPERVISOR_TOKEN ontbreekt")
        self.token = token
        self.supervisor_url = supervisor_url.rstrip("/")
        self.ha_url = self.supervisor_url + "/core/api"

    def _request(
        self,
        base: str,
        method: str,
        path: str,
        data: dict[str, Any] | list[Any] | None = None,
        *,
        timeout: int = 60,
    ) -> Any:
        encoded: bytes | None = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"DWARS-Auto-Updater/{VERSION}",
        }
        if data is not None:
            encoded = json.dumps(data, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            base + path,
            data=encoded,
            headers=headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                if not raw.strip():
                    return {}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {"raw": raw}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ApiError(
                f"{method} {path} gaf HTTP {exc.code}: {body[:500]}",
                status=exc.code,
                body=body,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError(f"{method} {path} mislukt: {exc}") from exc

    def supervisor(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | list[Any] | None = None,
        *,
        timeout: int = 60,
    ) -> Any:
        return self._request(self.supervisor_url, method, path, data, timeout=timeout)

    def ha(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | list[Any] | None = None,
        *,
        timeout: int = 60,
    ) -> Any:
        return self._request(self.ha_url, method, path, data, timeout=timeout)

    def wait_supervisor(self, timeout: int, interval: int = 10) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.supervisor("GET", "/info", timeout=20)
                return True
            except ApiError:
                time.sleep(interval)
        return False

    def wait_ha(self, timeout: int, interval: int = 10) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.ha("GET", "/config", timeout=20)
                return True
            except ApiError:
                time.sleep(interval)
        return False


@dataclass(frozen=True)
class UpdateItem:
    kind: str
    item_id: str
    name: str
    installed: str = ""
    latest: str = ""
    release_url: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "id": self.item_id,
            "name": self.name,
            "installed": self.installed,
            "latest": self.latest,
            "release_url": self.release_url,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "UpdateItem":
        return cls(
            kind=str(value.get("kind", "")),
            item_id=str(value.get("id", "")),
            name=str(value.get("name", value.get("id", ""))),
            installed=str(value.get("installed", "")),
            latest=str(value.get("latest", "")),
            release_url=str(value.get("release_url", "")),
        )


class AutoUpdater:
    def __init__(
        self,
        options: Options,
        state_store: JsonState,
        client: ApiClient,
        lock_path: str = DEFAULT_LOCK_PATH,
    ) -> None:
        self.options = options
        self.state_store = state_store
        self.client = client
        self.lock_path = Path(lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self.state_store.load()
        self._timezone: ZoneInfo | None = None
        self._host_name: str | None = None

    # ---------- generic state and reporting ----------

    def save(self) -> None:
        self.state_store.save(self.state)

    def append_log(self, message: str) -> None:
        entries = self.state.setdefault("log", [])
        if not isinstance(entries, list):
            entries = []
            self.state["log"] = entries
        entries.append({"at": iso_now(), "message": message[:1000]})
        del entries[:-100]
        log(message)
        self.save()
        self.publish_status()

    def set_stage(self, stage: str, message: str | None = None) -> None:
        if stage not in STAGES and stage not in {"scheduled", "complete", "failed"}:
            raise ValueError(f"Onbekende stage: {stage}")
        self.state["stage"] = stage
        self.state["current_item"] = None
        if message:
            self.append_log(message)
        else:
            self.save()
            self.publish_status()

    def add_success(self, item: UpdateItem | str, detail: str = "") -> None:
        if isinstance(item, str):
            value = {"kind": "system", "id": item, "name": item}
        else:
            value = item.as_dict()
        value.update({"at": iso_now(), "detail": detail[:500]})
        successes = self.state.setdefault("successes", [])
        if isinstance(successes, list):
            successes.append(value)
        self.save()

    def add_failure(self, item: UpdateItem | str, reason: str, *, final: bool = True) -> None:
        if isinstance(item, str):
            value = {"kind": "system", "id": item, "name": item}
        else:
            value = item.as_dict()
        value.update({"at": iso_now(), "reason": reason[:1000], "final": final})
        failures = self.state.setdefault("failures", [])
        if isinstance(failures, list):
            failures.append(value)
        self.save()

    def publish_status(self) -> None:
        stage = str(self.state.get("stage") or "idle")
        attrs = {
            "friendly_name": "DWARS automatische updates",
            "version": VERSION,
            "active": bool(self.state.get("active")),
            "stage": stage,
            "run_id": self.state.get("run_id"),
            "started_at": self.state.get("started_at"),
            "updated_at": self.state.get("updated_at"),
            "last_completed_at": self.state.get("last_completed_at"),
            "last_result": self.state.get("last_result"),
            "next_run": self.state.get("next_run"),
            "success_count": len(self.state.get("successes") or []),
            "failure_count": len([f for f in (self.state.get("failures") or []) if f.get("final", True)]),
            "backup": self.state.get("backup"),
            "current_item": self.state.get("current_item"),
        }
        try:
            self.client.ha("POST", f"/states/{STATUS_ENTITY}", {"state": stage, "attributes": attrs}, timeout=15)
        except ApiError:
            pass

    def final_notification(self) -> None:
        if not self.options.bool("auto_full_system_update_notify", True):
            return
        successes = self.state.get("successes") or []
        failures = [f for f in (self.state.get("failures") or []) if f.get("final", True)]
        pending = self.state.get("pending_after_verify") or []
        result = str(self.state.get("last_result", "onbekend"))
        lines = [
            f"DWARS automatische update: {result}",
            f"Geslaagd: {len(successes)}",
            f"Mislukt: {len(failures)}",
            f"Nog beschikbaar: {len(pending)}",
        ]
        if failures:
            lines.append("Mislukt: " + ", ".join(str(x.get("name") or x.get("id")) for x in failures[:12]))
        if pending:
            lines.append("Nog beschikbaar: " + ", ".join(str(x) for x in pending[:12]))
        payload = {
            "title": "DWARS automatische updates",
            "message": "\n".join(lines),
            "notification_id": NOTIFICATION_ID,
        }
        try:
            self.client.ha("POST", "/services/persistent_notification/create", payload, timeout=30)
        except ApiError as exc:
            log(f"Eindnotificatie kon niet worden geplaatst: {compact_exception(exc)}")

    # ---------- scheduler ----------

    def host_info(self) -> dict[str, Any]:
        try:
            value = unwrap(self.client.supervisor("GET", "/host/info", timeout=30))
            return value if isinstance(value, dict) else {}
        except ApiError:
            return {}

    def host_name(self) -> str:
        if self._host_name:
            return self._host_name
        info = self.host_info()
        self._host_name = str(info.get("hostname") or socket.gethostname() or "dwars")
        return self._host_name

    def timezone(self) -> ZoneInfo:
        if self._timezone is not None:
            return self._timezone
        configured = self.options.text("auto_full_system_update_timezone", "auto")
        candidates: list[str] = []
        if configured and configured.lower() != "auto":
            candidates.append(configured)
        else:
            info = self.host_info()
            if info.get("timezone"):
                candidates.append(str(info["timezone"]))
            try:
                config = self.client.ha("GET", "/config", timeout=20)
                if isinstance(config, dict) and config.get("time_zone"):
                    candidates.append(str(config["time_zone"]))
            except ApiError:
                pass
        candidates.extend(["Europe/Amsterdam", "UTC"])
        for name in candidates:
            try:
                self._timezone = ZoneInfo(name)
                self.state["timezone"] = name
                self.save()
                return self._timezone
            except ZoneInfoNotFoundError:
                continue
        self._timezone = ZoneInfo("UTC")
        return self._timezone

    def schedule_time(self) -> tuple[int, int]:
        raw = self.options.text("auto_full_system_update_time", "04:00")
        try:
            hour_s, minute_s = raw.split(":", 1)
            hour = int(hour_s)
            minute = int(minute_s)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
            return hour, minute
        except (ValueError, TypeError):
            log(f"Ongeldige update-tijd {raw!r}; 04:00 wordt gebruikt.")
            return 4, 0

    def allowed_weekdays(self) -> set[int]:
        aliases = {
            "mon": 0, "monday": 0, "ma": 0,
            "tue": 1, "tuesday": 1, "di": 1,
            "wed": 2, "wednesday": 2, "wo": 2,
            "thu": 3, "thursday": 3, "do": 3,
            "fri": 4, "friday": 4, "vr": 4,
            "sat": 5, "saturday": 5, "za": 5,
            "sun": 6, "sunday": 6, "zo": 6,
        }
        raw = self.options.text("auto_full_system_update_days", "mon,tue,wed,thu,fri,sat,sun")
        values = {aliases[token.strip().lower()] for token in raw.split(",") if token.strip().lower() in aliases}
        return values or set(range(7))

    def deterministic_jitter_minutes(self) -> int:
        maximum = self.options.integer("auto_full_system_update_jitter_minutes", 15, minimum=0, maximum=180)
        if maximum == 0:
            return 0
        digest = hashlib.sha256(self.host_name().encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big") % (maximum + 1)

    def next_scheduled_time(self, after: dt.datetime, *, strictly_future: bool = True) -> dt.datetime:
        zone = self.timezone()
        local_after = after.astimezone(zone)
        hour, minute = self.schedule_time()
        jitter = self.deterministic_jitter_minutes()
        weekdays = self.allowed_weekdays()
        for offset in range(0, 15):
            date = local_after.date() + dt.timedelta(days=offset)
            if date.weekday() not in weekdays:
                continue
            candidate = dt.datetime.combine(date, dt.time(hour, minute), tzinfo=zone) + dt.timedelta(minutes=jitter)
            if strictly_future and candidate <= local_after:
                continue
            return candidate.astimezone(dt.timezone.utc)
        raise RuntimeError("Kon binnen 14 dagen geen geldig updateslot bepalen")

    def initialize_schedule(self) -> None:
        if self.state.get("active"):
            return
        next_run = parse_iso(self.state.get("next_run"))
        if next_run is None:
            # An evening deployment schedules the next morning. If a Pi only
            # receives this installer after the planned time but still during
            # the configured morning catch-up window, run immediately so a
            # staggered fleet rollout still finishes that same morning.
            now = utc_now()
            local_now = now.astimezone(self.timezone())
            hour, minute = self.schedule_time()
            today_slot = dt.datetime.combine(
                local_now.date(), dt.time(hour, minute), tzinfo=self.timezone()
            ) + dt.timedelta(minutes=self.deterministic_jitter_minutes())
            catchup_raw = self.options.text("auto_full_system_update_first_start_catchup_until", "10:00")
            try:
                catch_hour, catch_minute = (int(x) for x in catchup_raw.split(":", 1))
                catchup = dt.datetime.combine(
                    local_now.date(), dt.time(catch_hour, catch_minute), tzinfo=self.timezone()
                )
            except (ValueError, TypeError):
                catchup = dt.datetime.combine(
                    local_now.date(), dt.time(10, 0), tzinfo=self.timezone()
                )
            if (
                self.options.bool("auto_full_system_update_run_if_missed", True)
                and today_slot <= local_now <= catchup
                and local_now.weekday() in self.allowed_weekdays()
            ):
                next_run = now + dt.timedelta(seconds=30)
            else:
                next_run = self.next_scheduled_time(now, strictly_future=True)
            self.state.update(
                {
                    "active": False,
                    "stage": "scheduled",
                    "scheduler_initialized_at": iso_now(),
                    "next_run": next_run.isoformat(timespec="seconds"),
                }
            )
            self.save()
        self.publish_status()

    # ---------- discovery and filtering ----------

    def self_addon_slug(self) -> str:
        try:
            data = unwrap(self.client.supervisor("GET", "/addons/self/info", timeout=30))
            if isinstance(data, dict):
                return str(data.get("slug") or "")
        except ApiError:
            pass
        return ""

    def discover_addons(self) -> list[UpdateItem]:
        data = unwrap(self.client.supervisor("GET", "/addons", timeout=60))
        raw = data.get("addons", []) if isinstance(data, dict) else []
        result: list[UpdateItem] = []
        for addon in raw if isinstance(raw, list) else []:
            if not isinstance(addon, dict):
                continue
            if not to_bool(addon.get("installed"), False) or not to_bool(addon.get("update_available"), False):
                continue
            slug = str(addon.get("slug") or "")
            if not slug:
                continue
            result.append(
                UpdateItem(
                    kind="addon",
                    item_id=slug,
                    name=str(addon.get("name") or slug),
                    installed=str(addon.get("version") or addon.get("installed") or ""),
                    latest=str(addon.get("version_latest") or ""),
                    release_url=str(addon.get("url") or ""),
                )
            )
        self_slug = self.self_addon_slug()
        # Update this updater last; if Supervisor replaces its container, all
        # other add-ons have already been processed and state is persisted.
        return sorted(result, key=lambda item: (item.item_id == self_slug, item.name.lower()))

    @staticmethod
    def entity_haystack(entity: dict[str, Any]) -> str:
        attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        fields = [
            entity.get("entity_id", ""),
            entity.get("state", ""),
            attrs.get("friendly_name", ""),
            attrs.get("title", ""),
            attrs.get("installed_version", ""),
            attrs.get("latest_version", ""),
            attrs.get("release_url", ""),
            attrs.get("entity_picture", ""),
            attrs.get("device_class", ""),
        ]
        return " ".join(str(x) for x in fields).lower()

    @classmethod
    def is_system_update_entity(cls, entity: dict[str, Any]) -> bool:
        entity_id = str(entity.get("entity_id") or "").lower()
        haystack = cls.entity_haystack(entity)
        exact_fragments = (
            "home_assistant_core",
            "home_assistant_supervisor",
            "home_assistant_operating_system",
            "home_assistant_os",
            "hass_os",
        )
        if any(fragment in entity_id for fragment in exact_fragments):
            return True
        return any(
            phrase in haystack
            for phrase in (
                "home assistant core update",
                "home assistant supervisor update",
                "home assistant operating system update",
            )
        )

    @classmethod
    def is_upstream_goodwe_entity(cls, entity: dict[str, Any]) -> bool:
        attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        # The exclusion is specifically for the original GoodWe software/HACS
        # repository. A physical inverter firmware update may also contain the
        # word GoodWe, but is a different update class and remains eligible when
        # device firmware updates are enabled.
        if str(attrs.get("device_class") or "").lower() == "firmware":
            return False
        haystack = cls.entity_haystack(entity)
        if "goodwe" not in haystack:
            return False
        if any(marker in haystack for marker in ("dwars", "dcent", "metdezon", "cryptowhizzard")):
            return False
        return True

    def is_explicitly_excluded(self, entity: dict[str, Any]) -> bool:
        raw = self.options.text("auto_full_system_update_exclude_entities", "")
        patterns = [x.strip().lower() for x in raw.split(",") if x.strip()]
        if not patterns:
            return False
        entity_id = str(entity.get("entity_id") or "").lower()
        haystack = self.entity_haystack(entity)
        return any(fnmatch.fnmatch(entity_id, pattern) or pattern in haystack for pattern in patterns)

    def discover_entity_updates(self) -> list[UpdateItem]:
        states = self.client.ha("GET", "/states", timeout=120)
        if not isinstance(states, list):
            return []
        include_device = self.options.bool("auto_full_system_update_include_device_updates", True)
        skip_goodwe = self.options.bool("skip_upstream_goodwe_updates", True)
        result: list[UpdateItem] = []
        for entity in states:
            if not isinstance(entity, dict):
                continue
            entity_id = str(entity.get("entity_id") or "")
            if not entity_id.startswith("update.") or entity.get("state") != "on":
                continue
            if self.is_system_update_entity(entity):
                continue
            if skip_goodwe and self.is_upstream_goodwe_entity(entity):
                log(f"{entity_id}: originele/upstream GoodWe-update wordt bewust overgeslagen.")
                continue
            if self.is_explicitly_excluded(entity):
                log(f"{entity_id}: uitgesloten via auto_full_system_update_exclude_entities.")
                continue
            attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
            if not include_device and str(attrs.get("device_class") or "").lower() == "firmware":
                continue
            result.append(
                UpdateItem(
                    kind="entity",
                    item_id=entity_id,
                    name=str(attrs.get("friendly_name") or attrs.get("title") or entity_id),
                    installed=str(attrs.get("installed_version") or ""),
                    latest=str(attrs.get("latest_version") or ""),
                    release_url=str(attrs.get("release_url") or ""),
                )
            )
        return sorted(result, key=lambda item: item.name.lower())

    def refresh_update_catalogs(self) -> list[str]:
        """Best-effort refresh of all update sources before inventory.

        The Supervisor/store metadata and Home Assistant ``update`` entities
        can otherwise still contain the previous polling cycle. Refreshing
        them immediately before the 04:00 run makes it much more likely that
        updates released shortly before the maintenance window are included.
        Failures are returned to preflight instead of aborting the full run.
        """
        if not self.options.bool("auto_full_system_update_refresh_catalogs", True):
            return []

        errors: list[str] = []
        try:
            self.client.supervisor("POST", "/store/reload", {}, timeout=180)
        except ApiError as exc:
            errors.append(f"add-on store: {compact_exception(exc)}")

        # /reload_updates is the dedicated current endpoint for OS, Core,
        # Supervisor and plug-in metadata. Older Supervisors may not expose it;
        # /supervisor/reload is retained as a compatibility fallback.
        try:
            self.client.supervisor("POST", "/reload_updates", {}, timeout=180)
        except ApiError as exc:
            if exc.status == 404:
                try:
                    self.client.supervisor("POST", "/supervisor/reload", {}, timeout=180)
                except ApiError as fallback_exc:
                    errors.append(
                        "Supervisor updatecatalogus: "
                        + compact_exception(fallback_exc)
                    )
            else:
                errors.append(f"Supervisor updatecatalogus: {compact_exception(exc)}")

        try:
            states = self.client.ha("GET", "/states", timeout=120)
            entity_ids = [
                str(entity.get("entity_id"))
                for entity in states
                if isinstance(entity, dict)
                and str(entity.get("entity_id") or "").startswith("update.")
            ] if isinstance(states, list) else []
            # Home Assistant accepts a list of entity IDs. Chunk the request so
            # a very large installation does not create an oversized payload.
            for offset in range(0, len(entity_ids), 100):
                chunk = entity_ids[offset:offset + 100]
                if chunk:
                    self.client.ha(
                        "POST",
                        "/services/homeassistant/update_entity",
                        {"entity_id": chunk},
                        timeout=180,
                    )
            if entity_ids:
                # Coordinators normally refresh asynchronously after the
                # service call. A brief pause gives them time to expose the new
                # state before discovery starts.
                time.sleep(5)
        except ApiError as exc:
            errors.append(f"Home Assistant update-entities: {compact_exception(exc)}")
        return errors

    # ---------- update primitives ----------

    def addon_needs_update(self, slug: str) -> tuple[bool, str]:
        try:
            data = unwrap(self.client.supervisor("GET", f"/addons/{slug}/info", timeout=45))
        except ApiError as exc:
            return True, compact_exception(exc)
        if not isinstance(data, dict):
            return True, "ongeldig add-on-info response"
        return to_bool(data.get("update_available"), False), str(data.get("version") or "")

    def update_addon(self, item: UpdateItem) -> tuple[bool, str]:
        needs_update, detail = self.addon_needs_update(item.item_id)
        if not needs_update:
            return True, f"reeds actueel ({detail})"
        timeout = self.options.integer("auto_full_system_update_item_timeout_sec", 1800, minimum=60, maximum=7200)
        try:
            self.client.supervisor(
                "POST",
                f"/store/addons/{item.item_id}/update",
                {"backup": False, "background": False},
                timeout=timeout,
            )
        except ApiError as exc:
            # Updating this very add-on can terminate the connection/container.
            # Only transport/gateway interruptions are ambiguous. An explicit
            # 4xx means Supervisor rejected the update and should fail fast.
            if not api_error_may_be_interrupted_update(exc):
                return False, compact_exception(exc)
            if not self.client.wait_supervisor(min(timeout, 600)):
                return False, compact_exception(exc)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            needs_update, version_or_error = self.addon_needs_update(item.item_id)
            if not needs_update:
                return True, f"geïnstalleerd {version_or_error}"
            time.sleep(10)
        return False, f"timeout na {timeout}s; laatste status: {version_or_error}"

    def entity_state(self, entity_id: str) -> dict[str, Any] | None:
        try:
            value = self.client.ha("GET", f"/states/{entity_id}", timeout=30)
            return value if isinstance(value, dict) else None
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def update_entity(self, item: UpdateItem) -> tuple[bool, str]:
        current = self.entity_state(item.item_id)
        if current is None or current.get("state") != "on":
            return True, "reeds actueel of entity verdwenen"
        timeout = self.options.integer("auto_full_system_update_item_timeout_sec", 1800, minimum=60, maximum=7200)
        try:
            # Deliberately no per-entity backup: one full pre-run backup is
            # created instead, matching the blueprint's current design.
            self.client.ha(
                "POST",
                "/services/update/install",
                {"entity_id": item.item_id},
                timeout=120,
            )
        except ApiError as exc:
            # A component-triggered Core restart can interrupt this request,
            # but explicit 4xx responses mean the service rejected it.
            if not api_error_may_be_interrupted_update(exc):
                return False, compact_exception(exc)
            log(f"{item.item_id}: install-call onderbroken; status wordt alsnog gecontroleerd ({compact_exception(exc)}).")
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            if not self.client.wait_ha(60, interval=5):
                last = "Home Assistant nog niet bereikbaar"
                continue
            try:
                current = self.entity_state(item.item_id)
            except ApiError as exc:
                last = compact_exception(exc)
                time.sleep(10)
                continue
            if current is None:
                return True, "entity verdwenen na update"
            attrs = current.get("attributes") if isinstance(current.get("attributes"), dict) else {}
            in_progress = attrs.get("in_progress")
            current_state = str(current.get("state") or "")
            last = (
                f"state={current_state} in_progress={in_progress} "
                f"installed={attrs.get('installed_version')} latest={attrs.get('latest_version')}"
            )
            if current_state == "off" and not to_bool(in_progress, False):
                return True, last
            # ``unavailable`` or ``unknown`` is not proof of a successful
            # update. Keep polling; if the integration does not recover, the
            # item enters the retry/failure path with the diagnostic above.
            time.sleep(10)
        return False, f"timeout na {timeout}s; {last}"

    def restart_core_and_wait(self) -> tuple[bool, str]:
        timeout = self.options.integer("auto_full_system_update_system_timeout_sec", 3600, minimum=300, maximum=14400)
        try:
            self.client.supervisor("POST", "/core/restart", {}, timeout=60)
        except ApiError as exc:
            if not api_error_may_be_interrupted_update(exc):
                return False, compact_exception(exc)
            log(f"Core restart-call werd onderbroken: {compact_exception(exc)}")
        if self.client.wait_ha(timeout, interval=10):
            return True, "Home Assistant is opnieuw beschikbaar"
        return False, f"Home Assistant niet terug binnen {timeout}s"

    def system_info(self, path: str) -> dict[str, Any]:
        value = unwrap(self.client.supervisor("GET", path, timeout=60))
        return value if isinstance(value, dict) else {}

    def update_system_component(
        self,
        *,
        label: str,
        info_path: str,
        update_path: str,
        payload: dict[str, Any] | None = None,
        wait_for_ha: bool = False,
    ) -> tuple[bool, bool, str]:
        """Return (success, updated, detail)."""
        before = self.system_info(info_path)
        if not to_bool(before.get("update_available"), False):
            return True, False, f"reeds actueel ({before.get('version', '')})"
        old_version = str(before.get("version") or "")
        target = str(before.get("version_latest") or "latest")
        timeout = self.options.integer("auto_full_system_update_system_timeout_sec", 3600, minimum=300, maximum=14400)
        try:
            self.client.supervisor("POST", update_path, payload or {}, timeout=timeout)
        except ApiError as exc:
            # Supervisor/Core replacement can close the connection. Only those
            # ambiguous transport/gateway failures are verified afterwards; an
            # explicit 4xx is a definitive rejection and fails immediately.
            if not api_error_may_be_interrupted_update(exc):
                return False, False, compact_exception(exc)
            log(f"{label}: update-call onderbroken; verificatie volgt ({compact_exception(exc)}).")
        if not self.client.wait_supervisor(timeout, interval=10):
            return False, True, f"Supervisor niet bereikbaar binnen {timeout}s"
        if wait_for_ha and not self.client.wait_ha(timeout, interval=10):
            return False, True, f"Home Assistant niet bereikbaar binnen {timeout}s"
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                after = self.system_info(info_path)
            except ApiError as exc:
                last = compact_exception(exc)
                time.sleep(10)
                continue
            new_version = str(after.get("version") or "")
            available = to_bool(after.get("update_available"), False)
            last = f"version={new_version}, latest={after.get('version_latest')}, update_available={available}"
            if new_version != old_version or not available:
                return True, True, f"{old_version or '?'} -> {new_version or target}; {last}"
            time.sleep(10)
        return False, True, f"timeout; {last}"

    # ---------- stage machine ----------

    def new_run(self, reason: str) -> None:
        local_date = utc_now().astimezone(self.timezone()).date().isoformat()
        self.state = {
            "state_version": STATE_VERSION,
            "active": True,
            "run_id": str(uuid.uuid4()),
            "run_reason": reason,
            "run_date": local_date,
            "stage": "preflight",
            "started_at": iso_now(),
            "updated_at": iso_now(),
            "next_run": None,
            "successes": [],
            "failures": [],
            "retry_queue": [],
            "log": [],
            "backup": {"requested": False, "success": None},
            "entity_updates_applied": False,
            "host_rebooted": False,
        }
        self.save()
        self.append_log(f"Update-run {self.state['run_id']} gestart ({reason}).")

    def preflight_system_updates(self) -> tuple[list[str], list[str]]:
        pending: list[str] = []
        errors: list[str] = []
        checks = (
            ("Home Assistant CLI", "/cli/info"),
            ("Home Assistant DNS", "/dns/info"),
            ("Home Assistant Audio", "/audio/info"),
            ("Home Assistant Multicast", "/multicast/info"),
            ("Home Assistant Observer", "/observer/info"),
            ("Supervisor", "/supervisor/info"),
            ("Home Assistant OS", "/os/info"),
            ("Home Assistant Core", "/core/info"),
        )
        for label, path in checks:
            try:
                if to_bool(self.system_info(path).get("update_available"), False):
                    pending.append(label)
            except ApiError as exc:
                if exc.status != 404:
                    errors.append(f"{label}: {compact_exception(exc)}")
        return pending, errors

    def run_preflight_stage(self) -> None:
        self.append_log("Voorcontrole: beschikbare add-on-, HACS-, systeem- en Core-updates inventariseren.")
        errors = self.refresh_update_catalogs()
        try:
            addons = self.discover_addons()
        except ApiError as exc:
            addons = []
            errors.append(f"add-ons: {compact_exception(exc)}")
        try:
            entities = self.discover_entity_updates()
        except ApiError as exc:
            entities = []
            errors.append(f"update-entities: {compact_exception(exc)}")
        systems, system_errors = self.preflight_system_updates()
        errors.extend(system_errors)
        self._save_queue("addon_queue", addons)
        self._save_queue("entity_queue", entities)
        self.state["preflight_pending"] = {
            "addons": [item.name for item in addons],
            "entities": [item.name for item in entities],
            "systems": systems,
            "errors": errors,
        }
        self.save()
        total = len(addons) + len(entities) + len(systems)
        if total == 0 and not errors:
            self.state["backup"] = {
                "requested": False,
                "success": None,
                "detail": "geen updates beschikbaar",
            }
            self.set_stage("verify", "Voorcontrole: alles is al actueel; er wordt geen onnodige backup gemaakt.")
            return
        self.set_stage(
            "backup",
            f"Voorcontrole vond {total} update(s) en {len(errors)} controlefout(en); pre-run backup volgt.",
        )

    def run_backup_stage(self) -> None:
        if not self.options.bool("auto_full_system_update_backup", True):
            self.state["backup"] = {"requested": False, "success": None, "detail": "uitgeschakeld"}
            self.set_stage("addons", "Volledige pre-run backup is uitgeschakeld.")
            return
        name = f"DWARS auto-update {utc_now().astimezone(self.timezone()).strftime('%Y-%m-%d %H:%M')}"
        self.state["backup"] = {"requested": True, "success": None, "name": name}
        self.save()
        self.append_log("Eén volledige Home Assistant-backup wordt gemaakt vóór alle updates.")
        timeout = self.options.integer("auto_full_system_update_system_timeout_sec", 3600, minimum=300, maximum=14400)
        try:
            response = unwrap(
                self.client.supervisor(
                    "POST",
                    "/backups/new/full",
                    {"name": name, "background": False},
                    timeout=timeout,
                )
            )
            slug = response.get("slug") if isinstance(response, dict) else None
            self.state["backup"] = {"requested": True, "success": True, "name": name, "slug": slug}
            self.add_success("full_backup", f"slug={slug or 'onbekend'}")
            self.prune_old_auto_update_backups(str(slug or ""))
            self.set_stage("addons", "Volledige backup is gereed; add-ons worden nu bijgewerkt.")
        except ApiError as exc:
            detail = compact_exception(exc)
            self.state["backup"] = {"requested": True, "success": False, "name": name, "detail": detail}
            self.add_failure("full_backup", detail)
            if self.options.bool("auto_full_system_update_abort_on_backup_failure", False):
                raise RuntimeError(f"Backup mislukt en abort_on_backup_failure staat aan: {detail}")
            self.set_stage("addons", "Backup mislukte; volgens configuratie gaat de updatecyclus toch verder.")

    def prune_old_auto_update_backups(self, current_slug: str) -> None:
        """Keep only the newest unprotected DWARS auto-update backups.

        Manual backups and protected backups are never touched. Retention is
        deliberately scoped to the exact name prefix used by this updater.
        """
        keep = self.options.integer(
            "auto_full_system_update_backup_keep", 3, minimum=1, maximum=30
        )
        try:
            response = unwrap(self.client.supervisor("GET", "/backups", timeout=120))
        except ApiError as exc:
            self.append_log(f"Backupretentie kon niet worden gecontroleerd: {compact_exception(exc)}.")
            return
        raw_backups = response.get("backups", []) if isinstance(response, dict) else []
        candidates: list[dict[str, Any]] = []
        for backup in raw_backups if isinstance(raw_backups, list) else []:
            if not isinstance(backup, dict):
                continue
            if not str(backup.get("name") or "").startswith("DWARS auto-update "):
                continue
            if to_bool(backup.get("protected"), False):
                continue
            slug = str(backup.get("slug") or "")
            if not slug:
                continue
            candidates.append(backup)

        # ISO timestamps sort lexicographically, but parse when possible so
        # malformed/missing dates simply sort oldest rather than crashing.
        def sort_key(backup: dict[str, Any]) -> dt.datetime:
            parsed = parse_iso(str(backup.get("date") or ""))
            return parsed or dt.datetime.min.replace(tzinfo=dt.timezone.utc)

        candidates.sort(key=sort_key, reverse=True)
        keep_slugs = {str(x.get("slug")) for x in candidates[:keep]}
        if current_slug:
            keep_slugs.add(current_slug)

        deleted = 0
        for backup in candidates:
            slug = str(backup.get("slug") or "")
            if slug in keep_slugs:
                continue
            try:
                quoted = urllib.parse.quote(slug, safe="")
                self.client.supervisor("DELETE", f"/backups/{quoted}", timeout=120)
                deleted += 1
            except ApiError as exc:
                self.append_log(
                    f"Oude DWARS-backup {slug} kon niet worden verwijderd: {compact_exception(exc)}."
                )
        if deleted:
            self.append_log(
                f"Backupretentie: {deleted} oude DWARS auto-update-backup(s) verwijderd; nieuwste {keep} bewaard."
            )

    def _load_queue(self, key: str) -> list[UpdateItem]:
        raw = self.state.get(key) or []
        return [UpdateItem.from_dict(x) for x in raw if isinstance(x, dict)]

    def _save_queue(self, key: str, items: Iterable[UpdateItem]) -> None:
        self.state[key] = [item.as_dict() for item in items]
        self.save()

    def queue_retry(self, item: UpdateItem, reason: str) -> None:
        queue = self.state.setdefault("retry_queue", [])
        if not isinstance(queue, list):
            queue = []
            self.state["retry_queue"] = queue
        if not any(x.get("kind") == item.kind and x.get("id") == item.item_id for x in queue if isinstance(x, dict)):
            value = item.as_dict()
            value.update({"attempt": 0, "last_error": reason[:1000]})
            queue.append(value)
        self.add_failure(item, reason, final=False)

    def run_addons_stage(self) -> None:
        if "addon_queue" not in self.state:
            queue = self.discover_addons()
            self._save_queue("addon_queue", queue)
            self.append_log(f"{len(queue)} geïnstalleerde add-onupdate(s) gevonden, inclusief Tailscale indien beschikbaar.")
        queue = self._load_queue("addon_queue")
        while queue:
            item = queue[0]
            self.state["current_item"] = item.as_dict()
            self.save()
            self.append_log(f"Add-on bijwerken: {item.name} ({item.installed or '?'} -> {item.latest or 'latest'}).")
            ok, detail = self.update_addon(item)
            queue.pop(0)
            self._save_queue("addon_queue", queue)
            self.state["current_item"] = None
            if ok:
                self.add_success(item, detail)
                self.append_log(f"Add-on gereed: {item.name}; {detail}.")
            else:
                self.queue_retry(item, detail)
                self.append_log(f"Add-onupdate wordt later opnieuw geprobeerd: {item.name}; {detail}.")
        self.set_stage("entities", "Add-onfase gereed; HACS, custom integrations en overige update-entities volgen.")

    def run_entities_stage(self) -> None:
        if "entity_queue" not in self.state:
            queue = self.discover_entity_updates()
            self._save_queue("entity_queue", queue)
            self.append_log(f"{len(queue)} Home Assistant update-entity/entities geselecteerd.")
        queue = self._load_queue("entity_queue")
        while queue:
            item = queue[0]
            self.state["current_item"] = item.as_dict()
            self.save()
            self.append_log(f"Update-entity installeren: {item.name} ({item.installed or '?'} -> {item.latest or 'latest'}).")
            try:
                ok, detail = self.update_entity(item)
            except ApiError as exc:
                ok, detail = False, compact_exception(exc)
            queue.pop(0)
            self._save_queue("entity_queue", queue)
            self.state["current_item"] = None
            if ok:
                self.state["entity_updates_applied"] = True
                self.add_success(item, detail)
                self.append_log(f"Update-entity gereed: {item.name}; {detail}.")
            else:
                self.queue_retry(item, detail)
                self.append_log(f"Update-entity wordt later opnieuw geprobeerd: {item.name}; {detail}.")
        self.set_stage("restart_after_entities", "Eerste HACS/update-entityfase gereed.")

    def run_restart_after_entities_stage(self) -> None:
        should_restart = self.state.get("entity_updates_applied") and self.options.bool(
            "auto_full_system_update_restart_core_after_entity_updates", True
        )
        if should_restart:
            self.append_log("Home Assistant Core wordt één keer herstart om bijgewerkte custom integrations te laden.")
            ok, detail = self.restart_core_and_wait()
            if ok:
                self.add_success("core_restart_after_entities", detail)
            else:
                self.add_failure("core_restart_after_entities", detail)
                self.append_log(f"Core-herstart kon niet worden bevestigd: {detail}.")
        self.set_stage("retry", "Retryfase voor mislukte add-on- en update-entityupdates gestart.")

    def run_retry_stage(self) -> None:
        raw_queue = self.state.get("retry_queue") or []
        max_retries = self.options.integer("auto_full_system_update_max_retries", 2, minimum=0, maximum=5)
        delay = self.options.integer("auto_full_system_update_retry_delay_sec", 120, minimum=5, maximum=1800)
        remaining: list[dict[str, Any]] = []
        for raw in raw_queue if isinstance(raw_queue, list) else []:
            if not isinstance(raw, dict):
                continue
            item = UpdateItem.from_dict(raw)
            attempt = to_int(raw.get("attempt"), 0) + 1
            if attempt > max_retries:
                self.add_failure(item, str(raw.get("last_error") or "maximum retries bereikt"), final=True)
                continue
            self.state["current_item"] = {**item.as_dict(), "retry": attempt}
            self.save()
            self.append_log(f"Retry {attempt}/{max_retries}: {item.name}.")
            time.sleep(delay)
            try:
                if item.kind == "addon":
                    ok, detail = self.update_addon(item)
                else:
                    ok, detail = self.update_entity(item)
            except ApiError as exc:
                ok, detail = False, compact_exception(exc)
            if ok:
                self.add_success(item, f"retry {attempt}: {detail}")
                self.append_log(f"Retry geslaagd: {item.name}; {detail}.")
            elif attempt < max_retries:
                value = item.as_dict()
                value.update({"attempt": attempt, "last_error": detail})
                remaining.append(value)
                self.append_log(f"Retry nog niet geslaagd: {item.name}; {detail}.")
            else:
                self.add_failure(item, f"na {attempt} retries: {detail}", final=True)
                self.append_log(f"Definitief mislukt na retries: {item.name}; {detail}.")
        self.state["retry_queue"] = remaining
        self.state["current_item"] = None
        self.save()
        if remaining:
            # Process remaining attempts during the same stage on the next loop.
            return
        self.set_stage("plugins", "Retryfase gereed; Supervisor-systeemplugins worden gecontroleerd.")

    def run_plugins_stage(self) -> None:
        if not self.options.bool("auto_full_system_update_supervisor_plugins", True):
            self.set_stage("supervisor", "Supervisor-systeemplugins zijn uitgeschakeld; Supervisor-update volgt.")
            return
        plugins = (
            ("Home Assistant CLI", "/cli/info", "/cli/update"),
            ("Home Assistant DNS", "/dns/info", "/dns/update"),
            ("Home Assistant Audio", "/audio/info", "/audio/update"),
            ("Home Assistant Multicast", "/multicast/info", "/multicast/update"),
            ("Home Assistant Observer", "/observer/info", "/observer/update"),
        )
        for label, info_path, update_path in plugins:
            try:
                ok, updated, detail = self.update_system_component(
                    label=label,
                    info_path=info_path,
                    update_path=update_path,
                )
            except ApiError as exc:
                # Not every deployment exposes every plugin. A 404 is treated
                # as unsupported rather than a failed fleet update.
                if exc.status == 404:
                    self.append_log(f"{label}: niet beschikbaar op dit installatietype; overgeslagen.")
                    continue
                ok, updated, detail = False, False, compact_exception(exc)
            if ok:
                self.add_success(label.lower().replace(" ", "_"), detail)
                self.append_log(f"{label}: {detail}.")
            else:
                self.add_failure(label.lower().replace(" ", "_"), detail)
                self.append_log(f"{label}-update mislukt: {detail}.")
        self.set_stage("supervisor", "Supervisor-systeemplugins gereed; Supervisor-update wordt gecontroleerd.")

    def run_supervisor_stage(self) -> None:
        try:
            ok, updated, detail = self.update_system_component(
                label="Supervisor",
                info_path="/supervisor/info",
                update_path="/supervisor/update",
            )
        except ApiError as exc:
            ok, updated, detail = False, False, compact_exception(exc)
        if ok:
            self.add_success("supervisor", detail)
            self.append_log(f"Supervisor: {detail}.")
        else:
            self.add_failure("supervisor", detail)
            self.append_log(f"Supervisor-update mislukt: {detail}.")
        self.set_stage("os", "Supervisorfase gereed; Home Assistant OS-update wordt gecontroleerd.")

    def current_boot_timestamp(self) -> int | None:
        info = self.host_info()
        value = info.get("boot_timestamp")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def run_os_stage(self) -> None:
        try:
            before = self.system_info("/os/info")
        except ApiError as exc:
            if exc.status == 404:
                self.add_success("home_assistant_os", "niet beschikbaar op dit installatietype; overgeslagen")
                self.set_stage(
                    "core",
                    "HA OS-beheer is niet beschikbaar op dit installatietype; Core wordt als laatste gecontroleerd.",
                )
                return
            self.add_failure("home_assistant_os", compact_exception(exc))
            self.set_stage("core", "HA OS-info kon niet worden gelezen; Core wordt als laatste gecontroleerd.")
            return
        if not to_bool(before.get("update_available"), False):
            self.add_success("home_assistant_os", f"reeds actueel ({before.get('version', '')})")
            self.set_stage("core", "Home Assistant OS is actueel; Core wordt als laatste gecontroleerd.")
            return
        old_version = str(before.get("version") or "")
        target = str(before.get("version_latest") or "latest")
        timeout = self.options.integer("auto_full_system_update_system_timeout_sec", 3600, minimum=300, maximum=14400)
        self.append_log(f"Home Assistant OS voorbereiden: {old_version or '?'} -> {target}.")
        call_error = ""
        try:
            self.client.supervisor("POST", "/os/update", {}, timeout=timeout)
        except ApiError as exc:
            # The OS is written to the inactive boot slot. A transport/gateway
            # interruption can therefore be harmless, but a 4xx means no OS
            # image was accepted and must never trigger a blind host reboot.
            call_error = compact_exception(exc)
            if not api_error_may_be_interrupted_update(exc):
                self.add_failure("home_assistant_os", call_error)
                self.set_stage(
                    "core",
                    "OS-update werd door Supervisor geweigerd; er wordt niet gereboot en Core volgt.",
                )
                return
            log(f"OS update-call onderbroken; post-reboot verificatie volgt ({call_error}).")
        if not self.client.wait_supervisor(timeout, interval=10):
            self.add_failure("home_assistant_os", f"Supervisor kwam niet terug na OS-update; {call_error}")
            self.set_stage("core", "OS-update kon niet worden voorbereid; Corefase volgt.")
            return
        try:
            after = self.system_info("/os/info")
        except ApiError:
            after = {}
        # Do not require .version to change before reboot: HAOS explicitly
        # activates the new slot only after /host/reboot. Persist target and
        # verify it after boot.
        self.state["os_version_before"] = old_version
        self.state["os_target_version"] = target
        self.state["os_pre_reboot_info"] = {
            "version": after.get("version"),
            "version_latest": after.get("version_latest"),
            "update_available": after.get("update_available"),
            "boot": after.get("boot"),
        }
        if not self.options.bool("auto_full_system_update_reboot", True):
            self.add_failure("host_reboot", "OS-update vereist reboot, maar auto_full_system_update_reboot=false")
            self.set_stage("core", "OS-update is voorbereid maar reboot is uitgeschakeld; Corefase volgt.")
            return
        self.state["stage"] = "os_wait_reboot"
        self.state["boot_timestamp_before"] = self.current_boot_timestamp()
        self.state["reboot_requested_at"] = iso_now()
        self.state["reboot_first_requested_at"] = self.state["reboot_requested_at"]
        self.state["current_item"] = {"kind": "system", "id": "host_reboot", "name": "Host reboot"}
        self.save()
        self.append_log("OS-update is voorbereid; verplichte hostreboot wordt nu aangevraagd. De run hervat automatisch na boot.")
        try:
            self.client.supervisor("POST", "/host/reboot", {}, timeout=30)
        except ApiError as exc:
            log(f"Reboot-call werd onderbroken (verwacht tijdens reboot): {compact_exception(exc)}")
        raise PauseRun("wachten op hostreboot")

    def run_os_wait_reboot_stage(self) -> None:
        before_boot = self.state.get("boot_timestamp_before")
        current_boot = self.current_boot_timestamp()
        if before_boot is not None and current_boot is not None and int(current_boot) != int(before_boot):
            self.state["host_rebooted"] = True
            self.state["current_item"] = None
            self.add_success("host_reboot", f"boot_timestamp {before_boot} -> {current_boot}")
            old_version = str(self.state.get("os_version_before") or "")
            target = str(self.state.get("os_target_version") or "latest")
            try:
                after = self.system_info("/os/info")
                new_version = str(after.get("version") or "")
                available = to_bool(after.get("update_available"), False)
                if new_version != old_version or not available:
                    self.add_success("home_assistant_os", f"{old_version or '?'} -> {new_version or target}")
                    message = f"Hostreboot en HAOS-update bevestigd ({old_version or '?'} -> {new_version or target}); Core volgt."
                else:
                    detail = f"na reboot nog versie={new_version}, latest={after.get('version_latest')}, update_available={available}"
                    self.add_failure("home_assistant_os", detail)
                    message = f"Hostreboot bevestigd, maar HAOS-update bleef beschikbaar ({detail}); Core volgt."
            except ApiError as exc:
                detail = compact_exception(exc)
                self.add_failure("home_assistant_os", f"post-reboot verificatie faalde: {detail}")
                message = "Hostreboot bevestigd; HAOS-versie kon niet worden geverifieerd. Core volgt."
            self.set_stage("core", message)
            return
        first_requested_at = parse_iso(self.state.get("reboot_first_requested_at"))
        reboot_timeout = self.options.integer(
            "auto_full_system_update_reboot_timeout_sec", 1800, minimum=300, maximum=14400
        )
        if first_requested_at and (utc_now() - first_requested_at).total_seconds() >= reboot_timeout:
            detail = f"hostreboot niet bevestigd binnen {reboot_timeout}s"
            self.add_failure("host_reboot", detail)
            self.set_stage("core", f"{detail}; Corefase volgt zodat de run niet permanent vastloopt.")
            return
        requested_at = parse_iso(self.state.get("reboot_requested_at"))
        if requested_at is None or (utc_now() - requested_at).total_seconds() >= 180:
            self.state["reboot_requested_at"] = iso_now()
            self.save()
            self.append_log("Host is nog niet opnieuw geboot; reboot-opdracht wordt opnieuw verstuurd.")
            try:
                self.client.supervisor("POST", "/host/reboot", {}, timeout=30)
            except ApiError:
                pass
        raise PauseRun("hostreboot nog niet bevestigd")

    def run_core_stage(self) -> None:
        if self.options.bool("auto_full_system_update_core_check", True):
            self.append_log("Home Assistant configuratiecontrole uitvoeren vóór Core-update.")
            try:
                self.client.supervisor("POST", "/core/check", {}, timeout=600)
                self.add_success("core_config_check", "geslaagd")
            except ApiError as exc:
                detail = compact_exception(exc)
                self.add_failure("core_config_check", detail)
                self.append_log(f"Core-update overgeslagen omdat configuratiecontrole faalde: {detail}.")
                self.set_stage("verify", "Corefase overgeslagen; eindcontrole gestart.")
                return
        try:
            ok, updated, detail = self.update_system_component(
                label="Home Assistant Core",
                info_path="/core/info",
                update_path="/core/update",
                payload={"backup": False},
                wait_for_ha=True,
            )
        except ApiError as exc:
            ok, updated, detail = False, False, compact_exception(exc)
        if ok:
            self.add_success("home_assistant_core", detail)
            self.append_log(f"Home Assistant Core: {detail}.")
        else:
            self.add_failure("home_assistant_core", detail)
            self.append_log(f"Home Assistant Core-update mislukt: {detail}.")
        self.set_stage("verify", "Corefase gereed; alle updatebronnen worden nogmaals gecontroleerd.")

    def pending_updates(self) -> list[str]:
        pending: list[str] = []
        try:
            pending.extend(f"add-on:{item.name}" for item in self.discover_addons())
        except ApiError as exc:
            pending.append(f"add-on-controle-fout:{compact_exception(exc)}")
        try:
            pending.extend(f"entity:{item.name}" for item in self.discover_entity_updates())
        except ApiError as exc:
            pending.append(f"entity-controle-fout:{compact_exception(exc)}")
        for label, path in (
            ("Supervisor", "/supervisor/info"),
            ("Home Assistant OS", "/os/info"),
            ("Home Assistant Core", "/core/info"),
        ):
            try:
                if to_bool(self.system_info(path).get("update_available"), False):
                    pending.append(label)
            except ApiError as exc:
                if label == "Home Assistant OS" and exc.status == 404:
                    continue
                pending.append(f"{label}-controle-fout:{compact_exception(exc)}")
        return pending

    def run_verify_stage(self) -> None:
        pending = self.pending_updates()
        self.state["pending_after_verify"] = pending
        final_failures = [f for f in (self.state.get("failures") or []) if f.get("final", True)]
        if final_failures:
            result = "completed_with_failures"
        elif pending:
            result = "completed_with_pending_updates"
        else:
            result = "completed"
        self.state.update(
            {
                "active": False,
                "stage": "complete",
                "last_result": result,
                "last_completed_at": iso_now(),
                "current_item": None,
            }
        )
        next_run = self.next_scheduled_time(utc_now() + dt.timedelta(minutes=1), strictly_future=True)
        self.state["next_run"] = next_run.isoformat(timespec="seconds")
        self.save()
        self.append_log(
            f"Update-run afgerond: resultaat={result}, geslaagd={len(self.state.get('successes') or [])}, "
            f"definitief_mislukt={len(final_failures)}, nog_beschikbaar={len(pending)}; "
            f"volgende run={self.state['next_run']}."
        )
        self.final_notification()
        self.publish_status()

    def execute_active_run(self) -> None:
        guard = 0
        while self.state.get("active"):
            guard += 1
            if guard > 10000:
                raise RuntimeError("Stage-machine guard bereikt")
            stage = str(self.state.get("stage") or "backup")
            if stage == "preflight":
                self.run_preflight_stage()
            elif stage == "backup":
                self.run_backup_stage()
            elif stage == "addons":
                self.run_addons_stage()
            elif stage == "entities":
                self.run_entities_stage()
            elif stage == "restart_after_entities":
                self.run_restart_after_entities_stage()
            elif stage == "retry":
                self.run_retry_stage()
            elif stage == "plugins":
                self.run_plugins_stage()
            elif stage == "supervisor":
                self.run_supervisor_stage()
            elif stage == "os":
                self.run_os_stage()
            elif stage == "os_wait_reboot":
                self.run_os_wait_reboot_stage()
            elif stage == "core":
                self.run_core_stage()
            elif stage == "verify":
                self.run_verify_stage()
            else:
                raise RuntimeError(f"Kan actieve run niet hervatten vanaf stage={stage!r}")

    @contextlib.contextmanager
    def maintenance_lock(self, *, wait_seconds: int = 5):
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            deadline = time.monotonic() + wait_seconds
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("onderhoudslock is bezet")
                    time.sleep(1)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def run_once(self, reason: str = "scheduled") -> bool:
        try:
            with self.maintenance_lock(wait_seconds=10):
                self.state = self.state_store.load()
                if not self.state.get("active"):
                    self.new_run(reason)
                self.execute_active_run()
                return True
        except PauseRun as exc:
            log(f"Update-run gepauzeerd: {exc}")
            return False
        except TimeoutError as exc:
            log(f"Update-run uitgesteld: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001 - keep daemon alive and persist diagnostics
            detail = compact_exception(exc)
            log(f"Onverwachte updatefout: {detail}")
            traceback.print_exc()
            self.state = self.state_store.load()
            self.state.update(
                {
                    "active": False,
                    "stage": "failed",
                    "last_result": "failed",
                    "last_completed_at": iso_now(),
                    "fatal_error": detail,
                }
            )
            try:
                self.state["next_run"] = self.next_scheduled_time(
                    utc_now() + dt.timedelta(minutes=1), strictly_future=True
                ).isoformat(timespec="seconds")
            except Exception:
                self.state["next_run"] = (utc_now() + dt.timedelta(days=1)).isoformat(timespec="seconds")
            self.save()
            self.final_notification()
            self.publish_status()
            return False

    def daemon(self) -> None:
        self.initialize_schedule()
        poll = self.options.integer("auto_full_system_update_scheduler_poll_sec", 60, minimum=15, maximum=900)
        log(
            f"Daemon actief; versie={VERSION}, tijd={self.options.text('auto_full_system_update_time', '04:00')}, "
            f"timezone={self.state.get('timezone', 'auto')}, jitter={self.deterministic_jitter_minutes()} min, "
            f"next_run={self.state.get('next_run')}."
        )
        while True:
            self.options = Options(str(self.options.path))
            self.state = self.state_store.load()
            if not self.options.bool("auto_full_system_update", True):
                self.state.update({"active": False, "stage": "scheduled", "last_result": "disabled"})
                self.save()
                self.publish_status()
                time.sleep(poll)
                continue
            if self.state.get("active"):
                self.run_once("resume")
                time.sleep(poll)
                continue
            next_run = parse_iso(self.state.get("next_run"))
            if next_run is None:
                self.initialize_schedule()
                next_run = parse_iso(self.state.get("next_run"))
            if next_run and utc_now() >= next_run:
                if self.options.bool("auto_full_system_update_run_if_missed", True):
                    self.run_once("scheduled_or_missed")
                else:
                    # When missed runs are disabled, only accept a 10-minute
                    # start window and otherwise schedule the next day.
                    lateness = (utc_now() - next_run).total_seconds()
                    if lateness <= 600:
                        self.run_once("scheduled")
                    else:
                        self.state["next_run"] = self.next_scheduled_time(
                            utc_now() + dt.timedelta(minutes=1), strictly_future=True
                        ).isoformat(timespec="seconds")
                        self.save()
                        self.publish_status()
            time.sleep(poll)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DWARS Home Assistant automatic updater")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--daemon", action="store_true", help="run the persistent scheduler")
    group.add_argument("--run-now", action="store_true", help="start or resume a run immediately")
    group.add_argument("--show-state", action="store_true", help="print persisted state")
    parser.add_argument("--options", default=DEFAULT_OPTIONS_PATH)
    parser.add_argument("--state", default=DEFAULT_STATE_PATH)
    parser.add_argument("--lock", default=DEFAULT_LOCK_PATH)
    parser.add_argument("--supervisor-url", default=os.environ.get("SUPERVISOR_API", DEFAULT_SUPERVISOR_URL))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    state_store = JsonState(args.state)
    if args.show_state:
        print(json.dumps(state_store.load(), indent=2, sort_keys=True))
        return 0
    token, token_source = resolve_supervisor_token()
    if not token:
        if args.daemon:
            token, token_source = wait_for_supervisor_token(60)
        else:
            print(
                "[DWARS AutoUpdater] Supervisor API-token ontbreekt: controleer hassio_api/homeassistant_api of gebruik een compatibele HASSIO_TOKEN-omgeving.",
                file=sys.stderr,
            )
            return 69
    print(f"[DWARS AutoUpdater] Supervisor API-auth via {token_source}; token wordt niet gelogd.", flush=True)
    client = ApiClient(token, args.supervisor_url)
    updater = AutoUpdater(Options(args.options), state_store, client, args.lock)
    if args.run_now:
        return 0 if updater.run_once("manual") else 1
    updater.daemon()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
