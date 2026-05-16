#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from sma10se_browser import apply_mode

OPTIONS_PATH = Path('/data/options.json')
STATE_PATH = Path('/data/sma10se_state.json')

VALID_MODES = {'off', 'charge', 'discharge'}
MODE_ALIASES = {
    'uit': 'off',
    'standby': 'off',
    'idle': 'off',
    'off': 'off',
    'stop': 'off',
    'laden': 'charge',
    'opladen': 'charge',
    'charge': 'charge',
    'charging': 'charge',
    'accu_opladen': 'charge',
    'ontladen': 'discharge',
    'discharge': 'discharge',
    'discharging': 'discharge',
    'accu_ontladen': 'discharge',
}


def now_ts() -> float:
    return time.time()


def load_options() -> dict[str, Any]:
    defaults = {
        'url': 'https://192.168.2.23/',
        'language': 'Nederlands',
        'user_group': 'Installateur',
        'password': '',
        'api_token': '',
        'port': 8099,
        'min_state_change_s': 300,
        'poll_interval_s': 10,
        'retry_interval_s': 60,
        'debug': True,
    }
    if OPTIONS_PATH.exists():
        try:
            with OPTIONS_PATH.open('r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    defaults.update(data)
        except Exception as exc:
            print(f'[SMA10SE] Could not load {OPTIONS_PATH}: {exc}', flush=True)
    defaults['port'] = int(defaults.get('port') or 8099)
    defaults['min_state_change_s'] = max(0, int(defaults.get('min_state_change_s') or 300))
    defaults['poll_interval_s'] = max(1, int(defaults.get('poll_interval_s') or 10))
    defaults['retry_interval_s'] = max(5, int(defaults.get('retry_interval_s') or 60))
    return defaults


def normalize_mode(mode: Any) -> str:
    raw = str(mode or '').strip().lower().replace('-', '_').replace(' ', '_')
    normalized = MODE_ALIASES.get(raw, raw)
    if normalized not in VALID_MODES:
        raise ValueError(f'Invalid mode {mode!r}; expected one of {sorted(VALID_MODES)}')
    return normalized


class Controller:
    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        self.lock = threading.RLock()
        self.running = False
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.state = self._load_state()

    def _default_state(self) -> dict[str, Any]:
        return {
            'actual_mode': None,
            'requested_mode': None,
            'pending_mode': None,
            'running': False,
            'last_change_ts': 0.0,
            'last_attempt_ts': 0.0,
            'last_success_ts': 0.0,
            'last_error': None,
            'last_result': None,
            'cooldown_remaining_s': 0,
        }

    def _load_state(self) -> dict[str, Any]:
        state = self._default_state()
        if STATE_PATH.exists():
            try:
                with STATE_PATH.open('r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        state.update(loaded)
            except Exception as exc:
                print(f'[SMA10SE] Could not load state: {exc}', flush=True)
        return state

    def _save_state(self) -> None:
        try:
            tmp = STATE_PATH.with_suffix('.tmp')
            with tmp.open('w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2, sort_keys=True)
            tmp.replace(STATE_PATH)
        except Exception as exc:
            print(f'[SMA10SE] Could not save state: {exc}', flush=True)

    def cooldown_remaining(self) -> int:
        last_change = float(self.state.get('last_change_ts') or 0.0)
        if last_change <= 0:
            return 0
        elapsed = now_ts() - last_change
        return max(0, int(round(self.options['min_state_change_s'] - elapsed)))

    def retry_remaining(self) -> int:
        last_attempt = float(self.state.get('last_attempt_ts') or 0.0)
        if last_attempt <= 0:
            return 0
        elapsed = now_ts() - last_attempt
        return max(0, int(round(self.options['retry_interval_s'] - elapsed)))

    def status(self) -> dict[str, Any]:
        with self.lock:
            out = dict(self.state)
            out['running'] = bool(self.running)
            out['cooldown_remaining_s'] = self.cooldown_remaining()
            out['retry_remaining_s'] = self.retry_remaining()
            out['valid_modes'] = sorted(VALID_MODES)
            return out

    def request_mode(self, mode: Any, source: str = 'api') -> dict[str, Any]:
        requested = normalize_mode(mode)
        with self.lock:
            self.state['requested_mode'] = requested
            self.state['last_request_ts'] = now_ts()
            self.state['last_request_source'] = source

            actual = self.state.get('actual_mode')
            pending = self.state.get('pending_mode')

            if actual == requested and not pending and not self.running:
                self.state['last_result'] = f'No-op: already {requested}'
                self.state['last_error'] = None
                self._save_state()
                return {'accepted': True, 'queued': False, 'started': False, 'reason': 'already_in_requested_mode', **self.status()}

            if self.running:
                self.state['pending_mode'] = requested
                self._save_state()
                return {'accepted': True, 'queued': True, 'started': False, 'reason': 'browser_run_active_pending_set', **self.status()}

            cooldown = self.cooldown_remaining()
            if cooldown > 0:
                self.state['pending_mode'] = requested
                self._save_state()
                return {'accepted': True, 'queued': True, 'started': False, 'reason': f'cooldown_{cooldown}s', **self.status()}

            retry = self.retry_remaining()
            if retry > 0 and self.state.get('last_error'):
                self.state['pending_mode'] = requested
                self._save_state()
                return {'accepted': True, 'queued': True, 'started': False, 'reason': f'retry_wait_{retry}s', **self.status()}

            self.state['pending_mode'] = None
            self._save_state()
            self._start_apply_locked(requested)
            return {'accepted': True, 'queued': False, 'started': True, 'reason': 'started', **self.status()}

    def _start_apply_locked(self, mode: str) -> None:
        self.running = True
        self.state['running'] = True
        self.state['last_attempt_ts'] = now_ts()
        self.state['last_error'] = None
        self.state['last_result'] = f'Starting browser automation for {mode}'
        self._save_state()
        self.worker = threading.Thread(target=self._apply_worker, args=(mode,), daemon=True)
        self.worker.start()

    def _apply_worker(self, mode: str) -> None:
        print(f'[SMA10SE] Applying mode={mode}', flush=True)
        ok = False
        result: dict[str, Any] | None = None
        error: str | None = None
        try:
            result = apply_mode(self.options, mode)
            ok = bool(result.get('ok'))
            if not ok:
                error = str(result.get('error') or 'Unknown browser automation error')
        except Exception as exc:
            error = f'{exc}\n{traceback.format_exc()}'

        with self.lock:
            self.running = False
            self.state['running'] = False
            self.state['last_result'] = result
            if ok:
                self.state['actual_mode'] = mode
                self.state['last_success_ts'] = now_ts()
                self.state['last_change_ts'] = now_ts()
                self.state['last_error'] = None
                print(f'[SMA10SE] Applied mode={mode} successfully', flush=True)
            else:
                self.state['last_error'] = error
                self.state['pending_mode'] = self.state.get('requested_mode') or mode
                print(f'[SMA10SE] Apply failed mode={mode}: {error}', flush=True)
            self._save_state()

    def scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                with self.lock:
                    pending = self.state.get('pending_mode')
                    if pending and not self.running and self.cooldown_remaining() == 0 and self.retry_remaining() == 0:
                        self.state['pending_mode'] = None
                        self._save_state()
                        self._start_apply_locked(str(pending))
                self.stop_event.wait(float(self.options['poll_interval_s']))
            except Exception as exc:
                print(f'[SMA10SE] Scheduler error: {exc}', flush=True)
                self.stop_event.wait(5)


OPTIONS = load_options()
CONTROLLER = Controller(OPTIONS)


class Handler(BaseHTTPRequestHandler):
    server_version = 'SMA10SEControl/0.1.0'

    def _auth_ok(self) -> bool:
        token = str(OPTIONS.get('api_token') or '').strip()
        if not token:
            return True
        got = self.headers.get('Authorization', '')
        return got == f'Bearer {token}'

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not self._auth_ok():
            self._send_json(401, {'error': 'unauthorized'})
            return
        if self.path.rstrip('/') in {'', '/status'}:
            self._send_json(200, CONTROLLER.status())
            return
        self._send_json(404, {'error': 'not_found'})

    def do_POST(self) -> None:  # noqa: N802
        if not self._auth_ok():
            self._send_json(401, {'error': 'unauthorized'})
            return
        if self.path.rstrip('/') != '/set_mode':
            self._send_json(404, {'error': 'not_found'})
            return
        try:
            length = int(self.headers.get('Content-Length') or '0')
            raw = self.rfile.read(length).decode('utf-8') if length else '{}'
            data = json.loads(raw or '{}')
            mode = data.get('mode')
            resp = CONTROLLER.request_mode(mode, source='http')
            self._send_json(202 if resp.get('queued') or resp.get('started') else 200, resp)
        except Exception as exc:
            self._send_json(400, {'error': str(exc), **CONTROLLER.status()})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f'[SMA10SE-HTTP] {self.address_string()} {fmt % args}', flush=True)


def main() -> None:
    if not str(OPTIONS.get('password') or '').strip():
        print('[SMA10SE] WARNING: add-on password option is empty; browser login will fail until configured.', flush=True)
    port = int(OPTIONS['port'])
    print(f'[SMA10SE] Starting API on 0.0.0.0:{port}', flush=True)
    print(f'[SMA10SE] Target URL={OPTIONS.get("url")} user_group={OPTIONS.get("user_group")} language={OPTIONS.get("language")}', flush=True)
    t = threading.Thread(target=CONTROLLER.scheduler_loop, daemon=True)
    t.start()
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    server.serve_forever()


if __name__ == '__main__':
    main()
