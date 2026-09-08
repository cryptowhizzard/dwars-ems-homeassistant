#!/usr/bin/env python3
"""Persistent DWARS OneShot + ingress UI. All jobs run outside the browser.

The legacy updater is launched only after commissioning (or in manual mode).
No long-lived token is created; every app uses its own Supervisor credential.
"""
from __future__ import annotations
import asyncio
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import signal
import time
from urllib.parse import urlsplit
import uuid

from aiohttp import ClientSession, ClientTimeout, web
from oneshot_common import (
    AGENT, DOMAIN, VERSION, REQUIRED, Blocked, api_base, atomic_json, bind_device,
    choose_mode, directory_hash, endpoint, extract_payload, install_component,
    load_json, merge_options, validate_profile,
)

STAGES = ["profile", "payload", "components", "restart", "bridge", "discover", "mapping", "agent", "verify", "complete"]
LABELS = {
    "profile": "Klantconfiguratie ophalen", "payload": "Installatiebestanden ophalen",
    "components": "Home Assistant-integraties plaatsen", "restart": "Home Assistant herstarten en controleren",
    "bridge": "Lokale installatiebrug activeren", "discover": "Omvormers ontdekken en toevoegen",
    "mapping": "Batterij en sensoren controleren", "agent": "DWARS-agent configureren en starten",
    "verify": "Ontvangst van telemetrie bij BMS controleren", "complete": "Installatie gereed",
}


class APIError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class OneShot:
    def __init__(self, data=None, app_dir=None):
        self.data = Path(data or os.environ.get("STATE_DIR", "/data"))
        self.app_dir = Path(app_dir or "/app")
        self.data.mkdir(parents=True, exist_ok=True)
        self.options_path = Path(os.environ.get("CONFIG_PATH", str(self.data / "options.json")))
        self.options = load_json(self.options_path)
        self.mode = choose_mode(self.options, self.data)
        self.state_path = self.data / "oneshot_state.json"
        self.credentials_path = self.data / "oneshot_credentials.json"
        self.state = load_json(self.state_path, {
            "installation_id": str(uuid.uuid4()), "stage": "profile", "status": "waiting",
            "message": "Voer de API-key in om te beginnen.", "devices": [], "restart_requests": 0,
        })
        self.credentials = load_json(self.credentials_path)
        self.csrf = secrets.token_urlsafe(32)
        self.wake = asyncio.Event()
        self.session = None
        self.maintenance = None
        self.worker_task = None
        self.config_dir = Path(os.environ.get("HA_CONFIG_DIR", "/config"))
        if not self.config_dir.exists() and Path("/homeassistant_config").exists():
            self.config_dir = Path("/homeassistant_config")
        self.base = api_base(self.options.get("oneshot_api_base_url", "https://api.metdezon.nl/bms/api/"))
        self.supervisor = os.environ.get("SUPERVISOR_API", "http://supervisor").rstrip("/")
        self.profile = load_json(self.data / "oneshot_profile.json")
        self.lock = None

    def token(self):
        for key in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
            if os.environ.get(key):
                return os.environ[key]
            for root in ("/run/s6/container_environment", "/var/run/s6/container_environment"):
                p = Path(root) / key
                if p.exists():
                    return p.read_text().strip("\x00\r\n")
        raise Blocked("Supervisor-token ontbreekt; start de app vanuit Home Assistant OS.")

    def safe(self, message):
        text = str(message)
        for secret in (self.credentials.get("api_key"), os.environ.get("SUPERVISOR_TOKEN"), os.environ.get("HASSIO_TOKEN")):
            if secret:
                text = text.replace(secret, "[afgeschermd]")
        return text[:900]

    def save(self, **values):
        self.state.update(values)
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(self.state_path, self.state)

    def public(self):
        return {
            **{key: self.state.get(key) for key in ("installation_id", "stage", "status", "message", "devices", "updated_at", "client_name", "platform")},
            "has_key": bool(self.credentials.get("api_key")), "bound": bool(self.state.get("client_id")), "version": VERSION,
            "mode": self.mode, "label": LABELS.get(self.state.get("stage"), ""),
            "steps": [{"id": key, "label": LABELS[key]} for key in STAGES],
        }

    async def request(self, method, url, payload=None, token=None, key=None, timeout=60):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if key:
            headers["X-API-Key"] = key
        async with self.session.request(method, url, json=payload, headers=headers,
                                        timeout=ClientTimeout(total=timeout), allow_redirects=False) as response:
            # Never follow redirects while carrying the customer's API key.
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > 4_000_000:
                    raise Blocked("API-antwoord te groot.")
                chunks.append(chunk)
            body = b"".join(chunks)
            if response.status >= 300:
                detail = ""
                with contextlib.suppress(ValueError, UnicodeDecodeError):
                    error_body = json.loads(body)
                    if isinstance(error_body, dict) and isinstance(error_body.get("message"), str):
                        detail = ": " + self.safe(error_body["message"])
                raise APIError(response.status, f"{method} {urlsplit(url).path}: HTTP {response.status}" + detail)
            try:
                parsed = json.loads(body) if body else {}
            except (ValueError, UnicodeDecodeError) as err:
                raise APIError(response.status, "API gaf geen geldig JSON-antwoord.") from err
            if isinstance(parsed, dict) and (parsed.get("result") == "error" or parsed.get("ok") is False):
                raise APIError(response.status, str(parsed.get("message") or parsed.get("error") or "API-fout"))
            return parsed

    async def sup(self, method, path, payload=None, timeout=60):
        response = await self.request(method, self.supervisor + path, payload, token=self.token(), timeout=timeout)
        return response.get("data", response)

    async def ha(self, method, path, payload=None, timeout=60):
        return await self.request(method, self.supervisor + "/core/api" + path, payload, token=self.token(), timeout=timeout)

    async def bms(self, name, payload=None, query=""):
        return await self.request("POST" if payload is not None else "GET", endpoint(self.base, name) + query,
                                  payload, key=self.credentials["api_key"])

    async def ws(self, command, **values):
        url = self.supervisor.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/core/websocket"
        async with self.session.ws_connect(url, timeout=30, heartbeat=20, max_msg_size=8_000_000) as socket:
            hello = await socket.receive_json(timeout=30)
            if hello.get("type") != "auth_required":
                raise APIError(401, "Onverwachte Home Assistant WebSocket-handshake.")
            await socket.send_json({"type": "auth", "access_token": self.token()})
            auth = await socket.receive_json(timeout=30)
            if auth.get("type") != "auth_ok":
                raise APIError(401, "Home Assistant weigert de eigen Supervisor-token.")
            await socket.send_json({"id": 1, "type": command, **values})
            while True:
                message = await socket.receive_json(timeout=60)
                if message.get("id") == 1:
                    if not message.get("success"):
                        raise APIError(400, str(message.get("error", {}).get("message", "WebSocket-fout")))
                    return message.get("result", {})

    async def report(self):
        if not self.credentials.get("api_key") or not self.state.get("client_id"):
            return
        payload = {key: self.state.get(key) for key in ("installation_id", "stage", "status", "message", "devices")}
        payload["installer_version"] = VERSION
        try:
            await self.bms("install_status.php", payload)
        except Exception as err:
            print("[DWARS OneShot] Statusmelding nog niet afgeleverd: " + self.safe(err), flush=True)

    async def profile_stage(self):
        response = await self.bms("install_profile.php")
        profile = validate_profile(response.get("profile"))
        if self.state.get("client_id") and self.state["client_id"] != profile["client_id"]:
            raise Blocked("Deze Raspberry is al aan een andere klant gekoppeld. Bestaande configuratie niet overschreven.")
        if self.state.get("platform") and self.state["platform"] != profile["platform"] and self.state["stage"] not in {"profile", "payload"}:
            raise Blocked("Omvormerplatform is tijdens de installatie gewijzigd. Bestaande agents worden niet omgezet of overschreven.")
        self.profile = profile
        atomic_json(self.data / "oneshot_profile.json", profile)
        self.save(client_id=profile["client_id"], client_name=profile.get("client_name", ""), platform=profile["platform"])

    async def payload_stage(self):
        root_path = self.state.get("payload_root")
        if root_path and (Path(root_path) / "custom_components/dwars_setup/manifest.json").exists():
            return
        url = self.options.get("github_repo_zip_url", "https://github.com/cryptowhizzard/dwars-ems-homeassistant/archive/refs/heads/main.zip")
        if urlsplit(url).scheme != "https":
            raise Blocked("Repository-download moet HTTPS gebruiken.")
        archive = self.data / "oneshot_repository.zip"
        async with self.session.get(url, timeout=ClientTimeout(total=180)) as response:
            if response.status != 200 or response.url.scheme != "https":
                raise APIError(response.status, "Repository-download mislukt.")
            total = 0
            with archive.open("wb") as stream:
                os.chmod(archive, 0o600)
                async for chunk in response.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > 50_000_000:
                        raise Blocked("Repository-download is te groot.")
                    stream.write(chunk)
        dest = self.data / "oneshot_payload"
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(mode=0o700)
        root = await asyncio.to_thread(extract_payload, archive, dest)
        needed = ["dwars_setup"] + ([DOMAIN[self.profile["platform"]]] if DOMAIN[self.profile["platform"]] else [])
        for component in needed:
            if not (root / "custom_components" / component / "manifest.json").exists():
                raise Blocked("Repository mist component: " + component)
        self.save(payload_root=str(root))

    async def components_stage(self):
        root = Path(self.state["payload_root"])
        components = ["dwars_setup"] + ([DOMAIN[self.profile["platform"]]] if DOMAIN[self.profile["platform"]] else [])
        for name in components:
            src = root / "custom_components" / name
            dst = self.config_dir / "custom_components" / name
            if directory_hash(src) != directory_hash(dst):
                # Persist BEFORE mutation, so power failure cannot lose restart intent.
                self.save(restart_needed=True)
                await asyncio.to_thread(install_component, src, dst)

    async def subprocess(self, *args, timeout=1800, env=None):
        process = await asyncio.create_subprocess_exec(*args, env=env, start_new_session=True)
        try:
            code = await asyncio.wait_for(process.wait(), timeout)
            if code:
                raise RuntimeError(f"Installatiestap eindigde met status {code}.")
        except BaseException:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            raise

    async def core_ready(self):
        info = await self.ha("GET", "/config")
        return isinstance(info, dict) and bool(info.get("version")) and str(info.get("state", "RUNNING")).lower() == "running"

    async def _request_core_restart(self):
        if self.state.get("restart_requests", 0) >= 2:
            raise Blocked("Core-herstart niet bevestigd na twee pogingen. Controleer het Core-log; sleutel en voortgang zijn bewaard. Geen herstartlus gestart.")
        self.save(restart_requested_at=time.time(), restart_acknowledged=False,
                  restart_requests=self.state.get("restart_requests", 0) + 1)
        try:
            await self.sup("POST", "/core/restart", {}, timeout=180)
            self.save(restart_acknowledged=True)
        except APIError as err:
            if 400 <= err.status < 500:
                raise  # An authorization/validation failure is not a successful restart.
        except (asyncio.TimeoutError, OSError):
            pass  # Response can be lost while Core is restarting.
        await asyncio.sleep(15)

    async def _ensure_core(self):
        env = {**os.environ, "CONFIG_PATH": str(self.options_path)}
        await self.subprocess("python3", str(self.app_dir / "auto_updater.py"), "--ensure-core", "--lock-held",
                              "--options", str(self.options_path), "--state", str(self.data / "dwars_auto_update_state.json"),
                              "--lock", str(self.data / "dwars_maintenance.lock"), timeout=1200, env=env)
        if not await self.core_ready():
            raise RuntimeError("Home Assistant Core is nog niet gereed; voortgang blijft bewaard.")

    async def restart_stage(self):
        if self.state.get("restart_needed") and not self.state.get("restart_requested_at"):
            await self._request_core_restart()
        await self._ensure_core()
        if self.state.get("restart_needed") and not self.state.get("restart_acknowledged"):
            # Recover the narrow crash window between saving intent and sending
            # the request. First try the actual new component; never blindly reboot.
            try:
                await self.bridge_stage()
                self.save(restart_acknowledged=True)
            except APIError as err:
                if err.status in (401, 403):
                    raise
                await self._request_core_restart()
                await self._ensure_core()
            except (Blocked, RuntimeError):
                await self._request_core_restart()
                await self._ensure_core()

    async def bridge_stage(self):
        entries = await self.ha("GET", "/config/config_entries/entry?domain=dwars_setup")
        if not entries:
            flow = await self.ha("POST", "/config/config_entries/flow", {"handler": "dwars_setup"})
            if flow.get("type") == "form":
                flow = await self.ha("POST", "/config/config_entries/flow/" + flow["flow_id"], {})
            if flow.get("type") != "create_entry" and flow.get("reason") != "already_configured":
                raise Blocked("Installatiebrug kon niet worden geactiveerd.")
        for _ in range(30):
            try:
                await self.ws("dwars_setup/status")
                return
            except APIError:
                await asyncio.sleep(2)
        raise RuntimeError("Installatiebrug is nog niet beschikbaar; controleer Home Assistant-log.")

    def save_devices(self, devices, selected=None):
        self.save(devices=[{
            "serial": d.get("serial", ""), "host": d.get("host", ""), "model": d.get("model", ""),
            "entry_id": d.get("entry_id", ""),
            "role": "besturing" if selected and d.get("serial") == selected.get("serial") else "monitoring",
        } for d in devices if d.get("platform") == self.profile["platform"]])

    async def discover_stage(self):
        if self.profile["platform"] == "other":
            return
        await self.ws("dwars_setup/run", profile={k: self.profile[k] for k in ("platform", "hosts", "unit_ids")})
        for _ in range(250):
            result = await self.ws("dwars_setup/status")
            self.save_devices(result.get("devices", []))
            if result.get("status") == "done":
                return
            if result.get("status") in {"error", "interrupted"}:
                raise RuntimeError(result.get("error") or "Ontdekking onderbroken; wordt hervat.")
            await asyncio.sleep(5)
        raise RuntimeError("Ontdekking duurt te lang. Voortgang blijft bewaard.")

    async def mapping_stage(self):
        platform = self.profile["platform"]
        last_error = None
        for _ in range(40):
            devices = [] if platform == "other" else (await self.ws("dwars_setup/status")).get("devices", [])
            self.save_devices(devices)
            try:
                device, mapping = bind_device(self.profile, devices)
                if platform == "other" and any(not mapping.get(k) for k in REQUIRED[platform]):
                    raise Blocked("Anders vereist een vooraf ingevuld entiteitsprofiel in EMS (SoC, netvermogen en modus-select).")
                disabled = [r["entity_id"] for r in device.get("entities", []) if r.get("disabled_by") and r["entity_id"] in mapping.values()]
                if disabled:
                    await self.ws("dwars_setup/enable", entities=disabled)
                await self.validate_entities(mapping, device)
                self.save_devices(devices, device)
                self.save(mapping=mapping, selected_device={k: device.get(k) for k in ("serial", "entry_id", "host")})
                return
            except Blocked as err:
                last_error = err
                if "Meerdere batterij" in str(err) or "Anders vereist" in str(err):
                    raise
            await asyncio.sleep(5)
        # Refresh discovery on retry: a newly connected second inverter must not
        # remain invisible just because the first scan already succeeded.
        self.save(stage="discover" if platform != "other" else "mapping")
        raise last_error or Blocked("Sensoren nog niet gereed.")

    async def validate_entities(self, options, device):
        platform = self.profile["platform"]
        owned = {r["entity_id"] for r in device.get("entities", [])}
        keys = list(REQUIRED[platform])
        if platform == "other":
            if options.get("ha_power_number"):
                keys.append("ha_power_number")
            elif options.get("ha_charge_power_number") and options.get("ha_discharge_power_number"):
                keys.extend(("ha_charge_power_number", "ha_discharge_power_number"))
            else:
                raise Blocked("Anders vereist naast sensoren en modus ook een vermogensbegrenzing in het EMS-entiteitsprofiel.")
        for key in keys:
            entity_id = options.get(key, "")
            if not isinstance(entity_id, str) or not entity_id or "," in entity_id or entity_id == "auto":
                raise Blocked("Geen eenduidige koppeling voor " + key)
            if platform != "other" and key not in {"grid_entity", "ha_grid_sensor"} and entity_id not in owned:
                raise Blocked("Bestaande mapping behoort niet aan de gekozen omvormer: " + key)
            try:
                state = await self.ha("GET", "/states/" + entity_id)
            except APIError as err:
                raise Blocked("Entiteit ontbreekt: " + entity_id) from err
            value = state.get("state")
            if value in (None, "unknown", "unavailable", "none", ""):
                raise Blocked("Entiteit nog niet beschikbaar: " + entity_id)
            if entity_id.startswith("sensor."):
                try:
                    number = float(value)
                    if not math.isfinite(number):
                        raise ValueError()
                except (TypeError, ValueError) as err:
                    raise Blocked("Geen geldige meetwaarde: " + entity_id) from err
                if "soc" in key and not 0 <= number <= 100:
                    raise Blocked("SoC buiten 0–100%: " + entity_id)
                timestamp = state.get("last_reported") or state.get("last_updated")
                if timestamp:
                    age = datetime.now(timezone.utc) - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if age.total_seconds() > 300:
                        raise Blocked("Meetwaarde ouder dan vijf minuten: " + entity_id)
            if key == "ha_mode_select" and platform == "other":
                choices = state.get("attributes", {}).get("options", [])
                expected = {options.get("ha_mode_idle_option", "auto"), options.get("ha_mode_charge_option", "charge"), options.get("ha_mode_discharge_option", "discharge")}
                if not expected.issubset(choices):
                    raise Blocked("Het Anders-profiel bevat modi die niet in de gekozen HA-select bestaan.")
            if key == "ha_ems_mode_select":
                choices = state.get("attributes", {}).get("options", [])
                if not {"charge_battery", "discharge_battery", "battery_standby"}.issubset(choices):
                    raise Blocked("GoodWe biedt niet de vereiste EMS-bedieningsmodi.")

    async def addon_slug(self, base):
        self_info = await self.sup("GET", "/addons/self/info")
        prefix = self_info["slug"].rsplit("_dwars_installer", 1)[0]
        wanted = prefix + "_" + base
        catalog = await self.sup("GET", "/store/addons")
        rows = catalog.get("addons", []) if isinstance(catalog, dict) else catalog
        if not any(row.get("slug") == wanted for row in rows):
            raise Blocked("Benodigde app ontbreekt in dezelfde DWARS-repository: " + base + ". Vernieuw de appwinkel na publicatie.")
        return wanted

    async def ensure_agent(self, slug):
        try:
            info = await self.sup("GET", f"/addons/{slug}/info")
        except APIError as err:
            if err.status not in (400, 404):
                raise
            # Synchronous endpoint is idempotently rechecked after a restart.
            await self.sup("POST", f"/store/addons/{slug}/install", {"background": False}, timeout=1800)
            info = await self.sup("GET", f"/addons/{slug}/info")
        current = info.get("options", {})
        if current.get("api_key") and current["api_key"] != self.credentials["api_key"]:
            raise Blocked("De bestaande agent heeft een andere API-key. Niet bijgewerkt of overschreven.")
        if current.get("installation_id") not in (None, "", self.state["installation_id"]):
            raise Blocked("Agent behoort aan een andere OneShot-installatie. Niet bijgewerkt of overschreven.")
        if info.get("update_available"):
            await self.sup("POST", f"/store/addons/{slug}/update", {"backup": True, "background": False}, timeout=1800)
            info = await self.sup("GET", f"/addons/{slug}/info")
        return info

    async def agent_stage(self):
        platform = self.profile["platform"]
        await self.sup("POST", "/store/reload", {}, timeout=120)
        slug = await self.addon_slug(AGENT[platform])
        installed = await self.sup("GET", "/addons")
        for row in installed.get("addons", []) if isinstance(installed, dict) else installed:
            other = row.get("slug", "")
            if other != slug and any(other.endswith("_" + base) for base in AGENT.values()):
                other_info = await self.sup("GET", "/addons/" + other + "/info")
                if other_info.get("state") == "started" and other_info.get("options", {}).get("ha_control_enabled", True):
                    raise Blocked("Een andere DWARS-besturingsagent is al actief: " + other + ". Stop deze eerst om dubbele besturing te voorkomen.")
        info = await self.ensure_agent(slug)
        current = info.get("options", {})
        other_key = current.get("api_key", "")
        if other_key and other_key != self.credentials["api_key"]:
            raise Blocked("De bestaande agent heeft een andere API-key. Niet overschreven.")
        defaults = load_json(Path(self.state["payload_root"]) / AGENT[platform] / "config.json")["options"]
        settings = self.profile.get("agent_config", {}).get("inverter", {})
        desired = {"api_key": self.credentials["api_key"], "client_id": self.profile["client_id"] if platform == "solaredge" else str(self.profile["client_id"]),
                   "api_url": endpoint(self.base, "next_action.php"),
                   "telemetry_url": endpoint(self.base, "heartbeat.php" if platform == "solaredge" else "telemetry.php"),
                   "installation_id": self.state["installation_id"], **self.state["mapping"]}
        if platform == "goodwe":
            desired.update({"power_watt": self.profile["power_watt"], "goodwe_serial_number": self.state["selected_device"]["serial"],
                "ha_url": "http://supervisor/core", "ha_control_enabled": True,
                "main_fuse_profile": settings.get("main_fuse_profile", "auto"),
                "goodwe_default_dod": int(settings.get("depth_of_discharge_pct", 90)),
                "goodwe_default_dod_on_grid": int(settings.get("depth_of_discharge_on_grid_pct", 90)),
                "standalone_enabled": bool(settings.get("standalone_enabled", False)),
                "standalone_pv_entity": settings.get("standalone_pv_entity", ""),
                "standalone_grid_entity": settings.get("standalone_grid_entity") or "auto",
                "standalone_max_charge_w": self.profile["power_watt"],
            })
        elif platform == "solaredge":
            desired.update({"hass_url": "http://supervisor/core", "auto_discover_entities": False})
            if not current.get("api_key"):
                desired["ha_pv_active_power_limit_default_percent"] = 100
                # An explicit EMS agent_options override is applied below.
        else:
            desired.update({"power_watt": self.profile["power_watt"], "ha_url": "http://supervisor/core", "ha_control_enabled": True})
        custom = self.profile.get("agent_options", {})
        for key, value in custom.items():
            if key not in defaults or key in {"api_key", "client_id", "api_url", "telemetry_url", "ha_token", "hass_token", "ha_url", "hass_url", "installation_id"} or isinstance(value, (dict, list)):
                raise Blocked("Niet toegestane profielinstelling: " + key)
            desired[key] = value
        merged, managed = merge_options(current, defaults, desired, self.state.get("managed_options", {}))
        for key, value in merged.items():
            if key in defaults and not isinstance(value, type(defaults[key])):
                # JSON integers are valid for numeric (float) fields, but booleans are not integers.
                if type(defaults[key]) is not float or type(value) not in (int, float):
                    raise Blocked("Verkeerd type voor agentinstelling: " + key)
            if key in defaults and type(defaults[key]) is int and type(value) is bool:
                raise Blocked("Verkeerd type voor agentinstelling: " + key)
        await self.validate_entities(merged, self.state.get("selected_inventory") or await self.selected_inventory())
        if current.get("installation_id") not in (None, "", self.state["installation_id"]):
            raise Blocked("Agent behoort al aan een andere OneShot-installatie. Niet overschreven.")
        if "installation_id" not in current and "installation_id" not in (info.get("schema") or {}):
            raise Blocked("Geïnstalleerde agent is nog niet de OneShot-versie; publiceer de volledige repository en vernieuw de winkel.")
        snapshot = self.data / ("oneshot_original_" + AGENT[platform] + ".json")
        if not snapshot.exists():
            atomic_json(snapshot, {"options": current, "boot": info.get("boot"), "state": info.get("state")})
        # Persist managed values and receipt baseline before changing/starting an app.
        receipt = (await self.bms("install_status.php", query="?installation_id=" + self.state["installation_id"])).get("receipt")
        self.save(agent_slug=slug, managed_options={k: v for k, v in managed.items() if k not in {"api_key", "ha_token", "hass_token"}},
                  effective_mapping={k: merged[k] for k in self.state["mapping"] if k in merged},
                  receipt_baseline=int((receipt or {}).get("receipt_count", 0)))
        # The Supervisor expects a flat options object INSIDE the single options key.
        if current != merged:
            if info.get("state") == "started":
                await self.sup("POST", f"/addons/{slug}/stop", {}, timeout=120)
            await self.sup("POST", f"/addons/{slug}/options", {"options": merged, "boot": "auto", "auto_update": True})
        else:
            await self.sup("POST", f"/addons/{slug}/options", {"boot": "auto", "auto_update": True})
        info = await self.sup("GET", f"/addons/{slug}/info")
        if info.get("state") != "started":
            await self.sup("POST", f"/addons/{slug}/start", {}, timeout=180)
        final = await self.sup("GET", f"/addons/{slug}/info")
        if final.get("state") != "started":
            raise RuntimeError("Agent is nog niet gestart.")

    async def selected_inventory(self):
        if self.profile["platform"] == "other":
            return {"entities": []}
        rows = (await self.ws("dwars_setup/status")).get("devices", [])
        selected = self.state["selected_device"]
        for row in rows:
            if row.get("entry_id") == selected.get("entry_id") and row.get("serial") == selected.get("serial"):
                return row
        raise Blocked("De gekozen omvormer is niet meer aanwezig.")

    async def verify_stage(self):
        for _ in range(45):
            info = await self.sup("GET", f"/addons/{self.state['agent_slug']}/info")
            if info.get("state") != "started":
                self.save(stage="agent")
                raise RuntimeError("Agent is gestopt; installatie is niet gereed.")
            result = await self.bms("install_status.php", query="?installation_id=" + self.state["installation_id"])
            receipt = result.get("receipt") or {}
            if int(receipt.get("receipt_count", 0)) > self.state.get("receipt_baseline", 0):
                if self.profile["platform"] == "goodwe" and str(receipt.get("inverter_serial", "")).upper() != self.state["selected_device"]["serial"]:
                    raise Blocked("BMS ontving telemetrie van een ander omvormerserienummer.")
                await self.validate_entities(self.state.get("effective_mapping", self.state["mapping"]), await self.selected_inventory())
                self.save(telemetry_verified_at=receipt.get("received_at"))
                return
            await asyncio.sleep(10)
        raise RuntimeError("Agent draait, maar BMS bevestigt nog geen nieuwe geldige telemetrie van deze installatie.")

    def effective_options(self):
        options = dict(self.options)
        options.update({"installation_mode": "manual", "inverter_type": "andere_omvormer" if self.profile["platform"] == "other" else self.profile["platform"],
                        "configure_agent_addons": False, "start_agent_addons": False,
                        "manage_oneshot_bridge": True})
        path = self.data / "oneshot_updater_options.json"
        atomic_json(path, options)
        return path

    async def start_maintenance(self):
        if self.maintenance and self.maintenance.returncode is None:
            return
        options_path = self.options_path if self.mode == "manual" else self.effective_options()
        env = {**os.environ, "DWARS_ONESHOT_BYPASS": "true", "CONFIG_PATH": str(options_path)}
        self.maintenance = await asyncio.create_subprocess_exec("bash", str(self.app_dir / "run.sh"), env=env, start_new_session=True)

    async def stop_maintenance(self):
        if self.maintenance and self.maintenance.returncode is None:
            os.killpg(self.maintenance.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.maintenance.wait(), 15)
            except asyncio.TimeoutError:
                os.killpg(self.maintenance.pid, signal.SIGKILL)
                await self.maintenance.wait()
        self.maintenance = None

    @contextlib.contextmanager
    def maintenance_lock(self):
        with (self.data / "dwars_maintenance.lock").open("a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as err:
                raise RuntimeError("Een onderhoudstaak is nog bezig; OneShot probeert opnieuw.") from err
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    async def worker(self):
        while True:
            self.wake.clear()
            if self.mode == "manual":
                await self.start_maintenance()
            elif self.credentials.get("api_key") and self.state.get("stage") != "complete":
                try:
                    with self.maintenance_lock():
                        await self.profile_stage()  # refresh profile/validity on every retry
                        while self.state["stage"] != "complete":
                            stage = self.state["stage"]
                            self.save(status="running", message=LABELS[stage])
                            await self.report()
                            await getattr(self, stage + "_stage")()
                            self.save(stage=STAGES[STAGES.index(stage) + 1])
                        self.save(status="complete", message="Installatie gereed. Geldige telemetrie is door BMS bevestigd.")
                        await self.report()
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    label = "blocked" if isinstance(err, Blocked) or isinstance(err, APIError) and err.status in (401, 403, 422) else "waiting"
                    message = self.safe(err)
                    if isinstance(err, APIError) and err.status in (401, 403):
                        message = "API-key of API-toegang geweigerd. Controleer de klant en bevoegdheden; geen nieuwe herstart uitgevoerd."
                    self.save(status=label, message=message)
                    print("[DWARS OneShot] " + message, flush=True)
                    await self.report()
            if self.mode == "oneshot" and self.state.get("stage") == "complete":
                await self.start_maintenance()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=max(30, int(self.options.get("oneshot_retry_seconds", 60))))
            except asyncio.TimeoutError:
                pass

    async def start(self, app):
        self.session = ClientSession()
        self.worker_task = asyncio.create_task(self.worker())

    async def stop(self, app):
        if self.worker_task:
            self.worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker_task
        await self.stop_maintenance()
        if self.session:
            await self.session.close()

    async def handle_status(self, request):
        return web.json_response(self.public(), headers={"Cache-Control": "no-store"})

    async def handle_start(self, request):
        if self.mode == "manual":
            raise web.HTTPConflict(text="Bestaande handmatige installatie: zet installation_mode expliciet op oneshot om deze functie te gebruiken.")
        if self.state.get("client_id") or self.state.get("status") == "running":
            raise web.HTTPConflict(text="Deze installatie is al gekoppeld. Gebruik opnieuw controleren; de sleutel blijft bewaard.")
        body = await request.json()
        key = body.get("api_key", "") if isinstance(body, dict) else ""
        if not isinstance(key, str) or not 8 <= len(key.strip()) <= 512 or any(ord(c) < 32 for c in key):
            raise web.HTTPBadRequest(text="Ongeldige API-key.")
        self.credentials = {"api_key": key.strip()}
        atomic_json(self.credentials_path, self.credentials)
        self.save(status="waiting", stage="profile", message="API-key ontvangen; installatie wordt gestart.")
        self.wake.set()
        return web.json_response({"ok": True})

    async def handle_retry(self, request):
        if self.mode == "manual" or not self.credentials.get("api_key"):
            raise web.HTTPConflict(text="Geen OneShot-installatie actief.")
        if self.state.get("status") == "running":
            raise web.HTTPConflict(text="Installatie is al bezig.")
        # Do not interrupt a running updater or allow an installation race.
        try:
            with self.maintenance_lock():
                await self.stop_maintenance()
                if self.state.get("stage") == "complete":
                    self.save(stage="discover" if self.profile["platform"] != "other" else "mapping")
                self.save(status="waiting", message="Opnieuw controleren met opgeslagen API-key.")
        except RuntimeError as err:
            raise web.HTTPConflict(text=self.safe(err)) from err
        self.wake.set()
        return web.json_response({"ok": True})

    async def handle_index(self, request):
        text = (self.app_dir / "oneshot.html").read_text()
        prefix = request.headers.get("X-Ingress-Path", "").rstrip("/")
        text = text.replace("__CSRF__", json.dumps(self.csrf)).replace("__BASE__", json.dumps(prefix).replace("<", "\\u003c"))
        return web.Response(text=text, content_type="text/html", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})

    def application(self):
        @web.middleware
        async def access(request, handler):
            # There are no published LAN ports. Also reject direct calls from
            # other containers: only Supervisor's ingress proxy is accepted.
            allowed = os.environ.get("DWARS_INGRESS_PROXY", "172.30.32.2")
            if request.remote != allowed:
                raise web.HTTPForbidden(text="Open deze app via Home Assistant.")
            if request.method == "POST" and not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), self.csrf):
                raise web.HTTPForbidden(text="Ververs de pagina om de sessie te vernieuwen.")
            return await handler(request)
        app = web.Application(middlewares=[access], client_max_size=8192)
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/api/status", self.handle_status)
        app.router.add_post("/api/start", self.handle_start)
        app.router.add_post("/api/retry", self.handle_retry)
        app.on_startup.append(self.start)
        app.on_cleanup.append(self.stop)
        return app


if __name__ == "__main__":
    os.umask(0o077)
    installer = OneShot()
    print(f"[DWARS OneShot] {VERSION}; mode={installer.mode}; stage={installer.state['stage']}", flush=True)
    web.run_app(installer.application(), host="0.0.0.0", port=8099, print=None, access_log=None)
