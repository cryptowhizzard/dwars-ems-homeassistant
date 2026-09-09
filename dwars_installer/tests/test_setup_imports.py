"""Execute actual import/probe method bodies against small HA/library doubles.
These are unit tests, not a test of a running Home Assistant integration.
"""
from __future__ import annotations
import ast
import asyncio
import inspect
import ipaddress
import logging
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

ROOT=Path(__file__).resolve().parents[2]


def source_function(file,name,namespace,cls=None):
    module=ast.parse(file.read_text())
    owner=next(n for n in module.body if isinstance(n,ast.ClassDef) and n.name==cls) if cls else module
    node=next(n for n in owner.body if isinstance(n,(ast.AsyncFunctionDef,ast.FunctionDef)) and n.name==name)
    node.decorator_list=[]
    tree=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),node],type_ignores=[])
    ast.fix_missing_locations(tree)
    exec(compile(tree,str(file),'exec'),namespace)
    return namespace[name]


class GoodWeImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        namespace={'asyncio':asyncio,'ipaddress':ipaddress,'InverterError':RuntimeError,
            'DOMAIN':'goodwe','CONF_AUTO_LOAD_CONTROL':'auto_load_control','CONF_HOST':'host',
            'CONF_PORT':'port','CONF_PROTOCOL':'protocol','CONF_NETWORK_TIMEOUT':'network_timeout',
            'CONF_DWARS_MANAGED':'dwars_managed','DEFAULT_NETWORK_RETRIES':10,'DEFAULT_MODBUS_ID':0,'_normalise_serial':lambda s:str(s or '').strip().upper()}
        self.connect=AsyncMock(return_value=(NS(serial_number='GW123',set_keep_alive=Mock(),
            read_runtime_data=AsyncMock(return_value={'battery_soc':50})),502,'TCP'))
        namespace['async_connect_and_detect_port']=self.connect
        namespace['build_updated_entry_data']=lambda data,**kwargs:{**data,**kwargs}
        namespace['build_updated_entry_options']=lambda data,**kwargs:dict(data)
        namespace['complete_entry_data']=lambda data:dict(data)
        namespace['entry_connection_options']=lambda *a:{'comm_addr':0,'timeout':2,'retries':10}
        namespace['entry_is_loaded']=lambda entry:getattr(entry,'state','')=='loaded'
        namespace['entry_is_dwars_managed']=lambda entry:getattr(entry,'source','')=='import'
        namespace['_entry_value']=lambda entry,key,default=None:entry.options.get(key,entry.data.get(key,default))
        namespace['_LOGGER']=logging.getLogger('goodwe-tests')
        self.method=source_function(ROOT/'custom_components/goodwe/config_flow.py','async_step_import',namespace,'GoodweFlowHandler')
        self.flow=NS(
            async_abort=lambda **k:{'type':'abort',**k},
            _entry_for_serial=lambda serial:None, _configured_entries=lambda:[],
            async_handle_successful_connection=AsyncMock(return_value={'type':'create_entry','data':{}}),
            _async_discover_unconfigured=AsyncMock(return_value={}),
            hass=NS(config_entries=NS(flow=NS(async_init=AsyncMock(return_value={'type':'create_entry'})),
                async_update_entry=Mock(),async_reload=AsyncMock()),async_create_task=Mock()))
    async def test_add_verified_device_without_form_and_disable_competing_control(self):
        result=await self.method(self.flow,{'host':'192.0.2.10','expected_serial':'GW123'})
        self.assertEqual(result['type'],'create_entry')
        self.assertFalse(result['data']['auto_load_control']);self.assertFalse(result['options']['auto_load_control'])
    async def test_invalid_ip_does_not_probe(self):
        result=await self.method(self.flow,{'host':'localhost'})
        self.assertEqual(result['type'],'abort');self.connect.assert_not_awaited()
    async def test_wrong_serial_not_added(self):
        result=await self.method(self.flow,{'host':'192.0.2.10','expected_serial':'OTHER'})
        self.assertEqual(result['reason'],'identity_changed')
        self.flow.async_handle_successful_connection.assert_not_awaited()
    async def test_all_discovered_devices_imported(self):
        self.flow._async_discover_unconfigured.return_value={str(i):NS(host=f'192.0.2.{i}',protocol='TCP',port=502,model_family='ET',mac=None,serial_number=f'GW{i}') for i in (10,11)}
        result=await self.method(self.flow,{'dwars_discover':True,'hosts':[]})
        self.assertEqual(result['reason'],'dwars_scan_complete')
        self.flow._async_discover_unconfigured.assert_awaited_once_with(include_configured=False)
        self.assertEqual(self.flow.hass.config_entries.flow.async_init.await_count,2)
    async def test_duplicate_keeps_existing_manual_options(self):
        entry=NS(data={'host':'192.0.2.11','keep':'manual'},options={'keep_setting':True},entry_id='existing',state='loaded',source='user')
        self.flow._entry_for_serial=lambda _:entry
        # Avoid scheduling a real reload coroutine in the dummy HA task registry.
        self.flow.hass.async_create_task=lambda coro:coro.close()
        result=await self.method(self.flow,{'host':'192.0.2.10'})
        self.assertEqual(result['reason'],'already_configured_inverter')
        self.flow.hass.config_entries.async_update_entry.assert_not_called()
        self.flow.hass.config_entries.async_reload.assert_not_awaited()


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self,manufacturer='SolarEdge',modern=True,short=False):
        registers=[0x5375,0x6e53,1,65]+[int.from_bytes(manufacturer.encode().ljust(32,b'\0')[i:i+2],'big') for i in range(0,32,2)]
        if short:registers=registers[:2]
        result=NS(registers=registers,isError=lambda:False)
        class Client:
            connected=True
            def __init__(self,**kwargs):self.kwargs=kwargs
            async def connect(self):return True
            def close(self):self.connected=False
        calls=[]
        if modern:
            async def read(self,*,address,count,device_id):calls.append((address,count,device_id));return result
        else:
            async def read(self,*,address,count,slave):calls.append((address,count,slave));return result
        Client.read_holding_registers=read
        fn=source_function(ROOT/'custom_components/solaredge_modbus_multi/network_discovery.py','async_probe_solaredge_modbus',{
            'asyncio':asyncio,'inspect':inspect,'AsyncModbusTcpClient':Client,'_LOGGER':logging.getLogger('test')})
        answer=await fn('192.0.2.10',unit_id=2)
        self.assertEqual(calls,[(40000,69,2)])
        return answer
    async def test_modern_pymodbus_device_id(self):self.assertTrue(await self.probe())
    async def test_older_pymodbus_slave(self):self.assertTrue(await self.probe(modern=False))
    async def test_other_sunspec_vendor_not_solaredge(self):self.assertFalse(await self.probe('Other Inverter'))
    async def test_short_reply_not_solaredge(self):self.assertFalse(await self.probe(short=True))


class SolarEdgeImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_entry_battery_and_storage_options_enabled(self):
        names={k:k.lower() for k in ('DEVICE_LIST','KEEP_MODBUS_OPEN','DETECT_METERS','DETECT_BATTERIES','DETECT_EXTRAS','ADV_PWR_CONTROL','ADV_STORAGE_CONTROL','ADV_SITE_LIMIT_CONTROL','SLEEP_AFTER_WRITE')}
        namespace={'DOMAIN':'solaredge_modbus_multi','CONF_HOST':'host','CONF_PORT':'port','CONF_NAME':'name','CONF_MAC':'mac','CONF_SCAN_INTERVAL':'scan_interval','ConfName':NS(**names),
                   'async_probe_solaredge_modbus':AsyncMock(return_value=True)}
        method=source_function(ROOT/'custom_components/solaredge_modbus_multi/config_flow.py','async_step_import',namespace,'SolaredgeModbusMultiConfigFlow')
        normalized={'host':'192.0.2.10','port':1502,'name':'SolarEdge','device_list':[1]}
        flow=NS(_async_validate_and_normalize=AsyncMock(return_value=(normalized,'mac:1502')),async_set_unique_id=AsyncMock(),_find_existing_entry=lambda *args:None,
            async_abort=lambda **k:{'type':'abort',**k},async_create_entry=lambda **k:{'type':'create_entry',**k})
        result=await method(flow,{'host':'192.0.2.10'})
        self.assertTrue(result['options']['detect_batteries']);self.assertTrue(result['options']['adv_storage_control'])
        self.assertEqual(result['type'],'create_entry')

if __name__=='__main__':unittest.main()
