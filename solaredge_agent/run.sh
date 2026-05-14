#!/usr/bin/with-contenv bashio
set -euo pipefail

cfg_required() {
  bashio::config "$1"
}

cfg_optional() {
  bashio::config "$1" 2>/dev/null || true
}

empty_if_null() {
  local value="${1:-}"
  if [ "$value" = "null" ]; then
    echo ""
  else
    echo "$value"
  fi
}

# ============================================================
# Add-on metadata + Home Assistant backup automation check
# ============================================================

ADDON_VERSION="3.4.3"
ADDON_NAME="DWARS SolarEdge Agent"
if [ -f /app/config.json ]; then
  ADDON_VERSION="$(jq -r '.version // "3.4.3"' /app/config.json 2>/dev/null || echo "3.4.3")"
  ADDON_NAME="$(jq -r '.name // "DWARS SolarEdge Agent"' /app/config.json 2>/dev/null || echo "DWARS SolarEdge Agent")"
fi
AGENT_TYPE="solaredge"

BACKUP_YAML_CHECK_ENABLED="$(empty_if_null "$(cfg_optional 'backup_yaml_check_enabled')")"
[ -n "$BACKUP_YAML_CHECK_ENABLED" ] || BACKUP_YAML_CHECK_ENABLED="true"
BACKUP_YAML_PATH="$(empty_if_null "$(cfg_optional 'backup_yaml_path')")"
[ -n "$BACKUP_YAML_PATH" ] || BACKUP_YAML_PATH="/config/backup.yaml"
BACKUP_YAML_OVERWRITE="$(empty_if_null "$(cfg_optional 'backup_yaml_overwrite')")"
[ -n "$BACKUP_YAML_OVERWRITE" ] || BACKUP_YAML_OVERWRITE="false"

backup_yaml_truthy() {
  case "${1:-}" in
    1|true|TRUE|True|yes|YES|Yes|on|ON|On|ja|JA|Ja|aan|AAN|Aan) return 0 ;;
    *) return 1 ;;
  esac
}

backup_yaml_content_ok() {
  local path="$1"
  [ -f "$path" ] || return 1
  grep -q "alias: Auto update everything" "$path" \
    && grep -q "backup.create_automatic" "$path" \
    && grep -q "update.install" "$path" \
    && grep -q "entity_id: all" "$path"
}

write_backup_yaml_content() {
  cat <<'EOF'
- alias: Auto update everything
  description: Automatically install updates
  trigger:
    - platform: time
      at: "03:00:00"

  action:
    - service: backup.create_automatic

    - delay: "00:02:00"

    - service: update.install
      target:
        entity_id: all

  mode: single
EOF
}

ensure_backup_yaml() {
  BACKUP_YAML_OK=""
  BACKUP_YAML_UPDATED_AT=""

  if ! backup_yaml_truthy "$BACKUP_YAML_CHECK_ENABLED"; then
    return 0
  fi

  local path="$BACKUP_YAML_PATH"
  local dir
  dir="$(dirname "$path")"
  mkdir -p "$dir" 2>/dev/null || true

  if backup_yaml_content_ok "$path"; then
    BACKUP_YAML_OK="true"
    BACKUP_YAML_UPDATED_AT="$(stat -c %Y "$path" 2>/dev/null || date +%s)"
    return 0
  fi

  if [ -f "$path" ] && ! backup_yaml_truthy "$BACKUP_YAML_OVERWRITE"; then
    {
      printf '\n\n# DWARS auto update automation\n'
      write_backup_yaml_content
    } >> "$path" 2>/dev/null || { BACKUP_YAML_OK="false"; BACKUP_YAML_UPDATED_AT="$(date +%s)"; return 0; }
  else
    write_backup_yaml_content > "$path" 2>/dev/null || { BACKUP_YAML_OK="false"; BACKUP_YAML_UPDATED_AT="$(date +%s)"; return 0; }
  fi

  BACKUP_YAML_OK="true"
  BACKUP_YAML_UPDATED_AT="$(date +%s)"
}

ensure_backup_yaml

API_KEY="$(empty_if_null "$(cfg_optional 'api_key')")"
CLIENT_ID="$(cfg_required 'client_id')"

API_URL="$(cfg_required 'api_url')"
TEL_URL="$(cfg_required 'telemetry_url')"

INTERVAL="$(cfg_required 'interval_sec')"
DEBUG="$(cfg_required 'debug')"
VERIFY_SSL="$(cfg_required 'verify_ssl')"

HASS_URL="$(cfg_required 'hass_url')"
HASS_TOKEN="$(empty_if_null "$(cfg_optional 'hass_token')")"

HA_GRID_SENSOR="$(cfg_required 'ha_grid_sensor')"
HA_PV_SENSOR="$(cfg_required 'ha_pv_sensor')"
HA_SOC_SENSOR="$(cfg_required 'ha_soc_sensor')"

HA_CMD_MODE_SELECT="$(cfg_required 'ha_cmd_mode_select')"
HA_DEFAULT_MODE_SELECT="$(cfg_required 'ha_default_mode_select')"
HA_CONTROL_MODE_SELECT="$(cfg_required 'ha_control_mode_select')"
HA_COMMAND_TIMEOUT_NUMBER="$(cfg_required 'ha_command_timeout_number')"
HA_COMMAND_TIMEOUT_SEC="$(cfg_required 'ha_command_timeout_sec')"

HA_REMOTE_CHARGE_LIMIT_NUMBER="$(empty_if_null "$(cfg_optional 'ha_remote_charge_limit_number')")"
HA_REMOTE_DISCHARGE_LIMIT_NUMBER="$(empty_if_null "$(cfg_optional 'ha_remote_discharge_limit_number')")"

HA_PV_ACTIVE_POWER_LIMIT_NUMBER="$(empty_if_null "$(cfg_optional 'ha_pv_active_power_limit_number')")"
HA_PV_ACTIVE_POWER_LIMIT_DEFAULT_PERCENT="$(empty_if_null "$(cfg_optional 'ha_pv_active_power_limit_default_percent')")"
[ -n "$HA_PV_ACTIVE_POWER_LIMIT_DEFAULT_PERCENT" ] || HA_PV_ACTIVE_POWER_LIMIT_DEFAULT_PERCENT="65"
HA_PV_ACTIVE_POWER_LIMIT_OFF_PERCENT="$(empty_if_null "$(cfg_optional 'ha_pv_active_power_limit_off_percent')")"
[ -n "$HA_PV_ACTIVE_POWER_LIMIT_OFF_PERCENT" ] || HA_PV_ACTIVE_POWER_LIMIT_OFF_PERCENT="0"
HA_PV_CURTAIL_BELOW_EUR_KWH="$(empty_if_null "$(cfg_optional 'ha_pv_curtail_below_eur_kwh')")"
HA_PV_CURTAIL_ENABLED="$(empty_if_null "$(cfg_optional 'ha_pv_curtail_enabled')")"
[ -n "$HA_PV_CURTAIL_ENABLED" ] || HA_PV_CURTAIL_ENABLED="true"

HA_ENTITY_AUTO_FIX="$(empty_if_null "$(cfg_optional 'auto_discover_entities')")"
[ -n "$HA_ENTITY_AUTO_FIX" ] || HA_ENTITY_AUTO_FIX="true"

export ADDON_VERSION ADDON_NAME AGENT_TYPE
export BACKUP_YAML_CHECK_ENABLED BACKUP_YAML_PATH BACKUP_YAML_OVERWRITE BACKUP_YAML_OK BACKUP_YAML_UPDATED_AT
export API_KEY CLIENT_ID API_URL TEL_URL
export INTERVAL DEBUG VERIFY_SSL
export HASS_URL HASS_TOKEN HA_GRID_SENSOR HA_PV_SENSOR HA_SOC_SENSOR
export HA_CMD_MODE_SELECT HA_DEFAULT_MODE_SELECT HA_CONTROL_MODE_SELECT HA_COMMAND_TIMEOUT_NUMBER HA_COMMAND_TIMEOUT_SEC
export HA_REMOTE_CHARGE_LIMIT_NUMBER HA_REMOTE_DISCHARGE_LIMIT_NUMBER
export HA_PV_ACTIVE_POWER_LIMIT_NUMBER HA_PV_ACTIVE_POWER_LIMIT_DEFAULT_PERCENT HA_PV_ACTIVE_POWER_LIMIT_OFF_PERCENT HA_PV_CURTAIL_BELOW_EUR_KWH HA_PV_CURTAIL_ENABLED
export HA_ENTITY_AUTO_FIX

echo "[BMS] Agent metadata: name=$ADDON_NAME version=$ADDON_VERSION type=$AGENT_TYPE backup_yaml_check=$BACKUP_YAML_CHECK_ENABLED backup_yaml_ok=${BACKUP_YAML_OK:-unknown} path=$BACKUP_YAML_PATH"
echo "[BMS] Start: api_url=$API_URL tel_url=$TEL_URL interval=${INTERVAL}s debug=$DEBUG verify_ssl=$VERIFY_SSL auto_discover=$HA_ENTITY_AUTO_FIX"
echo "[BMS] Using HA: cmd_mode=$HA_CMD_MODE_SELECT default_mode=$HA_DEFAULT_MODE_SELECT control_mode=$HA_CONTROL_MODE_SELECT timeout=$HA_COMMAND_TIMEOUT_NUMBER"
echo "[BMS] Using HA battery power limits: charge=$HA_REMOTE_CHARGE_LIMIT_NUMBER discharge=$HA_REMOTE_DISCHARGE_LIMIT_NUMBER"
echo "[BMS] Using HA PV active power limit: entities=$HA_PV_ACTIVE_POWER_LIMIT_NUMBER off=${HA_PV_ACTIVE_POWER_LIMIT_OFF_PERCENT}% default=${HA_PV_ACTIVE_POWER_LIMIT_DEFAULT_PERCENT}% threshold=${HA_PV_CURTAIL_BELOW_EUR_KWH:-api/default} enabled=$HA_PV_CURTAIL_ENABLED"

while true; do
  /app/se-agent-bms.sh || echo "[BMS] se-agent-bms.sh exit code=$?"
  sleep "$INTERVAL"
done
