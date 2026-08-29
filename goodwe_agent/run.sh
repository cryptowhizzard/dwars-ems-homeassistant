#!/usr/bin/with-contenv bashio
set -euo pipefail

OPT_FILE=/data/options.json

get_opt() {
  local key="$1" default="${2-}"
  if [ -f "$OPT_FILE" ]; then
    local value
    # jq's `//` treats boolean false as absent. Use has() so unchecked
    # checkboxes remain false instead of silently reverting to their defaults.
    value="$(jq -r --arg key "$key" 'if has($key) and .[$key] != null then .[$key] else empty end' "$OPT_FILE" 2>/dev/null || true)"
    if [ -n "$value" ] && [ "$value" != "null" ]; then
      printf '%s' "$value"
      return 0
    fi
  fi
  printf '%s' "$default"
}

# Add-on metadata
ADDON_VERSION="unknown"
ADDON_NAME="GoodWe Agent"
if [ -f /app/config.json ]; then
  ADDON_VERSION="$(jq -r '.version // "unknown"' /app/config.json 2>/dev/null || echo unknown)"
  ADDON_NAME="$(jq -r '.name // "GoodWe Agent"' /app/config.json 2>/dev/null || echo 'GoodWe Agent')"
fi
AGENT_TYPE=goodwe

# BMS API: only the API key is customer-specific. client_id remains optional.
API_URL="$(get_opt api_url 'https://api.metdezon.nl/bms/api/next_action.php')"
TELEMETRY_URL="$(get_opt telemetry_url 'https://api.metdezon.nl/bms/api/telemetry.php')"
API_KEY="$(get_opt api_key '')"
CLIENT_ID="$(get_opt client_id '')"

# Cadences
POLL_INTERVAL="$(get_opt poll_interval 60)"
SAFETY_INTERVAL="$(get_opt safety_interval 10)"
STANDALONE_INTERVAL="$(get_opt standalone_interval 30)"
DEFAULTS_INTERVAL="$(get_opt defaults_interval 60)"
ENTITY_DISCOVERY_INTERVAL="$(get_opt entity_discovery_interval 60)"
POWER_WATT="$(get_opt power_watt 5000)"
DEBUG="$(get_opt debug 1)"

# HA API/auth. http://supervisor/core becomes .../api in Python.
#
# The Supervisor Core proxy requires the injected SUPERVISOR_TOKEN. A manually
# configured long-lived token is retained only as a fallback (and is preferred
# when ha_url points directly to Home Assistant instead of the Supervisor proxy).
RAW_HA_URL="$(get_opt ha_url 'http://supervisor/core')"
CONFIGURED_HA_TOKEN="$(get_opt ha_token '')"
HA_URL="${RAW_HA_URL:-http://supervisor/core}"
if [ -z "$HA_URL" ] || [ "$HA_URL" = "null" ]; then
  HA_URL='http://supervisor/core'
fi
HA_TOKEN="$CONFIGURED_HA_TOKEN"
SUPERVISOR_ACCESS_TOKEN="${SUPERVISOR_TOKEN:-${HASSIO_TOKEN:-}}"

# Serial-specific auto discovery and telemetry entities
SOC_ENTITY="$(get_opt soc_entity auto)"
MODE_ENTITY="$(get_opt mode_entity '')"
PV_ENTITY="$(get_opt pv_entity auto)"
GRID_ENTITY="$(get_opt grid_entity auto)"
BATTERY_POWER_ENTITY="$(get_opt battery_power_entity auto)"
GOODWE_SERIAL_NUMBER="$(get_opt goodwe_serial_number '')"
GOODWE_SERIAL_ENTITY="$(get_opt goodwe_serial_entity auto)"
GOODWE_PHASE_ENTITY="$(get_opt goodwe_phase_entity auto)"
GOODWE_IP_ENTITY="$(get_opt goodwe_ip_entity auto)"
GOODWE_MAC_ENTITY="$(get_opt goodwe_mac_entity auto)"
GOODWE_LAST_SEEN_ENTITY="$(get_opt goodwe_last_seen_entity auto)"
HA_AUTO_ENTITY_DISCOVERY="$(get_opt ha_auto_entity_discovery true)"

HA_CONTROL_ENABLED="$(get_opt ha_control_enabled true)"
HA_EMS_MODE_SELECT="$(get_opt ha_ems_mode_select auto)"
HA_EMS_POWER_NUMBER="$(get_opt ha_ems_power_number auto)"
HA_EMS_POWER_VALUE="$(get_opt ha_ems_power_value server_power)"
HA_EMS_SET_POWER_MODES="$(get_opt ha_ems_set_power_modes '3,4')"
HA_EMS_SET_POWER_BEFORE_MODE="$(get_opt ha_ems_set_power_before_mode true)"

# Upgrade policy: corrected values are applied immediately. When the explicit
# override checkbox is enabled, the operator can deliberately choose alternatives.
OVERRIDE_DEFAULT_VALUES="$(get_opt override_default_values false)"
if [ "$OVERRIDE_DEFAULT_VALUES" = "true" ]; then
  HA_EMS_MODE_0_OPTION="$(get_opt ha_ems_mode_0_option auto)"
  HA_EMS_MODE_1_OPTION="$(get_opt ha_ems_mode_1_option battery_standby)"
  HA_EMS_MODE_3_OPTION="$(get_opt ha_ems_mode_3_option charge_battery)"
  HA_EMS_MODE_4_OPTION="$(get_opt ha_ems_mode_4_option discharge_battery)"
  HA_EMS_MODE_7_OPTION="$(get_opt ha_ems_mode_7_option auto)"
else
  HA_EMS_MODE_0_OPTION=auto
  HA_EMS_MODE_1_OPTION=battery_standby
  HA_EMS_MODE_3_OPTION=charge_battery
  HA_EMS_MODE_4_OPTION=discharge_battery
  HA_EMS_MODE_7_OPTION=auto
fi
# Never retain the old misleading import_ac/export_ac aliases after upgrade.
[ "$HA_EMS_MODE_3_OPTION" = "import_ac" ] && HA_EMS_MODE_3_OPTION=charge_battery
[ "$HA_EMS_MODE_4_OPTION" = "export_ac" ] && HA_EMS_MODE_4_OPTION=discharge_battery

GOODWE_DEFAULT_DOD="$(get_opt goodwe_default_dod 90)"
GOODWE_DEFAULT_DOD_ON_GRID="$(get_opt goodwe_default_dod_on_grid 90)"
GOODWE_DEFAULT_DOD_HOLDING="$(get_opt goodwe_default_dod_holding off)"
GOODWE_DEFAULT_BACKUP_SUPPLY="$(get_opt goodwe_default_backup_supply on)"
GOODWE_DEFAULT_OPERATION_MODE="$(get_opt goodwe_default_operation_mode general)"
HA_DOD_HOLDING_SWITCH="$(get_opt ha_dod_holding_switch auto)"
HA_BACKUP_SUPPLY_SWITCH="$(get_opt ha_backup_supply_switch auto)"
HA_DOD_NUMBER="$(get_opt ha_dod_number auto)"
HA_DOD_ON_GRID_NUMBER="$(get_opt ha_dod_on_grid_number auto)"
HA_OPERATION_MODE_SELECT="$(get_opt ha_operation_mode_select auto)"

HA_CHARGE_BLOCK_ENABLED="$(get_opt ha_charge_block_enabled true)"
HA_CHARGE_BLOCK_SENSOR="$(get_opt ha_charge_block_sensor auto)"
HA_CHARGE_BLOCK_BELOW_W="$(get_opt ha_charge_block_below_w auto)"
HA_CHARGE_BLOCK_RELEASE_ABOVE_W="$(get_opt ha_charge_block_release_above_w auto)"
HA_CHARGE_BLOCK_DURATION_SEC="$(get_opt ha_charge_block_duration_sec 300)"
HA_CHARGE_BLOCK_MODES="$(get_opt ha_charge_block_modes 3)"
HA_CHARGE_BLOCK_FALLBACK_OPTION="$(get_opt ha_charge_block_fallback_option auto)"

HA_GRID_EXPORT_LIMIT_NUMBER="$(get_opt ha_grid_export_limit_number auto)"
HA_GRID_EXPORT_LIMIT_SWITCH="$(get_opt ha_grid_export_limit_switch auto)"
HA_GRID_EXPORT_LIMIT_OFF_VALUE="$(get_opt ha_grid_export_limit_off_value 0)"
HA_GRID_EXPORT_LIMIT_DEFAULT_VALUE="$(get_opt ha_grid_export_limit_default_value auto)"
HA_GRID_EXPORT_LIMIT_SWITCH_CURTAIL_STATE="$(get_opt ha_grid_export_limit_switch_curtail_state on)"
HA_GRID_EXPORT_LIMIT_SWITCH_RESTORE_STATE="$(get_opt ha_grid_export_limit_switch_restore_state on)"
HA_PV_CURTAIL_BELOW_EUR_KWH="$(get_opt ha_pv_curtail_below_eur_kwh '')"
HA_PV_CURTAIL_ENABLED="$(get_opt ha_pv_curtail_enabled true)"

STANDALONE_ENABLED="$(get_opt standalone_enabled false)"
STANDALONE_PV_ENTITY="$(get_opt standalone_pv_entity '')"
STANDALONE_GRID_ENTITY="$(get_opt standalone_grid_entity auto)"
STANDALONE_DEADBAND_W="$(get_opt standalone_deadband_w 150)"
STANDALONE_MAX_CHARGE_W="$(get_opt standalone_max_charge_w 0)"

BACKUP_YAML_CHECK_ENABLED="$(get_opt backup_yaml_check_enabled true)"
BACKUP_YAML_PATH="$(get_opt backup_yaml_path /config/backup.yaml)"
BACKUP_YAML_OVERWRITE="$(get_opt backup_yaml_overwrite false)"

export ADDON_VERSION ADDON_NAME AGENT_TYPE
export API_URL API_KEY TELEMETRY_URL CLIENT_ID
export INTERVAL="$POLL_INTERVAL" SAFETY_INTERVAL STANDALONE_INTERVAL DEFAULTS_INTERVAL ENTITY_DISCOVERY_INTERVAL
export POWER="$POWER_WATT" DEBUG
export HA_URL HA_TOKEN CONFIGURED_HA_TOKEN SUPERVISOR_ACCESS_TOKEN HA_CONTROL_ENABLED HA_AUTO_ENTITY_DISCOVERY
export SOC_ENTITY MODE_ENTITY PV_ENTITY GRID_ENTITY BATTERY_POWER_ENTITY
export GOODWE_SERIAL_NUMBER GOODWE_SERIAL_ENTITY GOODWE_PHASE_ENTITY GOODWE_IP_ENTITY GOODWE_MAC_ENTITY GOODWE_LAST_SEEN_ENTITY
export HA_EMS_MODE_SELECT HA_EMS_POWER_NUMBER HA_EMS_POWER_VALUE HA_EMS_SET_POWER_MODES HA_EMS_SET_POWER_BEFORE_MODE
export HA_EMS_MODE_0_OPTION HA_EMS_MODE_1_OPTION HA_EMS_MODE_3_OPTION HA_EMS_MODE_4_OPTION HA_EMS_MODE_7_OPTION
export OVERRIDE_DEFAULT_VALUES GOODWE_DEFAULT_DOD GOODWE_DEFAULT_DOD_ON_GRID GOODWE_DEFAULT_DOD_HOLDING GOODWE_DEFAULT_BACKUP_SUPPLY GOODWE_DEFAULT_OPERATION_MODE
export HA_DOD_HOLDING_SWITCH HA_BACKUP_SUPPLY_SWITCH HA_DOD_NUMBER HA_DOD_ON_GRID_NUMBER HA_OPERATION_MODE_SELECT
export HA_CHARGE_BLOCK_ENABLED HA_CHARGE_BLOCK_SENSOR HA_CHARGE_BLOCK_BELOW_W HA_CHARGE_BLOCK_RELEASE_ABOVE_W HA_CHARGE_BLOCK_DURATION_SEC HA_CHARGE_BLOCK_MODES HA_CHARGE_BLOCK_FALLBACK_OPTION
export HA_GRID_EXPORT_LIMIT_NUMBER HA_GRID_EXPORT_LIMIT_SWITCH HA_GRID_EXPORT_LIMIT_OFF_VALUE HA_GRID_EXPORT_LIMIT_DEFAULT_VALUE HA_GRID_EXPORT_LIMIT_SWITCH_CURTAIL_STATE HA_GRID_EXPORT_LIMIT_SWITCH_RESTORE_STATE HA_PV_CURTAIL_BELOW_EUR_KWH HA_PV_CURTAIL_ENABLED
export STANDALONE_ENABLED STANDALONE_PV_ENTITY STANDALONE_GRID_ENTITY STANDALONE_DEADBAND_W STANDALONE_MAX_CHARGE_W
export BACKUP_YAML_CHECK_ENABLED BACKUP_YAML_PATH BACKUP_YAML_OVERWRITE

printf '[GoodWe] version=%s API=%s key_length=%s HA=%s decision=%ss safety=%ss standalone=%ss\n' \
  "$ADDON_VERSION" "$API_URL" "$(printf %s "$API_KEY" | wc -c | tr -d ' ')" "$HA_URL" \
  "$POLL_INTERVAL" "$SAFETY_INTERVAL" "$STANDALONE_INTERVAL"
printf '[GoodWe] HA auth: supervisor_token=%s configured_token=%s; supervisor proxy prefers SUPERVISOR_TOKEN\n' \
  "$( [ -n "$SUPERVISOR_ACCESS_TOKEN" ] && printf yes || printf no )" \
  "$( [ -n "$CONFIGURED_HA_TOKEN" ] && printf yes || printf no )"
printf '[GoodWe] modes: 1=%s 3=%s 4=%s 7=%s override_defaults=%s\n' \
  "$HA_EMS_MODE_1_OPTION" "$HA_EMS_MODE_3_OPTION" "$HA_EMS_MODE_4_OPTION" "$HA_EMS_MODE_7_OPTION" "$OVERRIDE_DEFAULT_VALUES"
printf '[GoodWe] phase defaults: thresholds=%s/%s export_limit=%s; standalone=%s external_pv=%s\n' \
  "$HA_CHARGE_BLOCK_BELOW_W" "$HA_CHARGE_BLOCK_RELEASE_ABOVE_W" "$HA_GRID_EXPORT_LIMIT_DEFAULT_VALUE" \
  "$STANDALONE_ENABLED" "${STANDALONE_PV_ENTITY:-auto}"

exec python3 /app/goodwe_agent.py
