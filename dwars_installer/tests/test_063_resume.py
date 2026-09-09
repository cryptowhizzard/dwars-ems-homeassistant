"""Commissioning resume regressions. All remote services/hardware are doubles."""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from test_oneshot import Simulator, remote_fixture, ROOT
from oneshot import OneShot
from oneshot_common import Blocked, VERSION, REQUIRED, atomic_json, load_json, bind_device


def literal_device():
    # Literal entity identities, not generated from the installer's matching map.
    rows = [
        ('sensor.accu_percentage','goodwe-battery_soc-GW123'),
        ('sensor.netvermogen','goodwe-active_power_total-GW123'),
        ('sensor.accuvermogen','goodwe-pbattery1-GW123'),
        ('select.accumodus','goodwe-ems_mode-GW123'),
        ('number.accuvermogen_limiet','goodwe-ems_power_limit-GW123'),
        ('switch.noodstroom','backup_supply-GW123'),
    ]
    return {'platform':'goodwe','serial':'GW123','entry_id':'manual-entry-new',
            'host':'192.0.2.10','model':'GW10K-ET','state':'loaded','reason':'',
            'entities':[{'entity_id':e,'unique_id':u,'translation_key':None,
                         'is_root':True,'disabled_by':None} for e,u in rows]}


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.remote=remote_fixture();self.remote['devices']=[literal_device()]
        self.remote['device']=self.remote['devices'][0]
        self.one=Simulator(self.path,self.remote)
        self.one.credentials={'api_key':'a'*64};atomic_json(self.one.credentials_path,self.one.credentials)
        self.one.profile=self.remote['profile']
        self.one.ws=AsyncMock(wraps=self.one.ws)
        self.one.save(stage='mapping',payload_root=str(ROOT),installer_version=VERSION)
    async def asyncTearDown(self):self.tmp.cleanup()
    def assert_no_discovery(self):
        self.assertFalse(any(call.args[0]=='dwars_setup/run' for call in self.one.ws.await_args_list))
        self.assertEqual(self.remote['restarts'],0)
    async def test_manual_device_reused_before_scan(self):
        self.one.save(stage='discover')
        await self.one.discover_stage()
        self.assert_no_discovery()
        self.assertTrue(self.one.state['discovery_completed'])
    async def test_forced_discovery_is_explicit(self):
        self.one.save(stage='discover',force_discovery=True)
        await self.one.discover_stage()
        self.assertTrue(any(c.args[0]=='dwars_setup/run' for c in self.one.ws.await_args_list))
        self.assertFalse(self.one.state['force_discovery'])
    async def test_fresh_empty_registry_triggers_discovery(self):
        real=self.one.ws
        async def ws(command,**kw):
            if command=='dwars_setup/status' and not getattr(self,'started',False):return {'status':'idle','devices':[]}
            if command=='dwars_setup/run':self.started=True
            return await real(command,**kw)
        self.one.ws=AsyncMock(side_effect=ws)
        await self.one.discover_stage()
        self.assertTrue(self.started)
    async def test_expected_missing_second_inverter_triggers_scan(self):
        self.one.profile['expected_inverters']=2
        await self.one.discover_stage()
        self.assertTrue(any(c.args[0]=='dwars_setup/run' for c in self.one.ws.await_args_list))
    async def test_loaded_manual_real_ids_bind(self):
        await self.one.mapping_stage()
        self.assertEqual(self.one.state['mapping']['soc_entity'],'sensor.accu_percentage')
        self.assertEqual(self.one.state['mapping']['ha_ems_mode_select'],'select.accumodus')
        self.assertEqual(self.one.state['mapping']['ha_backup_supply_switch'],'switch.noodstroom')
        self.assertEqual(self.one.state['selected_device']['entry_id'],'manual-entry-new')
        self.assert_no_discovery()
    async def test_unavailable_sensor_stays_mapping_not_install_or_discover(self):
        self.remote['values']={'sensor.accu_percentage':'unavailable'}
        with patch('oneshot.asyncio.sleep',new=AsyncMock()):
            with self.assertRaisesRegex(Blocked,'niet beschikbaar'):await self.one.mapping_stage()
        self.assertEqual(self.one.state['stage'],'mapping');self.assert_no_discovery()
    async def test_missing_control_is_mapping_problem_not_new_device(self):
        self.remote['devices'][0]['entities']=[r for r in self.remote['devices'][0]['entities'] if not r['entity_id'].startswith('number.')]
        with patch('oneshot.asyncio.sleep',new=AsyncMock()):
            with self.assertRaisesRegex(Blocked,'ha_ems_power_number'):await self.one.mapping_stage()
        self.assertEqual(self.one.state['stage'],'mapping');self.assert_no_discovery()
    async def test_setup_error_stays_mapping_and_names_real_state(self):
        self.remote['devices'][0].update(state='setup_error',reason='test connection failed')
        with patch('oneshot.asyncio.sleep',new=AsyncMock()):
            with self.assertRaisesRegex(Blocked,'setup_error'):await self.one.mapping_stage()
        self.assertEqual(self.one.state['stage'],'mapping');self.assert_no_discovery()
    async def test_sensor_recovers_after_app_restart_with_no_reinstallation(self):
        self.remote['values']={'sensor.accu_percentage':'unavailable'}
        with patch('oneshot.asyncio.sleep',new=AsyncMock()):
            with self.assertRaises(Blocked):await self.one.mapping_stage()
        reloaded=Simulator(self.path,self.remote);reloaded.profile=self.one.profile
        self.remote['values']={'sensor.accu_percentage':'52'}
        await reloaded.mapping_stage()
        self.assertEqual(reloaded.state['selected_device']['entry_id'],'manual-entry-new')
        self.assertEqual(reloaded.state['stage'],'mapping')
        self.assertEqual(self.remote['restarts'],0)
    async def test_hand_readded_same_serial_replaces_only_cached_ids(self):
        self.one.save(selected_device={'entry_id':'removed-entry','serial':'GW123','host':'192.0.2.2'},
                      mapping={'soc_entity':'sensor.removed'})
        await self.one.mapping_stage()
        self.assertEqual(self.one.state['selected_device']['entry_id'],'manual-entry-new')
        self.assertEqual(self.one.state['mapping']['soc_entity'],'sensor.accu_percentage')
        self.assert_no_discovery()
    async def test_remembered_serial_cannot_silently_switch_controller(self):
        self.one.save(selected_device={'entry_id':'removed-entry','serial':'DIFFERENT'})
        with patch('oneshot.asyncio.sleep',new=AsyncMock()):
            with self.assertRaises(Blocked):await self.one.mapping_stage()
        self.assertEqual(self.one.state['selected_device']['serial'],'DIFFERENT');self.assert_no_discovery()
    async def test_only_absent_device_returns_to_discovery(self):
        self.remote['devices']=[]
        with patch('oneshot.asyncio.sleep',new=AsyncMock()):
            with self.assertRaises(Blocked):await self.one.mapping_stage()
        self.assertEqual(self.one.state['stage'],'discover')
    async def test_user_disabled_control_not_reenabled(self):
        self.remote['devices'][0]['entities'][3]['disabled_by']='user'
        with self.assertRaisesRegex(Blocked,'handmatig uitgeschakeld'):await self.one.mapping_stage()
        self.assertFalse(any(c.args[0]=='dwars_setup/enable' for c in self.one.ws.await_args_list))
        self.assertEqual(self.one.state['stage'],'mapping')
    async def test_integration_disabled_control_may_be_enabled(self):
        self.remote['devices'][0]['entities'][3]['disabled_by']='integration'
        await self.one.mapping_stage()
        self.assertTrue(any(c.args[0]=='dwars_setup/enable' for c in self.one.ws.await_args_list))
    async def test_agent_stage_rebinds_after_manual_readd(self):
        self.one.save(selected_device={'serial':'GW123','entry_id':'old'},mapping={'soc_entity':'sensor.removed'})
        await self.one.agent_stage()
        options=self.remote['apps']['test_goodwe_agent']['options']
        self.assertEqual(options['soc_entity'],'sensor.accu_percentage')
        self.assertEqual(self.remote['apps']['test_goodwe_agent']['state'],'started')
        self.assert_no_discovery()
    def add_manual_agent(self):
        cfg=load_json(ROOT/'goodwe_agent/config.json')
        options=deepcopy(cfg['options'])
        _,mapping=bind_device(self.one.profile,self.remote['devices'])
        options.update(mapping)
        options.update(api_key='a'*64,client_id=str(self.one.profile['client_id']),goodwe_serial_number='GW123',
                       power_watt=9000,goodwe_default_dod=84,goodwe_default_dod_on_grid=83,
                       main_fuse_profile='3x25_plus',ha_charge_block_below_w='-9876')
        self.remote['apps']['test_goodwe_agent']={'state':'started','options':options,'schema':cfg['schema'],'boot':'auto'}
        return deepcopy(options)
    async def test_manual_agent_config_preserved_except_commissioning_id(self):
        before=self.add_manual_agent()
        await self.one.agent_stage()
        after=self.remote['apps']['test_goodwe_agent']['options']
        delta={k:(before.get(k),v) for k,v in after.items() if before.get(k)!=v}
        self.assertEqual(set(delta),{'installation_id'})
        self.assertEqual(after['installation_id'],self.one.state['installation_id'])
        self.assertFalse(any('/install' in p or '/update' in p for _,p,_ in self.remote['calls']))
        self.assert_no_discovery()
    async def test_already_registered_running_agent_not_restarted(self):
        self.add_manual_agent()
        self.remote['apps']['test_goodwe_agent']['options']['installation_id']=self.one.state['installation_id']
        await self.one.agent_stage()
        self.assertFalse(any(p.endswith('/stop') or p.endswith('/start') for _,p,_ in self.remote['calls']))
    async def test_existing_foreign_client_number_blocks(self):
        self.add_manual_agent()
        self.remote['apps']['test_goodwe_agent']['options']['client_id']='99999'
        with self.assertRaisesRegex(Blocked,'klantnummer'):await self.one.agent_stage()
        self.assertFalse(any(p.endswith('/options') or p.endswith('/stop') for _,p,_ in self.remote['calls']))
    async def test_existing_manual_mapping_not_guessed_from_missing_aliases(self):
        self.add_manual_agent()
        for row in self.remote['devices'][0]['entities']:row['unique_id']='manufacturer-specific-'+row['entity_id']
        await self.one.mapping_stage()
        self.assertEqual(self.one.state['mapping']['soc_entity'],'sensor.accu_percentage')
    async def test_manual_mapping_cannot_belong_to_other_inverter(self):
        self.add_manual_agent()
        self.remote['apps']['test_goodwe_agent']['options']['soc_entity']='sensor.someone_else'
        with patch('oneshot.asyncio.sleep',new=AsyncMock()):
            with self.assertRaises(Blocked):await self.one.mapping_stage()
        self.assertEqual(self.one.state['stage'],'mapping');self.assert_no_discovery()
    async def test_verify_after_manual_readd_returns_to_mapping_not_discovery(self):
        self.one.save(stage='verify',selected_device={'serial':'GW123','entry_id':'removed'},mapping={})
        with self.assertRaisesRegex(RuntimeError,'niet opnieuw installeren'):await self.one.verify_stage()
        self.assertEqual(self.one.state['stage'],'mapping');self.assert_no_discovery()
    async def test_old_bridge_is_not_accepted_as_new_code(self):
        self.one.ws=AsyncMock(return_value={'devices':[]})
        with self.assertRaisesRegex(Blocked,'oude installatiebrug'):await self.one.bridge_stage()
        self.assertEqual(self.remote['restarts'],0)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.one=OneShot(self.path,ROOT/'dwars_installer');self.one.profile=remote_fixture()['profile']
    def tearDown(self):self.tmp.cleanup()
    def test_upgrade_invalidates_old_payload_but_preserves_key_and_mapping(self):
        self.one.credentials={'api_key':'saved-key'};atomic_json(self.one.credentials_path,self.one.credentials)
        self.one.save(stage='mapping',payload_root='/old/0.6.2',mapping={'soc_entity':'sensor.manual'},
                      selected_device={'serial':'GW123','entry_id':'existing'},restart_requests=2)
        old_id=self.one.state['installation_id']
        self.one.prepare_release()
        self.assertEqual(self.one.state['stage'],'payload');self.assertIsNone(self.one.state['payload_root'])
        self.assertEqual(self.one.state['mapping'],{'soc_entity':'sensor.manual'})
        self.assertEqual(self.one.state['installation_id'],old_id)
        self.assertEqual(load_json(self.one.credentials_path),{'api_key':'saved-key'})
        self.assertEqual(self.one.state['restart_requests'],0)
        before=deepcopy(self.one.state);self.one.prepare_release();self.assertEqual(self.one.state,before)
        self.assertTrue((self.path/'oneshot_before_0.6.3.json').is_file())
    def test_fresh_install_stays_profile(self):
        self.one.prepare_release();self.assertEqual(self.one.state['stage'],'profile')
    def test_updated_payload_passes(self):self.one.check_payload(ROOT)
    def test_old_component_in_download_rejected_before_install(self):
        root=self.path/'payload'
        for component,version in [('dwars_setup','1.0.0'),('goodwe','0.9.9.35')]:
            atomic_json(root/'custom_components'/component/'manifest.json',{'version':version})
        with self.assertRaisesRegex(Blocked,'oude'):self.one.check_payload(root)
    def test_switch_identifier_without_goodwe_prefix_binds(self):
        _,mapping=bind_device(self.one.profile,[literal_device()])
        self.assertEqual(mapping['ha_backup_supply_switch'],'switch.noodstroom')

if __name__=='__main__':unittest.main()
