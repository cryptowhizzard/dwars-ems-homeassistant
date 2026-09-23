"""Production flow methods with a HA import-barrier lifecycle contract model.

This is NOT Home Assistant or an inverter emulator. Device communication and
registries are doubles. Unlike the previous tests, adding the first config entry
waits for *all pending imports of that domain*, just as HA Core 2026.9.0 does:
  homeassistant/config_entries.py:1392-1397,1441-1442,1516-1523,1615-1619,2076-2087
  homeassistant/setup.py:423-445
Sources: https://raw.githubusercontent.com/home-assistant/core/2026.9.0/homeassistant/config_entries.py
         https://raw.githubusercontent.com/home-assistant/core/2026.9.0/homeassistant/setup.py
The original 0.6.3 bridge and integration methods can be exercised via --baseline.
"""
from __future__ import annotations
import argparse
import ast
import asyncio
from collections import defaultdict
from copy import deepcopy
import json
import logging
from pathlib import Path
import sys
import time
from types import SimpleNamespace as NS, MethodType
import unittest
from unittest.mock import AsyncMock, Mock

from test_setup_imports import source_function
from test_063_goodwe_configuration import make_flow, entry, ET

ROOT=Path(__file__).resolve().parents[2]
DOMAINS={'goodwe':'goodwe','solaredge':'solaredge_modbus_multi'}

class ImportBarrierModel:
    """Small model of the upstream barrier, not a full HA ConfigEntries mock."""
    def __init__(self,root=ROOT,platform='goodwe',entries=None,count=1,loaded=False):
        self.root=root;self.platform=platform;self.domain=DOMAINS[platform]
        self.entries=list(entries or []);self.count=count
        self.pending=defaultdict(dict);self.next_id=0;self.calls=[];self.tasks=[]
        self.waiting=asyncio.Event();self.mutations=[];self.reloads=[];self.probes=[]
        self.components={self.domain} if loaded else set()
        self.hass=NS(data={'dwars_setup':{'status':'idle'}},config=NS(components=self.components))
        self.registry=NS(flow=self,async_entries=lambda domain:list(self.entries),
            async_update_entry=self.update,async_reload=self.reload,
            async_schedule_reload=self.schedule_reload,async_setup=self.setup)
        self.hass.config_entries=self.registry
        self.hass.async_create_task=self.schedule_task

    def schedule_task(self,coro,*args,**kwargs):
        task=asyncio.create_task(coro);self.tasks.append(task);return task
    def update(self,obj,**kw):
        self.mutations.append(kw)
        for key,value in kw.items():setattr(obj,key,value)
    def schedule_reload(self,entry_id):
        self.schedule_task(self.reload(entry_id))
    async def reload(self,entry_id):
        self.reloads.append(entry_id)
        e=next(e for e in self.entries if e.entry_id==entry_id)
        e.state='not_loaded'
        return await self.setup(entry_id)
    async def setup(self,entry_id):
        # HA first-domain setup waits pending imports BEFORE marking the domain
        # loaded and BEFORE starting each entry's async_setup_entry.
        if self.domain not in self.components:
            futures=list(self.pending[self.domain].values())
            if futures:
                if any(not f.done() for f in futures):self.waiting.set()
                await asyncio.wait(futures)
            self.components.add(self.domain)
        e=next(e for e in self.entries if e.entry_id==entry_id)
        e.state='setup_in_progress'
        await asyncio.sleep(0)
        e.state='loaded';e.entities=['sensor.fixture_soc']
        return True
    def new_flow(self):
        if self.platform=='goodwe':
            flow,ns,inv=make_flow(self.entries,root=self.root/'custom_components/goodwe')
            self.probes.append(ns['async_connect_and_detect_port'])
            def candidates():
                return {f'GW{i}':NS(host=f'192.0.2.{i+10}',protocol='TCP',port=502,
                    model_family='ET',mac=None,serial_number=f'GW{i}') for i in range(self.count)
                    if not any(e.unique_id==f'GW{i}' for e in self.entries)}
            flow._async_discover_unconfigured=AsyncMock(side_effect=lambda **kw:candidates())
            async def connect(**kw):
                serial=next((e.unique_id for e in self.entries if e.data.get('host')==kw['host']),None)
                serial=serial or 'GW'+str(int(kw['host'].split('.')[-1])-10)
                return ET(serial),502,'TCP'
            ns['async_connect_and_detect_port'].side_effect=connect
            path=self.root/'custom_components/goodwe/config_flow.py';cls='GoodweFlowHandler'
        else:
            path=self.root/'custom_components/solaredge_modbus_multi/config_flow.py';cls='SolaredgeModbusMultiConfigFlow'
            names={k:k.lower() for k in ('DEVICE_LIST','KEEP_MODBUS_OPEN','DETECT_METERS','DETECT_BATTERIES','DETECT_EXTRAS','ADV_PWR_CONTROL','ADV_STORAGE_CONTROL','ADV_SITE_LIMIT_CONTROL','SLEEP_AFTER_WRITE')}
            async def scan(hass,port,unit_id,limit):
                return [{'host':f'192.0.2.{i+10}','port':port,'mac':None} for i in range(self.count)] if port==1502 else []
            ns={'DOMAIN':self.domain,'CONF_HOST':'host','CONF_PORT':'port','CONF_NAME':'name',
                'CONF_MAC':'mac','CONF_SCAN_INTERVAL':'scan_interval','ConfName':NS(**names),
                'async_scan_solaredge_modbus':scan,'async_probe_solaredge_modbus':AsyncMock(return_value=True)}
            async def normalized(data,errors):
                data=deepcopy(data);data['device_list']=[int(i) for i in data['device_list'].split(',')]
                return data,data['host']+':'+str(data['port'])
            flow=NS(_async_validate_and_normalize=normalized,_find_existing_entry=lambda *a:None,
                    async_abort=lambda **kw:{'type':'abort',**kw},
                    async_create_entry=lambda **kw:{'type':'create_entry','version':2,'minor_version':2,**kw})
            flow.async_step_import=MethodType(source_function(path,'async_step_import',ns,cls),flow)
        tree=ast.parse(path.read_text());owner=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==cls)
        if any(isinstance(n,ast.AsyncFunctionDef) and n.name=='async_step_system' for n in owner.body):
            flow.async_step_system=MethodType(source_function(path,'async_step_system',ns,cls),flow)
        flow.hass=self.hass;flow.unique_id=None
        async def set_unique(value):flow.unique_id=value
        flow.async_set_unique_id=set_unique
        return flow
    async def async_init(self,handler,*,context,data):
        self.next_id+=1;flow_id=self.next_id;source=context['source']
        self.calls.append({'domain':handler,'source':source,'bulk':bool(data.get('dwars_discover'))})
        fut=asyncio.get_running_loop().create_future() if source=='import' else None
        if fut:self.pending[handler][flow_id]=fut
        flow=self.new_flow()
        try:
            result=await getattr(flow,'async_step_'+source)(data)
            if result['type']=='create_entry':
                # HA async_finish_flow releases this child's pending import
                # before async_add, but the parent import (0.6.3) stays pending.
                if fut and not fut.done():fut.set_result(None)
                e=NS(entry_id=f'fixture-entry-{flow_id}',unique_id=flow.unique_id,source=source,
                    state='not_loaded',reason='',entities=[],disabled_by=None,
                    data=deepcopy(result['data']),options=deepcopy(result.get('options',{})),
                    version=result['version'],minor_version=result['minor_version'])
                self.entries.append(e)
                await self.setup(e.entry_id)
                result['result']=e
            return result
        finally:
            if fut:
                if not fut.done():fut.set_result(None)
                self.pending[handler].pop(flow_id,None)
    async def scan(self):
        ns={'asyncio':asyncio,'time':time,'DOMAIN':'dwars_setup','DOMAINS':DOMAINS,
            '_LOGGER':logging.getLogger('barrier-reproduction'),'DISCOVERY_TIMEOUT':2}
        fn=source_function(self.root/'custom_components/dwars_setup/__init__.py','_scan',ns)
        await fn(self.hass,{'platform':self.platform,'hosts':[],'unit_ids':[1]})
    async def drain(self):
        if self.tasks:await asyncio.wait_for(asyncio.gather(*self.tasks),1)
    async def cancel_tasks(self):
        for t in self.tasks:
            if not t.done():t.cancel()
        if self.tasks:await asyncio.gather(*self.tasks,return_exceptions=True)

class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_goodwe_domain_load_no_pending_parent_import(self):
        m=ImportBarrierModel()
        await asyncio.wait_for(m.scan(),1);await m.drain()
        self.assertEqual(m.hass.data['dwars_setup']['status'],'done')
        self.assertEqual(m.entries[0].state,'loaded')
        self.assertEqual(m.calls,[{'domain':'goodwe','source':'system','bulk':True},
                                  {'domain':'goodwe','source':'import','bulk':False}])
        self.assertFalse(m.waiting.is_set())
    async def test_first_solaredge_domain_load_uses_same_safe_lifecycle(self):
        m=ImportBarrierModel(platform='solaredge')
        await asyncio.wait_for(m.scan(),1);await m.drain()
        self.assertEqual(m.entries[0].state,'loaded')
        self.assertEqual([c['source'] for c in m.calls],['system','import'])
    async def test_multiple_goodwe_entries_each_added_once(self):
        m=ImportBarrierModel(count=3)
        await asyncio.wait_for(m.scan(),1);await m.drain()
        self.assertEqual([e.unique_id for e in m.entries],['GW0','GW1','GW2'])
        self.assertTrue(all(e.state=='loaded' for e in m.entries))
    async def test_second_run_has_no_config_changes_or_reload(self):
        m=ImportBarrierModel()
        await m.scan();before=deepcopy(m.entries[0].data)
        await m.scan();await m.drain()
        self.assertEqual(len(m.entries),1);self.assertFalse(m.mutations);self.assertFalse(m.reloads)
        self.assertEqual(m.entries[0].data,before)
    async def test_never_loaded_entry_is_resumed_without_rewriting_connection(self):
        e=entry(source='import',state='not_loaded',serial='GW0',
                data={'host':'192.0.2.10','port':8899,'protocol':'UDP','model_family':'ET','dwars_managed':True},
                options={'network_timeout':9,'modbus_id':247,'scan_interval':23})
        before=(dict(e.data),dict(e.options));m=ImportBarrierModel(entries=[e],count=0)
        await asyncio.wait_for(m.scan(),1);await m.drain()
        self.assertEqual(e.state,'loaded');self.assertEqual((dict(e.data),dict(e.options)),before)
        self.assertEqual(m.mutations,[]);self.assertEqual(m.reloads,[e.entry_id])
        self.assertTrue(all(p.await_count==0 for p in m.probes))
    async def test_failed_import_reload_is_scheduled_after_flow_can_finish(self):
        e=entry(source='import',state='setup_error',serial='GW0',
                data={'host':'192.0.2.10','port':8899,'protocol':'UDP','model_family':'ET','dwars_managed':True})
        m=ImportBarrierModel(entries=[e],count=0)
        result=await asyncio.wait_for(m.async_init('goodwe',context={'source':'import'},
                           data={'host':'192.0.2.10','expected_serial':'GW0'}),1)
        await m.drain()
        self.assertEqual(result['reason'],'already_configured_inverter');self.assertEqual(e.state,'loaded')
        self.assertEqual(m.reloads,[e.entry_id])
    async def test_working_manual_entry_is_not_probed_mutated_or_reloaded(self):
        e=entry(source='user',state='loaded',serial='GW0');m=ImportBarrierModel(entries=[e],loaded=True)
        await m.scan();await m.drain()
        self.assertEqual(len(m.entries),1);self.assertFalse(m.mutations);self.assertFalse(m.reloads)
        self.assertTrue(all(p.await_count==0 for p in m.probes))
    async def test_user_disabled_import_not_loaded_or_reenabled(self):
        e=entry(source='import',state='not_loaded',serial='GW0',data={'host':'192.0.2.10','dwars_managed':True})
        e.disabled_by='user';m=ImportBarrierModel(entries=[e],count=0)
        await m.scan();await m.drain()
        self.assertEqual(e.state,'not_loaded');self.assertEqual(e.disabled_by,'user')
        self.assertFalse(m.mutations);self.assertFalse(m.reloads)
    async def test_active_setup_and_retry_are_not_repaired_concurrently(self):
        for state in ('setup_in_progress','setup_retry','unload_in_progress','failed_unload'):
            e=entry(source='import',state=state,serial='GW0',data={'host':'192.0.2.10','dwars_managed':True})
            m=ImportBarrierModel(entries=[e],count=0)
            await m.scan();await m.drain()
            self.assertEqual(e.state,state);self.assertFalse(m.mutations);self.assertFalse(m.reloads)
            self.assertTrue(all(p.await_count==0 for p in m.probes))
    async def test_legacy_bridge_bulk_import_fails_explicitly_not_deadlocks(self):
        for platform in DOMAINS:
            m=ImportBarrierModel(platform=platform)
            result=await asyncio.wait_for(m.async_init(m.domain,context={'source':'import'},data={'dwars_discover':True}),1)
            self.assertEqual(result['reason'],'dwars_scan_requires_system');self.assertFalse(m.entries)
    async def test_empty_system_flow_does_not_start_discovery(self):
        for platform in DOMAINS:
            m=ImportBarrierModel(platform=platform)
            result=await m.async_init(m.domain,context={'source':'system'},data={})
            self.assertEqual(result['reason'],'cannot_connect');self.assertFalse(m.entries)

async def before_after(baseline,output):
    report={'kind':'production-methods-with-HA-import-barrier-contract-model',
            'real_home_assistant_tested':False,'physical_inverter_tested':False,'results':[]}
    for label,root in [('0.6.3',baseline),('0.6.4',ROOT)]:
        for platform in DOMAINS:
            m=ImportBarrierModel(root=root,platform=platform)
            task=asyncio.create_task(m.scan());timed_out=False
            try:await asyncio.wait_for(asyncio.shield(task),.25)
            except asyncio.TimeoutError:timed_out=True
            snapshot={'release':label,'platform':platform,'operation':'first-domain bulk discovery',
                'deadlock_reproduced':timed_out and m.waiting.is_set(),
                'status':m.hass.data['dwars_setup']['status'],
                'states':[e.state for e in m.entries],
                'entity_counts':[len(getattr(e,'entities',[])) for e in m.entries],
                'flows':m.calls}
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True);await m.cancel_tasks()
            report['results'].append(snapshot)
    for label,root in [('0.6.3',baseline),('0.6.4',ROOT)]:
        e=entry(source='import',state='setup_error',serial='GW0',data={'host':'192.0.2.10','port':8899,'protocol':'UDP','model_family':'ET','dwars_managed':True})
        m=ImportBarrierModel(root=root,entries=[e],count=0)
        task=asyncio.create_task(m.async_init('goodwe',context={'source':'import'},data={'host':'192.0.2.10','expected_serial':'GW0'}))
        timed_out=False
        try:await asyncio.wait_for(asyncio.shield(task),.25);await m.drain()
        except asyncio.TimeoutError:timed_out=True
        report['results'].append({'release':label,'platform':'goodwe','operation':'failed-entry repair before first-domain setup',
            'deadlock_reproduced':timed_out and m.waiting.is_set(),'states':[e.state]})
        if not task.done():task.cancel()
        await asyncio.gather(task,return_exceptions=True);await m.cancel_tasks()
    Path(output).write_text(json.dumps(report,indent=2)+'\n')
    for row in report['results']:
        assert row['deadlock_reproduced']==(row['release']=='0.6.3'),row
    return report

if __name__=='__main__':
    if '--baseline' in sys.argv:
        p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
        a=p.parse_args();print(json.dumps(asyncio.run(before_after(a.baseline,a.output)),indent=2))
    else:unittest.main()
