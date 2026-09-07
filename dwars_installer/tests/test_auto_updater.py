from __future__ import annotations

import datetime as dt
import importlib.util
import json
import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MODULE_PATH = Path(__file__).resolve().parents[1] / "auto_updater.py"
spec = importlib.util.spec_from_file_location("dwars_auto_updater", MODULE_PATH)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class FakeClient:
    def __init__(self) -> None:
        self.calls = []
        self.os_info = {
            "version": "15.1",
            "version_latest": "16.0",
            "update_available": True,
            "boot": "A",
        }
        self.boot_timestamp = 100
        self.addons = []
        self.states = []
        self.backups = []
        self.system_infos = {
            "/cli/info": {"version": "1", "version_latest": "1", "update_available": False},
            "/dns/info": {"version": "1", "version_latest": "1", "update_available": False},
            "/audio/info": {"version": "1", "version_latest": "1", "update_available": False},
            "/multicast/info": {"version": "1", "version_latest": "1", "update_available": False},
            "/observer/info": {"version": "1", "version_latest": "1", "update_available": False},
            "/supervisor/info": {"version": "1", "version_latest": "1", "update_available": False},
            "/core/info": {"version": "1", "version_latest": "1", "update_available": False},
        }

    def supervisor(self, method, path, data=None, timeout=60):
        self.calls.append(("supervisor", method, path, data))
        if path == "/host/info":
            return {"data": {"hostname": "test-pi", "timezone": "Europe/Amsterdam", "boot_timestamp": self.boot_timestamp}}
        if path == "/os/info":
            return {"data": dict(self.os_info)}
        if method == "GET" and path in self.system_infos:
            return {"data": dict(self.system_infos[path])}
        if path == "/addons":
            return {"data": {"addons": list(self.addons)}}
        if path == "/backups":
            return {"data": {"backups": list(self.backups)}}
        if path == "/addons/self/info":
            return {"data": {"slug": "repo_dwars_installer"}}
        if path.startswith("/addons/") and path.endswith("/info"):
            slug = path.split("/")[2]
            for addon in self.addons:
                if addon.get("slug") == slug:
                    return {"data": addon}
            return {"data": {"slug": slug, "version": "1.0", "update_available": False}}
        if method == "POST" and path.startswith("/store/addons/") and path.endswith("/update"):
            slug = path.split("/")[3]
            for addon in self.addons:
                if addon.get("slug") == slug:
                    addon["version"] = addon.get("version_latest")
                    addon["update_available"] = False
            return {"data": {}}
        if method == "POST" and path.endswith("/update"):
            info_path = path.removesuffix("/update") + "/info"
            if info_path in self.system_infos:
                info = self.system_infos[info_path]
                info["version"] = info.get("version_latest")
                info["update_available"] = False
            return {"data": {}}
        if method == "POST" and path == "/backups/new/full":
            return {"data": {"slug": "new-backup"}}
        if path == "/info":
            return {"data": {"result": "ok"}}
        return {"data": {}}

    def ha(self, method, path, data=None, timeout=60):
        self.calls.append(("ha", method, path, data))
        if path == "/config":
            return {"time_zone": "Europe/Amsterdam", "version": "2026.8.3", "state": "RUNNING"}
        if path == "/states":
            return list(self.states)
        if path.startswith("/states/update."):
            entity_id = path.removeprefix("/states/")
            for entity in self.states:
                if entity.get("entity_id") == entity_id:
                    return entity
            raise mod.ApiError("not found", status=404)
        if method == "POST" and path == "/services/update/install":
            entity_ids = data.get("entity_id") if isinstance(data, dict) else []
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]
            for entity in self.states:
                if entity.get("entity_id") in entity_ids:
                    attrs = entity.setdefault("attributes", {})
                    attrs["installed_version"] = attrs.get("latest_version")
                    attrs["in_progress"] = False
                    entity["state"] = "off"
            return {}
        return {}

    def wait_supervisor(self, timeout, interval=10):
        return True

    def wait_ha(self, timeout, interval=10):
        return True


class UpdaterTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.options_path = self.root / "options.json"
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / "lock"
        self.client = FakeClient()

    def tearDown(self):
        self.tmp.cleanup()

    def write_options(self, **overrides):
        values = {
            "auto_full_system_update": True,
            "auto_full_system_update_time": "04:00",
            "auto_full_system_update_timezone": "Europe/Amsterdam",
            "auto_full_system_update_days": "mon,tue,wed,thu,fri,sat,sun",
            "auto_full_system_update_jitter_minutes": 0,
            "auto_full_system_update_backup": True,
            "auto_full_system_update_reboot": True,
            "skip_upstream_goodwe_updates": True,
        }
        values.update(overrides)
        self.options_path.write_text(json.dumps(values))
        return mod.Options(str(self.options_path))

    def updater(self, **options):
        return mod.AutoUpdater(
            self.write_options(**options),
            mod.JsonState(str(self.state_path)),
            self.client,
            str(self.lock_path),
        )

    def test_options_flattens_legacy_nested_map(self):
        self.options_path.write_text(json.dumps({"api_url": {"auto_full_system_update": True, "inverter_type": "both"}}))
        options = mod.Options(str(self.options_path))
        self.assertTrue(options.bool("auto_full_system_update"))
        self.assertEqual(options.text("inverter_type"), "both")

    def test_first_schedule_after_evening_is_next_morning(self):
        updater = self.updater()
        after = dt.datetime(2026, 9, 6, 21, 0, tzinfo=mod.ZoneInfo("Europe/Amsterdam"))
        next_run = updater.next_scheduled_time(after.astimezone(dt.timezone.utc), strictly_future=True)
        local = next_run.astimezone(mod.ZoneInfo("Europe/Amsterdam"))
        self.assertEqual(local.date(), dt.date(2026, 9, 7))
        self.assertEqual((local.hour, local.minute), (4, 0))

    def test_first_start_after_schedule_but_before_ten_runs_as_catchup(self):
        updater = self.updater(auto_full_system_update_first_start_catchup_until="10:00")
        morning = dt.datetime(2026, 9, 7, 7, 30, tzinfo=mod.ZoneInfo("Europe/Amsterdam"))
        with mock.patch.object(mod, "utc_now", return_value=morning.astimezone(dt.timezone.utc)):
            updater.initialize_schedule()
        next_run = mod.parse_iso(updater.state["next_run"])
        self.assertIsNotNone(next_run)
        self.assertLessEqual((next_run - morning.astimezone(dt.timezone.utc)).total_seconds(), 31)

    def test_upstream_goodwe_is_skipped_but_dwars_goodwe_is_not(self):
        upstream = {
            "entity_id": "update.goodwe_update",
            "state": "on",
            "attributes": {"friendly_name": "GoodWe", "release_url": "https://github.com/marcelblijleven/goodwe"},
        }
        managed = {
            "entity_id": "update.dwars_goodwe_update",
            "state": "on",
            "attributes": {"friendly_name": "DWARS GoodWe", "release_url": "https://github.com/cryptowhizzard/dwars-ems-homeassistant"},
        }
        firmware = {
            "entity_id": "update.goodwe_inverter_firmware",
            "state": "on",
            "attributes": {"friendly_name": "GoodWe inverter firmware", "device_class": "firmware"},
        }
        self.assertTrue(mod.AutoUpdater.is_upstream_goodwe_entity(upstream))
        self.assertFalse(mod.AutoUpdater.is_upstream_goodwe_entity(managed))
        self.assertFalse(mod.AutoUpdater.is_upstream_goodwe_entity(firmware))

    def test_system_update_entities_are_not_installed_as_generic_entities(self):
        updater = self.updater()
        self.client.states = [
            {"entity_id": "update.home_assistant_core_update", "state": "on", "attributes": {"friendly_name": "Home Assistant Core Update"}},
            {"entity_id": "update.hacs_update", "state": "on", "attributes": {"friendly_name": "HACS", "installed_version": "2", "latest_version": "3"}},
        ]
        found = updater.discover_entity_updates()
        self.assertEqual([item.item_id for item in found], ["update.hacs_update"])

    def test_addon_self_update_is_sorted_last(self):
        updater = self.updater()
        self.client.addons = [
            {"slug": "repo_dwars_installer", "name": "DWARS Installer", "installed": True, "version": "0.5.0", "version_latest": "0.5.1", "update_available": True},
            {"slug": "a0d7b954_tailscale", "name": "Tailscale", "installed": True, "version": "1", "version_latest": "2", "update_available": True},
        ]
        items = updater.discover_addons()
        self.assertEqual(items[-1].item_id, "repo_dwars_installer")
        self.assertEqual(items[0].item_id, "a0d7b954_tailscale")

    def test_entity_install_has_no_per_item_backup(self):
        updater = self.updater(auto_full_system_update_item_timeout_sec=60)
        entity = {
            "entity_id": "update.hacs_update",
            "state": "on",
            "attributes": {"friendly_name": "HACS", "in_progress": False},
        }
        self.client.states = [entity]

        def state_sequence(entity_id):
            if entity["state"] == "on":
                entity["state"] = "off"
                return {**entity, "state": "on", "attributes": {**entity["attributes"], "in_progress": True}}
            return entity

        updater.entity_state = state_sequence
        with mock.patch.object(mod.time, "sleep", return_value=None):
            ok, _ = updater.update_entity(mod.UpdateItem("entity", "update.hacs_update", "HACS"))
        self.assertTrue(ok)
        calls = [c for c in self.client.calls if c[0] == "ha" and c[2] == "/services/update/install"]
        self.assertEqual(calls[0][3], {"entity_id": "update.hacs_update"})
        self.assertNotIn("backup", calls[0][3])

    def test_entity_unavailable_is_not_treated_as_success(self):
        updater = self.updater(auto_full_system_update_item_timeout_sec=60)
        states = [
            {"entity_id": "update.example", "state": "on", "attributes": {"in_progress": False}},
            {"entity_id": "update.example", "state": "unavailable", "attributes": {"in_progress": False}},
            {"entity_id": "update.example", "state": "off", "attributes": {"in_progress": False}},
        ]
        seen = []

        def state_sequence(entity_id):
            value = states[min(len(seen), len(states) - 1)]
            seen.append(value["state"])
            return value

        updater.entity_state = state_sequence
        with mock.patch.object(mod.time, "sleep", return_value=None):
            ok, detail = updater.update_entity(mod.UpdateItem("entity", "update.example", "Example"))
        self.assertTrue(ok)
        self.assertEqual(seen, ["on", "unavailable", "off"])
        self.assertIn("state=off", detail)

    def test_os_update_enters_reboot_stage_even_if_active_version_did_not_change(self):
        updater = self.updater()
        updater.state = {
            "state_version": mod.STATE_VERSION,
            "active": True,
            "stage": "os",
            "successes": [],
            "failures": [],
            "log": [],
        }
        updater.save()
        with self.assertRaises(mod.PauseRun):
            updater.run_os_stage()
        self.assertEqual(updater.state["stage"], "os_wait_reboot")
        self.assertEqual(updater.state["os_version_before"], "15.1")
        self.assertEqual(updater.state["os_target_version"], "16.0")
        self.assertTrue(any(c[2] == "/host/reboot" for c in self.client.calls))

    def test_os_resume_verifies_new_version_after_boot(self):
        updater = self.updater()
        updater.state = {
            "state_version": mod.STATE_VERSION,
            "active": True,
            "stage": "os_wait_reboot",
            "successes": [],
            "failures": [],
            "log": [],
            "boot_timestamp_before": 100,
            "os_version_before": "15.1",
            "os_target_version": "16.0",
        }
        self.client.boot_timestamp = 200
        self.client.os_info = {"version": "16.0", "version_latest": "16.0", "update_available": False}
        updater.run_os_wait_reboot_stage()
        self.assertEqual(updater.state["stage"], "core")
        self.assertTrue(updater.state["host_rebooted"])
        success_ids = [x["id"] for x in updater.state["successes"]]
        self.assertIn("home_assistant_os", success_ids)
        self.assertIn("host_reboot", success_ids)

    def test_os_404_is_skipped_on_supervised_installation(self):
        updater = self.updater()
        updater.state = {
            "state_version": mod.STATE_VERSION,
            "active": True,
            "stage": "os",
            "successes": [],
            "failures": [],
            "log": [],
        }

        original = self.client.supervisor

        def supervisor(method, path, data=None, timeout=60):
            if path == "/os/info":
                raise mod.ApiError("not found", status=404)
            return original(method, path, data, timeout)

        self.client.supervisor = supervisor
        updater.run_os_stage()
        self.assertEqual(updater.state["stage"], "core")
        self.assertFalse(updater.state["failures"])
        self.assertIn("home_assistant_os", [x["id"] for x in updater.state["successes"]])

    def test_complete_no_update_run_reaches_completed_state(self):
        updater = self.updater(
            auto_full_system_update_backup=True,
            auto_full_system_update_supervisor_plugins=True,
            auto_full_system_update_core_check=True,
        )
        self.client.os_info = {"version": "16.0", "version_latest": "16.0", "update_available": False}
        with mock.patch.object(mod.time, "sleep", return_value=None):
            ok = updater.run_once("test")
        self.assertTrue(ok)
        self.assertFalse(updater.state["active"])
        self.assertEqual(updater.state["stage"], "complete")
        self.assertEqual(updater.state["last_result"], "completed")
        paths = [call[2] for call in self.client.calls]
        self.assertNotIn("/backups/new/full", paths)
        self.assertNotIn("/core/check", paths)

    def test_preflight_with_pending_addon_advances_to_backup(self):
        updater = self.updater()
        self.client.addons = [
            {"slug": "a0d7b954_tailscale", "name": "Tailscale", "installed": True, "version": "1", "version_latest": "2", "update_available": True}
        ]
        updater.state = {
            "state_version": mod.STATE_VERSION,
            "active": True,
            "stage": "preflight",
            "successes": [],
            "failures": [],
            "log": [],
        }
        updater.run_preflight_stage()
        self.assertEqual(updater.state["stage"], "backup")
        self.assertEqual(updater.state["addon_queue"][0]["id"], "a0d7b954_tailscale")

    def test_preflight_refreshes_store_supervisor_and_update_entities(self):
        updater = self.updater(auto_full_system_update_refresh_catalogs=True)
        self.client.states = [
            {
                "entity_id": "update.hacs_update",
                "state": "off",
                "attributes": {"friendly_name": "HACS"},
            }
        ]
        with mock.patch.object(mod.time, "sleep", return_value=None):
            updater.state = {
                "state_version": mod.STATE_VERSION,
                "active": True,
                "stage": "preflight",
                "successes": [],
                "failures": [],
                "log": [],
            }
            updater.run_preflight_stage()
        calls = {(scope, method, path) for scope, method, path, _ in self.client.calls}
        self.assertIn(("supervisor", "POST", "/store/reload"), calls)
        self.assertIn(("supervisor", "POST", "/reload_updates"), calls)
        self.assertIn(("ha", "POST", "/services/homeassistant/update_entity"), calls)


    def test_refresh_updates_404_uses_supervisor_reload_fallback(self):
        updater = self.updater(auto_full_system_update_refresh_catalogs=True)
        original = self.client.supervisor

        def supervisor(method, path, data=None, timeout=60):
            if path == "/reload_updates":
                self.client.calls.append(("supervisor", method, path, data))
                raise mod.ApiError("not found", status=404)
            return original(method, path, data, timeout)

        self.client.supervisor = supervisor
        with mock.patch.object(mod.time, "sleep", return_value=None):
            errors = updater.refresh_update_catalogs()
        self.assertEqual(errors, [])
        calls = {(scope, method, path) for scope, method, path, _ in self.client.calls}
        self.assertIn(("supervisor", "POST", "/reload_updates"), calls)
        self.assertIn(("supervisor", "POST", "/supervisor/reload"), calls)

    def test_addon_explicit_400_fails_fast(self):
        updater = self.updater(auto_full_system_update_item_timeout_sec=60)
        self.client.addons = [{
            "slug": "example", "name": "Example", "installed": True,
            "version": "1", "version_latest": "2", "update_available": True,
        }]
        original = self.client.supervisor

        def supervisor(method, path, data=None, timeout=60):
            if method == "POST" and path == "/store/addons/example/update":
                raise mod.ApiError("invalid options", status=400)
            return original(method, path, data, timeout)

        self.client.supervisor = supervisor
        ok, detail = updater.update_addon(mod.UpdateItem("addon", "example", "Example"))
        self.assertFalse(ok)
        self.assertIn("invalid options", detail)

    def test_system_component_explicit_400_fails_fast(self):
        updater = self.updater(auto_full_system_update_system_timeout_sec=300)
        self.client.system_infos["/supervisor/info"] = {
            "version": "1", "version_latest": "2", "update_available": True,
        }
        original = self.client.supervisor

        def supervisor(method, path, data=None, timeout=60):
            if method == "POST" and path == "/supervisor/update":
                raise mod.ApiError("rejected", status=400)
            return original(method, path, data, timeout)

        self.client.supervisor = supervisor
        ok, updated, detail = updater.update_system_component(
            label="Supervisor", info_path="/supervisor/info", update_path="/supervisor/update"
        )
        self.assertFalse(ok)
        self.assertFalse(updated)
        self.assertIn("rejected", detail)

    def test_os_explicit_400_does_not_reboot(self):
        updater = self.updater()
        updater.state = {
            "state_version": mod.STATE_VERSION, "active": True, "stage": "os",
            "successes": [], "failures": [], "log": [],
        }
        original = self.client.supervisor

        def supervisor(method, path, data=None, timeout=60):
            if method == "POST" and path == "/os/update":
                raise mod.ApiError("OS image rejected", status=400)
            return original(method, path, data, timeout)

        self.client.supervisor = supervisor
        updater.run_os_stage()
        self.assertEqual(updater.state["stage"], "core")
        self.assertFalse(any(call[2] == "/host/reboot" for call in self.client.calls))
        self.assertIn("home_assistant_os", [x["id"] for x in updater.state["failures"]])

    def test_backup_retention_only_deletes_old_unprotected_dwars_backups(self):
        updater = self.updater(auto_full_system_update_backup_keep=2)
        self.client.backups = [
            {"slug": "current", "name": "DWARS auto-update 2026-09-06 04:00", "date": "2026-09-06T02:00:00+00:00", "protected": False},
            {"slug": "previous", "name": "DWARS auto-update 2026-09-05 04:00", "date": "2026-09-05T02:00:00+00:00", "protected": False},
            {"slug": "old", "name": "DWARS auto-update 2026-09-04 04:00", "date": "2026-09-04T02:00:00+00:00", "protected": False},
            {"slug": "protected", "name": "DWARS auto-update 2026-09-03 04:00", "date": "2026-09-03T02:00:00+00:00", "protected": True},
            {"slug": "manual", "name": "Handmatige backup", "date": "2026-09-02T02:00:00+00:00", "protected": False},
        ]
        updater.state = {
            "state_version": mod.STATE_VERSION,
            "active": True,
            "stage": "backup",
            "successes": [],
            "failures": [],
            "log": [],
        }
        updater.prune_old_auto_update_backups("current")
        deleted = [call[2] for call in self.client.calls if call[1] == "DELETE"]
        self.assertEqual(deleted, ["/backups/old"])

    def test_stage_order_matches_safe_blueprint_order_with_plugins(self):
        self.assertLess(mod.STAGES.index("backup"), mod.STAGES.index("addons"))
        self.assertLess(mod.STAGES.index("addons"), mod.STAGES.index("entities"))
        self.assertLess(mod.STAGES.index("entities"), mod.STAGES.index("retry"))
        self.assertLess(mod.STAGES.index("retry"), mod.STAGES.index("supervisor"))
        self.assertLess(mod.STAGES.index("supervisor"), mod.STAGES.index("os"))
        self.assertLess(mod.STAGES.index("os"), mod.STAGES.index("core"))

    def test_end_to_end_addon_entity_supervisor_and_core_update(self):
        updater = self.updater(
            auto_full_system_update_backup=True,
            auto_full_system_update_backup_keep=3,
            auto_full_system_update_retry_delay_sec=5,
            auto_full_system_update_item_timeout_sec=60,
            auto_full_system_update_system_timeout_sec=300,
        )
        self.client.addons = [
            {
                "slug": "a0d7b954_tailscale",
                "name": "Tailscale",
                "installed": True,
                "version": "1",
                "version_latest": "2",
                "update_available": True,
            }
        ]
        self.client.states = [
            {
                "entity_id": "update.hacs_update",
                "state": "on",
                "attributes": {
                    "friendly_name": "HACS",
                    "installed_version": "2",
                    "latest_version": "3",
                    "in_progress": False,
                },
            }
        ]
        self.client.system_infos["/supervisor/info"] = {
            "version": "1",
            "version_latest": "2",
            "update_available": True,
        }
        self.client.system_infos["/core/info"] = {
            "version": "1",
            "version_latest": "2",
            "update_available": True,
        }
        self.client.os_info = {"version": "16", "version_latest": "16", "update_available": False}
        with mock.patch.object(mod.time, "sleep", return_value=None):
            ok = updater.run_once("integration-test")
        self.assertTrue(ok)
        self.assertEqual(updater.state["stage"], "complete")
        self.assertEqual(updater.state["last_result"], "completed")
        paths = [call[2] for call in self.client.calls]
        self.assertIn("/backups/new/full", paths)
        self.assertIn("/store/addons/a0d7b954_tailscale/update", paths)
        self.assertIn("/services/update/install", paths)
        self.assertIn("/supervisor/update", paths)
        self.assertIn("/core/check", paths)
        self.assertIn("/core/update", paths)


if __name__ == "__main__":
    unittest.main()
