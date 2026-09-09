"""GoodWe schema/import regression tests using actual integration method bodies.

HA and the inverter library are *not installed* in this test environment. The
flow/registry/transport boundary below is a test double, not a physical device.
The production helpers, migration, successful-connection and import bodies run
unchanged together, including the persisted data consumed by the setup helper.
"""
from __future__ import annotations
import ast
import asyncio
from copy import deepcopy
from datetime import timedelta
import ipaddress
import logging
from pathlib import Path
from types import SimpleNamespace as NS, MethodType, MappingProxyType
import unittest
from unittest.mock import AsyncMock, Mock

from test_setup_imports import source_function
ROOT = Path(__file__).resolve().parents[2]
GW = ROOT / 'custom_components/goodwe'


def integration_namespace(root=GW):
    ns = {'Any': object, 'asyncio': asyncio, 'ipaddress': ipaddress, 'timedelta': timedelta,
          'InverterError': RuntimeError, '_LOGGER': logging.getLogger('goodwe-regression'),
          'GOODWE_TCP_PORT': 502, 'GOODWE_UDP_PORT': 8899}
    # Load the real constants, excluding only the external Platform enum list.
    tree = ast.parse((root / 'const.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.Assign)
             and not any(isinstance(t, ast.Name) and t.id == 'PLATFORMS' for t in n.targets)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(root/'const.py'), 'exec'), ns)
    for name in ('HOST', 'PORT', 'PROTOCOL', 'SCAN_INTERVAL'):
        ns['CONF_' + name] = name.lower()
    helper_names = ('default_port_for_protocol', 'build_updated_entry_data',
                    'build_updated_entry_options', 'entry_is_loaded',
                    'entry_is_dwars_managed', 'complete_entry_data', 'entry_connection_options')
    for name in helper_names:
        if ('def '+name+'(') in (root/'discovery.py').read_text():
            source_function(root/'discovery.py', name, ns)
    source_function(root/'config_flow.py', '_normalise_serial', ns)
    source_function(root/'config_flow.py', '_entry_value', ns)
    ns['resolve_network_cidr'] = lambda *args: None
    ns['normalize_mac'] = lambda value: value
    return ns


def flow_versions(root=GW):
    cls = next(n for n in ast.parse((root/'config_flow.py').read_text()).body
               if isinstance(n, ast.ClassDef) and n.name=='GoodweFlowHandler')
    # HA FlowHandler defaults VERSION and MINOR_VERSION to 1.
    versions = {'VERSION': 1, 'MINOR_VERSION': 1}
    for n in cls.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id in versions:
                    versions[t.id] = ast.literal_eval(n.value)
    return versions['VERSION'], versions['MINOR_VERSION']


class ET:
    def __init__(self, serial='GW123', runtime=None):
        self.serial_number=serial; self.model_name='GW10K-ET'
        self.set_keep_alive=Mock()
        self.read_runtime_data=AsyncMock(return_value={'battery_soc':50,'active_power':0}
                                         if runtime is None else runtime)


def entry(source='user', state='loaded', data=None, options=None, serial='GW123'):
    return NS(entry_id='ha-entry-existing', unique_id=serial, source=source, state=state,
              disabled_by=None, version=2, minor_version=1,
              data=MappingProxyType(data or {'host':'192.0.2.10','port':8899,'protocol':'UDP','model_family':'ET'}),
              options=MappingProxyType(options or {}))


def make_flow(entries=(), inverter=None, root=GW):
    ns=integration_namespace(root)
    inv=inverter or ET()
    ns['async_connect_and_detect_port']=AsyncMock(return_value=(inv,502,'TCP'))
    async def update_entry(e, **kw):  # no coroutine is used for registry writes
        pass
    def update(e, **kw):
        for key,value in kw.items():setattr(e,key,MappingProxyType(value) if key in {'data','options'} else value)
    registry=NS(async_entries=lambda domain:list(entries),
                async_update_entry=Mock(side_effect=update), async_reload=AsyncMock(return_value=True),
                flow=NS(async_init=AsyncMock(return_value={'type':'create_entry'})))
    flow=NS(hass=NS(config_entries=registry,async_create_task=Mock()),
            async_set_unique_id=AsyncMock(), _abort_if_unique_id_configured=Mock(),
            async_abort=lambda **kw:{'type':'abort',**kw})
    def create(**kw):
        major,minor=flow_versions(root)
        return {'type':'create_entry','version':major,'minor_version':minor,**kw}
    flow.async_create_entry=Mock(side_effect=create)
    flow._async_discover_unconfigured=AsyncMock(return_value={})
    for name in ('_configured_entries','_entry_for_serial','async_handle_successful_connection','async_step_import'):
        fn=source_function(root/'config_flow.py',name,ns,'GoodweFlowHandler')
        setattr(flow,name,MethodType(fn,flow))
    return flow,ns,inv


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_major_and_minor_version_match_migration(self):
        self.assertEqual(flow_versions(),(2,3))
    async def test_full_creation_uses_real_shared_data_builder(self):
        f,ns,inv=make_flow()
        result=await f.async_step_import({'host':'192.0.2.10','expected_serial':'GW123'})
        self.assertEqual(result['type'],'create_entry')
        self.assertEqual((result['version'],result['minor_version']),(2,3))
        self.assertEqual(result['data']['model_family'],'ET')
        self.assertEqual(result['data']['protocol'],'TCP');self.assertEqual(result['data']['port'],502)
        self.assertEqual(result['data']['network_timeout'],2)
        self.assertEqual(result['data']['network_retries'],10)
        self.assertEqual(result['data']['modbus_id'],0)
        self.assertEqual(result['data']['scan_interval'],5)
        self.assertIs(result['data']['keep_alive'],False)
        self.assertIs(result['data']['dwars_managed'],True)
        self.assertIs(result['data']['auto_load_control'],False)
        inv.read_runtime_data.assert_awaited_once()
        connection=ns['entry_connection_options'](result['data'],result['options'])
        self.assertEqual(connection,{'protocol':'TCP','port':502,'family':'ET','comm_addr':0,'timeout':2,'retries':10})
    async def test_manual_path_produces_same_connection_shape(self):
        f,ns,inv=make_flow()
        result=await f.async_handle_successful_connection(inv,'192.0.2.10',8899,'UDP')
        self.assertEqual(result['version'],2)
        self.assertEqual(ns['entry_connection_options'](result['data'],{})['protocol'],'UDP')
        self.assertTrue(result['data']['auto_load_control'])
    async def test_identity_only_is_not_enough(self):
        f,ns,inv=make_flow(inverter=ET(runtime={}))
        result=await f.async_step_import({'host':'192.0.2.10'})
        self.assertEqual(result['reason'],'cannot_read_runtime')
        f.async_create_entry.assert_not_called()
        self.assertEqual(inv.read_runtime_data.await_count,2)
    async def test_runtime_failure_tries_other_transport_before_persisting(self):
        tcp,udp=ET(),ET()
        tcp.read_runtime_data.side_effect=RuntimeError('read failed')
        f,ns,_=make_flow(inverter=tcp)
        ns['async_connect_and_detect_port'].side_effect=[(tcp,502,'TCP'),(udp,8899,'UDP')]
        result=await f.async_step_import({'host':'192.0.2.10','protocol':'TCP','port':502})
        self.assertEqual(result['data']['protocol'],'UDP')
        self.assertEqual(result['data']['port'],8899)
        self.assertEqual(ns['entry_connection_options'](result['data'],result['options'])['port'],8899)
    async def test_changed_identity_does_not_create_device(self):
        f,ns,inv=make_flow(inverter=ET('DIFFERENT'))
        result=await f.async_step_import({'host':'192.0.2.10','expected_serial':'GW123'})
        self.assertEqual(result['reason'],'identity_changed')
        f.async_create_entry.assert_not_called();inv.read_runtime_data.assert_not_awaited()
    async def test_existing_loaded_manual_not_probed_or_mutated(self):
        e=entry(options={'port':8899,'protocol':'UDP','modbus_id':247,'keep_alive':True,'scan_interval':20,'network_timeout':5})
        before=(dict(e.data),dict(e.options))
        f,ns,_=make_flow([e])
        result=await f.async_step_import({'host':'192.0.2.10','port':502,'protocol':'TCP','expected_serial':'GW123'})
        self.assertEqual(result['reason'],'already_configured_inverter')
        ns['async_connect_and_detect_port'].assert_not_awaited()
        f.hass.config_entries.async_update_entry.assert_not_called()
        f.hass.config_entries.async_reload.assert_not_awaited()
        self.assertEqual((dict(e.data),dict(e.options)),before)
    async def test_loaded_import_not_reconfigured_either(self):
        e=entry(source='import',data={'host':'192.0.2.10','auto_load_control':False})
        f,ns,_=make_flow([e]);await f.async_step_import({'host':'192.0.2.10'})
        f.hass.config_entries.async_update_entry.assert_not_called()
        ns['async_connect_and_detect_port'].assert_not_awaited()
    async def test_unloaded_manual_not_silently_repaired(self):
        e=entry(state='setup_error')
        f,ns,_=make_flow([e]);await f.async_step_import({'host':'192.0.2.10'})
        f.hass.config_entries.async_update_entry.assert_not_called()
        f.hass.config_entries.async_reload.assert_not_awaited()
    async def test_disabled_import_not_reenabled(self):
        e=entry(source='import',state='not_loaded',data={'host':'192.0.2.10','auto_load_control':False})
        e.disabled_by='user'
        f,ns,_=make_flow([e]);await f.async_step_import({'host':'192.0.2.10'})
        ns['async_connect_and_detect_port'].assert_not_awaited()
    async def test_failed_own_import_repaired_only_after_runtime_read(self):
        e=entry(source='import',state='setup_error',data={'host':'192.0.2.10','port':8899,'protocol':'UDP','auto_load_control':False},
                options={'port':8899,'protocol':'UDP','modbus_id':247,'network_timeout':6,'custom':'keep'})
        f,ns,inv=make_flow([e]);await f.async_step_import({'host':'192.0.2.10','protocol':'TCP','expected_serial':'GW123'})
        self.assertEqual(ns['async_connect_and_detect_port'].call_args.kwargs['comm_addr'],247)
        self.assertEqual(e.options['port'],502);self.assertEqual(e.options['protocol'],'TCP')
        self.assertEqual(e.options['custom'],'keep');self.assertEqual(e.options['modbus_id'],247)
        self.assertEqual(e.data['network_timeout'],6)
        f.hass.config_entries.async_reload.assert_awaited_once_with(e.entry_id)
        inv.read_runtime_data.assert_awaited_once()
    async def test_failed_own_import_runtime_failure_preserves_old_settings(self):
        e=entry(source='import',state='setup_error',data={'host':'192.0.2.10','port':8899,'auto_load_control':False})
        f,ns,_=make_flow([e],ET(runtime={}))
        result=await f.async_step_import({'host':'192.0.2.10'})
        self.assertEqual(result['reason'],'cannot_read_runtime')
        f.hass.config_entries.async_update_entry.assert_not_called()
    async def test_discovery_hosts_deduplicated(self):
        f,ns,_=make_flow()
        f._async_discover_unconfigured.return_value={'GW123':NS(host='192.0.2.10',protocol='TCP',port=502,model_family='ET',mac=None,serial_number='GW123')}
        result=await f.async_step_import({'dwars_discover':True,'hosts':['192.0.2.10','192.0.2.10']})
        self.assertEqual(result['reason'],'dwars_scan_complete')
        self.assertEqual(f.hass.config_entries.flow.async_init.await_count,1)
    async def test_failed_owned_entry_included_for_bounded_repair(self):
        e=entry(source='import',state='setup_error',data={'host':'192.0.2.10','auto_load_control':False})
        f,ns,_=make_flow([e])
        await f.async_step_import({'dwars_discover':True})
        f.hass.config_entries.flow.async_init.assert_awaited_once()
        self.assertEqual(f.hass.config_entries.flow.async_init.call_args.kwargs['data']['expected_serial'],'GW123')
    async def test_discovery_keeps_existing_user_entry_out_of_repair_list(self):
        f,ns,_=make_flow([entry(state='setup_error')])
        await f.async_step_import({'dwars_discover':True})
        f.hass.config_entries.flow.async_init.assert_not_awaited()


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def migrate(self,e):
        ns=integration_namespace()
        fn=source_function(GW/'__init__.py','async_migrate_entry',ns)
        def update(obj,**kw):
            for key,value in kw.items():setattr(obj,key,value)
        h=NS(config_entries=NS(async_update_entry=Mock(side_effect=update)))
        self.assertTrue(await fn(h,e));return h
    async def test_v1_and_v2_upgrade_without_network_and_preserve_settings(self):
        for version in (1,2):
            with self.subTest(version=version):
                e=entry(data={'host':'192.0.2.10','protocol':'TCP','port':502,'model_family':'ET','scan_interval':17},
                        options={'modbus_id':247,'network_timeout':8,'keep_alive':True,'auto_load_control':False})
                e.version=version
                before=deepcopy(dict(e.options))
                await self.migrate(e)
                self.assertEqual((e.version,e.minor_version),(2,3))
                self.assertEqual(e.options,before)
                self.assertEqual(e.data['scan_interval'],17);self.assertEqual(e.data['protocol'],'TCP')
    async def test_null_migration_values_do_not_break_setup(self):
        e=entry(data={'host':'192.0.2.10','port':502,'protocol':None,'scan_interval':None,'network_timeout':None},
                options={'network_timeout':None,'modbus_id':None,'scan_interval':None})
        await self.migrate(e)
        self.assertEqual(e.data['protocol'],'TCP');self.assertEqual(e.data['scan_interval'],5)
        self.assertEqual(e.options,{})
        settings=integration_namespace()['entry_connection_options'](e.data,e.options)
        self.assertEqual(settings['comm_addr'],0);self.assertEqual(settings['timeout'],1)
    async def test_future_major_not_downgraded(self):
        ns=integration_namespace();fn=source_function(GW/'__init__.py','async_migrate_entry',ns)
        e=entry();e.version=3
        h=NS(config_entries=NS(async_update_entry=Mock()))
        self.assertFalse(await fn(h,e));h.config_entries.async_update_entry.assert_not_called()
    async def test_idempotent_data_and_options(self):
        e=entry();await self.migrate(e)
        before=(deepcopy(e.data),deepcopy(e.options))
        await self.migrate(e)
        self.assertEqual((e.data,e.options),before)


class ConnectionHelpers(unittest.TestCase):
    def test_data_connection_values_used_when_options_are_empty(self):
        ns=integration_namespace()
        result=ns['entry_connection_options']({'port':502,'protocol':'TCP','network_timeout':4,'network_retries':7,'modbus_id':247},{'auto_load_control':False})
        self.assertEqual(result['timeout'],4);self.assertEqual(result['retries'],7);self.assertEqual(result['comm_addr'],247)
    def test_valid_user_overrides_win(self):
        ns=integration_namespace()
        result=ns['entry_connection_options']({'port':502,'protocol':'TCP','network_timeout':4},{'port':8899,'protocol':'UDP','network_timeout':8,'modbus_id':247})
        self.assertEqual(result['port'],8899);self.assertEqual(result['protocol'],'UDP');self.assertEqual(result['timeout'],8)
    def test_null_options_do_not_mask_valid_data(self):
        ns=integration_namespace()
        self.assertEqual(ns['entry_connection_options']({'network_timeout':4},{'network_timeout':None})['timeout'],4)
    def test_version_declared_not_downgrade_for_existing_v2(self):
        major,_=flow_versions()
        self.assertFalse(2>major,'HA rejects entries with a higher major version than their flow')

if __name__=='__main__':unittest.main()
