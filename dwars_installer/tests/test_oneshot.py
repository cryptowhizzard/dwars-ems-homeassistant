"""OneShot unit/HTTP/state-machine tests. These use simulated HA/Supervisor/BMS.

Run: python3 -m unittest discover -s dwars_installer/tests -v
Requires aiohttp for the ingress tests. No hardware or actual HA is used.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'dwars_installer'))
from oneshot_common import (Blocked, AGENT, DOMAIN, GOODWE_MAP, SOLAREDGE_MAP, REQUIRED,
    api_base, atomic_json, bind_device, choose_mode, directory_hash, endpoint,
    extract_payload, install_component, load_json, merge_options, validate_profile)
from oneshot import OneShot, APIError, STAGES


def profile(platform='goodwe'):
    return {'schema_version': 1, 'client_id': 123, 'client_name': 'Testklant',
        'platform': platform, 'ems_enabled': True, 'power_watt': 5000,
        'hosts': [], 'unit_ids': [1], 'expected_inverters': 0, 'control_serial': '',
        'agent_options': {}, 'agent_config': {'inverter': {'main_fuse_profile': '3x25_plus',
        'depth_of_discharge_pct': 90, 'depth_of_discharge_on_grid_pct': 90}}}


def device(platform='goodwe', serial='GW123', battery=True):
    result = {'platform': platform, 'serial': serial, 'entry_id': 'entry_' + serial,
              'host': '192.0.2.10', 'model': 'Test', 'entities': []}
    schema = GOODWE_MAP if platform == 'goodwe' else SOLAREDGE_MAP
    for key, (domains, aliases) in schema.items():
        if not battery and ('soc' in key or 'ems' in key or 'storage' in aliases[0]):
            continue
        uid = 'goodwe-' + aliases[0] + '-' + serial if platform == 'goodwe' else serial + '_' + aliases[-1]
        result['entities'].append({'entity_id': domains[0] + '.' + serial.lower() + '_' + aliases[0],
             'unique_id': uid, 'translation_key': None, 'is_root': key not in {'ha_grid_sensor', 'ha_soc_sensor'},
             'disabled_by': None})
    return result


class PureTests(unittest.TestCase):
    def test_platforms(self):
        for brand in AGENT:
            self.assertEqual(validate_profile(profile(brand))['platform'], brand)
    def test_reject_profile_variants(self):
        for values in ({'platform':'unknown'}, {'client_id':True}, {'schema_version':2}, {'power_watt':0},
                       {'power_watt':True}, {'ems_enabled':False}, {'agent_options':[]}, {'hosts':['localhost']},
                       {'unit_ids':[True]}, {'unit_ids':[248]}, {'expected_inverters':65}, {'control_serial':'../x'}):
            with self.subTest(values=values), self.assertRaises(Blocked):
                validate_profile({**profile(), **values})
    def test_api_origin(self):
        self.assertEqual(endpoint('https://example.invalid/bms/api', 'install_profile.php'), 'https://example.invalid/bms/api/install_profile.php')
        for url in ('http://example.invalid', 'https://u:p@example.invalid/', 'https://example.invalid/?x=1'):
            with self.subTest(url=url), self.assertRaises(Blocked): api_base(url)
        with self.assertRaises(Blocked): endpoint('https://example.invalid', '../x.php')
    def test_persistent_json_private(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'secret.json'
            atomic_json(path, {'key':'test'})
            self.assertEqual(load_json(path), {'key':'test'})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.write_text('[]')
            with self.assertRaises(Blocked): load_json(path)
    def test_atomic_json_reject_nan_preserves_old(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'state.json'; atomic_json(path, {'v':1})
            with self.assertRaises(ValueError): atomic_json(path, {'v':float('nan')})
            self.assertEqual(load_json(path), {'v':1})
    def test_choose_mode_existing_and_new(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)
            self.assertEqual(choose_mode({},path),'oneshot')
            self.assertEqual(choose_mode({'goodwe_agent_api_key':'old'},path),'manual')
            (path/'dwars_auto_update_state.json').write_text('{}')
            self.assertEqual(choose_mode({},path),'manual')
            self.assertEqual(choose_mode({'installation_mode':'oneshot'},path),'oneshot')
            (path/'oneshot_state.json').write_text('{}')
            self.assertEqual(choose_mode({},path),'oneshot')
    def test_binding_goodwe_pv_plus_battery(self):
        selected,mapping=bind_device(profile(),[device(serial='PV123',battery=False),device()])
        self.assertEqual(selected['serial'],'GW123')
        self.assertTrue(mapping['soc_entity'].startswith('sensor.gw123_'))
    def test_two_batteries_blocked(self):
        with self.assertRaisesRegex(Blocked,'Meerdere batterij'):
            bind_device(profile(),[device(),device(serial='GW999')])
    def test_explicit_serial(self):
        selected,_=bind_device({**profile(),'control_serial':'GW999'},[device(),device(serial='GW999')])
        self.assertEqual(selected['serial'],'GW999')
    def test_expected_count(self):
        with self.assertRaisesRegex(Blocked,'1 van 2'):
            bind_device({**profile(),'expected_inverters':2},[device()])
    def test_no_inverter(self):
        with self.assertRaises(Blocked): bind_device(profile(),[])
    def test_solaredge_meter_not_inverter_power(self):
        d=device('solaredge','SE123')
        d['entities'].append({'entity_id':'sensor.inverter_ac_power','unique_id':'SE123_ac_power','is_root':True})
        _,m=bind_device(profile('solaredge'),[d])
        self.assertNotEqual(m['ha_grid_sensor'],'sensor.inverter_ac_power')
    def test_ambiguous_meter_rejected(self):
        d=device('solaredge','SE123')
        d['entities'].append({'entity_id':'sensor.other_meter','unique_id':'M2_ac_power','is_root':False})
        with self.assertRaises(Blocked): bind_device(profile('solaredge'),[d])
    def test_preserve_manual_mapping(self):
        merged,managed=merge_options({'soc_entity':'sensor.manual'}, {'soc_entity':'auto'}, {'soc_entity':'sensor.auto'})
        self.assertEqual(merged['soc_entity'],'sensor.manual');self.assertNotIn('soc_entity',managed)
    def test_owned_mapping_can_change(self):
        merged,_=merge_options({'soc_entity':'sensor.old'}, {'soc_entity':'auto'}, {'soc_entity':'sensor.new'}, {'soc_entity':'sensor.old'})
        self.assertEqual(merged['soc_entity'],'sensor.new')
    def test_generic_mapping_preserved(self):
        merged,_=merge_options({'ha_power_number':'number.manual'}, {'ha_power_number':''}, {'ha_power_number':'number.new'})
        self.assertEqual(merged['ha_power_number'],'number.manual')
    def test_component_swap_idempotent_and_rollback_copy(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'source';dst=Path(td)/'cc'/'goodwe';src.mkdir()
            (src/'manifest.json').write_text('{"version":"1"}')
            self.assertTrue(install_component(src,dst));self.assertFalse(install_component(src,dst))
            (src/'manifest.json').write_text('{"version":"2"}')
            self.assertTrue(install_component(src,dst));self.assertEqual(directory_hash(src),directory_hash(dst))
            self.assertIn('"1"',(dst.parent/'.dwars-backup-goodwe'/'manifest.json').read_text())
    def test_zip_traversal_refused(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td);z=path/'repo.zip'
            with zipfile.ZipFile(z,'w') as f: f.writestr('../escape','bad')
            with self.assertRaises(Blocked): extract_payload(z,path/'out')
            self.assertFalse((path/'escape').exists())
    def test_valid_zip_and_missing_release(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td);z=path/'repo.zip'
            with zipfile.ZipFile(z,'w') as f: f.writestr('repo/custom_components/dwars_setup/manifest.json','{}')
            self.assertEqual(extract_payload(z,path/'out'),path/'out'/'repo')
            with zipfile.ZipFile(z,'w') as f: f.writestr('repo/README.md','old')
            with self.assertRaises(Blocked): extract_payload(z,path/'out2')


class Simulator(OneShot):
    """Models remote APIs without claiming a real HA or Modbus integration test."""
    def __init__(self, path, remote):
        super().__init__(path, ROOT/'dwars_installer')
        self.remote=remote;self.config_dir=Path(path)/'ha_config'
        self.config_dir.mkdir(exist_ok=True)
    async def bms(self, name, payload=None, query=''):
        if name=='install_profile.php': return {'ok':True,'profile':deepcopy(self.remote['profile'])}
        if name=='install_status.php':
            if payload is not None: self.remote['reports'].append(payload)
            active=any(x.get('state')=='started' for x in self.remote['apps'].values())
            return {'ok':True,'receipt':{'receipt_count':1 if active else 0,'inverter_serial':self.remote['device']['serial'],
                     'received_at':'2026-09-08 12:00:00'}}
        raise AssertionError(name)
    async def payload_stage(self): self.save(payload_root=str(ROOT))
    async def subprocess(self,*args,**kwargs): self.remote['processes'].append(args)
    async def ha(self,method,path,payload=None,**kwargs):
        if path=='/config':return {'version':'2026.test','state':'RUNNING'}
        if path.startswith('/config/config_entries/entry'):return [{'entry_id':'bridge'}]
        if path.startswith('/states/'):
            entity=path.split('/states/',1)[1]
            if entity in self.remote.get('missing',[]): raise APIError(404,'missing')
            return {'state':self.remote.get('values',{}).get(entity,'50' if entity.startswith(('sensor.','number.')) else 'auto'),
                    'attributes':{'options':['auto','battery_standby','charge_battery','discharge_battery','charge','discharge']},
                    'last_updated':self.remote.get('timestamp',datetime.now(timezone.utc).isoformat())}
        raise AssertionError((method,path))
    async def ws(self,command,**kwargs):
        if command in {'dwars_setup/status','dwars_setup/run'}:
            return {'status':'done','devices':self.remote['devices']}
        if command=='dwars_setup/enable':return {'enabled':kwargs['entities']}
        raise AssertionError(command)
    async def sup(self,method,path,payload=None,**kwargs):
        self.remote['calls'].append((method,path,deepcopy(payload)))
        if path=='/core/restart': self.remote['restarts']+=1;return {}
        if path=='/store/reload':return {}
        if path=='/addons/self/info':return {'slug':'test_dwars_installer'}
        if path=='/store/addons':return {'addons':[{'slug':'test_'+name} for name in AGENT.values()]}
        if path=='/addons':return {'addons':[{'slug':slug} for slug in self.remote['apps']]}
        if path.startswith('/store/addons/'):
            _,_,_,slug,action=path.split('/')
            if action=='install':
                base=slug.removeprefix('test_');cfg=load_json(ROOT/base/'config.json')
                self.remote['apps'][slug]={'state':'stopped','options':deepcopy(cfg['options']),'schema':cfg['schema'],'boot':'auto'}
                return {}
            raise AssertionError(action)
        if path.startswith('/addons/'):
            _,_,slug,action=path.split('/')
            if slug not in self.remote['apps']: raise APIError(404,'not installed')
            app=self.remote['apps'][slug]
            if action=='info':return deepcopy(app)
            if action=='options':
                self.assert_options_flat(payload)
                app.update(deepcopy(payload));return {}
            if action=='stop':app['state']='stopped';return {}
            if action=='start':app['state']='started';return {}
        raise AssertionError((method,path,payload))
    @staticmethod
    def assert_options_flat(payload):
        if 'options' in payload:
            assert 'options' not in payload['options'], 'nested options regression'
            assert all(not isinstance(v,(dict,list)) for v in payload['options'].values())


def remote_fixture(platform='goodwe'):
    d=device(platform,'GW123' if platform=='goodwe' else 'SE123')
    p=profile(platform)
    if platform=='other':
        d={'serial':'','entry_id':None,'entities':[]}
        p['agent_options']={'soc_entity':'sensor.soc','grid_entity':'sensor.grid','ha_mode_select':'select.mode','ha_power_number':'number.power'}
    return {'profile':p,'device':d,'devices':[d] if platform!='other' else [], 'calls':[], 'reports':[], 'apps':{}, 'processes':[], 'restarts':0}


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.remote=remote_fixture();self.one=Simulator(self.path,self.remote)
        self.one.credentials={'api_key':'a'*64};atomic_json(self.one.credentials_path,self.one.credentials)
    async def asyncTearDown(self):self.tmp.cleanup()
    async def run_pipeline(self,platform,restore=False):
        self.remote=remote_fixture(platform);one=Simulator(self.path,self.remote)
        with patch('oneshot.asyncio.sleep',new=AsyncMock()):
            for stage in STAGES[:-1]:
                one.save(stage=stage)
                await getattr(one,stage+'_stage')()
                one.save(stage=STAGES[STAGES.index(stage)+1])
                if restore:one=Simulator(self.path,self.remote)
        self.assertEqual(one.state['stage'],'complete')
        self.assertEqual(self.remote['restarts'],1)
        options=self.remote['apps']['test_'+AGENT[platform]]['options']
        self.assertEqual(options['api_key'],'a'*64)
        self.assertEqual(options['installation_id'],one.state['installation_id'])
        self.assertEqual(options.get('ha_token',options.get('hass_token','')),'')
        self.assertIn('telemetry_verified_at',one.state)
        self.assertNotIn('a'*64,json.dumps(one.public()))
        self.assertFalse(any('long_lived' in path for _,path,_ in self.remote['calls']))
    async def test_goodwe_pipeline(self):await self.run_pipeline('goodwe')
    async def test_solaredge_pipeline(self):await self.run_pipeline('solaredge')
    async def test_generic_profile_pipeline(self):await self.run_pipeline('other')
    async def test_resume_after_every_stage(self):await self.run_pipeline('goodwe',restore=True)
    async def test_restart_acknowledged_not_repeated(self):
        self.one.save(restart_needed=True,restart_requested_at=1,restart_acknowledged=True,restart_requests=1)
        await self.one.restart_stage();self.assertEqual(self.remote['restarts'],0)
    async def test_restart_lost_response_new_bridge_present(self):
        self.one.save(restart_needed=True,restart_requested_at=1,restart_acknowledged=False,restart_requests=1)
        await self.one.restart_stage();self.assertEqual(self.remote['restarts'],0)
        self.assertTrue(self.one.state['restart_acknowledged'])
    async def test_intent_saved_before_unsent_request_recovers(self):
        self.one.save(restart_needed=True,restart_requested_at=1,restart_acknowledged=False,restart_requests=1)
        self.one.bridge_stage=AsyncMock(side_effect=APIError(404,'not loaded'))
        with patch('oneshot.asyncio.sleep',new=AsyncMock()):await self.one.restart_stage()
        self.assertEqual(self.remote['restarts'],1)
    async def test_never_infinite_restart(self):
        self.one.save(restart_needed=True,restart_requested_at=1,restart_acknowledged=False,restart_requests=2)
        self.one.bridge_stage=AsyncMock(side_effect=APIError(404,'not loaded'))
        with self.assertRaises(Blocked):await self.one.restart_stage()
        self.assertEqual(self.remote['restarts'],0)
    async def test_restart_auth_error_not_swallowed(self):
        self.one.save(restart_needed=True);self.one.sup=AsyncMock(side_effect=APIError(403,'denied'))
        with self.assertRaises(APIError):await self.one.restart_stage()
    async def test_customer_change_blocked(self):
        self.one.save(client_id=999)
        with self.assertRaises(Blocked):await self.one.profile_stage()
        self.assertEqual(self.remote['calls'],[])
    async def test_other_key_never_updates_existing_agent(self):
        self.remote['apps']['test_goodwe_agent']={'options':{'api_key':'different'},'update_available':True}
        with self.assertRaises(Blocked):await self.one.ensure_agent('test_goodwe_agent')
        self.assertFalse(any('/update' in path for _,path,_ in self.remote['calls']))
    async def test_telemetry_from_wrong_serial_blocked(self):
        self.one.profile=profile();_,m=bind_device(profile(),[self.remote['device']])
        self.one.save(agent_slug='test_goodwe_agent',selected_device={'serial':'OTHER','entry_id':'other'},mapping=m)
        self.remote['apps']['test_goodwe_agent']={'state':'started'}
        with self.assertRaisesRegex(Blocked,'ander omvormerserienummer'):await self.one.verify_stage()
    async def test_stale_values_rejected(self):
        self.one.profile=profile();d=self.remote['device'];_,m=bind_device(profile(),[d])
        self.remote['timestamp']=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        with self.assertRaisesRegex(Blocked,'ouder dan'):await self.one.validate_entities(m,d)
    async def test_zero_grid_is_valid(self):
        self.one.profile=profile();d=self.remote['device'];_,m=bind_device(profile(),[d])
        self.remote['values']={m['grid_entity']:'0'}
        await self.one.validate_entities(m,d)
    async def test_nan_soc_rejected(self):
        self.one.profile=profile();d=self.remote['device'];_,m=bind_device(profile(),[d])
        self.remote['values']={m['soc_entity']:'nan'}
        with self.assertRaises(Blocked):await self.one.validate_entities(m,d)
    async def test_mapping_to_foreign_control_rejected(self):
        self.one.profile=profile();d=self.remote['device'];_,m=bind_device(profile(),[d]);m['ha_ems_mode_select']='select.foreign'
        with self.assertRaises(Blocked):await self.one.validate_entities(m,d)
    async def test_lock_contention(self):
        other=Simulator(self.path,self.remote)
        with self.one.maintenance_lock():
            with self.assertRaises(RuntimeError):
                with other.maintenance_lock():pass
    async def test_secret_redaction(self):
        with patch.dict(os.environ,{'SUPERVISOR_TOKEN':'super-secret'}):
            self.assertEqual(self.one.safe('a'*64+' super-secret'),'[afgeschermd] [afgeschermd]')
    async def test_two_controllers_rejected(self):
        self.one.profile=profile();self.remote['apps']['test_solaredge_agent']={'state':'started','options':{}}
        with self.assertRaisesRegex(Blocked,'andere DWARS-besturingsagent'):await self.one.agent_stage()
        self.assertFalse(any('/install' in path for _,path,_ in self.remote['calls']))


class IngressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient,TestServer
        self.tmp=tempfile.TemporaryDirectory();self.one=OneShot(self.tmp.name,ROOT/'dwars_installer')
        self.env=patch.dict(os.environ,{'DWARS_INGRESS_PROXY':'127.0.0.1'});self.env.start()
        app=self.one.application();app.on_startup.clear();app.on_cleanup.clear()
        self.client=TestClient(TestServer(app));await self.client.start_server()
    async def asyncTearDown(self):await self.client.close();self.env.stop();self.tmp.cleanup()
    async def test_post_requires_csrf(self):
        response=await self.client.post('/api/start',json={'api_key':'a'*64})
        self.assertEqual(response.status,403)
    async def test_start_persists_without_echo(self):
        response=await self.client.post('/api/start',json={'api_key':'a'*64},headers={'X-CSRF-Token':self.one.csrf})
        self.assertEqual(response.status,200);self.assertNotIn('a'*64,await response.text())
        self.assertEqual(load_json(self.one.credentials_path)['api_key'],'a'*64)
        state=await self.client.get('/api/status');self.assertNotIn('a'*64,await state.text())
    async def test_replace_bound_key_refused(self):
        self.one.save(client_id=123)
        response=await self.client.post('/api/start',json={'api_key':'a'*64},headers={'X-CSRF-Token':self.one.csrf})
        self.assertEqual(response.status,409)
    async def test_direct_container_access_refused(self):
        with patch.dict(os.environ,{'DWARS_INGRESS_PROXY':'172.30.32.2'}):
            response=await self.client.get('/api/status');self.assertEqual(response.status,403)
    async def test_page_has_ingress_prefix_and_password_field(self):
        response=await self.client.get('/',headers={'X-Ingress-Path':'/api/hassio_ingress/test'})
        html=await response.text();self.assertIn('type="password"',html);self.assertIn('/api/hassio_ingress/test',html)
        self.assertNotIn('__CSRF__',html)

if __name__=='__main__':unittest.main()
