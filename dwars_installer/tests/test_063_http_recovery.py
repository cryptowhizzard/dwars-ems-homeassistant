"""Real local HTTP/WebSocket transport; external HA/BMS/Modbus are contract doubles."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import TestClient, TestServer

import test_062_regressions as earlier
from test_063_resume import literal_device as manual_device
from test_oneshot import Simulator, remote_fixture
from oneshot_common import atomic_json, bind_device


class ManualRecoveryWorkerTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the fixture, not the previous TestCase's tests.
    asyncSetUp = earlier.WorkerHTTPTests.asyncSetUp
    asyncTearDown = earlier.WorkerHTTPTests.asyncTearDown
    fixture = earlier.WorkerHTTPTests.fixture
    installer = earlier.WorkerHTTPTests.installer
    finish = earlier.WorkerHTTPTests.finish

    async def test_resume_entire_worker_after_manual_integration_and_agent_install(self):
        remote=await self.fixture(leftovers=False)
        remote.discovered=True
        remote.devices=[manual_device()]
        remote.entries=[{'entry_id':'bridge','domain':'dwars_setup'},
                        {'entry_id':'manual-entry-new','domain':'goodwe'}]
        agent=remote.new_app('goodwe_agent')
        _,mapping=bind_device(remote.profile,remote.devices)
        agent['options'].update(mapping)
        agent['options'].update(api_key='customer-key',client_id=str(remote.profile['client_id']),
            goodwe_serial_number='GW123',power_watt=9000,goodwe_default_dod=84,
            goodwe_default_dod_on_grid=83,main_fuse_profile='3x25_plus')
        agent['state']='started'
        remote.apps['repo_goodwe_agent']=agent
        before=deepcopy(agent['options'])
        atomic_json(self.path/'oneshot_credentials.json',{'api_key':'customer-key'})
        one=self.installer(remote)
        # Use the installer's own paths to avoid depending on a private filename.
        one.credentials={'api_key':'customer-key'}
        atomic_json(one.credentials_path,one.credentials)
        one.save(stage='mapping',status='waiting',
                 selected_device={'serial':'GW123','entry_id':'old-removed-entry'},
                 mapping={'soc_entity':'sensor.old_removed'})
        with redirect_stdout(io.StringIO()):
            await self.finish(one)
        self.assertEqual(one.state['selected_device']['entry_id'],'manual-entry-new')
        self.assertEqual(one.state['mapping']['soc_entity'],'sensor.accu_percentage')
        self.assertFalse(any(msg['type']=='dwars_setup/run' for msg in remote.messages))
        self.assertFalse(any(path.endswith('/install') for _,path,_ in remote.calls))
        self.assertEqual(remote.restarts,0)
        after=remote.apps['repo_goodwe_agent']['options']
        self.assertEqual({k for k in after if before.get(k)!=after[k]}, {'installation_id'})
        self.assertIn('telemetry_verified_at',one.state)
        self.assertEqual(remote.receipts,1)


class UIRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.remote=remote_fixture();self.remote['devices']=[manual_device()]
        self.one=Simulator(self.path,self.remote);self.one.profile=self.remote['profile']
        self.one.mode='oneshot';self.one.credentials={'api_key':'private-client-key'}
        self.one.token=lambda:'private-supervisor-token'
        self.one.stop_maintenance=AsyncMock()
        self.env=patch.dict(os.environ,{'DWARS_INGRESS_PROXY':'127.0.0.1'});self.env.start()
        app=self.one.application();app.on_startup.clear();app.on_cleanup.clear()
        self.client=TestClient(TestServer(app));await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close();self.env.stop();self.tmp.cleanup()
    async def test_diagnostics_whitelist_and_credentials_redacted(self):
        self.one.options['goodwe_agent_api_key']='private-legacy-key'
        self.one.save(stage='mapping',message='private-client-key private-supervisor-token private-legacy-key')
        d=self.remote['devices'][0]
        d.update(reason='private-client-key echoed',options={'password':'arbitrary-password'},
                 raw_data={'secret':'raw-secret'})
        d['entities'][0]['attributes']={'sensitive':'attribute-secret'}
        response=await self.client.get('/api/diagnostics')
        self.assertEqual(response.status,200)
        text=await response.text();payload=json.loads(text)
        for secret in ('private-client-key','private-supervisor-token','private-legacy-key',
                       'arbitrary-password','raw-secret','attribute-secret'):
            self.assertNotIn(secret,text)
        self.assertEqual(payload['devices'][0]['entry_id'],'manual-entry-new')
        self.assertEqual(payload['devices'][0]['state'],'loaded')
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertIn('attachment;',response.headers['Content-Disposition'])
    async def test_diagnostics_inventory_outage_does_not_leak_token(self):
        self.one.ws=AsyncMock(side_effect=RuntimeError('denied private-supervisor-token'))
        response=await self.client.get('/api/diagnostics')
        self.assertEqual(response.status,200)
        body=await response.json()
        self.assertEqual(body['inventory_error'],'denied [afgeschermd]')
    async def test_diagnostics_only_accessible_through_ingress(self):
        with patch.dict(os.environ,{'DWARS_INGRESS_PROXY':'172.30.32.2'}):
            response=await self.client.get('/api/diagnostics')
        self.assertEqual(response.status,403)
    async def test_discovery_requires_csrf(self):
        response=await self.client.post('/api/discover')
        self.assertEqual(response.status,403)
    async def test_retry_complete_goes_to_mapping_without_forced_discovery(self):
        self.one.save(stage='complete',status='complete')
        response=await self.client.post('/api/retry',headers={'X-CSRF-Token':self.one.csrf})
        self.assertEqual(response.status,200)
        self.assertEqual(self.one.state['stage'],'mapping')
        self.assertFalse(self.one.state.get('force_discovery'))
    async def test_explicit_discovery_only_marks_separate_action(self):
        self.one.save(stage='mapping',status='blocked')
        response=await self.client.post('/api/discover',headers={'X-CSRF-Token':self.one.csrf})
        self.assertEqual(response.status,200)
        self.assertEqual(self.one.state['stage'],'discover')
        self.assertTrue(self.one.state['force_discovery'])
    async def test_discovery_during_worker_denied(self):
        self.one.save(stage='agent',status='running')
        response=await self.client.post('/api/discover',headers={'X-CSRF-Token':self.one.csrf})
        self.assertEqual(response.status,409)
        self.assertEqual(self.one.state['stage'],'agent')
    async def test_diagnostics_and_discovery_links_support_ingress_prefix(self):
        response=await self.client.get('/',headers={'X-Ingress-Path':'/api/hassio_ingress/test-prefix'})
        self.assertEqual(response.status,200)
        html=await response.text()
        self.assertIn('/api/hassio_ingress/test-prefix',html)
        self.assertIn('/api/diagnostics',html)
        self.assertIn("post('discover',{})",html)
        self.assertIn("base+'/api/'+path",html)


if __name__=='__main__':unittest.main()
