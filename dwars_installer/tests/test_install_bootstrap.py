"""0.6.1 regressions: real curl/aiohttp against a local Supervisor contract double.

No production network, hardware, Docker, or real API keys are used. In
particular /addons only lists INSTALLED apps and /info has no installed flag.
"""
from __future__ import annotations
import asyncio
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'dwars_installer'))
from aiohttp import ClientSession
from oneshot import OneShot, APIError
from oneshot_common import atomic_json, choose_mode, choose_mode_details


def schema_error(options, schema):
    """Test double for required fields + JSON types, NOT actual Supervisor.

    Reads the real app manifest. This catches the previous partial-options
    bug without inventing a permissive fixture which always accepts writes.
    Range, password security, and all Supervisor internals are not emulated.
    """
    if not isinstance(options, dict):
        return 'expected options object'
    missing = [k for k, spec in schema.items() if isinstance(spec, str) and not spec.endswith('?') and k not in options]
    if missing:
        return 'required key not provided: ' + ', '.join(sorted(missing))
    for key, value in options.items():
        spec = schema.get(key)
        if not isinstance(spec, str):
            continue
        kind = spec.rstrip('?').split('(', 1)[0]
        valid = {'str': isinstance(value, str), 'password': isinstance(value, str),
                 'bool': type(value) is bool, 'int': type(value) is int,
                 'float': type(value) in (int, float), 'list': isinstance(value, str)}.get(kind, True)
        if not valid:
            return 'invalid JSON type for ' + key
    return ''


class SupervisorFixture:
    def __init__(self):
        self.calls = []
        self.catalog = [{'slug': 'repo_goodwe_agent', 'version': '1.9.0', 'installed': None}]
        self.apps = {}
        self.fail_install = False
        self.fail_installed_list = False
        self.fail_store = False
        self.fail_configure = False
        self.fail_start = False
        self.result_error_200 = False
        self.error_message = 'forced options rejection'
        self.entries = []
        self.fixture = self
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self): self.dispatch('GET')
            def do_POST(self): self.dispatch('POST')
            def dispatch(self, method):
                size = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(size)) if size else None
                outer.calls.append((method, self.path, body))
                if self.headers.get('Authorization') != 'Bearer test-supervisor-token':
                    self.reply(401, {'message': 'unauthorized'}); return
                path = self.path
                if method == 'GET' and path == '/addons/self/info':
                    data = {'slug': 'repo_dwars_installer', 'version': '0.6.1', 'options': {}}
                elif method == 'GET' and path == '/addons':
                    if outer.fail_installed_list:
                        self.reply(503, {'message': 'temporary outage'}); return
                    data = {'addons': [{'slug': 'repo_dwars_installer'}, *[{'slug': s, 'version': a['version']} for s, a in outer.apps.items()]]}
                elif method == 'GET' and path == '/store/addons':
                    if outer.fail_store:
                        self.reply(403, {'message': 'forbidden'}); return
                    data = outer.catalog
                elif method == 'POST' and path in ('/store/reload', '/addons/reload'):
                    data = {}
                elif method == 'POST' and path.startswith('/store/addons/') and path.endswith('/install'):
                    slug = path.split('/')[3]
                    if outer.fail_install:
                        self.reply(400, {'message': 'build failed'}); return
                    if not any(r['slug'] == slug for r in outer.catalog):
                        self.reply(404, {'message': 'no store app'}); return
                    if slug in outer.apps:
                        self.reply(400, {'message': 'already installed'}); return
                    if body != {'background': False}:
                        self.reply(400, {'message': 'wrong install payload'}); return
                    outer.apps[slug] = {'slug': slug, 'version': '1.9.0', 'version_latest': '1.9.0', 'state': 'stopped', 'options': {}, 'update_available': False}
                    data = {}
                elif method == 'GET' and path.startswith('/addons/') and path.endswith('/info'):
                    slug = path.split('/')[2]
                    if slug not in outer.apps:
                        self.reply(404, {'message': 'not installed'}); return
                    data = outer.apps[slug]
                elif method == 'GET' and path.startswith('/core/api/config/config_entries/entry'):
                    self.reply(200, outer.entries); return
                elif method == 'POST' and path.endswith('/options/validate'):
                    slug = path.split('/')[2]
                    if body is None:
                        self.reply(400, {'message': 'request must be JSON'}); return
                    info = outer.apps[slug]
                    error = schema_error(body or info['options'], info.get('schema', {}))
                    data = {'valid': not bool(error), 'message': error}
                elif method == 'POST' and path.startswith('/addons/') and path.endswith('/options'):
                    slug = path.split('/')[2]
                    if slug == 'self':
                        data = {}
                    else:
                        info = outer.apps[slug]
                        if 'options' in body:
                            if outer.fail_configure or outer.result_error_200:
                                self.reply(200 if outer.result_error_200 else 400, {'result': 'error', 'message': outer.error_message}); return
                            error = schema_error(body['options'], info.get('schema', {}))
                            if error:
                                self.reply(400, {'result': 'error', 'message': error}); return
                        info.update(body)  # full replacement of the options map
                        data = {}
                elif method == 'POST' and path.startswith('/addons/') and path.endswith('/start'):
                    if outer.fail_start:
                        self.reply(400, {'message': 'cannot start'}); return
                    outer.apps[path.split('/')[2]]['state'] = 'started'
                    data = {}
                elif method == 'GET' and path.startswith('/store/addons/'):
                    slug = path.split('/')[3]
                    row = next((r for r in outer.catalog if r['slug'] == slug), None)
                    if row is None:
                        self.reply(404, {'message': 'not in store'}); return
                    data = row
                elif method == 'POST' and path.startswith('/store/addons/') and path.endswith('/update'):
                    slug = path.split('/')[3]
                    if slug not in outer.apps:
                        self.reply(404, {'message': 'not installed'}); return
                    outer.apps[slug]['version'] = outer.apps[slug]['version_latest']
                    outer.apps[slug]['update_available'] = False
                    data = {}
                else:
                    self.reply(404, {'message': 'unsupported route'}); return
                self.reply(200, {'result': 'ok', 'data': data})
            def reply(self, code, payload):
                encoded = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()


class ShellCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.fixture = SupervisorFixture()
        self.options = {'force_update_agent_addons': False, 'enable_addon_auto_update': False,
            'configure_agent_addons': False, 'start_agent_addons': False, 'inverter_type': 'goodwe',
            'rebuild_agent_addons_when_repo_changed': False, 'restart_agent_addons_after_update': False}
        atomic_json(self.path / 'options.json', self.options)
    def tearDown(self): self.fixture.close(); self.tmp.cleanup()
    def shell(self, command, success=True):
        result = subprocess.run(['bash', '-c', 'source "$SCRIPT"; ' + command], text=True, capture_output=True, timeout=15,
            env={**os.environ, 'DWARS_INSTALLER_LIB_ONLY': 'true', 'CONFIG_PATH': str(self.path / 'options.json'),
                 'STATE_DIR': str(self.path), 'HA_CONFIG_DIR': str(self.path), 'SCRIPT': str(ROOT / 'dwars_installer/run.sh'),
                 'SUPERVISOR_API': self.fixture.url, 'SUPERVISOR_TOKEN': 'test-supervisor-token'})
        if success: self.assertEqual(result.returncode, 0, result.stderr)
        else: self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result
    def test_finds_store_only_agent(self):
        self.assertEqual(self.shell('find_addon_slug goodwe_agent').stdout.strip(), 'repo_goodwe_agent')
        self.assertTrue(any(path == '/store/addons' for _, path, _ in self.fixture.calls))
    def test_installs_missing_agent_and_only_then_reads_info(self):
        result = self.shell("ensure_addon_installed goodwe_agent 'GoodWe Agent / BMS'")
        self.assertEqual(result.stdout.strip(), 'repo_goodwe_agent')
        paths = [p for _, p, _ in self.fixture.calls]
        self.assertLess(paths.index('/store/addons/repo_goodwe_agent/install'), paths.index('/addons/repo_goodwe_agent/info'))
        self.assertNotIn('test-supervisor-token', result.stdout + result.stderr)
    def test_installed_info_without_installed_boolean_not_reinstalled(self):
        self.fixture.apps['repo_goodwe_agent'] = {'slug':'repo_goodwe_agent', 'version':'1.9.0','version_latest':'1.9.0', 'options': {}}
        self.shell("ensure_addon_installed goodwe_agent 'GoodWe Agent / BMS'")
        self.assertFalse(any(p.endswith('/install') for _, p, _ in self.fixture.calls))
    def test_installed_agent_still_resolves_when_store_unavailable(self):
        self.fixture.apps['repo_goodwe_agent'] = {'slug':'repo_goodwe_agent', 'version':'1.9.0', 'options': {}}
        self.fixture.fail_store = True
        self.assertEqual(self.shell('find_addon_slug goodwe_agent').stdout.strip(), 'repo_goodwe_agent')
    def test_other_repository_not_chosen_first(self):
        self.fixture.catalog.insert(0, {'slug':'other_goodwe_agent', 'version':'1.9.0', 'installed':None})
        self.assertEqual(self.shell('find_addon_slug goodwe_agent').stdout.strip(), 'repo_goodwe_agent')
    def test_missing_same_repo_not_replaced_by_foreign_agent(self):
        self.fixture.catalog = [{'slug':'other_goodwe_agent', 'version':'1.9.0', 'installed':None}]
        self.shell("ensure_addon_installed goodwe_agent 'GoodWe'", success=False)
        self.assertFalse(any(p.endswith('/install') for _, p, _ in self.fixture.calls))
    def test_failed_install_propagates(self):
        self.fixture.fail_install = True
        self.shell("ensure_addon_installed goodwe_agent 'GoodWe'", success=False)
        self.assertEqual(self.fixture.apps, {})
        self.assertFalse(any(p == '/addons/repo_goodwe_agent/install' for _, p, _ in self.fixture.calls))
    def test_failed_install_not_reported_success_by_agent_cycle(self):
        self.fixture.fail_install = True
        self.shell('install_or_configure_agents', success=False)
    def test_unavailable_installed_list_cannot_trigger_install(self):
        self.fixture.fail_installed_list = True
        self.shell("ensure_addon_installed goodwe_agent 'GoodWe'", success=False)
        self.assertFalse(any(p.endswith('/install') for _, p, _ in self.fixture.calls))
    def test_catalog_accepts_documented_envelope_and_payload_shapes(self):
        for body in ([{'slug':'s'}], {'addons':[{'slug':'s'}]}, {'result':'ok','data':[{'slug':'s'}]}, {'result':'ok','data':{'addons':[{'slug':'s'}]}}):
            atomic_json(self.path / 'fixture.json', {'payload':body})
            result = self.shell('jq .payload "$STATE_DIR/fixture.json" | addon_catalog_rows')
            self.assertEqual(json.loads(result.stdout), [{'slug':'s'}])
    def test_update_info_without_boolean_is_still_updated(self):
        self.options['force_update_agent_addons'] = True
        atomic_json(self.path / 'options.json', self.options)
        self.fixture.apps['repo_goodwe_agent'] = {'slug':'repo_goodwe_agent', 'version':'1.8.9', 'version_latest':'1.9.0', 'options':{}, 'update_available':True, 'state':'stopped'}
        self.shell("update_addon_if_available repo_goodwe_agent goodwe_agent 'GoodWe'")
        self.assertTrue(any(p == '/store/addons/repo_goodwe_agent/update' for _, p, _ in self.fixture.calls))


class ModeTests(unittest.TestCase):
    def test_updater_markers_alone_do_not_prove_configured_installation(self):
        for marker in ('dwars_auto_update_state.json', 'goodwe.payload.sha256'):
            with tempfile.TemporaryDirectory() as td:
                path=Path(td); (path/marker).write_text('{}')
                self.assertEqual(choose_mode({'installation_mode':'auto'}, path), 'oneshot')
    def test_manual_remains_explicit_and_reason_does_not_leak_key(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)
            self.assertEqual(choose_mode({'installation_mode':'manual'},path),'manual')
            mode, reason=choose_mode_details({'goodwe_agent_api_key':'legacy-secret-value'},path)
            self.assertEqual(mode,'oneshot'); self.assertNotIn('legacy-secret-value',reason)
    def test_explicit_oneshot_and_saved_credentials_survive_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td); atomic_json(path/'options.json',{'installation_mode':'oneshot','goodwe_agent_api_key':'legacy'})
            first=OneShot(path,ROOT/'dwars_installer')
            atomic_json(first.credentials_path,{'api_key':'test-customer-key'})
            with redirect_stdout(io.StringIO()): first.save(stage='restart',status='waiting')
            second=OneShot(path,ROOT/'dwars_installer')
            self.assertEqual(second.mode,'oneshot');self.assertEqual(second.state['stage'],'restart')
            self.assertEqual(second.credentials['api_key'],'test-customer-key')
            self.assertNotIn('test-customer-key', json.dumps(second.public()))


class OneShotHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)
        self.fixture=SupervisorFixture(); self.one=OneShot(self.path,ROOT/'dwars_installer')
        self.one.supervisor=self.fixture.url; self.one.token=lambda:'test-supervisor-token'
        self.one.credentials={}
        self.one.session=ClientSession()
    async def asyncTearDown(self):
        await self.one.session.close(); self.fixture.close(); self.tmp.cleanup()
    async def test_real_aiohttp_parses_store_list_and_installs_agent(self):
        self.one.credentials={'api_key':'test-customer-key'}
        slug=await self.one.addon_slug('goodwe_agent')
        self.assertEqual(slug,'repo_goodwe_agent')
        with redirect_stdout(io.StringIO()): info=await self.one.ensure_agent(slug)
        self.assertEqual(info['version'],'1.9.0')
        self.assertEqual(sum(p.endswith('/install') for _,p,_ in self.fixture.calls),1)
    async def test_existing_agent_of_other_customer_not_adopted(self):
        self.fixture.apps['repo_goodwe_agent']={'slug':'repo_goodwe_agent','version':'1.9.0','options':{'api_key':'another-client'},'update_available':True}
        from oneshot_common import Blocked
        self.one.credentials={'api_key':'test-customer-key'}
        with self.assertRaises(Blocked): await self.one.ensure_agent('repo_goodwe_agent')
        self.assertFalse(any(method=='POST' for method,_,_ in self.fixture.calls))
    async def test_legacy_configured_agent_preserves_manual_updater(self):
        (self.path/'dwars_auto_update_state.json').write_text('{}')
        self.fixture.apps['repo_goodwe_agent']={'slug':'repo_goodwe_agent','version':'1.9.0','options':{'api_key':'legacy-client'},'state':'started'}
        await self.one.inspect_legacy_installation()
        self.assertEqual(self.one.mode,'manual')
        self.assertNotIn('legacy-client',self.one.mode_reason)
        self.assertFalse(any(method=='POST' for method,_,_ in self.fixture.calls))
    async def test_updater_leftover_with_no_agent_stays_oneshot(self):
        (self.path/'dwars_auto_update_state.json').write_text('{}')
        await self.one.inspect_legacy_installation()
        self.assertEqual(self.one.mode,'oneshot')
    async def test_unconfigured_agent_does_not_disable_onboarding(self):
        (self.path/'dwars_auto_update_state.json').write_text('{}')
        self.fixture.apps['repo_goodwe_agent']={'slug':'repo_goodwe_agent','version':'1.9.0','options':{'api_key':''}}
        await self.one.inspect_legacy_installation()
        self.assertEqual(self.one.mode,'oneshot')
    async def test_explicit_oneshot_skips_legacy_probe(self):
        (self.path/'dwars_auto_update_state.json').write_text('{}')
        self.one.options['installation_mode']='oneshot'
        await self.one.inspect_legacy_installation()
        self.assertEqual(self.one.mode,'oneshot'); self.assertEqual(self.fixture.calls,[])
    async def test_probe_failure_does_not_start_legacy_fallback(self):
        (self.path/'dwars_auto_update_state.json').write_text('{}')
        self.fixture.fail_installed_list=True
        with self.assertRaises(APIError):
            await self.one.inspect_legacy_installation()
        self.assertEqual(self.one.mode,'oneshot')
    async def test_saved_key_skips_legacy_probe(self):
        (self.path/'dwars_auto_update_state.json').write_text('{}')
        self.one.credentials={'api_key':'test-customer-key'}
        atomic_json(self.one.credentials_path,self.one.credentials)
        await self.one.inspect_legacy_installation()
        self.assertEqual(self.one.mode,'oneshot');self.assertEqual(self.fixture.calls,[])
    async def test_new_install_without_updater_leftovers_needs_no_probe(self):
        await self.one.inspect_legacy_installation()
        self.assertEqual(self.one.mode,'oneshot');self.assertEqual(self.fixture.calls,[])

if __name__ == '__main__': unittest.main()
