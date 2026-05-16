#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select

MODE_OPTIONS = {
    'off': [
        'Uit',
        'Off',
        'Standby',
    ],
    'charge': [
        'Accu opladen',
        'Lading van de batterij voorkeur gegeven',
        'Batterij laden voorkeur gegeven',
        'Battery charging preferred',
        'Charge battery',
        'Charge',
    ],
    'discharge': [
        'Accu ontladen',
        'Ontlading van de batterij voorkeur gegeven',
        'Batterij ontladen voorkeur gegeven',
        'Battery discharging preferred',
        'Discharge battery',
        'Discharge',
    ],
}

TARGET_ROW_LABELS = [
    'Fallback van de batterijregeling bij uitval van het meetpunt',
    'Fallback battery control',
    'Fallback of battery control',
    'Battery control fallback',
]


def log(msg: str) -> None:
    print(f'[SMA10SE-BROWSER] {msg}', flush=True)


def clean(text: Any) -> str:
    return ' '.join(str(text or '').strip().split())


def safe_filename(name: str) -> str:
    return ''.join(c if c.isalnum() or c in '._-' else '_' for c in name)


class BrowserControl:
    def __init__(self, options: dict[str, Any], mode: str) -> None:
        self.options = options
        self.mode = mode
        self.debug = bool(options.get('debug', True))
        self.profile_dir = tempfile.mkdtemp(prefix='sma10se-chrome-')
        self.driver: webdriver.Chrome | None = None
        self.step = 0
        self.screenshots: list[str] = []

    def make_driver(self) -> webdriver.Chrome:
        opts = Options()
        opts.binary_location = '/usr/bin/chromium-browser'
        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-setuid-sandbox')
        opts.add_argument('--disable-dev-shm-usage')
        opts.add_argument('--disable-gpu')
        opts.add_argument('--window-size=2048,1152')
        opts.add_argument('--window-position=0,0')
        opts.add_argument('--force-device-scale-factor=1')
        opts.add_argument('--ignore-certificate-errors')
        opts.add_argument('--allow-insecure-localhost')
        opts.add_argument('--password-store=basic')
        opts.add_argument(f'--user-data-dir={self.profile_dir}')
        service = Service('/usr/bin/chromedriver')
        driver = webdriver.Chrome(service=service, options=opts)
        driver.set_window_size(2048, 1152)
        return driver

    @property
    def d(self) -> webdriver.Chrome:
        if self.driver is None:
            raise RuntimeError('driver not initialized')
        return self.driver

    def shot(self, label: str) -> None:
        self.step += 1
        path = f'/tmp/sma10se_{self.step:02d}_{safe_filename(label)}.png'
        try:
            self.d.save_screenshot(path)
            self.screenshots.append(path)
            log(f'screenshot: {path}')
        except Exception as exc:
            log(f'screenshot failed: {exc}')

    def body_text(self) -> str:
        try:
            return self.d.execute_script('return document.body.innerText || "";') or ''
        except Exception:
            return ''

    def cdp_click(self, x: float, y: float, label: str) -> None:
        log(f'klik {label}: {x:.1f},{y:.1f}')
        self.d.execute_cdp_cmd('Input.dispatchMouseEvent', {
            'type': 'mouseMoved',
            'x': float(x),
            'y': float(y),
            'button': 'none',
        })
        self.d.execute_cdp_cmd('Input.dispatchMouseEvent', {
            'type': 'mousePressed',
            'x': float(x),
            'y': float(y),
            'button': 'left',
            'clickCount': 1,
        })
        self.d.execute_cdp_cmd('Input.dispatchMouseEvent', {
            'type': 'mouseReleased',
            'x': float(x),
            'y': float(y),
            'button': 'left',
            'clickCount': 1,
        })
        time.sleep(1.5)

    def find_text_rect(self, texts: list[str], min_y: int = 0, max_y: int | None = None, prefer: str = 'smallest', contains: bool = False) -> dict[str, Any] | None:
        rects = self.d.execute_script(
            """
            const texts = arguments[0].map(t => String(t).toLowerCase());
            const minY = arguments[1];
            const maxY = arguments[2];
            const contains = arguments[3];

            function visible(el) {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            }

            const out = [];
            for (const el of Array.from(document.querySelectorAll('*'))) {
                if (!visible(el)) continue;
                const txt = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                if (!txt || txt.length > 160) continue;
                const low = txt.toLowerCase();
                let match = false;
                for (const wanted of texts) {
                    if (contains) {
                        if (low.includes(wanted)) match = true;
                    } else {
                        if (low === wanted) match = true;
                    }
                }
                if (!match) continue;
                const r = el.getBoundingClientRect();
                if (r.top < minY) continue;
                if (maxY !== null && r.top > maxY) continue;
                out.push({
                    text: txt,
                    tag: el.tagName,
                    x: r.left,
                    y: r.top,
                    w: r.width,
                    h: r.height,
                    cx: r.left + r.width / 2,
                    cy: r.top + r.height / 2,
                    area: r.width * r.height,
                });
            }
            return out;
            """,
            texts,
            min_y,
            max_y,
            contains,
        )
        if not rects:
            return None
        if prefer == 'rightmost':
            rects.sort(key=lambda r: r['cx'], reverse=True)
        elif prefer == 'lowest':
            rects.sort(key=lambda r: r['cy'], reverse=True)
        elif prefer == 'largest':
            rects.sort(key=lambda r: r['area'], reverse=True)
        else:
            rects.sort(key=lambda r: r['area'])
        r = rects[0]
        log(f"gevonden tekst '{r['text']}' tag={r['tag']} rect={r['x']:.0f},{r['y']:.0f},{r['w']:.0f},{r['h']:.0f}")
        return r

    def click_text(self, texts: list[str], label: str, min_y: int = 0, max_y: int | None = None, prefer: str = 'smallest', contains: bool = False) -> bool:
        r = self.find_text_rect(texts, min_y=min_y, max_y=max_y, prefer=prefer, contains=contains)
        if not r:
            return False
        self.cdp_click(r['cx'], r['cy'], label)
        return True

    def visible_inputs(self) -> list[Any]:
        out = []
        for el in self.d.find_elements(By.TAG_NAME, 'input'):
            try:
                if el.is_displayed():
                    out.append(el)
            except Exception:
                pass
        return out

    def visible_selects(self) -> list[Any]:
        out = []
        for el in self.d.find_elements(By.TAG_NAME, 'select'):
            try:
                if el.is_displayed():
                    out.append(el)
            except Exception:
                pass
        return out

    def select_visible_text_or_contains(self, select_el: Any, candidates: list[str], label: str) -> str:
        sel = Select(select_el)
        options = [(clean(o.text), o) for o in sel.options]
        lower_candidates = [clean(c).lower() for c in candidates if clean(c)]

        log(f'{label} opties: {[t for t, _ in options]}')

        # Exact match first.
        for wanted in lower_candidates:
            for text, _opt in options:
                if text.lower() == wanted:
                    sel.select_by_visible_text(text)
                    time.sleep(1)
                    return text

        # Contains match.
        for wanted in lower_candidates:
            for text, _opt in options:
                if wanted and wanted in text.lower():
                    sel.select_by_visible_text(text)
                    time.sleep(1)
                    return text

        # Token fallback for charge/discharge/off.
        mode_tokens = {
            'charge': ['oplad', 'laden', 'charging', 'charge'],
            'discharge': ['ontlad', 'discharging', 'discharge'],
            'off': ['uit', 'off', 'standby'],
        }.get(self.mode, [])
        for text, _opt in options:
            low = text.lower()
            if any(tok in low for tok in mode_tokens):
                sel.select_by_visible_text(text)
                time.sleep(1)
                return text

        raise RuntimeError(f'Geen passende optie gevonden voor {label}; candidates={candidates}; options={[t for t, _ in options]}')

    def set_input_value(self, el: Any, value: str) -> None:
        el.click()
        el.send_keys(Keys.CONTROL, 'a')
        el.send_keys(Keys.DELETE)
        el.send_keys(value)
        self.d.execute_script(
            """
            const el = arguments[0];
            const value = arguments[1];
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
            """,
            el,
            value,
        )

    def login_if_needed(self) -> None:
        txt = self.body_text()
        if 'Apparaatparameters' in txt or 'Device parameters' in txt or 'Momentane waarden' in txt:
            log('lijkt al ingelogd')
            return

        selects = self.visible_selects()
        language = clean(self.options.get('language') or '')
        user_group = clean(self.options.get('user_group') or '')
        password = str(self.options.get('password') or '')
        if not password:
            raise RuntimeError('Geen wachtwoord geconfigureerd in add-on options')

        if selects and language:
            try:
                self.select_visible_text_or_contains(selects[0], [language], 'taal')
            except Exception as exc:
                log(f'taal-select overgeslagen: {exc}')

        time.sleep(1)
        selects = self.visible_selects()
        if len(selects) >= 2 and user_group:
            self.select_visible_text_or_contains(selects[1], [user_group, 'Installer', 'Installateur', 'Service Provider', 'Serviceverlener'], 'gebruikersgroep')
        elif len(selects) >= 2:
            self.select_visible_text_or_contains(selects[1], ['Installateur', 'Installer', 'Service Provider', 'Serviceverlener'], 'gebruikersgroep')
        else:
            raise RuntimeError('Gebruikersgroep dropdown niet gevonden')

        inputs = self.visible_inputs()
        pw_fields = []
        for i in inputs:
            try:
                typ = (i.get_attribute('type') or '').lower()
                placeholder = (i.get_attribute('placeholder') or '').lower()
                if typ == 'password' or 'password' in placeholder or 'wachtwoord' in placeholder:
                    pw_fields.append(i)
            except Exception:
                pass
        if not pw_fields:
            pw_fields = inputs
        if not pw_fields:
            raise RuntimeError('Wachtwoordveld niet gevonden')

        self.set_input_value(pw_fields[-1], password)
        self.shot('login_password_filled')

        if not self.click_text(['Aanmelden', 'Login'], 'aanmelden knop', min_y=250, prefer='lowest'):
            # Fallback: press ENTER in password field.
            pw_fields[-1].send_keys(Keys.ENTER)
            time.sleep(4)

        for _ in range(40):
            txt = self.body_text()
            if 'Apparaatparameters' in txt or 'Device parameters' in txt or 'Momentane waarden' in txt or 'Home' in txt:
                return
            time.sleep(0.5)

        self.shot('login_failed')
        raise RuntimeError('Login lijkt niet gelukt; pagina toont geen Home/Apparaatparameters')

    def navigate_to_parameters(self) -> None:
        if not self.click_text(['Apparaatparameters', 'Device parameters'], 'Apparaatparameters tab', min_y=50, max_y=190, prefer='largest'):
            self.shot('no_device_parameters_tab')
            raise RuntimeError('Apparaatparameters tab niet gevonden')
        time.sleep(4)
        self.shot('device_parameters')

        # Enter edit mode if necessary.
        txt = self.body_text()
        if 'Alle opslaan' not in txt and 'Save all' not in txt:
            if self.click_text(['Parameters bewerken', 'Edit parameters'], 'Parameters bewerken', min_y=120, max_y=320, prefer='smallest'):
                time.sleep(4)
            else:
                log('Parameters bewerken niet gevonden; mogelijk al in edit mode')
        self.shot('parameters_edit_mode')

    def search_or_expand_battery_section(self) -> None:
        # First try search box with a short Dutch term.
        inputs = self.visible_inputs()
        search_inputs = []
        for i in inputs:
            try:
                placeholder = (i.get_attribute('placeholder') or '').lower()
                typ = (i.get_attribute('type') or '').lower()
                if 'zoek' in placeholder or 'search' in placeholder or typ in {'search', 'text'}:
                    search_inputs.append(i)
            except Exception:
                pass

        if search_inputs:
            try:
                self.set_input_value(search_inputs[0], 'Fallback')
                time.sleep(2)
                self.shot('search_fallback')
                if self.find_target_select() is not None:
                    return
                self.set_input_value(search_inputs[0], 'Accu')
                time.sleep(2)
                self.shot('search_accu')
            except Exception as exc:
                log(f'zoekveld overgeslagen: {exc}')

        # Expand Accu/Battery accordions. Click multiple candidates if needed.
        for _ in range(4):
            if self.find_target_select() is not None:
                return
            if not self.click_text(['Accu', 'Battery'], 'Accu sectie', min_y=250, prefer='lowest', contains=False):
                if not self.click_text(['Accu', 'Battery'], 'Accu sectie contains', min_y=250, prefer='lowest', contains=True):
                    break
            time.sleep(2)
            self.shot('after_click_accu')

    def find_target_select(self) -> Any | None:
        labels = TARGET_ROW_LABELS
        return self.d.execute_script(
            """
            const labels = arguments[0].map(x => String(x).toLowerCase());
            function clean(txt) { return String(txt || '').replace(/\\s+/g, ' ').trim(); }
            function visible(el) {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            }
            function matches(txt) {
                const low = clean(txt).toLowerCase();
                return labels.some(label => low.includes(label));
            }

            const containers = Array.from(document.querySelectorAll('tr, div, li, section'))
                .filter(visible)
                .map(el => ({el, txt: clean(el.innerText || el.textContent)}))
                .filter(o => o.txt && matches(o.txt))
                .sort((a, b) => a.txt.length - b.txt.length);

            for (const item of containers) {
                let select = item.el.querySelector('select');
                if (select && visible(select)) return select;
                let p = item.el.parentElement;
                for (let i = 0; i < 8 && p; i++, p = p.parentElement) {
                    select = p.querySelector('select');
                    if (select && visible(select) && matches(clean(p.innerText || p.textContent))) return select;
                }
            }
            return null;
            """,
            labels,
        )

    def set_battery_fallback_mode(self) -> str:
        self.search_or_expand_battery_section()
        target_select = self.find_target_select()
        if target_select is None:
            self.shot('target_select_not_found')
            raise RuntimeError('Doelparameter niet gevonden: Fallback van de batterijregeling bij uitval van het meetpunt')

        self.d.execute_script('arguments[0].scrollIntoView({block:"center"});', target_select)
        time.sleep(1)
        selected = self.select_visible_text_or_contains(target_select, MODE_OPTIONS[self.mode], 'accu fallback modus')
        self.shot(f'mode_selected_{self.mode}')
        return selected

    def save_all(self) -> None:
        # Click Save All / Alle opslaan.
        if not self.click_text(['Alle opslaan', 'Save all'], 'Alle opslaan', min_y=120, max_y=320, prefer='smallest'):
            self.shot('save_all_not_found')
            raise RuntimeError('Knop Alle opslaan / Save all niet gevonden')
        time.sleep(3)
        self.shot('after_save_all')

        # Confirm prompt if present.
        candidates = ['Bevestigen', 'Confirm', 'OK', 'Ok', 'Ja', 'Yes', 'Opslaan', 'Save', 'Toepassen', 'Apply']
        for _ in range(20):
            if self.click_text(candidates, 'bevestiging', min_y=200, prefer='lowest'):
                time.sleep(4)
                self.shot('after_confirm')
                break
            time.sleep(0.5)

        # Result popup may require another Bevestigen.
        txt = self.body_text()
        if 'Succes' in txt or 'Success' in txt or 'success' in txt.lower():
            self.shot('success_popup')
            for _ in range(20):
                if self.click_text(['Bevestigen', 'Confirm', 'OK', 'Ok', 'Sluiten', 'Close'], 'eindbevestiging', min_y=200, prefer='lowest'):
                    time.sleep(3)
                    self.shot('after_final_confirm')
                    return
                time.sleep(0.5)

    def run(self) -> dict[str, Any]:
        self.driver = self.make_driver()
        try:
            url = str(self.options.get('url') or 'https://192.168.2.23/')
            log(f'open {url}')
            self.d.get(url)
            time.sleep(8)
            self.shot('start')

            self.login_if_needed()
            self.shot('after_login')

            self.navigate_to_parameters()
            selected = self.set_battery_fallback_mode()
            self.save_all()

            return {
                'ok': True,
                'mode': self.mode,
                'selected_option': selected,
                'screenshots': self.screenshots,
            }
        except Exception as exc:
            self.shot('error')
            return {
                'ok': False,
                'mode': self.mode,
                'error': str(exc),
                'screenshots': self.screenshots,
            }
        finally:
            try:
                self.d.quit()
            except Exception:
                pass
            shutil.rmtree(self.profile_dir, ignore_errors=True)


def apply_mode(options: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode not in MODE_OPTIONS:
        raise ValueError(f'Invalid mode {mode!r}; expected one of {sorted(MODE_OPTIONS)}')
    return BrowserControl(options, mode).run()


if __name__ == '__main__':
    import json
    import sys

    mode_arg = sys.argv[1] if len(sys.argv) > 1 else 'off'
    opts = {
        'url': os.environ.get('SMA10SE_URL', 'https://192.168.2.23/'),
        'language': os.environ.get('SMA10SE_LANGUAGE', 'Nederlands'),
        'user_group': os.environ.get('SMA10SE_USER_GROUP', 'Installateur'),
        'password': os.environ.get('SMA10SE_PASSWORD', ''),
        'debug': True,
    }
    print(json.dumps(apply_mode(opts, mode_arg), indent=2))
