"""Admin-only setup bridge. No credentials and no direct .storage manipulation."""
from __future__ import annotations
import asyncio
import logging
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.helpers import device_registry as dr, entity_registry as er

DOMAIN = "dwars_setup"
BRIDGE_VERSION = "1.1.0"
_LOGGER = logging.getLogger(__name__)
DOMAINS = {"goodwe": "goodwe", "solaredge": "solaredge_modbus_multi"}


def inventory(hass):
    """Use registries, not entity-name guessing, to establish device ownership."""
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    result = []
    for platform, domain in DOMAINS.items():
        for entry in hass.config_entries.async_entries(domain):
            rows = er.async_entries_for_config_entry(registry, entry.entry_id)
            devs = {d.id: d for d in dr.async_entries_for_config_entry(devices, entry.entry_id)}
            roots = [d for d in devs.values() if not d.via_device_id or d.via_device_id not in devs]
            if platform == "goodwe":
                roots = roots[:1] or [None]
            for root in roots:
                owned = {root.id} if root else set(devs)
                changed = True
                while changed:
                    before = len(owned)
                    owned.update(d.id for d in devs.values() if d.via_device_id in owned)
                    changed = before != len(owned)
                serial = (root.serial_number if root else None) or (entry.unique_id if platform == "goodwe" else "")
                serial = str(serial or "").strip().upper()
                snapshots = []
                for row in rows:
                    if row.device_id not in owned:
                        continue
                    state = hass.states.get(row.entity_id)
                    dev = devs.get(row.device_id)
                    snapshots.append({
                        "entity_id": row.entity_id, "unique_id": row.unique_id,
                        "translation_key": row.translation_key,
                        "disabled_by": str(row.disabled_by) if row.disabled_by else None,
                        "device_id": row.device_id, "is_root": root is None or row.device_id == root.id,
                        "device_model": dev.model if dev else "",
                        "state": state.state if state else None,
                        "attributes": dict(state.attributes) if state else {},
                        "last_reported": getattr(state, "last_reported", state.last_updated).isoformat() if state else None,
                    })
                result.append({
                    "platform": platform, "entry_id": entry.entry_id, "serial": serial,
                    "host": entry.options.get("host", entry.data.get("host", "")),
                    "model": root.model if root else "",
                    "state": str(getattr(entry.state, "value", entry.state)),
                    "reason": str(getattr(entry, "reason", "") or "")[:400],
                    "disabled_by": str(getattr(entry, "disabled_by", "") or ""),
                    "config_version": entry.version,
                    "config_minor_version": entry.minor_version,
                    "entities": snapshots,
                })
    return result


async def _scan(hass, profile):
    data = hass.data[DOMAIN]
    data["status"] = "running"
    data["error"] = ""
    try:
        domain = DOMAINS[profile["platform"]]
        result = await asyncio.wait_for(hass.config_entries.flow.async_init(
            domain, context={"source": "import"},
            data={"dwars_discover": True, "hosts": profile.get("hosts", []),
                  "unit_ids": profile.get("unit_ids", [1])},
        ), timeout=1200)
        if result.get("reason") != "dwars_scan_complete":
            raise RuntimeError("Ontdekking kon niet worden afgerond: " + str(result.get("reason") or result.get("errors") or result.get("type")))
        data["status"] = "done"
    except asyncio.CancelledError:
        data["status"] = "interrupted"
        raise
    except Exception as err:
        data["status"] = "error"
        data["error"] = str(err)[:500]
        _LOGGER.exception("DWARS automatic discovery failed")


@websocket_api.websocket_command({vol.Required("type"): "dwars_setup/run", vol.Required("profile"): dict})
@websocket_api.async_response
async def ws_run(hass, connection, msg):
    if not connection.user or not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Administrator required")
        return
    profile = msg["profile"]
    if profile.get("platform") not in DOMAINS:
        connection.send_error(msg["id"], "invalid_platform", "Unknown platform")
        return
    # API-side bounds also protect this admin endpoint from malformed automation.
    import ipaddress
    try:
        hosts = profile.get("hosts", [])
        units = profile.get("unit_ids", [1])
        if not isinstance(hosts, list) or len(hosts) > 64 or not isinstance(units, list) or not 1 <= len(units) <= 32:
            raise ValueError("Invalid discovery scope")
        for host in hosts:
            ipaddress.IPv4Address(host)
        if any(type(i) is not int or not 1 <= i <= 247 for i in units):
            raise ValueError("Invalid Modbus unit ID")
    except (ValueError, TypeError) as err:
        connection.send_error(msg["id"], "invalid_scope", str(err))
        return
    data = hass.data[DOMAIN]
    task = data.get("task")
    if not task or task.done():
        data["status"] = "running"
        data["task"] = hass.async_create_background_task(_scan(hass, profile), "DWARS discovery")
    connection.send_result(msg["id"], {"status": data["status"]})


@websocket_api.websocket_command({vol.Required("type"): "dwars_setup/status"})
@websocket_api.async_response
async def ws_status(hass, connection, msg):
    if not connection.user or not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Administrator required")
        return
    data = hass.data[DOMAIN]
    connection.send_result(msg["id"], {"bridge_version": BRIDGE_VERSION, "status": data.get("status", "idle"),
                                     "error": data.get("error", ""), "devices": inventory(hass)})


@websocket_api.websocket_command({vol.Required("type"): "dwars_setup/enable", vol.Required("entities"): [str]})
@websocket_api.async_response
async def ws_enable(hass, connection, msg):
    if not connection.user or not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Administrator required")
        return
    registry = er.async_get(hass)
    enabled = []
    for entity_id in msg["entities"][:64]:
        row = registry.async_get(entity_id)
        if row and row.platform in DOMAINS.values() and row.disabled_by == er.RegistryEntryDisabler.INTEGRATION:
            registry.async_update_entity(entity_id, disabled_by=None)
            enabled.append(entity_id)
    connection.send_result(msg["id"], {"enabled": enabled})


async def async_setup_entry(hass, entry):
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {"status": "idle"}
        for handler in (ws_run, ws_status, ws_enable):
            websocket_api.async_register_command(hass, handler)
    return True


async def async_unload_entry(hass, entry):
    task = hass.data.get(DOMAIN, {}).get("task")
    if task and not task.done():
        task.cancel()
    return True
