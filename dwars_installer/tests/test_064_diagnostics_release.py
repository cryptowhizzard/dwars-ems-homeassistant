"""Bridge diagnostics, stale-payload protection and persisted 0.6.4 recovery."""
from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock
from test_setup_imports import source_function
from test_oneshot import Simulator,remote_fixture,ROOT
from oneshot_common import VERSION,Blocked,atomic_json,load_json

BRIDGE=ROOT/'custom_components/dwars_setup/__init__.py'
DOMAINS={'goodwe':'goodwe','solaredge':'solaredge_modbus_multi'}

class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def namespace(self):
        return {'asyncio':asyncio,'time':time,'DOMAIN':'dwars_setup','DOMAINS':DOMAINS,
                'DISCOVERY_TIMEOUT':.005,'_LOGGER':logging.getLogger('test-064-bridge'),
                'BRIDGE_VERSION':'1.2.0','HA_VERSION':'fixture-version','inventory':lambda h:[]}
    def hass(self,call=None):
        return NS(data={'dwars_setup':{}},config=NS(components={'goodwe'}),
            config_entries=NS(flow=NS(async_init=call or AsyncMock(return_value={'type':'abort','reason':'dwars_scan_complete'}))))
    async def test_scan_parent_is_system_source(self):
        h=self.hass();fn=source_function(BRIDGE,'_scan',self.namespace())
        await fn(h,{'platform':'goodwe','hosts':[],'unit_ids':[1]})
        self.assertEqual(h.config_entries.flow.async_init.call_args.kwargs['context'],{'source':'system'})
        self.assertEqual(h.data['dwars_setup']['status'],'done')
        self.assertEqual(h.data['dwars_setup']['phase'],'scan_finished')
    async def test_timeout_is_visible_and_not_an_empty_error(self):
        h=self.hass(AsyncMock(side_effect=lambda *a,**k:None))
        async def blocked(*a,**k):await asyncio.Event().wait()
        h.config_entries.flow.async_init=blocked
        await source_function(BRIDGE,'_scan',self.namespace())(h,{'platform':'goodwe'})
        d=h.data['dwars_setup'];self.assertEqual(d['status'],'error');self.assertEqual(d['phase'],'timeout')
        self.assertIn('niet afgerond',d['error']);self.assertIsNotNone(d['finished_monotonic'])
    async def test_cancellation_visible_not_success(self):
        h=self.hass(AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await source_function(BRIDGE,'_scan',self.namespace())(h,{'platform':'goodwe'})
        self.assertEqual(h.data['dwars_setup']['status'],'interrupted')
        self.assertTrue(h.data['dwars_setup']['error'])
    async def test_legacy_flow_abort_becomes_clear_bridge_error(self):
        h=self.hass(AsyncMock(return_value={'type':'abort','reason':'dwars_scan_requires_system'}))
        with self.assertLogs('test-064-bridge',level='ERROR'):
            await source_function(BRIDGE,'_scan',self.namespace())(h,{'platform':'goodwe'})
        self.assertEqual(h.data['dwars_setup']['status'],'error')
        self.assertIn('dwars_scan_requires_system',h.data['dwars_setup']['error'])
    async def test_status_includes_uninitialized_flows_without_raw_context(self):
        h=self.hass()
        h.data['dwars_setup'].update(status='running',phase='scanning_and_loading',started_monotonic=time.monotonic()-5,error='')
        h.config_entries.flow.async_progress_by_handler=Mock(side_effect=lambda domain,**kw:[{
            'flow_id':'test','context':{'source':'import','api_key':'must-not-export'},
            'data':{'password':'not-exported'}}] if domain=='goodwe' else [])
        c=NS(user=NS(is_admin=True),send_result=Mock(),send_error=Mock())
        await source_function(BRIDGE,'ws_status',self.namespace())(h,c,{'id':1})
        result=c.send_result.call_args.args[1]
        self.assertEqual(result['flows'],[{'domain':'goodwe','source':'import','step_id':''}])
        self.assertEqual(result['loaded_domains']['goodwe'],True)
        self.assertEqual(result['ha_version'],'fixture-version');self.assertGreaterEqual(result['elapsed_seconds'],5)
        self.assertNotIn('must-not-export',json.dumps(result));self.assertNotIn('not-exported',json.dumps(result))
        for call in h.config_entries.flow.async_progress_by_handler.call_args_list:
            self.assertTrue(call.kwargs['include_uninitialized'])
    async def test_status_is_admin_only(self):
        h=self.hass();c=NS(user=NS(is_admin=False),send_result=Mock(),send_error=Mock())
        await source_function(BRIDGE,'ws_status',self.namespace())(h,c,{'id':1})
        c.send_result.assert_not_called();c.send_error.assert_called_once()
    def test_not_loaded_inventory_includes_only_transport_whitelist(self):
        ns=self.namespace()
        ns['er']=NS(async_get=lambda h:None,async_entries_for_config_entry=lambda r,i:[])
        ns['dr']=NS(async_get=lambda h:None,async_entries_for_config_entry=lambda r,i:[])
        e=NS(entry_id='existing-id',unique_id='GW123',state=NS(value='not_loaded'),source='import',reason='',disabled_by=None,
            version=2,minor_version=3,data={'host':'192.0.2.10','port':502,'protocol':'TCP','password':'private'},
            options={'network_timeout':5,'api_key':'private-key'})
        h=NS(config=NS(components=set()),config_entries=NS(async_entries=lambda d:[e] if d=='goodwe' else []))
        rows=source_function(BRIDGE,'inventory',ns)(h)
        self.assertEqual(rows[0]['state'],'not_loaded');self.assertFalse(rows[0]['component_loaded'])
        self.assertEqual(rows[0]['connection'],{'host':'192.0.2.10','port':502,'protocol':'TCP','network_timeout':5})
        self.assertNotIn('private',json.dumps(rows));self.assertEqual(rows[0]['entities'],[])

class ReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name);self.remote=remote_fixture()
        self.one=Simulator(self.path,self.remote);self.one.profile=self.remote['profile']
    async def asyncTearDown(self):self.tmp.cleanup()
    async def test_upgrade_restarts_even_if_new_files_are_already_on_disk(self):
        self.one.save(stage='discover',installer_version='0.6.3',payload_root=str(ROOT),payload_version='0.6.3',
                      restart_requests=1,restart_acknowledged=True,restart_needed=False)
        key='test-key-saved-once';self.one.credentials={'api_key':key};atomic_json(self.one.credentials_path,self.one.credentials)
        self.one.prepare_release()
        self.assertEqual(self.one.state['stage'],'payload');self.assertTrue(self.one.state['restart_needed'])
        self.assertEqual(self.one.state['restart_requests'],0)
        self.assertEqual(load_json(self.one.credentials_path),{'api_key':key})
        self.one.save(restart_requests=1,restart_acknowledged=True)
        self.one.prepare_release()
        self.assertEqual(self.one.state['restart_requests'],1,'repeat must not reset the restart limit')
        self.assertTrue((self.path/('oneshot_before_'+VERSION+'.json')).exists())
    async def test_running_old_bridge_is_rejected(self):
        self.one.ws=AsyncMock(return_value={'bridge_version':'1.1.0'})
        with self.assertRaisesRegex(Blocked,'oude installatiebrug'):await self.one.bridge_stage()
        self.assertFalse(any(c.args[0]=='dwars_setup/run' for c in self.one.ws.call_args_list))
    def test_old_goodwe_or_bridge_payload_is_rejected_before_copy(self):
        for component,version in [('goodwe','0.9.9.36'),('dwars_setup','1.1.0')]:
            root=self.path/component
            for name,new in [('goodwe','0.9.9.37'),('dwars_setup','1.2.0')]:
                d=root/'custom_components'/name;d.mkdir(parents=True,exist_ok=True)
                (d/'manifest.json').write_text(json.dumps({'version':version if name==component else new}))
            with self.assertRaisesRegex(Blocked,component):self.one.check_payload(root)
    def test_old_solaredge_bulk_route_also_requires_new_payload(self):
        self.one.profile['platform']='solaredge';root=self.path/'old-se'
        for name,version in [('dwars_setup','1.2.0'),('solaredge_modbus_multi','3.2.7')]:
            d=root/'custom_components'/name;d.mkdir(parents=True);(d/'manifest.json').write_text(json.dumps({'version':version}))
        with self.assertRaisesRegex(Blocked,'solaredge_modbus_multi'):self.one.check_payload(root)
    def test_current_release_payload_is_accepted(self):self.one.check_payload(ROOT)
    def test_unselected_role_is_not_misreported_as_monitoring(self):
        self.one.save_devices([{'platform':'goodwe','serial':'GW123','host':'192.0.2.10','state':'not_loaded'}])
        self.assertEqual(self.one.state['devices'][0]['role'],'nog niet bepaald')

if __name__=='__main__':unittest.main()
