"""Pure OneShot validation, entity binding and durable file operations."""
from __future__ import annotations
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from urllib.parse import urljoin, urlsplit
import zipfile

VERSION = "0.6.0"
DOMAIN = {"goodwe": "goodwe", "solaredge": "solaredge_modbus_multi", "other": None}
AGENT = {"goodwe": "goodwe_agent", "solaredge": "solaredge_agent", "other": "dwars_addon"}


class Blocked(Exception):
    """Configuration must change; no unsafe defaults or arbitrary device choice."""


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dwars-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_json(path: Path, default=None):
    if not path.exists():
        return {} if default is None else default
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise Blocked(f"Ongeldige JSON-opslag: {path.name}; niet automatisch overschreven.")
    return value


def api_base(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Blocked("BMS-basisadres moet HTTPS zijn, zonder credentials of queryparameters.")
    return value.rstrip("/") + "/"


def endpoint(base: str, name: str) -> str:
    if not re.fullmatch(r"[a-z_]+\.php", name):
        raise Blocked("BMS gaf een onveilig endpoint terug.")
    return urljoin(api_base(base), name)


def validate_profile(profile):
    if not isinstance(profile, dict) or profile.get("schema_version") != 1:
        raise Blocked("Onbekend BMS-installatiecontract. Werk BMS bij.")
    if profile.get("platform") not in DOMAIN or type(profile.get("client_id")) is not int or profile["client_id"] <= 0:
        raise Blocked("Onbekend platform of klantnummer.")
    if profile.get("ems_enabled", True) is not True:
        raise Blocked("EMS staat voor deze klant uit. Er wordt geen batterijbesturing gestart.")
    if type(profile.get("power_watt")) is not int or not 0 < profile["power_watt"] <= 1000000:
        raise Blocked("Batterijvermogen ontbreekt of is ongeldig.")
    if not isinstance(profile.get("agent_options", {}), dict):
        raise Blocked("agent_options moet een object zijn.")
    if not isinstance(profile.get("agent_config", {}), dict):
        raise Blocked("agent_config moet een object zijn.")
    hosts, units = profile.get("hosts", []), profile.get("unit_ids", [1])
    if not isinstance(hosts, list) or len(hosts) > 64 or not isinstance(units, list) or not 1 <= len(units) <= 32:
        raise Blocked("Ongeldig netwerkscanprofiel.")
    try:
        for host in hosts:
            if not isinstance(host, str):
                raise ValueError()
            ipaddress.IPv4Address(host)
    except ValueError as err:
        raise Blocked("Het installatieprofiel bevat een ongeldig IPv4-adres.") from err
    if any(type(unit) is not int or not 1 <= unit <= 247 for unit in units):
        raise Blocked("Ongeldig Modbus unit-ID.")
    if type(profile.get("expected_inverters", 0)) is not int or not 0 <= profile.get("expected_inverters", 0) <= 64:
        raise Blocked("Ongeldig verwacht aantal omvormers.")
    serial = profile.get("control_serial", "")
    if not isinstance(serial, str) or (serial and not re.fullmatch(r"[A-Za-z0-9_.:-]{3,96}", serial)):
        raise Blocked("Ongeldig besturingsserienummer.")
    return {**profile, "hosts": hosts, "unit_ids": units}


def choose_mode(options: dict, data: Path) -> str:
    requested = options.get("installation_mode", "auto")
    if requested in {"manual", "oneshot"}:
        return requested
    if (data / "oneshot_state.json").exists() or (data / "oneshot_credentials.json").exists():
        return "oneshot"
    if any(options.get(k) for k in ("goodwe_agent_api_key", "solaredge_agent_api_key", "dwars_addon_api_key")):
        return "manual"
    if any(data.glob("*.payload.sha256")) or (data / "dwars_auto_update_state.json").exists():
        return "manual"
    return "oneshot"


def extract_payload(archive: Path, dest: Path) -> Path:
    """Reject traversal, symlinks and zip bombs before writing anything."""
    with zipfile.ZipFile(archive) as z:
        items = z.infolist()
        if len(items) > 20000 or sum(i.file_size for i in items) > 150_000_000:
            raise Blocked("Repository-ZIP is te groot.")
        for item in items:
            target = (dest / item.filename).resolve()
            if not target.is_relative_to(dest.resolve()) or stat.S_ISLNK(item.external_attr >> 16) or "\\" in item.filename:
                raise Blocked("Onveilige repository-ZIP geweigerd.")
        z.extractall(dest)
    roots = [p for p in [dest, *dest.iterdir()] if p.is_dir() and (p / "custom_components/dwars_setup/manifest.json").exists()]
    if len(roots) != 1:
        raise Blocked("GitHub bevat nog niet de OneShot-release. Publiceer eerst het volledige GitHub-pakket.")
    return roots[0]


def directory_hash(directory: Path) -> str:
    if not directory.is_dir():
        return "missing"
    h = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        h.update(str(path.relative_to(directory)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())
    return h.hexdigest()


def install_component(src: Path, dst: Path):
    """Stage full copy, swap on the config filesystem, retain one rollback copy."""
    if directory_hash(src) == directory_hash(dst):
        return False
    if not (src / "manifest.json").is_file():
        raise Blocked("Onvolledige custom component: " + src.name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    staged = dst.with_name(".dwars-stage-" + dst.name)
    backup = dst.with_name(".dwars-backup-" + dst.name)
    shutil.rmtree(staged, ignore_errors=True)
    shutil.copytree(src, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Recover interruption between old->backup and staged->destination.
    had_previous = dst.exists()
    if had_previous:
        shutil.rmtree(backup, ignore_errors=True)
        os.replace(dst, backup)
    try:
        os.replace(staged, dst)
    except BaseException:
        if not dst.exists() and backup.exists():
            os.replace(backup, dst)
        raise
    return True


def _normalized(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")


def match_entity(device, domains, aliases, root=None):
    """Select by integration identity, NEVER across physical inverter groups."""
    ranked = []
    for row in device.get("entities", []):
        if row["entity_id"].split(".")[0] not in domains:
            continue
        if root is not None and bool(row.get("is_root")) != root:
            continue
        uid = _normalized(row.get("unique_id"))
        # GoodWe's unique-id is goodwe-<sensor key>-<serial>.
        serial = _normalized(device.get("serial"))
        if uid.startswith("goodwe_") and serial and uid.endswith("_" + serial):
            uid = uid[len("goodwe_"):-len(serial)-1]
        fields = [_normalized(row.get("translation_key")), uid]
        best = 0
        for priority, alias in enumerate(aliases):
            for field in fields:
                if field == alias or field.endswith("_" + alias):
                    best = max(best, 100 - priority)
        if best:
            ranked.append((best, row))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    tied = [row for score, row in ranked if score == ranked[0][0]]
    if len(tied) > 1:
        raise Blocked("Meer dan één passende entiteit voor " + aliases[0] + "; leg de mapping vast in EMS.")
    return ranked[0][1]


GOODWE_MAP = {
    "soc_entity": (("sensor",), ("battery_soc", "battery_state_of_charge", "battery_soc_1", "soc")),
    "pv_entity": (("sensor",), ("ppv", "pv_power", "total_pv_power")),
    "grid_entity": (("sensor",), ("active_power_total", "meter_active_power_total", "grid_active_power", "active_power", "pgrid")),
    "battery_power_entity": (("sensor",), ("pbattery1", "pbattery", "battery_power", "battery_power_1")),
    "ha_ems_mode_select": (("select",), ("ems_mode",)),
    "ha_ems_power_number": (("number",), ("ems_power_limit",)),
    "ha_dod_holding_switch": (("switch",), ("dod_holding", "dod_holding_switch")),
    "ha_backup_supply_switch": (("switch",), ("backup_supply", "backup_supply_switch")),
    "ha_dod_number": (("number",), ("battery_discharge_depth_offline",)),
    "ha_dod_on_grid_number": (("number",), ("battery_discharge_depth",)),
    "ha_operation_mode_select": (("select",), ("operation_mode",)),
    "ha_grid_export_limit_number": (("number",), ("grid_export_limit",)),
    "ha_grid_export_limit_switch": (("switch",), ("grid_export_limit_switch",)),
}
SOLAREDGE_MAP = {
    "ha_soc_sensor": (("sensor",), ("state_of_energy", "battery_soe", "battery_soc", "soe")),
    "ha_pv_sensor": (("sensor",), ("dc_power",)),
    "ha_grid_sensor": (("sensor",), ("ac_power",)),
    "ha_cmd_mode_select": (("select",), ("storage_command_mode", "storage_remote_command_mode")),
    "ha_default_mode_select": (("select",), ("storage_default_mode",)),
    "ha_control_mode_select": (("select",), ("storage_control_mode",)),
    "ha_command_timeout_number": (("number",), ("storage_command_timeout", "storage_remote_command_timeout")),
    "ha_remote_charge_limit_number": (("number",), ("storage_charge_limit", "storage_remote_charge_limit")),
    "ha_remote_discharge_limit_number": (("number",), ("storage_discharge_limit", "storage_remote_discharge_limit")),
    "ha_pv_active_power_limit_number": (("number",), ("active_power_limit",)),
}
REQUIRED = {
    "goodwe": ("soc_entity", "grid_entity", "battery_power_entity", "ha_ems_mode_select", "ha_ems_power_number"),
    "solaredge": ("ha_soc_sensor", "ha_grid_sensor", "ha_cmd_mode_select", "ha_default_mode_select", "ha_control_mode_select", "ha_command_timeout_number", "ha_remote_charge_limit_number", "ha_remote_discharge_limit_number"),
    "other": ("soc_entity", "grid_entity", "ha_mode_select"),
}


def bind_device(profile, devices):
    platform = profile["platform"]
    candidates = [d for d in devices if d.get("platform") == platform]
    expected = int(profile.get("expected_inverters", 0))
    if expected and len(candidates) < expected:
        raise Blocked(f"{len(candidates)} van {expected} verwachte omvormers gevonden. Controleer netwerk/Modbus.")
    if platform == "other":
        return {"serial": "", "entities": []}, dict(profile.get("agent_options", {}))
    schema = GOODWE_MAP if platform == "goodwe" else SOLAREDGE_MAP
    targets = []
    for device in candidates:
        wanted = profile.get("control_serial", "").strip().upper()
        if wanted and device.get("serial", "").upper() != wanted:
            continue
        mappings = {}
        for option, (domains, aliases) in schema.items():
            root = (False if option == "ha_grid_sensor" else True if option == "ha_pv_sensor" else None)
            explicit = profile.get("agent_options", {}).get(option)
            if explicit:
                mappings[option] = explicit
                continue
            row = match_entity(device, domains, aliases, root=root)
            if row:
                mappings[option] = row["entity_id"]
        soc_key = "soc_entity" if platform == "goodwe" else "ha_soc_sensor"
        control_key = "ha_ems_mode_select" if platform == "goodwe" else "ha_cmd_mode_select"
        if mappings.get(soc_key) and mappings.get(control_key):
            targets.append((device, mappings))
    if not targets:
        raise Blocked("Nog geen ondersteunde batterij-omvormer met SoC en bedieningsentiteiten gevonden. Controleer verbinding, entiteiten en het eventuele serienummer in EMS.")
    if len(targets) > 1:
        raise Blocked("Meerdere batterij-omvormers gevonden. Vul in EMS het te besturen serienummer in; er wordt geen totaalvermogen naar meerdere apparaten gestuurd.")
    device, mapping = targets[0]
    for key in REQUIRED[platform]:
        if not mapping.get(key):
            raise Blocked("Benodigde sensor/bediening ontbreekt: " + key)
    return device, mapping


def merge_options(current, defaults, desired, prior_managed=None):
    """Preserve explicit existing mappings; update only our own last values."""
    merged = dict(defaults)
    merged.update(current)
    managed = {}
    prior_managed = prior_managed or {}
    entity_keys = set(GOODWE_MAP) | set(SOLAREDGE_MAP) | {
        "ha_mode_select", "ha_power_number", "ha_charge_power_number", "ha_discharge_power_number",
        "ha_idle_power_number", "inverter_mode_entity",
    }
    for key, value in desired.items():
        if key in entity_keys:
            old = current.get(key)
            installer_owns = key in prior_managed and old == prior_managed[key]
            if old not in (None, "", "auto") and old != defaults.get(key) and not installer_owns:
                continue
        merged[key] = value
        managed[key] = value
    return merged, managed
