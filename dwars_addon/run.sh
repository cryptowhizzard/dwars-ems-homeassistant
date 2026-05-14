#!/usr/bin/env bash
set -euo pipefail

if [ -f /data/options.json ]; then
  OPT_FILE="/data/options.json"
elif [ -f /app/config.json ]; then
  OPT_FILE="/app/config.json"
elif [ -f ./config.json ]; then
  OPT_FILE="./config.json"
else
  echo "[DWARS Generic] ERROR: no config found. Expected /data/options.json or config.json."
  exit 1
fi

get_opt() {
  local key="$1"
  local default="${2-}"
  jq -r --arg k "$key" --arg d "$default" '
    if has($k) then
      .[$k]
    elif ((.options? | type) == "object" and (.options | has($k))) then
      .options[$k]
    else
      $d
    end // $d
  ' "$OPT_FILE"
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

ADDON_VERSION="unknown"
ADDON_NAME="DWARS Generic"
if [ -f /app/config.json ]; then
  ADDON_VERSION="$(jq -r '.version // "unknown"' /app/config.json 2>/dev/null || echo "unknown")"
  ADDON_NAME="$(jq -r '.name // "DWARS Generic"' /app/config.json 2>/dev/null || echo "DWARS Generic")"
fi
AGENT_TYPE="dwars"

BACKUP_YAML_CHECK_ENABLED=$(get_opt "backup_yaml_check_enabled" "true")
BACKUP_YAML_PATH=$(get_opt "backup_yaml_path" "/config/backup.yaml")
BACKUP_YAML_OVERWRITE=$(get_opt "backup_yaml_overwrite" "false")

export ADDON_VERSION ADDON_NAME AGENT_TYPE
export BACKUP_YAML_CHECK_ENABLED BACKUP_YAML_PATH BACKUP_YAML_OVERWRITE

API_URL=$(get_opt "api_url" "https://api.metdezon.nl/bms/api/next_action.php")
TELEMETRY_URL=$(get_opt "telemetry_url" "https://api.metdezon.nl/bms/api/telemetry.php")
API_KEY=$(get_opt "api_key" "")
CLIENT_ID=$(get_opt "client_id" "")
POLL_INTERVAL=$(get_opt "poll_interval" "60")
POWER_WATT=$(get_opt "power_watt" "5000")
DEBUG=$(get_opt "debug" "true")

RAW_HA_URL=$(empty_if_null "$(get_opt "ha_url" "")")
RAW_HA_TOKEN=$(empty_if_null "$(get_opt "ha_token" "")")

if [ -n "$RAW_HA_TOKEN" ]; then
  HA_TOKEN="$RAW_HA_TOKEN"
  HA_URL="$RAW_HA_URL"
  if [ -z "$HA_URL" ] || [[ "$HA_URL" == *"supervisor/core"* ]]; then
    HA_URL="http://homeassistant:8123"
  fi
  unset SUPERVISOR_TOKEN
  unset HASSIO_TOKEN
else
  HA_TOKEN="${SUPERVISOR_TOKEN:-${HASSIO_TOKEN:-}}"
  HA_URL="http://supervisor/core/api"
fi

SOC_ENTITY=$(get_opt "soc_entity" "")
PV_ENTITY=$(get_opt "pv_entity" "")
GRID_ENTITY=$(get_opt "grid_entity" "")
BATTERY_POWER_ENTITY=$(get_opt "battery_power_entity" "")
INVERTER_MODE_ENTITY=$(get_opt "inverter_mode_entity" "")

HA_CONTROL_ENABLED=$(get_opt "ha_control_enabled" "true")
HA_MODE_SELECT=$(get_opt "ha_mode_select" "")
HA_MODE_MAP_JSON=$(get_opt "ha_mode_map_json" "")
HA_MODE_IDLE_OPTION=$(get_opt "ha_mode_idle_option" "auto")
HA_MODE_CHARGE_OPTION=$(get_opt "ha_mode_charge_option" "charge")
HA_MODE_DISCHARGE_OPTION=$(get_opt "ha_mode_discharge_option" "discharge")
HA_SERVER_MODES_IDLE=$(get_opt "ha_server_modes_idle" "1,7")
HA_SERVER_MODES_CHARGE=$(get_opt "ha_server_modes_charge" "3")
HA_SERVER_MODES_DISCHARGE=$(get_opt "ha_server_modes_discharge" "4")

HA_POWER_NUMBER=$(get_opt "ha_power_number" "")
HA_CHARGE_POWER_NUMBER=$(get_opt "ha_charge_power_number" "")
HA_DISCHARGE_POWER_NUMBER=$(get_opt "ha_discharge_power_number" "")
HA_IDLE_POWER_NUMBER=$(get_opt "ha_idle_power_number" "")
HA_CHARGE_POWER_VALUE=$(get_opt "ha_charge_power_value" "server_power")
HA_DISCHARGE_POWER_VALUE=$(get_opt "ha_discharge_power_value" "server_power")
HA_IDLE_POWER_VALUE=$(get_opt "ha_idle_power_value" "skip")
HA_SET_POWER_BEFORE_MODE=$(get_opt "ha_set_power_before_mode" "true")

export API_URL TELEMETRY_URL API_KEY CLIENT_ID
export INTERVAL="$POLL_INTERVAL"
export POWER="$POWER_WATT"
export DEBUG
export HA_URL HA_TOKEN
export SOC_ENTITY PV_ENTITY GRID_ENTITY BATTERY_POWER_ENTITY INVERTER_MODE_ENTITY
export HA_CONTROL_ENABLED HA_MODE_SELECT HA_MODE_MAP_JSON
export HA_MODE_IDLE_OPTION HA_MODE_CHARGE_OPTION HA_MODE_DISCHARGE_OPTION
export HA_SERVER_MODES_IDLE HA_SERVER_MODES_CHARGE HA_SERVER_MODES_DISCHARGE
export HA_POWER_NUMBER HA_CHARGE_POWER_NUMBER HA_DISCHARGE_POWER_NUMBER HA_IDLE_POWER_NUMBER
export HA_CHARGE_POWER_VALUE HA_DISCHARGE_POWER_VALUE HA_IDLE_POWER_VALUE HA_SET_POWER_BEFORE_MODE

APIKEY_LEN=$(printf '%s' "$API_KEY" | wc -c | tr -d '[:space:]')
HATOKEN_LEN=$(printf '%s' "$HA_TOKEN" | wc -c | tr -d '[:space:]')
SUP_TOKEN_LEN=$(printf '%s' "${SUPERVISOR_TOKEN-}" | wc -c | tr -d '[:space:]')

printf '[DWARS Generic] Using config: %s\n' "$OPT_FILE"
printf '[DWARS Generic] Start: API_URL=%s interval=%ss power=%sW api_key_length=%s\n' "$API_URL" "$POLL_INTERVAL" "$POWER_WATT" "$APIKEY_LEN"
printf '[DWARS Generic] HA_URL=%s ha_token_length=%s supervisor_token_length=%s\n' "$HA_URL" "$HATOKEN_LEN" "$SUP_TOKEN_LEN"
printf '[DWARS Generic] Control: enabled=%s mode_select=%s idle=%s charge=%s discharge=%s modes idle=%s charge=%s discharge=%s\n' \
  "$HA_CONTROL_ENABLED" "$HA_MODE_SELECT" "$HA_MODE_IDLE_OPTION" "$HA_MODE_CHARGE_OPTION" "$HA_MODE_DISCHARGE_OPTION" \
  "$HA_SERVER_MODES_IDLE" "$HA_SERVER_MODES_CHARGE" "$HA_SERVER_MODES_DISCHARGE"
printf '[DWARS Generic] Power: default_number=%s charge_number=%s discharge_number=%s idle_number=%s charge_value=%s discharge_value=%s idle_value=%s before_mode=%s\n' \
  "$HA_POWER_NUMBER" "$HA_CHARGE_POWER_NUMBER" "$HA_DISCHARGE_POWER_NUMBER" "$HA_IDLE_POWER_NUMBER" \
  "$HA_CHARGE_POWER_VALUE" "$HA_DISCHARGE_POWER_VALUE" "$HA_IDLE_POWER_VALUE" "$HA_SET_POWER_BEFORE_MODE"

printf '[DWARS Generic] Add-on metadata: name=%s version=%s type=%s backup_yaml_check=%s path=%s overwrite=%s\n' \
  "$ADDON_NAME" "$ADDON_VERSION" "$AGENT_TYPE" "$BACKUP_YAML_CHECK_ENABLED" "$BACKUP_YAML_PATH" "$BACKUP_YAML_OVERWRITE"

exec python3 /app/dwars_agent.py
