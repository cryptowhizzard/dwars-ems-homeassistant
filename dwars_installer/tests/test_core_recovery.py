"""Regression tests of real updater paths, with stopped Core and Supervisor available."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_auto_updater import FakeClient, mod
from core_recovery import CoreRecovery, core_config_ready


class Clock:
    now = 1000.0

    def time(self):
        return self.now

    def sleep(self, amount):
        self.now += amount


class StoppedClient(FakeClient):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.running = False
        self.ready_at = None
        self.auth_error = False
        self.stats_error = None
        self.jobs_error = False
        self.jobs = []
        self.jobs_until = 0
        self.start_works = True
        self.stop_after_update = True
        self.start_timeout_after_accept = False
        self.starts = []
        self.system_infos["/core/info"].update({"boot": True, "watchdog": True})
        self.system_infos["/supervisor/info"]["state"] = "running"

    def ha(self, method, path, data=None, timeout=60):
        if path == "/config":
            self.calls.append(("ha", method, path, data))
            if self.auth_error:
                raise mod.ApiError("unauthorized", status=401)
            if self.running:
                return {"version": "2026.9.1", "time_zone": "Europe/Amsterdam", "state": "RUNNING"}
            if self.ready_at is not None:
                if self.clock.time() >= self.ready_at:
                    self.running = True
                    return {"version": "2026.9.1", "state": "RUNNING"}
                return {"version": "2026.9.1", "state": "STARTING"}
            raise mod.ApiError("Bad gateway", status=502)
        return super().ha(method, path, data, timeout)

    def supervisor(self, method, path, data=None, timeout=60):
        if path == "/jobs/info":
            if self.jobs_error:
                raise mod.ApiError("Job service unavailable", status=503)
            return {"data": {"jobs": self.jobs if self.clock.time() < self.jobs_until else []}}
        if path == "/core/stats":
            if self.stats_error:
                raise self.stats_error
            if self.running or self.ready_at is not None:
                return {"data": {"cpu_percent": 1, "memory_usage": 200000000}}
            raise mod.ApiError("Home Assistant is not running", status=400,
                               body='{"error_key":"homeassistant_not_running_error"}')
        if path == "/core/start":
            self.calls.append(("supervisor", method, path, data))
            self.starts.append(self.clock.time())
            if not self.start_works:
                raise mod.ApiError("Invalid configuration", status=400)
            self.running = True
            if self.start_timeout_after_accept:
                raise mod.ApiError("Request timeout", status=None)
            return {"result": "ok", "data": {}}
        if path == "/core/options":
            self.system_infos["/core/info"].update(data)
        if path == "/core/update" and self.stop_after_update:
            self.running = False
        if path == "/core/restart":
            self.running = False
        return super().supervisor(method, path, data, timeout)


class CoreRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.addCleanup(self.tmp.cleanup)
        for attr, side in (("time", self.clock.time), ("monotonic", self.clock.time), ("sleep", self.clock.sleep)):
            patch = mock.patch.object(mod.time, attr, side_effect=side)
            patch.start()
            self.addCleanup(patch.stop)
        self.client = StoppedClient(self.clock)
        self.path = self.root / "options.json"
        self.write_options()
        self.updater = mod.AutoUpdater(mod.Options(str(self.path)), mod.JsonState(str(self.root / "state.json")), self.client, str(self.root / "lock"))
        self.guard = self.updater.core_recovery

    def write_options(self, **kwargs):
        values = {"auto_full_system_update": True, "auto_full_system_update_core_start_grace_sec": 30,
                  "auto_full_system_update_core_recovery_timeout_sec": 420,
                  "auto_full_system_update_timezone": "Europe/Amsterdam"}
        values.update(kwargs)
        self.path.write_text(json.dumps(values))

    def test_ready_core_not_started_or_restarted(self):
        self.client.running = True
        self.assertTrue(self.guard.ensure("ready", 180)[0])
        self.assertEqual(self.client.starts, [])
        self.assertFalse(self.guard.state["pending"])

    def test_latest_version_but_core_stopped_still_started(self):
        ok, updated, detail = self.updater.update_system_component(
            label="Core", info_path="/core/info", update_path="/core/update", wait_for_ha=True)
        self.assertTrue(ok, detail)
        self.assertFalse(updated)
        self.assertEqual(len(self.client.starts), 1)
        self.assertNotIn("/core/update", [x[2] for x in self.client.calls])

    def test_new_version_installed_but_stopped_is_started(self):
        self.client.running = True
        self.client.system_infos["/core/info"].update(version_latest="2", update_available=True)
        ok, updated, detail = self.updater.update_system_component(
            label="Core", info_path="/core/info", update_path="/core/update", wait_for_ha=True)
        self.assertTrue(ok, detail)
        self.assertTrue(updated)
        self.assertEqual(len(self.client.starts), 1)
        self.assertEqual(self.client.system_infos["/core/info"]["version"], "2")

    def test_core_started_before_config_check_after_reboot(self):
        self.updater.state = {"active": True, "stage": "core", "host_rebooted": True}
        self.updater.run_core_stage()
        calls = [x[2] for x in self.client.calls]
        self.assertLess(calls.index("/core/start"), calls.index("/core/check"))
        self.assertEqual(self.updater.state["stage"], "verify")

    def test_restart_path_also_recovers_when_core_does_not_return(self):
        self.client.running = True
        ok, detail = self.updater.restart_core_and_wait()
        self.assertTrue(ok, detail)
        calls = [x[2] for x in self.client.calls]
        self.assertEqual(calls.count("/core/restart"), 1)
        self.assertEqual(calls.count("/core/start"), 1)
        self.assertNotIn("/core/stop", calls)

    def test_stopped_core_component_update_uses_start_not_restart(self):
        self.assertTrue(self.updater.restart_core_and_wait()[0])
        self.assertEqual(len(self.client.starts), 1)
        self.assertNotIn("/core/restart", [x[2] for x in self.client.calls])

    def test_running_slow_core_or_migration_not_interrupted(self):
        self.client.ready_at = self.clock.now + 200
        ok, detail = self.guard.ensure("migration", 300)
        self.assertTrue(ok, detail)
        self.assertEqual(self.client.starts, [])

    def test_active_update_job_waited_for_before_start(self):
        self.client.jobs = [{"name": "home_assistant_core_update", "done": False}]
        self.client.jobs_until = self.clock.now + 200
        self.assertTrue(self.guard.ensure("job", 300)[0])
        self.assertGreaterEqual(self.client.starts[0], 1200)

    def test_restore_job_never_interrupted(self):
        self.client.jobs = [{"name": "backup_manager_full_restore", "done": False}]
        self.client.jobs_until = self.clock.now + 1000
        self.assertFalse(self.guard.ensure("restore", 180)[0])
        self.assertEqual(self.client.starts, [])

    def test_nested_core_job_blocks_start(self):
        jobs = [{"name": "job_chain", "done": False, "child_jobs": [{"name": "home_assistant_core_start", "done": False}]}]
        self.assertTrue(CoreRecovery._job_blocks_start(jobs))
        jobs[0]["child_jobs"][0]["done"] = True
        self.assertFalse(CoreRecovery._job_blocks_start(jobs))

    def test_core_auth_failure_never_causes_start(self):
        self.client.auth_error = True
        self.assertFalse(self.guard.ensure("auth", 300)[0])
        self.assertEqual(self.client.starts, [])
        self.assertEqual(self.guard.state["status"], "authentication_error")

    def test_generic_stats_timeout_never_assumed_stopped(self):
        self.client.stats_error = mod.ApiError("Timed out getting stats for Home Assistant", status=500)
        self.assertFalse(self.guard.ensure("network", 180)[0])
        self.assertEqual(self.client.starts, [])

    def test_supervisor_jobs_unavailable_no_start(self):
        self.client.jobs_error = True
        self.assertFalse(self.guard.ensure("jobs", 180)[0])
        self.assertEqual(self.client.starts, [])

    def test_start_request_timeout_followed_by_healthy_api_is_success(self):
        self.client.start_timeout_after_accept = True
        self.assertTrue(self.guard.ensure("timeout", 180)[0])
        self.assertEqual(len(self.client.starts), 1)

    def test_start_failure_is_bounded_and_survives_process_restart(self):
        self.client.start_works = False
        self.assertFalse(self.guard.ensure("failed", 600)[0])
        self.assertEqual(len(self.client.starts), 3)
        self.assertEqual(self.guard.state["status"], "exhausted")
        other = CoreRecovery(self.guard.options, self.client, self.guard.path, lambda _: None)
        self.assertFalse(other.ensure("new process", 600)[0])
        self.assertEqual(len(self.client.starts), 3)
        self.assertTrue(all(b-a >= 120 for a,b in zip(self.client.starts, self.client.starts[1:])))

    def test_manual_start_can_clear_exhausted_health_without_more_retries(self):
        self.client.start_works = False
        self.guard.ensure("failed", 600)
        self.client.running = True
        self.assertTrue(self.guard.ensure("manual start", 10)[0])
        self.assertFalse(self.guard.state["pending"])
        self.assertEqual(len(self.client.starts), 3)

    def test_boot_policy_enabled_but_can_be_disabled(self):
        self.client.system_infos["/core/info"].update(boot=False, watchdog=False)
        self.write_options(auto_full_system_update_core_autostart=False)
        self.guard.options = mod.Options(str(self.path))
        self.guard.enforce_boot_policy()
        self.assertFalse(self.client.system_infos["/core/info"]["boot"])
        self.assertFalse(self.guard.ensure("manual maintenance", 100)[0])
        self.assertEqual(self.client.starts, [])
        self.write_options()
        self.guard.options = mod.Options(str(self.path))
        self.guard.enforce_boot_policy()
        self.assertTrue(self.client.system_infos["/core/info"]["boot"])
        self.assertTrue(self.client.system_infos["/core/info"]["watchdog"])

    def test_recovery_disabled_is_honored(self):
        self.write_options(auto_full_system_update_core_recovery=False)
        self.guard.options = mod.Options(str(self.path))
        self.assertFalse(self.guard.ensure("disabled", 100)[0])
        self.assertEqual(self.client.starts, [])

    def test_failed_health_cannot_produce_successful_final_result(self):
        self.client.start_works = False
        self.updater.state = {"active": True, "stage": "verify"}
        self.updater.run_verify_stage()
        self.assertEqual(self.updater.state["last_result"], "completed_with_failures")
        self.assertNotEqual(self.updater.state["core_health"]["status"], "ready")

    def test_startup_recovers_a_completed_052_run(self):
        self.updater.state = {"state_version": mod.STATE_VERSION, "active": False, "stage": "complete"}
        self.updater.save()
        self.updater.recover_core_outside_run(startup=True)
        self.assertEqual(len(self.client.starts), 1)
        self.assertEqual(self.updater.state["core_health"]["status"], "ready")

    def test_pending_os_reboot_not_interfered_with(self):
        self.updater.state = {"active": True, "stage": "os_wait_reboot"}
        self.updater.save()
        self.updater.recover_core_outside_run(startup=True)
        self.assertEqual(self.client.starts, [])

    def test_idle_manual_stop_after_success_not_overridden(self):
        self.client.running = True
        self.updater.recover_core_outside_run(startup=True)
        self.client.running = False
        self.updater.recover_core_outside_run()
        self.assertEqual(self.client.starts, [])

    def test_stale_or_missing_config_is_not_readiness(self):
        for value in ({}, {"result":"ok"}, {"raw":"<html>login</html>"}, {"version":"v", "state":"STARTING"}):
            self.assertFalse(core_config_ready(value))
        self.assertTrue(core_config_ready({"version":"2026.9.1", "state":"RUNNING"}))
        self.assertTrue(core_config_ready({"version":"2024.1.0"}))

    def test_supervisor_200_error_envelope_is_not_accepted(self):
        client = mod.ApiClient("test-placeholder")
        with mock.patch.object(client, "_request", return_value={"result":"error", "message":"Home Assistant is not running"}):
            with self.assertRaises(mod.ApiError):
                client.supervisor("GET", "/core/stats")

    def test_wait_ha_does_not_treat_200_login_html_as_ready(self):
        client = mod.ApiClient("test-placeholder")
        with mock.patch.object(client, "ha", side_effect=[{"raw":"login"}, {"version":"2026.9.1", "state":"STARTING"}, {"version":"2026.9.1", "state":"RUNNING"}]):
            before = self.clock.now
            self.assertTrue(client.wait_ha(100))
            self.assertEqual(self.clock.now - before, 20)


class ShellIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.options = Path(self.tmp.name)/"options.json"
        self.options.write_text('{"auto_full_system_update":false,"goodwe_agent_client_id":0}')

    def run_shell(self, code):
        env = dict(os.environ, DWARS_INSTALLER_LIB_ONLY="true", CONFIG_PATH=str(self.options), SUPERVISOR_TOKEN="test-placeholder")
        return subprocess.run(["bash", "-c", f'source "{self.root / "run.sh"}"\n' + code],
                              env=env, capture_output=True, text=True, timeout=3, check=True)

    def test_supervisor_curl_is_nonrecursive_and_wrapper_exists(self):
        result = self.run_shell('curl() { while [ $# -gt 0 ]; do if [ \"$1\" = -o ]; then printf CALLED > \"$2\"; fi; shift; done; printf 200; }; supervisor_curl GET /info; try_supervisor_curl GET /info')
        self.assertEqual(result.stdout.strip(), 'CALLEDCALLED')

    def test_shell_preserves_false_and_zero(self):
        result = self.run_shell('get_bool auto_full_system_update true; get_opt goodwe_agent_client_id 50')
        self.assertEqual(result.stdout.strip(), 'false\n0')

    def test_component_restart_uses_shared_recovery_helper_under_lock(self):
        result = self.run_shell('python3() { printf "%s\\n" "$@"; }; restart_homeassistant_core')
        self.assertIn('--restart-core', result.stdout)
        self.assertIn('--lock-held', result.stdout)
        self.assertIn('/app/auto_updater.py', result.stdout)

    def test_boot_flags_and_package_dependencies(self):
        config = json.loads((self.root/'config.json').read_text())
        self.assertTrue(config['hassio_api'])
        self.assertTrue(config['homeassistant_api'])
        self.assertEqual(config['hassio_role'], 'manager')
        self.assertEqual(config['version'], '0.6.3')
        self.assertEqual(set(config['options']), set(config['schema']))
        self.assertIn('COPY core_recovery.py /app/core_recovery.py', (self.root/'Dockerfile').read_text())


if __name__ == '__main__':
    unittest.main()
