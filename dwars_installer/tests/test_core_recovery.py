"""Regression cases for the stopped Core reported after the 0.5.2 fleet run.

All device/Supervisor actions are simulated; no production installation is used.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import test_auto_updater as base

FakeClient = base.FakeClient
mod = base.mod


class CoreClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.running = False
        self.runtime_state = "RUNNING"
        self.api_error = None
        self.stats_error = None
        self.start_works = True
        self.start_error = None
        self.update_error = None
        self.check_error = None
        self.jobs = []
        self.jobs_error = None
        self.offline_migration = False
        self.safe_mode = False
        self.state_404 = False
        self.config_invalid = False
        self.system_infos["/core/info"].update({"boot": True, "watchdog": True})
        self.os_info.update({"version": "16", "version_latest": "16", "update_available": False})

    def supervisor(self, method, path, data=None, timeout=60):
        if path == "/jobs/info":
            self.calls.append(("supervisor", method, path, data))
            if self.jobs_error:
                raise self.jobs_error
            return {"result": "ok", "data": {"jobs": self.jobs}}
        if path == "/core/stats":
            self.calls.append(("supervisor", method, path, data))
            if self.stats_error:
                raise self.stats_error
            if not self.running:
                raise mod.ApiError("Home Assistant Core is not running", status=400)
            return {"data": {"cpu_percent": 1.0}}
        if method == "POST" and path == "/core/start":
            self.calls.append(("supervisor", method, path, data))
            if self.start_works:
                self.running = True
            if self.start_error:
                raise self.start_error
            return {"result": "ok", "data": {}}
        if method == "POST" and path == "/core/options":
            self.calls.append(("supervisor", method, path, data))
            self.system_infos["/core/info"].update(data)
            return {"result": "ok", "data": {}}
        if method == "POST" and path == "/core/check":
            self.calls.append(("supervisor", method, path, data))
            if self.check_error:
                raise self.check_error
            if not self.running:
                raise mod.ApiError("Home Assistant Core is not running", status=400)
            return {"result": "ok", "data": {}}
        if method == "POST" and path == "/core/restart":
            self.calls.append(("supervisor", method, path, data))
            self.running = False  # Simulate a restart that stopped but didn't start.
            return {"result": "ok", "data": {}}
        if method == "POST" and path == "/core/update":
            response = super().supervisor(method, path, data, timeout)
            self.running = False  # Version is current; its container is STILL stopped.
            if self.update_error:
                raise self.update_error
            return response
        return super().supervisor(method, path, data, timeout)

    def ha(self, method, path, data=None, timeout=60):
        if path in {"/config", "/core/state"}:
            self.calls.append(("ha", method, path, data))
            if self.api_error:
                raise self.api_error
            if path == "/core/state" and self.state_404:
                raise mod.ApiError("not found", status=404)
            if not self.running:
                raise mod.ApiError("Core proxy unavailable", status=503)
            if path == "/core/state":
                return {"state": self.runtime_state, "recorder_state": {
                    "migration_in_progress": self.offline_migration, "migration_is_live": False,
                }}
            if self.config_invalid:
                return {"raw": "<html>proxy error</html>"}
            return {"version": "2026.9.0", "components": ["api", "frontend"], "safe_mode": self.safe_mode}
        return super().ha(method, path, data, timeout)


# Use the existing fixture, but do not inherit its 21 test methods a second time.
class CoreRecoveryTests(unittest.TestCase):
    setUp = base.UpdaterTestCase.setUp
    tearDown = base.UpdaterTestCase.tearDown
    write_options = base.UpdaterTestCase.write_options
    updater = base.UpdaterTestCase.updater

    def setUp(self):
        base.UpdaterTestCase.setUp(self)
        self.client = CoreClient()
        self.seconds = 0.0
        self.on_sleep = lambda: None
        self.monotonic_patch = mock.patch.object(mod.time, "monotonic", side_effect=lambda: self.seconds)
        self.sleep_patch = mock.patch.object(mod.time, "sleep", side_effect=self.sleep)
        self.utc_patch = mock.patch.object(mod, "utc_now", side_effect=lambda: dt.datetime(2026, 9, 7, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=self.seconds))
        for patch in (self.monotonic_patch, self.sleep_patch, self.utc_patch):
            patch.start()
            self.addCleanup(patch.stop)

    def sleep(self, seconds):
        self.seconds += seconds
        self.on_sleep()

    def starts(self):
        return [c for c in self.client.calls if c[1:3] == ("POST", "/core/start")]

    def update_core(self, updater):
        return updater.update_system_component(label="Core", info_path="/core/info", update_path="/core/update", wait_for_ha=True)

    def test_current_version_but_stopped_is_started(self):
        ok, updated, detail = self.update_core(self.updater())
        self.assertTrue(ok)
        self.assertFalse(updated)
        self.assertEqual(len(self.starts()), 1)
        self.assertIn("RUNNING", detail)

    def test_updated_version_but_stopped_is_started(self):
        self.client.running = True
        self.client.system_infos["/core/info"].update(version_latest="2", update_available=True)
        ok, updated, _ = self.update_core(self.updater())
        self.assertTrue(ok and updated)
        self.assertEqual(len(self.starts()), 1)
        self.assertTrue(self.client.running)

    def test_core_already_healthy_is_never_restarted_or_started(self):
        self.client.running = True
        ok, _ = self.updater().ensure_core_running()
        self.assertTrue(ok)
        self.assertFalse(self.starts())
        self.assertFalse([c for c in self.client.calls if c[1] == "POST"])

    def test_broken_restart_recovers_with_start(self):
        self.client.running = True
        ok, _ = self.updater().restart_core_and_wait()
        self.assertTrue(ok)
        posts = [c[2] for c in self.client.calls if c[1] == "POST"]
        self.assertEqual(posts, ["/core/check", "/core/restart", "/core/start"])

    def test_restart_of_stopped_core_uses_start_not_restart(self):
        ok, _ = self.updater().restart_core_and_wait()
        self.assertTrue(ok)
        self.assertEqual(len(self.starts()), 1)
        self.assertNotIn("/core/restart", [c[2] for c in self.client.calls])

    def test_start_transport_timeout_still_verifies_success(self):
        self.client.start_error = mod.ApiError("connection dropped")
        ok, _ = self.updater().ensure_core_running()
        self.assertTrue(ok)
        self.assertEqual(len(self.starts()), 1)

    def test_update_disconnect_still_recovers_and_checks_version(self):
        self.client.running = True
        self.client.system_infos["/core/info"].update(version_latest="2", update_available=True)
        self.client.update_error = mod.ApiError("Bad gateway", status=502)
        ok, updated, _ = self.update_core(self.updater())
        self.assertTrue(ok and updated)
        self.assertEqual(len(self.starts()), 1)

    def test_auth_failure_never_triggers_start(self):
        self.client.api_error = mod.ApiError("Unauthorized", status=401)
        ok, detail = self.updater().ensure_core_running()
        self.assertFalse(ok)
        self.assertIn("Unauthorized", detail)
        self.assertFalse(self.starts())

    def test_jobs_permission_failure_never_triggers_start(self):
        self.client.jobs_error = mod.ApiError("Forbidden", status=403)
        ok, _ = self.updater().ensure_core_running()
        self.assertFalse(ok)
        self.assertFalse(self.starts())

    def test_backup_restore_job_blocks_start_until_done(self):
        self.client.jobs = [{"name": "backup_manager_restore_full", "done": False, "child_jobs": []}]
        def complete_job():
            self.assertFalse(self.starts())
            self.client.jobs[0]["done"] = True
            self.on_sleep = lambda: None
        self.on_sleep = complete_job
        ok, _ = self.updater().ensure_core_running()
        self.assertTrue(ok)
        self.assertEqual(len(self.starts()), 1)

    def test_nested_migration_job_blocks_start(self):
        self.client.jobs = [{"name": "parent", "done": True, "child_jobs": [{"name": "home_assistant_core_update", "done": False}]}]
        ok, _ = self.updater(auto_core_recovery_timeout_sec=60).ensure_core_running()
        self.assertFalse(ok)
        self.assertFalse(self.starts())

    def test_offline_database_migration_never_gets_restarted(self):
        self.client.running = True
        self.client.offline_migration = True
        ok, detail = self.updater(auto_core_recovery_timeout_sec=60).ensure_core_running()
        self.assertFalse(ok)
        self.assertIn("databasemigratie", detail)
        self.assertFalse(self.starts())
        self.assertNotIn("/core/restart", [c[2] for c in self.client.calls])

    def test_starting_container_waits_until_running(self):
        self.client.running = True
        self.client.runtime_state = "STARTING"
        self.on_sleep = lambda: setattr(self.client, "runtime_state", "RUNNING")
        ok, _ = self.updater().ensure_core_running()
        self.assertTrue(ok)
        self.assertFalse(self.starts())

    def test_stats_running_with_bad_gateway_does_not_restart(self):
        self.client.running = True
        self.client.api_error = mod.ApiError("Gateway timeout", status=504)
        ok, _ = self.updater(auto_core_recovery_timeout_sec=60).ensure_core_running()
        self.assertFalse(ok)
        self.assertFalse(self.starts())

    def test_arbitrary_stats_error_not_treated_as_stopped(self):
        self.client.stats_error = mod.ApiError("Docker statistics timeout", status=500)
        ok, _ = self.updater(auto_core_recovery_timeout_sec=60).ensure_core_running()
        self.assertFalse(ok)
        self.assertFalse(self.starts())

    def test_start_attempt_limit_survives_process_restart(self):
        self.client.start_works = False
        first = self.updater(auto_core_recovery_timeout_sec=900)
        ok, detail = first.ensure_core_running()
        self.assertFalse(ok)
        self.assertIn("3 startpogingen", detail)
        self.assertEqual(len(self.starts()), 3)
        second = self.updater(auto_core_recovery_timeout_sec=900)
        self.assertEqual(second.state["core_recovery"]["attempts"], 3)
        second.ensure_core_running()
        self.assertEqual(len(self.starts()), 3)

    def test_recovery_disabled_does_not_start_or_change_boot(self):
        self.client.system_infos["/core/info"].update(boot=False, watchdog=False)
        ok, _ = self.updater(auto_core_recovery_enabled=False).ensure_core_running()
        self.assertFalse(ok)
        self.assertFalse([c for c in self.client.calls if c[1] == "POST"])

    def test_boot_and_watchdog_only_change_disabled_flags(self):
        self.client.running = True
        self.client.system_infos["/core/info"].update(boot=False, watchdog=False, ssl=True, port=8123)
        self.updater().ensure_core_running()
        writes = [c[3] for c in self.client.calls if c[2] == "/core/options"]
        self.assertEqual(writes, [{"boot": True, "watchdog": True}])
        self.assertFalse(self.starts())

    def test_boot_management_can_be_disabled(self):
        self.client.running = True
        self.client.system_infos["/core/info"].update(boot=False, watchdog=False)
        self.updater(auto_core_boot_watchdog=False).ensure_core_running()
        self.assertNotIn("/core/options", [c[2] for c in self.client.calls])

    def test_verify_must_not_complete_with_core_unavailable(self):
        self.client.start_works = False
        updater = self.updater(auto_core_recovery_timeout_sec=60)
        updater.state.update(active=True, stage="verify")
        with self.assertRaises(mod.PauseRun):
            updater.run_verify_stage()
        self.assertTrue(updater.state["active"])
        self.assertEqual(updater.state["stage"], "core_recovery")
        self.assertNotIn("last_completed_at", updater.state)

    def test_recovery_resume_continues_verify_without_redoing_updates(self):
        self.client.running = True
        updater = self.updater()
        updater.state.update(active=True, stage="core_recovery", core_recovery_resume_stage="verify")
        updater.execute_active_run()
        self.assertEqual(updater.state["last_result"], "completed")
        self.assertFalse([c for c in self.client.calls if c[2].endswith("/update")])

    def test_startup_repairs_completed_052_run(self):
        updater = self.updater()
        updater.state.update(active=False, stage="complete", last_result="completed_with_pending_updates")
        updater.save()
        updater.startup_core_check()
        self.assertEqual(len(self.starts()), 1)
        self.assertTrue(self.client.running)
        self.assertEqual(updater.state["last_result"], "completed_with_pending_updates")

    def test_startup_does_not_race_pending_reboot(self):
        updater = self.updater()
        updater.state.update(active=True, stage="os_wait_reboot")
        updater.save()
        updater.startup_core_check()
        self.assertFalse(self.client.calls)

    def test_after_confirmed_reboot_core_starts_before_config_check(self):
        updater = self.updater()
        updater.state.update(active=True, stage="os_wait_reboot", boot_timestamp_before=100, os_target_version="16")
        self.client.boot_timestamp = 200
        updater.run_os_wait_reboot_stage()
        self.assertEqual(updater.state["stage"], "core")
        updater.run_core_stage()
        posts = [c[2] for c in self.client.calls if c[1] == "POST"]
        self.assertLess(posts.index("/core/start"), posts.index("/core/check"))
        self.assertNotIn("/core/update", posts)
        self.assertEqual(updater.state["stage"], "verify")

    def test_failed_config_check_leaves_running_core_alone(self):
        self.client.running = True
        self.client.check_error = mod.ApiError("Invalid configuration", status=400)
        ok, _ = self.updater().restart_core_and_wait()
        self.assertFalse(ok)
        self.assertTrue(self.client.running)
        self.assertNotIn("/core/restart", [c[2] for c in self.client.calls])

    def test_full_run_recovers_after_backup_leaves_core_stopped(self):
        self.client.running = True
        self.client.system_infos["/core/info"].update(version_latest="2", update_available=True)
        original = self.client.supervisor
        def leave_stopped(method, path, data=None, timeout=60):
            result = original(method, path, data, timeout)
            if path == "/backups/new/full":
                self.client.running = False
            return result
        self.client.supervisor = leave_stopped
        updater = self.updater()
        self.assertTrue(updater.run_once("backup-stopped-regression"))
        self.assertTrue(self.client.running)
        self.assertEqual(updater.state["last_result"], "completed")
        self.assertEqual(len(self.starts()), 2)  # after backup + after simulated Core update

    def test_failed_run_still_attempts_core_recovery(self):
        updater = self.updater()
        with mock.patch.object(updater, "execute_active_run", side_effect=RuntimeError("simulated stage failure")):
            self.assertFalse(updater.run_once("failed-run"))
        self.assertTrue(self.client.running)
        self.assertEqual(updater.state["last_result"], "failed")
        self.assertEqual(len(self.starts()), 1)

    def test_old_core_state_endpoint_falls_back_to_valid_config(self):
        self.client.running = True
        self.client.state_404 = True
        self.assertTrue(self.updater().ensure_core_running()[0])

    def test_http_200_html_is_not_ready(self):
        self.client.running = True
        self.client.config_invalid = True
        self.assertFalse(self.updater(auto_core_recovery_timeout_sec=60).ensure_core_running()[0])

    def test_safe_mode_is_not_full_recovery(self):
        self.client.running = True
        self.client.safe_mode = True
        self.assertFalse(self.updater(auto_core_recovery_timeout_sec=60).ensure_core_running()[0])
        self.assertFalse(self.starts())

    def test_api_200_error_envelope_rejected(self):
        client = mod.ApiClient("test-only-token")
        with mock.patch.object(client, "_request", return_value={"result": "error", "message": "start rejected"}):
            with self.assertRaises(mod.ApiError):
                client.supervisor("POST", "/core/start", {})

    def test_hassio_legacy_token_fallback_preserved(self):
        with mock.patch.dict(mod.os.environ, {"HASSIO_TOKEN": "legacy-test-only"}, clear=True):
            token, source = mod.resolve_supervisor_token()
        self.assertEqual(token, "legacy-test-only")
        self.assertIn("HASSIO_TOKEN", source)

    def test_real_http_client_with_simulated_supervisor(self):
        model = self.client
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def handle_request(self):
                if self.headers.get("Authorization") != "Bearer test-only-token":
                    self.send_error(401)
                    return
                body = None
                if self.command == "POST":
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                try:
                    if self.path.startswith("/core/api"):
                        response = model.ha(self.command, self.path[len("/core/api"):], body)
                    else:
                        response = model.supervisor(self.command, self.path, body)
                    payload, status = json.dumps(response).encode(), 200
                except mod.ApiError as exc:
                    payload, status = json.dumps({"result": "error", "message": str(exc)}).encode(), exc.status or 500
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            do_GET = handle_request
            do_POST = handle_request
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            updater = self.updater()
            updater.client = mod.ApiClient("test-only-token", f"http://127.0.0.1:{server.server_port}")
            ok, _ = updater.ensure_core_running("local HTTP simulator")
            self.assertTrue(ok)
            self.assertEqual(len(self.starts()), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_shell_false_options_and_core_restart_helper(self):
        self.options_path.write_text(json.dumps({"auto_full_system_update": False, "restart_homeassistant_after_custom_component": False}))
        script = mod.Path(mod.__file__).with_name("run.sh")
        env = dict(mod.os.environ, DWARS_INSTALLER_LIB_ONLY="true", CONFIG_PATH=str(self.options_path), STATE_DIR=str(self.root))
        result = subprocess.run(["bash", "-c", f'source "{script}"; get_bool auto_full_system_update true; get_bool restart_homeassistant_after_custom_component true; declare -f restart_homeassistant_core'], env=env, text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.splitlines()[:2], ["false", "false"])
        self.assertIn("--restart-core", result.stdout)
        self.assertIn("--lock-held", result.stdout)


if __name__ == "__main__":
    unittest.main()
