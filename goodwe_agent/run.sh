#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Config source
# - In a real Home Assistant add-on: /data/options.json
# - Fallback while testing locally: /app/config.json or ./config.json
# ============================================================

if [ -f /data/options.json ]; then
  OPT_FILE="/data/options.json"
elif [ -f /app/config.json ]; then
  OPT_FILE="/app/config.json"
elif [ -f ./config.json ]; then
  OPT_FILE="./config.json"
else
  echo "[GoodWe] ERROR: no config found. Expected /data/options.json or config.json."
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

# ============================================================
# External BMS / MetDeZon API
# ============================================================

API_URL=$(get_opt "api_url" "https://api.metdezon.nl/bms/api/next_action.php")
API_KEY=$(get_opt "api_key" "")
TELEMETRY_URL=$(get_opt "telemetry_url" "https://api.metdezon.nl/bms/api/telemetry.php")
CLIENT_ID=$(get_opt "client_id" "")

# ============================================================
# Telemetry entities
# ============================================================

SOC_ENTITY=$(get_opt "soc_entity" "sensor.goodwe_battery_state_of_charge")
MODE_ENTITY=$(get_opt "mode_entity" "")
PV_ENTITY=$(get_opt "pv_entity" "sensor.goodwe_pv_power")
GRID_ENTITY=$(get_opt "grid_entity" "sensor.goodwe_active_power")

POLL_INTERVAL=$(get_opt "poll_interval" "60")
POWER_WATT=$(get_opt "power_watt" "5000")
DEBUG=$(get_opt "debug" "1")

# ============================================================
# Home Assistant API/auth
# ============================================================

RAW_HA_URL=$(get_opt "ha_url" "")
RAW_HA_TOKEN=$(get_opt "ha_token" "")

# Als er een long-lived token is ingevuld, gebruik normale HA API.
# Als ha_token leeg blijft, gebruik de Supervisor Core API proxy met SUPERVISOR_TOKEN.
# Dat voorkomt dat gebruikers verplicht een long-lived access token moeten aanmaken.
if [ -n "$RAW_HA_TOKEN" ] && [ "$RAW_HA_TOKEN" != "null" ]; then
  HA_TOKEN="$RAW_HA_TOKEN"
  HA_URL="$RAW_HA_URL"
  if [ -z "$HA_URL" ] || [ "$HA_URL" = "null" ] || [[ "$HA_URL" == *"supervisor/core"* ]]; then
    HA_URL="http://homeassistant:8123"
  fi
  unset SUPERVISOR_TOKEN
  unset HASSIO_TOKEN
else
  HA_TOKEN="${SUPERVISOR_TOKEN:-${HASSIO_TOKEN:-}}"
  HA_URL="http://supervisor/core/api"
fi

# ============================================================
# GoodWe EMS control via HA select/number entities
# ============================================================

HA_CONTROL_ENABLED=$(get_opt "ha_control_enabled" "true")
HA_EMS_MODE_SELECT=$(get_opt "ha_ems_mode_select" "select.goodwe_ems_mode")
HA_EMS_POWER_NUMBER=$(get_opt "ha_ems_power_number" "number.goodwe_eco_mode_power")
HA_EMS_POWER_VALUE=$(get_opt "ha_ems_power_value" "max")
HA_EMS_SET_POWER_MODES=$(get_opt "ha_ems_set_power_modes" "3,4")
HA_EMS_SET_POWER_BEFORE_MODE=$(get_opt "ha_ems_set_power_before_mode" "true")

# Server mode -> GoodWe EMS option.
# Gebruik de API/select values uit HA, niet de UI-labels.
HA_EMS_MODE_1_OPTION=$(get_opt "ha_ems_mode_1_option" "battery_standby")
HA_EMS_MODE_3_OPTION=$(get_opt "ha_ems_mode_3_option" "import_ac")
HA_EMS_MODE_4_OPTION=$(get_opt "ha_ems_mode_4_option" "export_ac")
HA_EMS_MODE_7_OPTION=$(get_opt "ha_ems_mode_7_option" "charge_pv")

# ============================================================
# GoodWe PV/export curtailment via HA number/switch entities
# ============================================================

HA_GRID_EXPORT_LIMIT_NUMBER=$(get_opt "ha_grid_export_limit_number" "number.goodwe_net_exportlimiet")
HA_GRID_EXPORT_LIMIT_SWITCH=$(get_opt "ha_grid_export_limit_switch" "switch.goodwe_grid_export_limit_switch")
HA_GRID_EXPORT_LIMIT_OFF_VALUE=$(get_opt "ha_grid_export_limit_off_value" "0")
HA_GRID_EXPORT_LIMIT_DEFAULT_VALUE=$(get_opt "ha_grid_export_limit_default_value" "max")
HA_GRID_EXPORT_LIMIT_SWITCH_CURTAIL_STATE=$(get_opt "ha_grid_export_limit_switch_curtail_state" "on")
HA_GRID_EXPORT_LIMIT_SWITCH_RESTORE_STATE=$(get_opt "ha_grid_export_limit_switch_restore_state" "off")
HA_PV_CURTAIL_BELOW_EUR_KWH=$(get_opt "ha_pv_curtail_below_eur_kwh" "")
HA_PV_CURTAIL_ENABLED=$(get_opt "ha_pv_curtail_enabled" "true")

# ============================================================
# Export environment vars for Python
# ============================================================

export API_URL API_KEY TELEMETRY_URL CLIENT_ID
export SOC_ENTITY MODE_ENTITY PV_ENTITY GRID_ENTITY
export INTERVAL="$POLL_INTERVAL"
export POWER="$POWER_WATT"
export DEBUG

export HA_URL HA_TOKEN

export HA_CONTROL_ENABLED
export HA_EMS_MODE_SELECT
export HA_EMS_POWER_NUMBER
export HA_EMS_POWER_VALUE
export HA_EMS_SET_POWER_MODES
export HA_EMS_SET_POWER_BEFORE_MODE
export HA_EMS_MODE_1_OPTION
export HA_EMS_MODE_3_OPTION
export HA_EMS_MODE_4_OPTION
export HA_EMS_MODE_7_OPTION

export HA_GRID_EXPORT_LIMIT_NUMBER
export HA_GRID_EXPORT_LIMIT_SWITCH
export HA_GRID_EXPORT_LIMIT_OFF_VALUE
export HA_GRID_EXPORT_LIMIT_DEFAULT_VALUE
export HA_GRID_EXPORT_LIMIT_SWITCH_CURTAIL_STATE
export HA_GRID_EXPORT_LIMIT_SWITCH_RESTORE_STATE
export HA_PV_CURTAIL_BELOW_EUR_KWH
export HA_PV_CURTAIL_ENABLED

# ============================================================
# Logging
# ============================================================

APIKEY_LEN=$(printf '%s' "$API_KEY" | wc -c | tr -d '[:space:]')
HATOKEN_LEN=$(printf '%s' "$HA_TOKEN" | wc -c | tr -d '[:space:]')
SUP_TOKEN_LEN=$(printf '%s' "${SUPERVISOR_TOKEN-}" | wc -c | tr -d '[:space:]')

printf '[GoodWe] Using config: %s\n' "$OPT_FILE"
printf '[GoodWe] Start agent: API_URL=%s interval=%ss power=%sW api_key_length=%s\n' "$API_URL" "$POLL_INTERVAL" "$POWER_WATT" "$APIKEY_LEN"
printf '[GoodWe] HA_URL=%s ha_token_length=%s supervisor_token_length=%s\n' "$HA_URL" "$HATOKEN_LEN" "$SUP_TOKEN_LEN"
printf '[GoodWe] HA EMS control: enabled=%s select_entities=%s power_number_entities=%s power_value=%s power_modes=%s map: 1=%s 3=%s 4=%s 7=%s\n' \
  "$HA_CONTROL_ENABLED" "$HA_EMS_MODE_SELECT" "$HA_EMS_POWER_NUMBER" "$HA_EMS_POWER_VALUE" "$HA_EMS_SET_POWER_MODES" \
  "$HA_EMS_MODE_1_OPTION" "$HA_EMS_MODE_3_OPTION" "$HA_EMS_MODE_4_OPTION" "$HA_EMS_MODE_7_OPTION"
printf '[GoodWe] Grid export curtailment: enabled=%s number_entities=%s switch_entities=%s off=%s restore=%s switch_curtail=%s switch_restore=%s threshold=%s\n' \
  "$HA_PV_CURTAIL_ENABLED" "$HA_GRID_EXPORT_LIMIT_NUMBER" "$HA_GRID_EXPORT_LIMIT_SWITCH" \
  "$HA_GRID_EXPORT_LIMIT_OFF_VALUE" "$HA_GRID_EXPORT_LIMIT_DEFAULT_VALUE" \
  "$HA_GRID_EXPORT_LIMIT_SWITCH_CURTAIL_STATE" "$HA_GRID_EXPORT_LIMIT_SWITCH_RESTORE_STATE" \
  "${HA_PV_CURTAIL_BELOW_EUR_KWH:-api/default}"

# ============================================================
# Start agent
# ============================================================

exec python3 /app/goodwe_agent.py
