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

export API_KEY CLIENT_ID API_URL TEL_URL
export INTERVAL DEBUG VERIFY_SSL
export HASS_URL HASS_TOKEN HA_GRID_SENSOR HA_PV_SENSOR HA_SOC_SENSOR
export HA_CMD_MODE_SELECT HA_DEFAULT_MODE_SELECT HA_CONTROL_MODE_SELECT HA_COMMAND_TIMEOUT_NUMBER HA_COMMAND_TIMEOUT_SEC
export HA_REMOTE_CHARGE_LIMIT_NUMBER HA_REMOTE_DISCHARGE_LIMIT_NUMBER
export HA_PV_ACTIVE_POWER_LIMIT_NUMBER HA_PV_ACTIVE_POWER_LIMIT_DEFAULT_PERCENT HA_PV_ACTIVE_POWER_LIMIT_OFF_PERCENT HA_PV_CURTAIL_BELOW_EUR_KWH HA_PV_CURTAIL_ENABLED
export HA_ENTITY_AUTO_FIX

echo "[BMS] Start: api_url=$API_URL tel_url=$TEL_URL interval=${INTERVAL}s debug=$DEBUG verify_ssl=$VERIFY_SSL auto_discover=$HA_ENTITY_AUTO_FIX"
echo "[BMS] Using HA: cmd_mode=$HA_CMD_MODE_SELECT default_mode=$HA_DEFAULT_MODE_SELECT control_mode=$HA_CONTROL_MODE_SELECT timeout=$HA_COMMAND_TIMEOUT_NUMBER"
echo "[BMS] Using HA battery power limits: charge=$HA_REMOTE_CHARGE_LIMIT_NUMBER discharge=$HA_REMOTE_DISCHARGE_LIMIT_NUMBER"
echo "[BMS] Using HA PV active power limit: entities=$HA_PV_ACTIVE_POWER_LIMIT_NUMBER off=${HA_PV_ACTIVE_POWER_LIMIT_OFF_PERCENT}% default=${HA_PV_ACTIVE_POWER_LIMIT_DEFAULT_PERCENT}% threshold=${HA_PV_CURTAIL_BELOW_EUR_KWH:-api/default} enabled=$HA_PV_CURTAIL_ENABLED"

while true; do
  /app/se-agent-bms.sh || echo "[BMS] se-agent-bms.sh exit code=$?"
  sleep "$INTERVAL"
done
