"""Bounded Core start recovery, independent of the Home Assistant frontend.

Use /core/start, never /core/stop, forced restarts or reboot as a recovery action.
/core/info is version/configuration metadata, NOT a running-state endpoint.
A failed health request alone is never evidence that the container is stopped.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable


def core_config_ready(value: Any) -> bool:
    """Require a real Core config response, and RUNNING where state is supplied."""
    if not isinstance(value, dict) or not isinstance(value.get("version"), str):
        return False
    if not value["version"].strip():
        return False
    state = value.get("state")
    return state is None or str(state).casefold() == "running"


def data_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        return value["data"]
    return value if isinstance(value, dict) else {}


class CoreRecovery:
    def __init__(self, options: Any, client: Any, path: Path, log: Callable[[str], None]) -> None:
        self.options = options
        self.client = client
        self.path = Path(path)
        self.log = log
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            self.state = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            self.state = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        # Open with restrictive permissions from creation, not only after write.
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self.state, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def arm(self, context: str) -> None:
        """Persist BEFORE an operation; never reset retries of an unfinished recovery."""
        if not self.state.get("pending"):
            self.state = {
                "pending": True, "context": context, "started_at": time.time(),
                "attempts": 0, "last_attempt_at": None, "status": "waiting",
            }
            self.save()

    def record(self, status: str, detail: str) -> tuple[bool, str]:
        changed = self.state.get("status") != status or self.state.get("detail") != detail
        self.state.update({"status": status, "detail": detail, "checked_at": time.time()})
        if status == "ready":
            self.state.update({"pending": False, "verified_at": time.time()})
        self.save()
        if changed:
            self.log(f"Core-herstel: {detail}")
        return status == "ready", detail

    def enforce_boot_policy(self) -> None:
        """Fleet policy is explicit and separately switchable for maintenance."""
        if not self.options.bool("auto_full_system_update_core_autostart", True):
            return
        try:
            info = data_dict(self.client.supervisor("GET", "/core/info", timeout=15))
            changes = {key: True for key in ("boot", "watchdog") if info.get(key) is False}
            if changes:
                self.client.supervisor("POST", "/core/options", changes, timeout=30)
                self.log("Core: automatisch starten bij boot/watchdog ingeschakeld.")
        except RuntimeError:
            self.log("Core: boot/watchdog-instellingen konden niet worden gecontroleerd; geen succes aangenomen.")

    def healthy(self) -> bool:
        try:
            return core_config_ready(self.client.ha("GET", "/config", timeout=15))
        except RuntimeError:
            return False

    @staticmethod
    def _job_blocks_start(jobs: list[Any]) -> bool:
        for job in jobs:
            if not isinstance(job, dict):
                # A malformed job cannot establish that starting Core is safe.
                return True
            if job.get("done") is not True:
                name = str(job.get("name", "")).casefold()
                reference = str(job.get("reference", "")).casefold()
                if (
                    not name
                    or name.startswith(("home_assistant_", "homeassistant_", "backup_", "os_", "supervisor_", "host_"))
                    or reference in {"homeassistant", "hassio_supervisor"}
                ):
                    return True
                if CoreRecovery._job_blocks_start(job.get("child_jobs") or []):
                    return True
        return False

    def check_once(self) -> tuple[bool, str]:
        """One health/recovery iteration. Called under the shared maintenance lock."""
        try:
            config = self.client.ha("GET", "/config", timeout=15)
            if core_config_ready(config):
                return self.record("ready", "Home Assistant Core-API is beschikbaar (RUNNING).")
        except RuntimeError as exc:
            if getattr(exc, "status", None) in {401, 403}:
                return self.record("authentication_error", "Core-API weigert authenticatie; geen start/restart uitgevoerd.")

        if not self.options.bool("auto_full_system_update_core_recovery", True):
            return self.record("disabled", "Core niet gereed; automatisch Core-herstel is uitgeschakeld.")

        try:
            info = data_dict(self.client.supervisor("GET", "/core/info", timeout=15))
            if info.get("boot") is False:
                return self.record("autostart_disabled", "Core boot=false; respecteer uitschakeling, geen start uitgevoerd.")
            supervisor = data_dict(self.client.supervisor("GET", "/supervisor/info", timeout=15))
            if str(supervisor.get("state", "running")).casefold() != "running":
                return self.record("busy", "Supervisor is nog aan het starten of afsluiten; wachten.")
            job_info = data_dict(self.client.supervisor("GET", "/jobs/info", timeout=15))
            jobs = job_info.get("jobs")
            if not isinstance(jobs, list):
                return self.record("unknown", "Supervisor jobstatus onbekend; geen start uitgevoerd.")
            if self._job_blocks_start(jobs):
                return self.record("busy", "Core-/backup-/systeemtaak is nog actief; niet ingrijpen.")
        except RuntimeError as exc:
            if getattr(exc, "status", None) in {401, 403}:
                return self.record("authentication_error", "Supervisor weigert authenticatie; geen start uitgevoerd.")
            return self.record("unknown", "Supervisor of jobstatus onbereikbaar; geen start uitgevoerd.")

        try:
            stats = data_dict(self.client.supervisor("GET", "/core/stats", timeout=15))
            if "memory_usage" in stats or "cpu_percent" in stats:
                return self.record("starting", "Core-container draait; wachten op API/opstart/migratie, geen restart.")
            return self.record("unknown", "Core-containerstatus onbekend; geen start uitgevoerd.")
        except RuntimeError as exc:
            # Match the Supervisor's explicit not-running error, not a generic
            # HTTP 500/502/404, stats timeout, connection failure or auth error.
            message = (str(exc) + " " + str(getattr(exc, "body", ""))).casefold()
            normalized = "".join(c for c in message if c.isalnum())
            stopped = "homeassistant" in normalized and "notrunning" in normalized
            if getattr(exc, "status", None) in {401, 403} or not stopped:
                return self.record("unknown", "Geen bevestiging dat Core gestopt is; geen start uitgevoerd.")

        now = time.time()
        grace = self.options.integer("auto_full_system_update_core_start_grace_sec", 120, minimum=30, maximum=900)
        if now - float(self.state.get("started_at") or now) < grace:
            return self.record("grace", "Core is gestopt; korte opstartmarge voordat start wordt aangevraagd.")
        limit = self.options.integer("auto_full_system_update_core_start_max_attempts", 3, minimum=1, maximum=5)
        attempts = int(self.state.get("attempts") or 0)
        if attempts >= limit:
            return self.record("exhausted", f"Core blijft gestopt na {limit} startpogingen; handmatige controle nodig.")
        last = self.state.get("last_attempt_at")
        if last is not None and now - float(last) < 120:
            return self.record("cooldown", "Vorige startpoging afwachten; geen herstartlus.")
        self.state.update({"attempts": attempts + 1, "last_attempt_at": now})
        self.save()  # survive an add-on or host restart during this request
        self.log(f"Core is gestopt: POST /core/start (poging {attempts + 1}/{limit}).")
        try:
            self.client.supervisor("POST", "/core/start", {}, timeout=60)
        except RuntimeError as exc:
            if getattr(exc, "status", None) in {401, 403}:
                return self.record("authentication_error", "Core-start geweigerd: authenticatie/rechten controleren.")
            # A timeout is not a failed start, but is certainly not proof of
            # health either. The next iteration rechecks jobs/stats/API.
            return self.record("start_unconfirmed", "Core-start nog niet bevestigd; status wordt opnieuw gecontroleerd.")
        return self.record("start_requested", "Core-start aangevraagd; wachten tot de Core-API RUNNING bevestigt.")

    def ensure(self, context: str, timeout: int = 900) -> tuple[bool, str]:
        self.arm(context)
        deadline = time.monotonic() + max(0, timeout)
        while True:
            ok, detail = self.check_once()
            if ok:
                return True, detail
            if self.state.get("status") in {"authentication_error", "disabled", "autostart_disabled", "exhausted"}:
                return False, detail
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, f"Core niet bevestigd binnen {timeout}s: {detail}"
            time.sleep(min(10, remaining))
