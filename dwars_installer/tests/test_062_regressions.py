"""Regression for 0.6.1 log: key -> manual -> both -> stopped agents -> false success.

Uses real shell, curl, aiohttp request parsing and WebSocket transport against
local contract doubles. Not a real Home Assistant, BMS, Docker or inverter test.
The complete worker is exercised; only external Core-recovery subprocess,
GitHub download (cached payload) and BMS HTTPS transport origin are substituted.
"""
from __future__ import annotations
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer, TestClient
import test_install_bootstrap as catalog_tests
from test_install_bootstrap import SupervisorFixture, schema_error
from test_oneshot import profile, device

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'dwars_installer'))
from oneshot import OneShot, APIError
from oneshot_common import AGENT, DOMAIN, Blocked, atomic_json, load_json, legacy_api_key


class KeyTests(unittest.TestCase):
    def test_single_old_key_is_reused(self):
        self.assertEqual(legacy_api_key({'goodwe_agent_api_key': '  customer-key  '}), 'customer-key')
    def test_identical_keys_across_platforms_are_unambiguous(self):
        self.assertEqual(legacy_api_key({'goodwe_agent_api_key':'customer-key', 'solaredge_agent_api_key':'customer-key'}), 'customer-key')
    def test_conflicting_keys_rejected(self):
        with self.assertRaisesRegex(Blocked, 'verschillende'):
            legacy_api_key({'goodwe_agent_api_key':'customer-one', 'solaredge_agent_api_key':'customer-two'})
    def test_invalid_values_are_not_stringified(self):
        for value in ({'key':'nested'}, [], 123, 'short', 'key\nnewline'):
            with self.subTest(value=value), self.assertRaises(Blocked):
                legacy_api_key({'goodwe_agent_api_key': value})


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)
        self.fixture=SupervisorFixture()
        self.one=OneShot(self.path,ROOT/'dwars_installer')
        self.one.options['goodwe_agent_api_key']='customer-key'
        self.one.supervisor=self.fixture.url; self.one.token=lambda:'test-supervisor-token'
        self.one.session=ClientSession()
    async def asyncTearDown(self):
        await self.one.session.close(); self.fixture.close(); self.tmp.cleanup()
    def app(self, key='customer-key', state='stopped'):
        return {'slug':'repo_goodwe_agent', 'version':'1.8.7', 'state':state, 'options':{'api_key':key}}
    async def test_exact_key_plus_stopped_agent_no_hardware_migrates(self):
        self.fixture.apps['repo_goodwe_agent']=self.app()
        self.fixture.apps['repo_solaredge_agent']=self.app(key='')
        (self.path/'dwars_auto_update_state.json').write_text('{}')
        output=io.StringIO()
        with redirect_stdout(output): await self.one.prepare_startup()
        self.assertEqual(self.one.mode,'oneshot')
        self.assertTrue(self.one.startup_ready)
        self.assertEqual(load_json(self.one.credentials_path),{'api_key':'customer-key'})
        self.assertNotIn('customer-key',output.getvalue())
        self.assertNotIn('customer-key',json.dumps(self.one.public()))
        self.assertFalse(any(m=='POST' for m,_,_ in self.fixture.calls))
    async def test_explicit_oneshot_also_adopts_existing_key(self):
        self.one.options['installation_mode']='oneshot'
        await self.one.prepare_startup()
        self.assertEqual(self.one.credentials['api_key'],'customer-key')
        self.assertEqual(self.fixture.calls,[])
    async def test_explicit_manual_remains_manual_without_key_copy(self):
        self.one.options['installation_mode']='manual'; self.one.mode='manual'
        await self.one.prepare_startup()
        self.assertEqual(self.one.mode,'manual'); self.assertEqual(self.one.credentials,{})
        self.assertFalse(self.one.credentials_path.exists())
    async def test_live_legacy_controller_is_not_recommissioned(self):
        self.fixture.apps['repo_goodwe_agent']=self.app(state='started')
        await self.one.prepare_startup()
        self.assertEqual(self.one.mode,'manual')
        self.assertEqual(self.one.legacy_platforms,['goodwe'])
        self.assertFalse(self.one.credentials_path.exists())
    async def test_deliberately_stopped_but_configured_inverter_is_protected(self):
        self.fixture.apps['repo_goodwe_agent']=self.app()
        self.fixture.entries=[{'domain':'goodwe','entry_id':'already-configured'}]
        await self.one.prepare_startup()
        self.assertEqual(self.one.mode,'manual')
        self.assertTrue(self.one.public()['can_use_saved_key'])
    async def test_probe_outage_never_falls_back_to_old_both_installer(self):
        self.fixture.fail_installed_list=True
        with self.assertRaises(APIError): await self.one.prepare_startup()
        self.assertFalse(self.one.startup_ready)
        self.assertEqual(self.one.mode,'oneshot')
        self.assertFalse(self.one.credentials_path.exists())
        self.assertFalse(any(m=='POST' for m,_,_ in self.fixture.calls))
    async def test_existing_oneshot_key_is_not_overwritten_by_old_field(self):
        self.one.credentials={'api_key':'oneshot-key'}
        atomic_json(self.one.credentials_path,self.one.credentials)
        await self.one.prepare_startup()
        self.assertEqual(self.one.credentials['api_key'],'oneshot-key')
    async def test_invalid_old_key_does_not_crash_error_redaction(self):
        self.one.options['goodwe_agent_api_key']={'nested':'bad'}
        self.assertEqual(self.one.safe('invalid key'),'invalid key')
        with self.assertRaises(Blocked):await self.one.prepare_startup()
    async def test_protected_legacy_maintenance_never_installs_second_platform(self):
        from types import SimpleNamespace
        self.one.mode='manual';self.one.legacy_platforms=['goodwe']
        with patch('oneshot.asyncio.create_subprocess_exec',new=AsyncMock(return_value=SimpleNamespace(returncode=None))):
            await self.one.start_maintenance()
        opts=load_json(self.path/'legacy_protected_updater_options.json')
        self.assertEqual(opts['inverter_type'],'goodwe')
        self.assertFalse(opts['install_agent_addons'])
        self.assertFalse(opts['configure_agent_addons'])
        self.one.maintenance=None
    async def test_ui_can_activate_manual_mode_with_saved_key_no_yaml(self):
        self.one.mode='manual';self.one.options['installation_mode']='manual'
        self.one.stop_maintenance=AsyncMock()
        app=self.one.application();app.on_startup.clear();app.on_cleanup.clear()
        with patch.dict(os.environ,{'DWARS_INGRESS_PROXY':'127.0.0.1'}):
            async with TestClient(TestServer(app)) as client:
                res=await client.post('/api/start',json={'use_saved_key':True},headers={'X-CSRF-Token':self.one.csrf})
                self.assertEqual(res.status,200,await res.text())
        self.assertEqual(self.one.mode,'oneshot')
        self.assertEqual(self.one.credentials['api_key'],'customer-key')
        saved=next(b for m,p,b in self.fixture.calls if p=='/addons/self/options')
        self.assertEqual(saved['options']['installation_mode'],'oneshot')
        self.one.stop_maintenance.assert_awaited_once()


class AgentOptionsTests(catalog_tests.ShellCatalogTests):
    """Inherited catalog tests also run with this stricter installed manifest."""
    def setUp(self):
        super().setUp()
        cfg=load_json(ROOT/'solaredge_agent/config.json')
        self.fixture.catalog.append({'slug':'repo_solaredge_agent','version':cfg['version']})
        self.fixture.apps['repo_solaredge_agent']={'slug':'repo_solaredge_agent','version':cfg['version'],
            'options':deepcopy(cfg['options']), 'schema':cfg['schema'], 'state':'stopped'}
        self.options.update(inverter_type='solaredge',configure_agent_addons=True,start_agent_addons=True,
                            solaredge_agent_api_key='customer-key')
        atomic_json(self.path/'options.json',self.options)
    # Only execute new tests on this subclass; catalog coverage is in its base.
    def test_complete_options_include_all_required_backup_yaml_fields(self):
        self.shell('configure_solaredge_agent repo_solaredge_agent')
        opts=self.fixture.apps['repo_solaredge_agent']['options']
        self.assertEqual(opts['api_key'],'customer-key')
        self.assertIn('backup_yaml_check_enabled',opts)
        self.assertIn('backup_yaml_path',opts);self.assertIn('backup_yaml_overwrite',opts)
        self.assertEqual(schema_error(opts,self.fixture.apps['repo_solaredge_agent']['schema']),'')
    def test_missing_required_fields_are_actually_rejected_by_fixture(self):
        cfg=self.fixture.apps['repo_solaredge_agent']
        self.assertIn('backup_yaml_path',schema_error({'api_key':'customer-key'},cfg['schema']))
    def test_valid_configuration_starts_and_confirms_state(self):
        self.shell("configure_and_start_agent repo_solaredge_agent 'SolarEdge' configure_solaredge_agent")
        self.assertEqual(self.fixture.apps['repo_solaredge_agent']['state'],'started')
    def test_http400_is_failed_cycle_not_success_and_no_start(self):
        self.fixture.fail_configure=True
        result=self.shell('if install_or_configure_agents; then echo FALSE_SUCCESS; else echo EXPECTED_FAILURE; fi')
        self.assertIn('EXPECTED_FAILURE',result.stdout); self.assertNotIn('FALSE_SUCCESS',result.stdout)
        self.assertIn('HTTP 400',result.stderr); self.assertIn('MISLUKT',result.stderr)
        self.assertFalse(any(p.endswith('/start') for _,p,_ in self.fixture.calls))
    def test_json_error_under_http200_is_not_success(self):
        self.fixture.result_error_200=True
        self.shell('configure_solaredge_agent repo_solaredge_agent',success=False)
    def test_start_failure_is_not_suppressed(self):
        self.fixture.fail_start=True
        self.shell("configure_and_start_agent repo_solaredge_agent 'SolarEdge' configure_solaredge_agent",success=False)
    def test_schema_validation_failure_prevents_start(self):
        self.options['configure_agent_addons']=False
        atomic_json(self.path/'options.json',self.options)
        self.fixture.apps['repo_solaredge_agent']['options'].pop('backup_yaml_path')
        self.shell("configure_and_start_agent repo_solaredge_agent 'SolarEdge' configure_solaredge_agent",success=False)
        self.assertFalse(any(p.endswith('/start') for _,p,_ in self.fixture.calls))
    def test_error_echo_is_redacted_including_nested_ha_token(self):
        self.fixture.fail_configure=True
        self.fixture.error_message='rejected customer-key hidden-ha-token test-supervisor-token'
        result=self.shell('supervisor_curl POST /addons/repo_solaredge_agent/options \'{"options":{"api_key":"customer-key","hass_token":"hidden-ha-token"}}\'',success=False)
        for value in ('customer-key','hidden-ha-token','test-supervisor-token'):
            self.assertNotIn(value,result.stderr+result.stdout)

# Prevent rerunning parent tests with deliberately different preinstalled apps.
for name in catalog_tests.ShellCatalogTests.__dict__:
    if name.startswith('test_') and name not in AgentOptionsTests.__dict__:
        setattr(AgentOptionsTests,name,None)


class ProvisioningFixture:
    def __init__(self,platform='goodwe',leftovers=True):
        self.platform=platform;self.profile=profile(platform)
        self.calls=[];self.messages=[];self.entries=[];self.discovered=False;self.restarts=0;self.receipts=0
        self.deny_profile=False;self.apps={};self.own_options={}
        self.cfgs={base:load_json(ROOT/base/'config.json') for base in AGENT.values()}
        self.devices=[device(platform, 'GW123' if platform=='goodwe' else 'SE123')]
        if platform=='goodwe': self.devices.insert(0,device('goodwe','PV123',False))
        if leftovers:
            for base in ('goodwe_agent','solaredge_agent'):
                info=self.new_app(base)
                if base==AGENT[platform]:info['options']['api_key']='customer-key'
                self.apps['repo_'+base]=info
    def new_app(self,base):
        cfg=self.cfgs[base]
        return {'slug':'repo_'+base,'version':cfg['version'],'state':'stopped', 'boot':'auto','watchdog':False,
                'options':deepcopy(cfg['options']),'schema':deepcopy(cfg['schema']),'update_available':False}
    async def websocket(self,request):
        socket=web.WebSocketResponse();await socket.prepare(request)
        await socket.send_json({'type':'auth_required','ha_version':'2026.fixture'})
        auth=await socket.receive_json()
        if auth.get('access_token')!='fixture-supervisor-token':
            await socket.send_json({'type':'auth_invalid'});return socket
        await socket.send_json({'type':'auth_ok','ha_version':'2026.fixture'})
        command=await socket.receive_json();self.messages.append(command)
        kind=command['type']
        if kind=='dwars_setup/run':self.discovered=True
        result={'status':'done' if self.discovered else 'idle','devices':self.devices if self.discovered else []}
        if kind=='dwars_setup/enable':result={'enabled':command['entities']}
        await socket.send_json({'id':command['id'],'type':'result','success':True,'result':result})
        await socket.close();return socket
    async def handler(self,request):
        method,path=request.method,request.path
        body=await request.json() if request.can_read_body else None
        self.calls.append((method,path,deepcopy(body)))
        if path.startswith('/bms/'):
            if request.headers.get('X-API-Key')!='customer-key' or self.deny_profile:
                return web.json_response({'ok':False,'message':'invalid key'},status=401)
            if path.endswith('/install_profile.php'):
                return web.json_response({'ok':True,'profile':self.profile})
            if path.endswith('/install_status.php'):
                return web.json_response({'ok':True,'receipt':{'receipt_count':self.receipts,
                    'inverter_serial':self.devices[-1]['serial'],'received_at':datetime.now(timezone.utc).isoformat()}})
        if path=='/core/websocket':return await self.websocket(request)
        if request.headers.get('Authorization')!='Bearer fixture-supervisor-token':
            return web.json_response({'message':'unauthorized'},status=401)
        if path=='/core/api/config':return web.json_response({'state':'RUNNING','version':'2026.fixture'})
        if path=='/core/api/config/config_entries/entry':
            rows=self.entries
            if request.query.get('domain'):rows=[e for e in rows if e['domain']==request.query['domain']]
            return web.json_response(rows)
        if path=='/core/api/config/config_entries/flow':return web.json_response({'type':'form','flow_id':'setup'})
        if path=='/core/api/config/config_entries/flow/setup':
            self.entries.append({'entry_id':'bridge','domain':'dwars_setup'})
            return web.json_response({'type':'create_entry','result':{'entry_id':'bridge'}})
        if path.startswith('/core/api/states/'):
            entity=path.split('/states/',1)[1]
            if not any(entity==e['entity_id'] for d in self.devices for e in d['entities']):
                return web.json_response({'message':'missing entity'},status=404)
            return web.json_response({'state':'50' if entity.startswith(('sensor.','number.')) else 'auto',
                'last_updated':datetime.now(timezone.utc).isoformat(),
                'attributes':{'options':['auto','charge_battery','discharge_battery','battery_standby']}})
        if path=='/addons/self/info':data={'slug':'repo_dwars_installer','options':self.own_options}
        elif path=='/addons/self/options':self.own_options=body['options'];data={}
        elif path=='/addons':data={'addons':[{'slug':slug} for slug in self.apps]}
        elif path=='/store/addons':data={'addons':[{'slug':'repo_'+base} for base in AGENT.values()]}
        elif path=='/store/reload':data={}
        elif path=='/core/restart':self.restarts+=1;data={}
        elif path.startswith('/store/addons/') and path.endswith('/install'):
            slug=path.split('/')[3];base=slug.removeprefix('repo_')
            if slug in self.apps:return web.json_response({'message':'already installed'},status=400)
            self.apps[slug]=self.new_app(base);data={}
        elif path.startswith('/addons/'):
            slug=path.split('/')[2]
            if slug not in self.apps:return web.json_response({'message':'not installed'},status=404)
            info=self.apps[slug];action=path.split('/')[3]
            if action=='info':data=info
            elif action=='options':
                if 'options' in body:
                    error=schema_error(body['options'],info['schema'])
                    if error:return web.json_response({'message':error},status=400)
                info.update(body);data={}
            elif action=='start':info['state']='started';self.receipts+=1;data={}
            elif action=='stop':info['state']='stopped';data={}
            else:return web.json_response({'message':'unsupported '+path},status=404)
        else:return web.json_response({'message':'unsupported '+path},status=404)
        return web.json_response({'result':'ok','data':data})


class WorkerHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        atomic_json(self.path/'options.json',{'installation_mode':'auto','goodwe_agent_api_key':'customer-key',
                                           'inverter_type':'both','start_agent_addons':False})
        self.original_sleep=asyncio.sleep
        async def no_delay(*args,**kwargs):await self.original_sleep(0)
        self.sleep_patch=patch('oneshot.asyncio.sleep',new=no_delay);self.sleep_patch.start()
        self.servers=[];self.ones=[]
    async def asyncTearDown(self):
        for one in self.ones:
            if one.session and not one.session.closed:await one.stop(None)
        for server in self.servers:await server.close()
        self.sleep_patch.stop();self.tmp.cleanup()
    async def fixture(self,platform='goodwe',leftovers=True):
        remote=ProvisioningFixture(platform,leftovers)
        app=web.Application();app.router.add_route('*','/{path:.*}',remote.handler)
        server=TestServer(app);await server.start_server();self.servers.append(server)
        remote.url=str(server.make_url('')).rstrip('/')
        return remote
    def installer(self,remote):
        one=OneShot(self.path,ROOT/'dwars_installer');self.ones.append(one)
        one.config_dir=self.path/'ha';one.config_dir.mkdir(exist_ok=True)
        one.supervisor=remote.url;one.token=lambda:'fixture-supervisor-token'
        # Use the supplied payload as a downloaded cache, not the public repo.
        one.state['payload_root']=str(ROOT)
        async def bms(name,payload=None,query=''):
            return await one.request('POST' if payload is not None else 'GET',remote.url+'/bms/'+name+query,
                                     payload,key=one.credentials['api_key'])
        one.bms=bms;one.subprocess=AsyncMock()
        done=asyncio.Event();one.start_maintenance=AsyncMock(side_effect=done.set)
        one.done=done
        return one
    async def finish(self,one):
        await one.start(None)
        await asyncio.wait_for(one.done.wait(),3)
        self.assertEqual(one.state['status'],'complete',one.state)
        await one.stop(None)
    async def test_reported_situation_runs_entire_worker_no_extra_key_or_mode(self):
        remote=await self.fixture();one=self.installer(remote)
        with redirect_stdout(io.StringIO()) as logs:await self.finish(one)
        self.assertEqual(one.mode,'oneshot')
        self.assertEqual(remote.restarts,1)
        self.assertTrue(remote.discovered)
        self.assertEqual(len(one.state['devices']),2)
        self.assertEqual(remote.apps['repo_goodwe_agent']['state'],'started')
        self.assertEqual(remote.apps['repo_goodwe_agent']['options']['goodwe_serial_number'],'GW123')
        self.assertEqual(remote.apps['repo_solaredge_agent']['state'],'stopped')
        self.assertEqual(remote.apps['repo_solaredge_agent']['boot'],'manual')
        self.assertEqual(load_json(one.credentials_path)['api_key'],'customer-key')
        self.assertIn('telemetry_verified_at',one.state)
        self.assertNotIn('customer-key',logs.getvalue());self.assertNotIn('fixture-supervisor-token',logs.getvalue())
        self.assertFalse(any(p.endswith('/install') for _,p,_ in remote.calls))
        self.assertTrue((one.config_dir/'custom_components/goodwe/manifest.json').exists())
        self.assertFalse((one.config_dir/'custom_components/solaredge_modbus_multi').exists())
    async def test_fresh_install_installs_only_goodwe_from_catalog(self):
        remote=await self.fixture(leftovers=False);one=self.installer(remote)
        with redirect_stdout(io.StringIO()):await self.finish(one)
        self.assertEqual(list(remote.apps),['repo_goodwe_agent'])
        self.assertEqual(sum(p.endswith('/install') for _,p,_ in remote.calls),1)
    async def test_platform_comes_from_bms_not_key_field_or_both_default(self):
        remote=await self.fixture('solaredge',leftovers=False);one=self.installer(remote)
        with redirect_stdout(io.StringIO()):await self.finish(one)
        self.assertEqual(list(remote.apps),['repo_solaredge_agent'])
        self.assertEqual(remote.apps['repo_solaredge_agent']['state'],'started')
        self.assertTrue((one.config_dir/'custom_components/solaredge_modbus_multi/manifest.json').exists())
        self.assertFalse((one.config_dir/'custom_components/goodwe').exists())
    async def test_interruption_after_core_restart_resumes_without_key_or_second_restart(self):
        remote=await self.fixture();one=self.installer(remote);request_restart=one._request_core_restart
        async def interrupt():
            await request_restart()
            raise asyncio.CancelledError()
        one._request_core_restart=interrupt
        with redirect_stdout(io.StringIO()):
            await one.start(None)
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(one.worker_task,3)
            await one.stop(None)
            resumed=self.installer(remote)
            self.assertEqual(resumed.credentials['api_key'],'customer-key')
            self.assertEqual(resumed.state['installation_id'],one.state['installation_id'])
            await self.finish(resumed)
        self.assertEqual(remote.restarts,1)
        self.assertEqual(len([e for e in remote.entries if e['domain']=='dwars_setup']),1)
    async def test_rejected_key_does_not_mutate_homeassistant_or_start_updater(self):
        remote=await self.fixture();remote.deny_profile=True;one=self.installer(remote)
        with redirect_stdout(io.StringIO()):
            await one.start(None)
            for _ in range(100):
                if one.state['status']=='blocked':break
                await self.original_sleep(.005)
            await one.stop(None)
        self.assertEqual(one.state['status'],'blocked')
        self.assertEqual(remote.restarts,0)
        self.assertFalse(any(m=='POST' and not p.startswith('/bms/') for m,p,_ in remote.calls))
        one.start_maintenance.assert_not_awaited()
    async def test_foreign_key_blocks_before_component_changes(self):
        remote=await self.fixture();remote.apps['repo_goodwe_agent']['options']['api_key']='different-customer'
        one=self.installer(remote)
        with redirect_stdout(io.StringIO()):
            await one.start(None)
            for _ in range(100):
                if one.state['status']=='blocked':break
                await self.original_sleep(.005)
            await one.stop(None)
        self.assertEqual(one.state['status'],'blocked')
        self.assertEqual(remote.restarts,0)
        self.assertFalse((one.config_dir/'custom_components').exists())
        one.start_maintenance.assert_not_awaited()


if __name__=='__main__':unittest.main()
