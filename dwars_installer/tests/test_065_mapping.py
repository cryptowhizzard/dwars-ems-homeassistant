"""Literal, anonymised production inventory: do NOT generate IDs from GOODWE_MAP.

The 194-entity fixture retains the reported register names, readings and disabled
flags. Serial, config entry, IP and MAC are anonymised. UI diagnosis omits select
attributes; HTTP tests supply simulated capabilities and refresh sample times.
No real inverter or Home Assistant/Supervisor runtime is used.
"""
from __future__ import annotations
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp import web
from aiohttp.test_utils import TestServer

import test_062_regressions as earlier
from test_oneshot import profile, device, ROOT
from oneshot import OneShot, LABELS
from oneshot_common import (
    VERSION, Blocked, GOODWE_MAP, REQUIRED, atomic_json,
    bind_device, load_json, match_entity,
)

FIXTURE = Path(__file__).parent / 'fixtures/goodwe_mapping_194_entities.json'
SERIAL = 'TESTGW00000001'
PREFIX = 'goodwe_testgw00000001_'


def inventory():
    return json.loads(FIXTURE.read_text())


def eid(suffix, domain='sensor'):
    return domain + '.' + PREFIX + suffix


def grid(d):
    return match_entity(d, ('sensor',), GOODWE_MAP['grid_entity'][1])


def remove_register(d, name):
    d['entities'] = [r for r in d['entities'] if r['unique_id'] != 'goodwe-' + name + '-' + SERIAL]


class IdentityTests(unittest.TestCase):
    def test_complete_reported_inventory_binds_all_13_options(self):
        d = inventory()
        self.assertEqual(len(d['entities']), 194)
        selected, m = bind_device(profile(), [d])
        self.assertEqual(selected['serial'], SERIAL)
        expected = {
            'soc_entity': eid('battery_state_of_charge'),
            'pv_entity': eid('pv_power'),
            'grid_entity': eid('active_power_total'),
            'battery_power_entity': eid('battery_power'),
            'ha_ems_mode_select': eid('ems_mode', 'select'),
            'ha_ems_power_number': eid('ems_power_limit', 'number'),
            'ha_dod_holding_switch': eid('dod_holding', 'switch'),
            'ha_backup_supply_switch': eid('backup_supply_switch', 'switch'),
            'ha_dod_number': eid('depth_of_discharge_backup', 'number'),
            'ha_dod_on_grid_number': eid('depth_of_discharge_on_grid', 'number'),
            'ha_operation_mode_select': eid('inverter_operation_mode', 'select'),
            'ha_grid_export_limit_number': eid('grid_export_limit', 'number'),
            'ha_grid_export_limit_switch': eid('grid_export_limit_switch', 'switch'),
        }
        self.assertEqual(m, expected)

    def test_dual_total_sensors_are_not_equal_priority(self):
        d = inventory()
        d['entities'] = [r for r in d['entities'] if r['entity_id'] in {eid('active_power_total'), eid('meter_active_power_total')}]
        self.assertEqual(len(d['entities']), 2)
        self.assertEqual(grid(d)['entity_id'], eid('active_power_total'))

    def test_optional_switch_alias_prefix_is_not_a_false_tie(self):
        d = inventory()
        d['entities'].append({'entity_id': 'switch.not_the_dod_control',
                              'unique_id': 'other_dod_holding_switch-' + SERIAL})
        _, m = bind_device(profile(), [d])
        self.assertEqual(m['ha_dod_holding_switch'], eid('dod_holding', 'switch'))

    def test_mapping_is_independent_of_inventory_order(self):
        d = inventory(); expected = bind_device(profile(), [d])[1]
        for seed in range(25):
            random.Random(seed).shuffle(d['entities'])
            with self.subTest(seed=seed):
                self.assertEqual(bind_device(profile(), [d])[1], expected)

    def test_renamed_entity_ids_still_bind_by_unique_id(self):
        d = inventory()
        for i, r in enumerate(d['entities']):
            r['entity_id'] = r['entity_id'].split('.')[0] + '.renamed_' + str(i)
        row = grid(d)
        self.assertEqual(row['unique_id'], 'goodwe-active_power_total-' + SERIAL)
        self.assertTrue(bind_device(profile(), [d])[1]['grid_entity'].startswith('sensor.renamed_'))

    def test_exact_grid_total_survives_generic_translation_on_meter(self):
        d = inventory()
        for r in d['entities']:
            if r['entity_id'] == eid('meter_active_power_total'):
                r['translation_key'] = 'active_power_total'
        self.assertEqual(grid(d)['entity_id'], eid('active_power_total'))

    def test_fallback_to_explicit_meter_total_alias(self):
        d = inventory(); remove_register(d, 'active_power_total')
        self.assertEqual(grid(d)['entity_id'], eid('meter_active_power_total'))

    def test_fallback_to_active_power_when_no_total_register(self):
        d = inventory()
        remove_register(d, 'active_power_total'); remove_register(d, 'meter_active_power_total')
        self.assertEqual(grid(d)['entity_id'], eid('active_power'))

    def test_phase_reactive_apparent_or_second_meter_not_total_fallback(self):
        d = inventory()
        keys = ['active_power1', 'active_power2', 'active_power3',
                'reactive_power_total', 'meter_reactive_power_total', 'apparent_power_total',
                'meter_apparent_power_total', 'meter2_active_power_total', 'load_active_power_total']
        d['entities'] = [{'entity_id': 'sensor.test_' + k, 'unique_id': 'goodwe-' + k + '-' + SERIAL}
                         for k in keys]
        self.assertIsNone(grid(d))

    def test_true_duplicate_register_still_blocks_with_candidate_names(self):
        d = inventory()
        duplicate = deepcopy(next(r for r in d['entities'] if r['entity_id'] == eid('active_power_total')))
        duplicate['entity_id'] = 'sensor.second_copy_same_identity'; d['entities'].append(duplicate)
        with self.assertRaises(Blocked) as error:
            bind_device(profile(), [d])
        self.assertIn('sensor.second_copy_same_identity', str(error.exception))
        self.assertIn(eid('active_power_total'), str(error.exception))

    def test_zero_or_unknown_reading_does_not_reselect_a_different_register(self):
        d = inventory()
        for reading in ('0', 'unknown', 'unavailable', None):
            for r in d['entities']:
                if r['entity_id'] == eid('active_power_total'): r['state'] = reading
            self.assertEqual(grid(d)['entity_id'], eid('active_power_total'))

    def test_manual_mapping_to_alternate_total_takes_precedence(self):
        p = profile(); p['agent_options']['grid_entity'] = eid('meter_active_power_total')
        _, m = bind_device(p, [inventory()])
        self.assertEqual(m['grid_entity'], eid('meter_active_power_total'))

    def test_missing_control_not_silently_bypassed(self):
        d = inventory(); remove_register(d, 'ems_power_limit')
        with self.assertRaisesRegex(Blocked, 'ha_ems_power_number'):
            bind_device(profile(), [d])

    def test_two_physical_battery_inverters_still_require_explicit_serial(self):
        d = inventory(); other = json.loads(json.dumps(d).replace(SERIAL, 'SECONDGW').replace(SERIAL.lower(), 'secondgw'))
        with self.assertRaisesRegex(Blocked, 'Meerdere batterij'):
            bind_device(profile(), [d, other])
        p = profile(); p['control_serial'] = SERIAL
        self.assertEqual(bind_device(p, [d, other])[0]['serial'], SERIAL)

    def test_solaredge_prefixed_unique_id_and_meter_scope_unchanged(self):
        d = device('solaredge', 'SE123')
        _, m = bind_device(profile('solaredge'), [d])
        self.assertEqual(m['ha_grid_sensor'], 'sensor.se123_ac_power')
        d['entities'].append({'entity_id': 'sensor.other_meter', 'unique_id': 'M2_ac_power', 'is_root': False})
        with self.assertRaises(Blocked): bind_device(profile('solaredge'), [d])

    def test_translation_only_exact_key_supported(self):
        d = {'platform': 'goodwe', 'serial': SERIAL, 'entities': [
            {'entity_id': 'sensor.legacy_soc', 'unique_id': '', 'translation_key': 'battery_soc'}]}
        self.assertEqual(match_entity(d, ('sensor',), ('battery_soc',))['entity_id'], 'sensor.legacy_soc')

    def test_step_label_describes_mapping_not_new_battery_discovery(self):
        self.assertEqual(LABELS['mapping'], 'Sensoren automatisch aan de agent koppelen')


class InstallerReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)
        self.one = OneShot(self.path, ROOT / 'dwars_installer'); self.one.profile = profile()
        self.one.credentials = {'api_key': 'saved-key'}
        atomic_json(self.one.credentials_path, self.one.credentials)
        self.one.save(installer_version='0.6.4', payload_version='0.6.4', payload_root=str(ROOT),
                      stage='mapping', status='blocked', restart_needed=True,
                      restart_requests=1, restart_requested_at=12345, restart_acknowledged=True,
                      mapping={'soc_entity': 'sensor.manual'}, selected_device={'serial': SERIAL})
    def tearDown(self): self.tmp.cleanup()

    def test_same_component_release_preserves_position_payload_and_restart_counters(self):
        original = deepcopy(self.one.state)
        self.one.prepare_release()
        self.assertEqual(self.one.state['installer_version'], VERSION)
        self.assertEqual(self.one.state['payload_version'], VERSION)
        for key in ('stage', 'payload_root', 'restart_needed', 'restart_requests', 'restart_requested_at',
                    'restart_acknowledged', 'mapping', 'selected_device', 'installation_id'):
            self.assertEqual(self.one.state[key], original[key], key)
        self.assertEqual(load_json(self.one.credentials_path), {'api_key': 'saved-key'})
        self.assertEqual(load_json(self.path / ('oneshot_before_' + VERSION + '.json')), original)

    def test_release_resume_is_idempotent(self):
        self.one.prepare_release(); expected = deepcopy(self.one.state)
        self.one.prepare_release(); self.assertEqual(self.one.state, expected)

    def test_other_late_stages_not_reset_to_discovery(self):
        for stage in ('agent', 'verify', 'complete'):
            self.one.state.update(installer_version='0.6.4', payload_version='0.6.4', stage=stage)
            self.one.prepare_release(); self.assertEqual(self.one.state['stage'], stage)

    def test_old_integration_release_still_gets_required_refresh(self):
        self.one.state.update(installer_version='0.6.3', payload_version='0.6.3')
        self.one.prepare_release()
        self.assertEqual(self.one.state['stage'], 'payload')
        self.assertTrue(self.one.state['restart_needed'])

    def test_missing_cache_does_not_use_fast_resume(self):
        self.one.state['payload_root'] = str(self.path / 'missing')
        self.one.prepare_release(); self.assertEqual(self.one.state['stage'], 'payload')

    def test_old_cached_manifest_does_not_use_fast_resume(self):
        p = self.path / 'stale'
        atomic_json(p / 'custom_components/dwars_setup/manifest.json', {'version': '1.1.0'})
        self.one.state['payload_root'] = str(p)
        self.one.prepare_release(); self.assertEqual(self.one.state['stage'], 'payload')

    def test_missing_agent_defaults_does_not_use_fast_resume(self):
        p = self.path / 'incomplete'
        for name, ver in [('dwars_setup', '1.2.0'), ('goodwe', '0.9.9.37')]:
            atomic_json(p / 'custom_components' / name / 'manifest.json', {'version': ver})
        self.one.state['payload_root'] = str(p)
        self.one.prepare_release(); self.assertEqual(self.one.state['stage'], 'payload')


class DiagnosticHTTPFixture(earlier.ProvisioningFixture):
    """HTTP responses use literal diagnosis readings, not generated 50s.

    HA's /states attributes weren't exported in the source diagnosis. The select
    capabilities below are simulated. Sample timestamps advance for freshness.
    """
    def __init__(self):
        super().__init__('goodwe', False)
        self.devices = [inventory()]
        self.profile['power_watt'] = 6000
        self.discovered = True
        self.entries = [{'entry_id': 'bridge', 'domain': 'dwars_setup'},
                        {'entry_id': 'test-ha-config-entry', 'domain': 'goodwe'}]

    async def handler(self, request):
        if request.path.startswith('/core/api/states/'):
            entity = request.path.split('/states/', 1)[1]
            self.calls.append((request.method, request.path, None))
            if request.headers.get('Authorization') != 'Bearer fixture-supervisor-token':
                return web.json_response({'message': 'unauthorized'}, status=401)
            r = next((r for d in self.devices for r in d['entities'] if r['entity_id'] == entity), None)
            if r is None: return web.json_response({'message': 'missing entity'}, status=404)
            attrs = {}
            if entity == eid('ems_mode', 'select'):
                attrs['options'] = ['auto', 'battery_standby', 'charge_battery', 'discharge_battery']
            return web.json_response({'entity_id': entity, 'state': r['state'], 'attributes': attrs,
                                      'last_reported': datetime.now(timezone.utc).isoformat()})
        return await super().handler(request)


class DiagnosticWorkerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = earlier.WorkerHTTPTests.asyncSetUp
    asyncTearDown = earlier.WorkerHTTPTests.asyncTearDown
    installer = earlier.WorkerHTTPTests.installer
    finish = earlier.WorkerHTTPTests.finish

    async def fixture(self):
        remote = DiagnosticHTTPFixture()
        app = web.Application(); app.router.add_route('*', '/{path:.*}', remote.handler)
        server = TestServer(app); await server.start_server(); self.servers.append(server)
        remote.url = str(server.make_url('')).rstrip('/')
        return remote

    def resume(self, remote):
        atomic_json(self.path / 'oneshot_credentials.json', {'api_key': 'customer-key'})
        one = self.installer(remote)
        one.save(stage='mapping', status='blocked', installer_version='0.6.4', payload_version='0.6.4', client_id=remote.profile['client_id'], platform='goodwe',
                 message='Meer dan één passende entiteit voor active_power_total; leg de mapping vast in EMS.',
                 restart_requests=1, restart_needed=True, restart_acknowledged=True, restart_requested_at=12345)
        return one

    def assert_no_inverter_mutation(self, one, remote):
        self.assertEqual(remote.restarts, 0)
        self.assertFalse(any(m['type'] in ('dwars_setup/run', 'dwars_setup/enable') for m in remote.messages))
        self.assertFalse(any(method == 'POST' and '/config/config_entries/' in path
                             for method, path, _ in remote.calls))
        self.assertFalse((one.config_dir / 'custom_components').exists())

    async def test_fresh_oneshot_with_literal_inventory_also_completes(self):
        remote = await self.fixture()
        remote.discovered = False; remote.entries = []
        one = self.installer(remote)
        with redirect_stdout(io.StringIO()): await self.finish(one)
        self.assertEqual(one.state['mapping']['grid_entity'], eid('active_power_total'))
        self.assertEqual(len(one.state['mapping']), 13)
        self.assertEqual(remote.restarts, 1)
        self.assertEqual(sum(m['type'] == 'dwars_setup/run' for m in remote.messages), 1)
        self.assertEqual(set(remote.apps), {'repo_goodwe_agent'})
        self.assertIn('telemetry_verified_at', one.state)

    async def test_exact_diagnosis_resumes_installs_only_agent_and_verifies_receipt(self):
        remote = await self.fixture(); one = self.resume(remote)
        with redirect_stdout(io.StringIO()): await self.finish(one)
        self.assertEqual(one.state['stage'], 'complete')
        self.assertEqual(one.state['mapping']['grid_entity'], eid('active_power_total'))
        self.assertEqual(len(one.state['mapping']), 13)
        self.assertEqual(set(remote.apps), {'repo_goodwe_agent'})
        self.assertEqual(remote.apps['repo_goodwe_agent']['state'], 'started')
        self.assertEqual(remote.apps['repo_goodwe_agent']['options']['power_watt'], 6000)
        self.assertEqual(sum(p.endswith('/install') for _, p, _ in remote.calls), 1)
        self.assertIn('telemetry_verified_at', one.state)
        self.assert_no_inverter_mutation(one, remote)

    async def test_manual_key_only_agent_reused_and_non_mapping_settings_preserved(self):
        remote = await self.fixture()
        agent = remote.new_app('goodwe_agent')
        agent['state'] = 'started'; agent['options'].update(api_key='customer-key', power_watt=8500,
            goodwe_default_dod=83, goodwe_default_dod_on_grid=81, main_fuse_profile='1x35',
            ha_charge_block_below_w='-4321', standalone_enabled=True)
        remote.apps['repo_goodwe_agent'] = agent
        before = deepcopy(agent['options']); one = self.resume(remote)
        with redirect_stdout(io.StringIO()): await self.finish(one)
        after = remote.apps['repo_goodwe_agent']['options']
        for key in before:
            if before[key] not in (None, '', 'auto'):
                self.assertEqual(after[key], before[key], key)
        self.assertEqual(after['grid_entity'], eid('active_power_total'))
        self.assertEqual(after['goodwe_serial_number'], SERIAL)
        self.assertEqual(after['installation_id'], one.state['installation_id'])
        self.assertEqual(sum(p.endswith('/stop') for _, p, _ in remote.calls), 1)
        self.assertEqual(sum(p.endswith('/start') for _, p, _ in remote.calls), 1)
        self.assertFalse(any(p.endswith(('/install', '/update', '/rebuild')) for _, p, _ in remote.calls))
        self.assert_no_inverter_mutation(one, remote)

    async def test_explicit_existing_meter_map_and_all_manual_options_stay_unchanged(self):
        remote = await self.fixture(); agent = remote.new_app('goodwe_agent')
        _, mapping = bind_device(remote.profile, remote.devices)
        mapping['grid_entity'] = eid('meter_active_power_total')
        agent['options'].update(mapping)
        agent['options'].update(api_key='customer-key', client_id=str(remote.profile['client_id']),
            goodwe_serial_number=SERIAL, power_watt=7100, main_fuse_profile='1x35')
        agent['state'] = 'started'; remote.apps['repo_goodwe_agent'] = agent
        before = deepcopy(agent['options']); one = self.resume(remote)
        with redirect_stdout(io.StringIO()): await self.finish(one)
        after = remote.apps['repo_goodwe_agent']['options']
        self.assertEqual({k for k in after if before.get(k) != after[k]}, {'installation_id'})
        self.assertEqual(one.state['effective_mapping']['grid_entity'], eid('meter_active_power_total'))
        self.assert_no_inverter_mutation(one, remote)

    async def test_unavailable_required_sensor_remains_blocked_and_agent_not_started(self):
        remote = await self.fixture()
        next(r for r in remote.devices[0]['entities'] if r['entity_id'] == eid('battery_state_of_charge'))['state'] = 'unavailable'
        one = self.resume(remote); one.profile = remote.profile
        from aiohttp import ClientSession
        one.session = ClientSession()
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(Blocked, 'niet beschikbaar'):
            await one.mapping_stage()
        self.assertEqual(one.state['stage'], 'mapping')
        self.assertFalse(remote.apps)
        self.assert_no_inverter_mutation(one, remote)

    async def test_no_receipt_cannot_be_reported_as_success(self):
        remote = await self.fixture(); one = self.resume(remote); one.profile = remote.profile
        from aiohttp import ClientSession
        one.session = ClientSession()
        with redirect_stdout(io.StringIO()):
            await one.mapping_stage(); await one.agent_stage()
        remote.receipts = 0  # explicitly suppress receipt after start in the test double
        with self.assertRaisesRegex(RuntimeError, 'bevestigt nog geen'):
            await one.verify_stage()
        self.assertNotEqual(one.state['status'], 'complete')
        self.assert_no_inverter_mutation(one, remote)

    async def test_true_duplicate_is_not_solved_by_starting_a_random_agent(self):
        remote = await self.fixture()
        duplicate = deepcopy(next(r for r in remote.devices[0]['entities'] if r['entity_id'] == eid('active_power_total')))
        duplicate['entity_id'] = 'sensor.duplicate'; remote.devices[0]['entities'].append(duplicate)
        one = self.resume(remote); one.profile = remote.profile
        from aiohttp import ClientSession
        one.session = ClientSession()
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(Blocked, 'Meer dan één'):
            await one.mapping_stage()
        self.assertFalse(remote.apps)
        self.assert_no_inverter_mutation(one, remote)


if __name__ == '__main__':
    unittest.main()
