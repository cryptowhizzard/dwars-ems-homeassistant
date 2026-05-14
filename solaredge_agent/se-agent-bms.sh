#!/bin/bash
# se-agent-bms.sh — SolarEdge BMS agent (Home Assistant API, no Modbus/Python)
#
# Correct SolarEdge storage sequence:
#   1) Storage Control Mode -> Remote Control
#   2) Storage Remote Command Timeout -> desired seconds
#   3) Optional PV Active Power Limit -> 0% when API/price says curtail PV, default % otherwise
#   4) Optional remote charge/discharge power limit -> power_watt from API
#   5) Storage Default Mode -> fallback mode
#   6) Storage Remote Command Mode -> active mode
#
# Multi-inverter support:
# - Provide multiple entity_ids separated by comma, space, semicolon or newline.
# - The first SolarEdge inverter often has no "1" in the entity_id, while inverter
#   2/3 do, e.g. select.solaredge_storage_control_mode,
#   select.solaredge2_storage_control_mode, select.solaredge3_storage_control_mode.
# - This script auto-corrects common wrong names such as storage_command_mode ->
#   storage_remote_command_mode and storage_command_timeout ->
#   storage_remote_command_timeout.

set -euo pipefail

API_KEY="${API_KEY:-}"
CLIENT_ID="${CLIENT_ID:-0}"
API_URL="${API_URL:-https://api.metdezon.nl/bms/api/next_action.php}"
TEL_URL="${TEL_URL:-https://api.metdezon.nl/bms/api/heartbeat.php}"

DEBUG="${DEBUG:-0}"
VERIFY_SSL="${VERIFY_SSL:-true}"

AGENT_NAME="${ADDON_NAME:-DWARS SolarEdge Agent}"
AGENT_VERSION="${ADDON_VERSION:-unknown}"
AGENT_TYPE="${AGENT_TYPE:-solaredge}"
BACKUP_YAML_PATH="${BACKUP_YAML_PATH:-/config/backup.yaml}"
BACKUP_YAML_OK="${BACKUP_YAML_OK:-}"
BACKUP_YAML_UPDATED_AT="${BACKUP_YAML_UPDATED_AT:-}"

HASS_URL="${HASS_URL:-http://supervisor/core}"
HA_SOC_SENSOR="${HA_SOC_SENSOR:-sensor.solaredge_storage_level}"
HA_PV_SENSOR="${HA_PV_SENSOR:-sensor.solaredge_zonne_energie}"
HA_GRID_SENSOR="${HA_GRID_SENSOR:-sensor.electricity_meter_power_consumption}"

# One-or-many entity lists. Correct current names use "remote_command" for the
# active command and timeout entities.
HA_CMD_MODE_SELECT="${HA_CMD_MODE_SELECT:-select.solaredge_storage_remote_command_mode,select.solaredge2_storage_remote_command_mode,select.solaredge3_storage_remote_command_mode}"
HA_DEFAULT_MODE_SELECT="${HA_DEFAULT_MODE_SELECT:-select.solaredge_storage_default_mode,select.solaredge2_storage_default_mode,select.solaredge3_storage_default_mode}"
HA_CONTROL_MODE_SELECT="${HA_CONTROL_MODE_SELECT:-select.solaredge_storage_control_mode,select.solaredge2_storage_control_mode,select.solaredge3_storage_control_mode}"
HA_COMMAND_TIMEOUT_NUMBER="${HA_COMMAND_TIMEOUT_NUMBER:-number.solaredge_storage_remote_command_timeout,number.solaredge2_storage_remote_command_timeout,number.solaredge3_storage_remote_command_timeout}"
HA_COMMAND_TIMEOUT_SEC="${HA_COMMAND_TIMEOUT_SEC:-21600}"

# Optional power-limit entities. If left empty, the script tries to auto-discover
# them when power_watt is present in the API response.
HA_REMOTE_CHARGE_LIMIT_NUMBER="${HA_REMOTE_CHARGE_LIMIT_NUMBER:-number.solaredge_storage_remote_charge_limit,number.solaredge2_storage_remote_charge_limit,number.solaredge3_storage_remote_charge_limit}"
HA_REMOTE_DISCHARGE_LIMIT_NUMBER="${HA_REMOTE_DISCHARGE_LIMIT_NUMBER:-number.solaredge_storage_remote_discharge_limit,number.solaredge2_storage_remote_discharge_limit,number.solaredge3_storage_remote_discharge_limit}"

# Optional PV curtailment via SolarEdge Active Power Limit number entities.
# The BMS API can expose pv_curtail_recommended / epex.price_eur_kwh. If it does
# not, this agent falls back to parsing "EPEX now €x.xxx/kWh" from reason.
# Threshold default is empty here so the BMS API threshold wins when present;
# if neither API nor config supplies one, -0.12 €/kWh is used.
HA_PV_ACTIVE_POWER_LIMIT_NUMBER="${HA_PV_ACTIVE_POWER_LIMIT_NUMBER:-}"
HA_PV_ACTIVE_POWER_LIMIT_DEFAULT_PERCENT="${HA_PV_ACTIVE_POWER_LIMIT_DEFAULT_PERCENT:-65}"
HA_PV_ACTIVE_POWER_LIMIT_OFF_PERCENT="${HA_PV_ACTIVE_POWER_LIMIT_OFF_PERCENT:-0}"
HA_PV_CURTAIL_BELOW_EUR_KWH="${HA_PV_CURTAIL_BELOW_EUR_KWH:-}"
HA_PV_CURTAIL_ENABLED="${HA_PV_CURTAIL_ENABLED:-true}"

# Keep this on unless you explicitly want hard failures for stale entity names.
HA_ENTITY_AUTO_FIX="${HA_ENTITY_AUTO_FIX:-true}"
AUTO_DISCOVER_HELPERS="${AUTO_DISCOVER_HELPERS:-true}"

CURL_TLS=()
[ "${VERIFY_SSL,,}" = "true" ] || CURL_TLS+=(-k)

log(){ echo "[BMS] $(date '+%F %T') $*"; }
debug(){ [ "$DEBUG" = "1" ] && log "DEBUG: $*" || true; }

if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
  HA_TOKEN="$SUPERVISOR_TOKEN"
elif [ -n "${HASS_TOKEN:-}" ]; then
  HA_TOKEN="$HASS_TOKEN"
else
  log "ERROR: No SUPERVISOR_TOKEN (or HASS_TOKEN) available for HA API"
  exit 1
fi

ha_get() {
  local entity="$1"
  curl -sS "${CURL_TLS[@]}" \
    -H "Authorization: Bearer $HA_TOKEN" \
    -H "Content-Type: application/json" \
    "$HASS_URL/api/states/$entity"
}

ha_get_all_states() {
  curl -sS "${CURL_TLS[@]}" \
    -H "Authorization: Bearer $HA_TOKEN" \
    -H "Content-Type: application/json" \
    "$HASS_URL/api/states"
}

ha_config_version() {
  curl -sS "${CURL_TLS[@]}" \
    -H "Authorization: Bearer $HA_TOKEN" \
    -H "Content-Type: application/json" \
    "$HASS_URL/api/config" | jq -r '.version // empty' 2>/dev/null || true
}

ALL_STATES_CACHE=""
ha_get_all_states_cached() {
  if [ -z "$ALL_STATES_CACHE" ]; then
    ALL_STATES_CACHE="$(ha_get_all_states || true)"
  fi
  printf '%s\n' "$ALL_STATES_CACHE"
}

ha_entity_exists() {
  local entity="${1:-}"
  local json
  [ -n "$entity" ] || return 1
  json="$(ha_get "$entity" || true)"
  echo "$json" | jq -e --arg entity "$entity" 'type == "object" and .entity_id == $entity' >/dev/null 2>&1
}

ha_entity_json_valid() {
  local json="$1"
  local entity="$2"
  echo "$json" | jq -e --arg entity "$entity" 'type == "object" and .entity_id == $entity' >/dev/null 2>&1
}

# Returns: first line HTTP code, remaining lines body
ha_post_raw() {
  local path="$1"
  local json="$2"
  local bodyfile
  bodyfile="$(mktemp)"
  local code
  code="$(curl -sS "${CURL_TLS[@]}" -o "$bodyfile" -w '%{http_code}' \
    -H "Authorization: Bearer $HA_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$json" \
    "$HASS_URL$path" || true)"
  echo "$code"
  cat "$bodyfile" 2>/dev/null || true
  rm -f "$bodyfile" >/dev/null 2>&1 || true
}

num_or_empty() {
  [[ "${1:-}" =~ ^-?[0-9]+([.][0-9]+)?$ ]] && echo "$1" || echo ""
}

truthy() {
  case "${1:-}" in
    1|true|TRUE|True|yes|YES|Yes|on|ON|On) return 0 ;;
    *) return 1 ;;
  esac
}

mode_to_label() {
  local mode="$1"
  case "$mode" in
    7) echo "Maximize self consumption";;
    6) echo "Maximize self consumption";;
    5) echo "Discharge to match load";;
    4) echo "Maximize export";;
    3) echo "Charge from PV and AC";;
    2) echo "Charge from PV first";;
    1) echo "Charge from excess PV power only";;
    0) echo "Off";;
    *) echo "";;
  esac
}

label_aliases() {
  local desired="$1"
  case "$desired" in
    "Maximize self consumption")
      printf '%s\n' \
        "Maximize self consumption" \
        "Maximize Self Consumption" \
        "Maximise self consumption" \
        "Maximise Self Consumption"
      ;;
    "Maximize export")
      printf '%s\n' \
        "Maximize export" \
        "Maximize Export" \
        "Maximise export" \
        "Maximise Export" \
        "Discharge to Maximize Export" \
        "Discharge to Maximise Export"
      ;;
    "Charge from PV and AC")
      printf '%s\n' \
        "Charge from PV and AC" \
        "Charge from Solar Power and Grid" \
        "Charge from PV and Grid" \
        "Charge from Solar and Grid" \
        "Charge from Clipped Solar Power Charge from Solar Power" \
        "Charge from Clipped Solar Power" \
        "Charge from Solar Power"
      ;;
    "Charge from PV first")
      printf '%s\n' \
        "Charge from PV first" \
        "Charge from Solar Power first" \
        "Charge from Solar Power" \
        "Charge from Clipped Solar Power Charge from Solar Power"
      ;;
    "Charge from excess PV power only")
      printf '%s\n' \
        "Charge from excess PV power only" \
        "Charge from excess solar power only" \
        "Charge from excess PV only"
      ;;
    "Discharge to match load")
      printf '%s\n' \
        "Discharge to match load" \
        "Discharge to Match Load" \
        "Discharge to Minimize Import" \
        "Discharge to Minimise Import"
      ;;
    "Off")
      printf '%s\n' "Off" "OFF" "Solar Power Only (Off)"
      ;;
    *)
      printf '%s\n' "$desired"
      ;;
  esac
}

select_options() {
  local select_json="$1"
  echo "$select_json" | jq -r '(.attributes.options // [])[]' 2>/dev/null || true
}

select_options_inline() {
  local select_json="$1"
  local opts
  opts="$(select_options "$select_json" | paste -sd ', ' - 2>/dev/null || true)"
  echo "${opts:-<none>}"
}

resolve_select_option() {
  local select_json="$1"
  local desired="$2"
  local options aliases opt alias
  options="$(select_options "$select_json")"
  [ -n "$options" ] || return 0

  aliases="$(label_aliases "$desired")"

  # Exact alias match first.
  while IFS= read -r alias; do
    [ -n "$alias" ] || continue
    if echo "$options" | grep -Fxq "$alias"; then
      echo "$alias"
      return 0
    fi
  done <<< "$aliases"

  # Case-insensitive alias match next, returning the actual HA option spelling.
  while IFS= read -r alias; do
    [ -n "$alias" ] || continue
    while IFS= read -r opt; do
      [ -n "$opt" ] || continue
      if [ "${opt,,}" = "${alias,,}" ]; then
        echo "$opt"
        return 0
      fi
    done <<< "$options"
  done <<< "$aliases"

  return 0
}

remote_control_option() {
  local select_json="$1"
  local opt
  opt="$(select_options "$select_json" | awk 'tolower($0) ~ /remote/ {print; exit}')"
  echo "$opt"
}

# ===== Helpers: parse "one-or-many" entity_id strings into arrays =====
list_to_array() {
  local raw="${1:-}"
  local -n _out="$2"

  raw="${raw//$'\n'/ }"
  raw="${raw//,/ }"
  raw="${raw//;/ }"
  raw="${raw//null/ }"

  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"

  _out=()
  if [ -n "$raw" ]; then
    IFS=$' \t\r\n' read -r -a _out <<< "$raw"
  fi
}

pick_from_array() {
  local -n _arr="$1"
  local idx="$2"
  if [ "${#_arr[@]}" -eq 0 ]; then
    echo ""
  elif [ "$idx" -lt "${#_arr[@]}" ]; then
    echo "${_arr[$idx]}"
  elif [ "${#_arr[@]}" -eq 1 ]; then
    echo "${_arr[0]}"
  else
    echo ""
  fi
}

array_max_len() {
  local max=0
  local -n a1="$1"
  local -n a2="$2"
  local -n a3="$3"
  local -n a4="$4"
  local -n a5="$5"
  local -n a6="$6"
  for len in "${#a1[@]}" "${#a2[@]}" "${#a3[@]}" "${#a4[@]}" "${#a5[@]}" "${#a6[@]}"; do
    [ "$len" -gt "$max" ] && max="$len"
  done
  echo "$max"
}

entity_rank() {
  local entity="$1"
  local short="${entity#*.}"
  if [[ "$short" =~ solaredge_i([0-9]+)_storage ]]; then
    echo "${BASH_REMATCH[1]}"
  elif [[ "$short" =~ solaredge([0-9]+)_storage ]]; then
    echo "${BASH_REMATCH[1]}"
  elif [[ "$short" =~ solaredge_storage ]]; then
    echo "1"
  elif [[ "$short" =~ _i([0-9]+)_storage ]]; then
    echo "${BASH_REMATCH[1]}"
  else
    echo "999"
  fi
}

rank_candidates() {
  local entity rank
  while IFS= read -r entity; do
    [ -n "$entity" ] || continue
    rank="$(entity_rank "$entity")"
    printf '%04d\t%s\n' "$rank" "$entity"
  done | sort -k1,1n -k2,2 | cut -f2-
}

discover_candidates() {
  local kind="$1"
  local all
  all="$(ha_get_all_states_cached)"
  if ! echo "$all" | jq -e 'type == "array"' >/dev/null 2>&1; then
    return 0
  fi

  case "$kind" in
    command_select)
      echo "$all" | jq -r '
        .[]
        | select(.entity_id|test("^select\\."))
        | . as $e
        | ((.entity_id + " " + (.attributes.friendly_name // "")) | ascii_downcase) as $h
        | select($h | test("storage"))
        | select($h | test("remote.*command.*mode|command.*mode"))
        | select(($h | test("default|control|charge policy|ac charge")) | not)
        | $e.entity_id
      ' | rank_candidates
      ;;
    default_select)
      echo "$all" | jq -r '
        .[]
        | select(.entity_id|test("^select\\."))
        | . as $e
        | ((.entity_id + " " + (.attributes.friendly_name // "")) | ascii_downcase) as $h
        | select($h | test("storage"))
        | select($h | test("default.*mode"))
        | $e.entity_id
      ' | rank_candidates
      ;;
    control_select)
      echo "$all" | jq -r '
        .[]
        | select(.entity_id|test("^select\\."))
        | . as $e
        | ((.entity_id + " " + (.attributes.friendly_name // "")) | ascii_downcase) as $h
        | select($h | test("storage"))
        | select($h | test("control.*mode"))
        | $e.entity_id
      ' | rank_candidates
      ;;
    timeout_number)
      echo "$all" | jq -r '
        .[]
        | select(.entity_id|test("^number\\."))
        | . as $e
        | ((.entity_id + " " + (.attributes.friendly_name // "")) | ascii_downcase) as $h
        | select($h | test("storage"))
        | select($h | test("remote.*command.*timeout|command.*timeout"))
        | $e.entity_id
      ' | rank_candidates
      ;;
    charge_limit_number)
      echo "$all" | jq -r '
        .[]
        | select(.entity_id|test("^number\\."))
        | . as $e
        | ((.entity_id + " " + (.attributes.friendly_name // "")) | ascii_downcase) as $h
        | select($h | test("storage"))
        | select($h | test("remote.*charge.*limit|charge.*limit"))
        | select(($h | test("discharge")) | not)
        | $e.entity_id
      ' | rank_candidates
      ;;
    discharge_limit_number)
      echo "$all" | jq -r '
        .[]
        | select(.entity_id|test("^number\\."))
        | . as $e
        | ((.entity_id + " " + (.attributes.friendly_name // "")) | ascii_downcase) as $h
        | select($h | test("storage"))
        | select($h | test("remote.*discharge.*limit|discharge.*limit"))
        | $e.entity_id
      ' | rank_candidates
      ;;
    active_power_limit_number)
      echo "$all" | jq -r '
        .[]
        | select(.entity_id|test("^number\\."))
        | . as $e
        | ((.entity_id + " " + (.attributes.friendly_name // "")) | ascii_downcase) as $h
        | select($h | test("active.*power.*limit|active_power_limit"))
        | select(($h | test("storage.*remote|charge.*limit|discharge.*limit|command.*timeout")) | not)
        | $e.entity_id
      ' | rank_candidates
      ;;
  esac
}

pick_discovered_candidate() {
  local kind="$1"
  local idx="$2"
  local candidates=()
  mapfile -t candidates < <(discover_candidates "$kind")
  if [ "${#candidates[@]}" -eq 0 ]; then
    echo ""
  elif [ "$idx" -lt "${#candidates[@]}" ]; then
    echo "${candidates[$idx]}"
  elif [ "${#candidates[@]}" -eq 1 ]; then
    echo "${candidates[0]}"
  else
    echo ""
  fi
}

entity_variants() {
  local entity="${1:-}"
  local kind="$2"
  [ -n "$entity" ] || return 0

  local bases=()
  local b v
  bases+=("$entity")

  # If a number entity was accidentally configured as select.*, fix the domain first.
  # This happens easily because SolarEdge helper names are very similar.
  case "$kind" in
    timeout_number|charge_limit_number|discharge_limit_number|active_power_limit_number)
      bases+=("$(echo "$entity" | sed -E 's/^select\./number./')")
      ;;
    command_select|default_select|control_select)
      bases+=("$(echo "$entity" | sed -E 's/^number\./select./')")
      ;;
  esac

  bases+=("$(echo "$entity" | sed -E 's/solaredge1_storage/solaredge_storage/g')")
  bases+=("$(echo "$entity" | sed -E 's/solaredge_i1_storage/solaredge_storage/g')")
  bases+=("$(echo "$entity" | sed -E 's/_i1_storage/_storage/g')")
  bases+=("$(echo "$entity" | sed -E 's/_(mode|timeout|limit)_[0-9]+$/_\1/g')")

  for b in "${bases[@]}"; do
    [ -n "$b" ] || continue
    echo "$b"
    case "$kind" in
      command_select)
        echo "$b" | sed -E 's/_storage_command_mode($|_)/_storage_remote_command_mode\1/g'
        echo "$b" | sed -E 's/_storage_remote_command_mode($|_)/_storage_command_mode\1/g'
        ;;
      timeout_number)
        echo "$b" | sed -E 's/_storage_command_timeout($|_)/_storage_remote_command_timeout\1/g'
        echo "$b" | sed -E 's/_storage_remote_command_timeout($|_)/_storage_command_timeout\1/g'
        ;;
      charge_limit_number)
        echo "$b" | sed -E 's/_storage_charge_limit($|_)/_storage_remote_charge_limit\1/g'
        ;;
      discharge_limit_number)
        echo "$b" | sed -E 's/_storage_discharge_limit($|_)/_storage_remote_discharge_limit\1/g'
        ;;
      active_power_limit_number)
        echo "$b" | sed -E 's/_power_limit($|_)/_active_power_limit\1/g'
        ;;
    esac
  done | awk 'NF && !seen[$0]++'
}

kind_name() {
  case "$1" in
    command_select) echo "command mode";;
    default_select) echo "default mode";;
    control_select) echo "control mode";;
    timeout_number) echo "command timeout";;
    charge_limit_number) echo "remote charge limit";;
    discharge_limit_number) echo "remote discharge limit";;
    active_power_limit_number) echo "PV active power limit";;
    *) echo "$1";;
  esac
}

resolve_entity() {
  local configured="${1:-}"
  local kind="$2"
  local idx="$3"
  local name variant fallback target_rank variant_rank fallback_rank

  if [ "${HA_ENTITY_AUTO_FIX,,}" != "true" ]; then
    echo "$configured"
    return 0
  fi

  name="$(kind_name "$kind")"
  target_rank="$((idx+1))"
  fallback="$(pick_discovered_candidate "$kind" "$idx" || true)"

  while IFS= read -r variant; do
    [ -n "$variant" ] || continue
    if ha_entity_exists "$variant"; then
      # In multi-inverter configs, do not let an unnumbered inverter-1 variant
      # accidentally satisfy inverter 2/3 if discovery found a rank-matching entity.
      if [ -n "$fallback" ] && [ "$fallback" != "$variant" ] && [ "$idx" -gt 0 ]; then
        variant_rank="$(entity_rank "$variant")"
        fallback_rank="$(entity_rank "$fallback")"
        if [ "$variant_rank" != "$target_rank" ] && [ "$fallback_rank" = "$target_rank" ]; then
          continue
        fi
      fi

      if [ "$variant" != "$configured" ]; then
        log "INFO: Auto-corrected $name for inverter[$((idx+1))]: ${configured:-<empty>} -> $variant" >&2
      fi
      echo "$variant"
      return 0
    fi
  done < <(entity_variants "$configured" "$kind")

  if [ -n "$fallback" ]; then
    if [ -n "$configured" ]; then
      log "INFO: Auto-discovered $name for inverter[$((idx+1))]: using $fallback (configured $configured not found)" >&2
    else
      log "INFO: Auto-discovered $name for inverter[$((idx+1))]: using $fallback" >&2
    fi
    echo "$fallback"
    return 0
  fi

  echo "$configured"
}

discover_helpers() {
  [ "${AUTO_DISCOVER_HELPERS,,}" = "true" ] || return 0
  log "INFO: Auto-discovery: scanning HA states for possible SolarEdge helper entities…"
  local all
  all="$(ha_get_all_states_cached)"
  if ! echo "$all" | jq -e 'type=="array"' >/dev/null 2>&1; then
    log "WARN: Auto-discovery failed: /api/states did not return an array"
    return 0
  fi

  local kind title candidates
  for kind in command_select default_select control_select timeout_number charge_limit_number discharge_limit_number active_power_limit_number; do
    title="$(kind_name "$kind")"
    candidates="$(discover_candidates "$kind" | head -n 20)"
    if [ -n "$candidates" ]; then
      log "INFO: Possible ${title} entities:"
      while IFS= read -r entity; do
        [ -n "$entity" ] || continue
        local j st opts min max step
        j="$(echo "$all" | jq -r --arg entity "$entity" '.[] | select(.entity_id == $entity)' 2>/dev/null || true)"
        st="$(echo "$j" | jq -r '.state // empty' 2>/dev/null || true)"
        if [[ "$entity" == select.* ]]; then
          opts="$(echo "$j" | jq -r '(.attributes.options // []) | join(", ")' 2>/dev/null || true)"
          echo "[BMS]   $entity | state=${st:-?} | options=${opts:-<none>}"
        else
          min="$(echo "$j" | jq -r '.attributes.min // "n/a"' 2>/dev/null || true)"
          max="$(echo "$j" | jq -r '.attributes.max // "n/a"' 2>/dev/null || true)"
          step="$(echo "$j" | jq -r '.attributes.step // "n/a"' 2>/dev/null || true)"
          echo "[BMS]   $entity | state=${st:-?} | min=$min max=$max step=$step"
        fi
      done <<< "$candidates"
    else
      log "INFO: No obvious ${title} candidates found."
    fi
  done

  log "INFO: Tip: copy the correct entity_id lists into add-on options if auto-discovery picked the wrong inverter order."
}

set_select_option_if_needed() {
  local entity="$1"
  local desired="$2"
  local cur_json cur_state payload out code body

  if [ -z "$entity" ] || [ -z "$desired" ]; then
    log "WARN: set_select_option_if_needed called with empty entity or option (entity='${entity:-}', option='${desired:-}')"
    return 1
  fi

  cur_json="$(ha_get "$entity" || true)"
  if ! ha_entity_json_valid "$cur_json" "$entity"; then
    log "WARN: $entity not found in Home Assistant; cannot select '$desired'"
    return 1
  fi

  cur_state="$(echo "$cur_json" | jq -r '.state // empty' 2>/dev/null || true)"
  if [ "$cur_state" = "unavailable" ]; then
    log "WARN: $entity is unavailable; cannot select '$desired'"
    return 1
  fi

  if [ "$cur_state" = "$desired" ]; then
    debug "$entity already '$desired' (skip)"
    return 0
  fi

  payload="$(jq -n --arg entity_id "$entity" --arg option "$desired" '{entity_id:$entity_id, option:$option}')"
  out="$(ha_post_raw "/api/services/select/select_option" "$payload")"
  code="$(echo "$out" | head -n1)"
  body="$(echo "$out" | tail -n +2 | tr '\n' ' ' | head -c 240)"

  if [[ "$code" =~ ^2 ]]; then
    debug "set $entity => '$desired' OK ($code) resp=$body"
    return 0
  fi
  log "WARN: set $entity => '$desired' failed HTTP $code resp=$body"
  return 1
}

clamp_to_number_bounds() {
  local desired="$1"
  local json="$2"
  local entity="$3"
  local min max clamped

  min="$(echo "$json" | jq -r '.attributes.min // empty' 2>/dev/null || true)"
  max="$(echo "$json" | jq -r '.attributes.max // empty' 2>/dev/null || true)"
  clamped="$desired"

  if [ -n "$(num_or_empty "$min")" ]; then
    clamped="$(awk -v v="$clamped" -v min="$min" 'BEGIN{ if (v < min) v = min; printf "%.6f", v }')"
  fi
  if [ -n "$(num_or_empty "$max")" ]; then
    clamped="$(awk -v v="$clamped" -v max="$max" 'BEGIN{ if (v > max) v = max; printf "%.6f", v }')"
  fi

  if awk -v a="$desired" -v b="$clamped" 'BEGIN{exit !((a-b < -0.0001) || (a-b > 0.0001))}'; then
    log "INFO: Clamped $entity value from $desired to $clamped due to HA min/max"
  fi
  echo "$clamped"
}

set_number_value() {
  local entity="$1"
  local desired="$2"
  local mode="${3:-exact}" # exact or min
  local cur_json cur_state cur_num target payload out code body

  if [ -z "$entity" ] || [ -z "$(num_or_empty "$desired")" ]; then
    log "WARN: set_number_value called with empty/invalid entity or value (entity='${entity:-}', value='${desired:-}')"
    return 1
  fi

  cur_json="$(ha_get "$entity" || true)"
  if ! ha_entity_json_valid "$cur_json" "$entity"; then
    log "WARN: $entity not found in Home Assistant; cannot set number to $desired"
    return 1
  fi

  cur_state="$(echo "$cur_json" | jq -r '.state // empty' 2>/dev/null || true)"
  if [ "$cur_state" = "unavailable" ]; then
    log "WARN: $entity is unavailable; cannot set number to $desired"
    return 1
  fi

  cur_num="$(num_or_empty "$cur_state")"
  target="$(clamp_to_number_bounds "$desired" "$cur_json" "$entity")"

  if [ "$mode" = "min" ] && [ -n "$cur_num" ] && awk -v cur="$cur_num" -v want="$target" 'BEGIN{exit !(cur >= want)}'; then
    debug "$entity already >= $target (cur=$cur_num) (skip)"
    return 0
  fi

  if [ "$mode" != "min" ] && [ -n "$cur_num" ] && awk -v cur="$cur_num" -v want="$target" 'BEGIN{d=cur-want; if (d<0) d=-d; exit !(d < 0.001)}'; then
    debug "$entity already $target (cur=$cur_num) (skip)"
    return 0
  fi

  payload="$(jq -n --arg entity_id "$entity" --arg value "$target" '{entity_id:$entity_id, value:($value|tonumber)}')"
  out="$(ha_post_raw "/api/services/number/set_value" "$payload")"
  code="$(echo "$out" | head -n1)"
  body="$(echo "$out" | tail -n +2 | tr '\n' ' ' | head -c 240)"

  if [[ "$code" =~ ^2 ]]; then
    debug "set $entity => $target OK ($code) resp=$body"
    return 0
  fi
  log "WARN: set $entity => $target failed HTTP $code resp=$body"
  return 1
}

apply_power_limit_if_possible() {
  local mode="$1"
  local power_watt="$2"
  local charge_entity="$3"
  local discharge_entity="$4"
  local power_num

  power_num="$(num_or_empty "$power_watt")"
  [ -n "$power_num" ] || return 0

  case "$mode" in
    1|2|3)
      if [ -n "$charge_entity" ]; then
        set_number_value "$charge_entity" "$power_num" exact || true
      else
        log "NOTE: power_watt not applied for charge mode: ${power_num}W (no remote charge limit number found)"
      fi
      ;;
    4|5)
      if [ -n "$discharge_entity" ]; then
        set_number_value "$discharge_entity" "$power_num" exact || true
      else
        log "NOTE: power_watt not applied for discharge/export mode: ${power_num}W (no remote discharge limit number found)"
      fi
      ;;
    *)
      debug "power_watt=${power_num}W not applicable for mode=$mode"
      ;;
  esac
}

extract_action_epex_price() {
  local raw="$1"
  local reason="$2"
  local val

  val="$(printf '%s' "$raw" | jq -r '
    def n:
      if . == null then empty
      elif type == "number" then .
      elif type == "string" and test("^-?[0-9]+([.][0-9]+)?$") then tonumber
      else empty end;
    ([.epex_price_eur_kwh, ((.epex // {}).price_eur_kwh), .price_eur_kwh, .epex_now_eur_kwh] | map(n) | .[0] // empty)
  ' 2>/dev/null || true)"

  if [ -z "$(num_or_empty "$val")" ]; then
    val="$(printf '%s\n' "$reason" | sed -nE 's/.*EPEX now €(-?[0-9]+([.][0-9]+)?).*/\1/p' | head -n1)"
  fi

  num_or_empty "$val"
}

extract_action_pv_curtail_threshold() {
  local raw="$1"
  local val

  val="$(printf '%s' "$raw" | jq -r '
    def n:
      if . == null then empty
      elif type == "number" then .
      elif type == "string" and test("^-?[0-9]+([.][0-9]+)?$") then tonumber
      else empty end;
    ([.pv_curtail_below_eur_kwh, ((.epex // {}).pv_curtail_below_eur_kwh)] | map(n) | .[0] // empty)
  ' 2>/dev/null || true)"

  num_or_empty "$val"
}

extract_action_pv_curtail_recommended() {
  local raw="$1"
  local val

  val="$(printf '%s' "$raw" | jq -r '
    if (.pv_curtail_recommended? != null) then .pv_curtail_recommended
    elif (((.epex // {}).pv_curtail_recommended?) != null) then (.epex.pv_curtail_recommended)
    else empty end
  ' 2>/dev/null || true)"

  case "${val,,}" in
    true|1|yes|on) echo "true" ;;
    false|0|no|off) echo "false" ;;
    *) echo "" ;;
  esac
}

decide_pv_curtail_from_action() {
  local api_recommended="${1:-}"
  local price_now="${2:-}"
  local threshold="${3:-}"
  local reason="${4:-}"

  case "${api_recommended,,}" in
    true|1|yes|on) echo "true api"; return 0 ;;
    false|0|no|off) echo "false api"; return 0 ;;
  esac

  if [ -n "$(num_or_empty "$price_now")" ] && [ -n "$(num_or_empty "$threshold")" ]; then
    if awk -v p="$price_now" -v t="$threshold" 'BEGIN{exit !(p < t)}'; then
      echo "true price"
    else
      echo "false price"
    fi
    return 0
  fi

  if printf '%s\n' "$reason" | grep -qi 'PV curtailed'; then
    echo "true reason"
    return 0
  fi

  echo " unknown"
}

apply_pv_active_power_limit_if_needed() {
  local recommended="$1"
  local price_now="$2"
  local threshold="$3"
  local source="$4"

  truthy "$HA_PV_CURTAIL_ENABLED" || return 0

  local target action target_num
  case "${recommended,,}" in
    true|1|yes|on)
      target="$HA_PV_ACTIVE_POWER_LIMIT_OFF_PERCENT"
      action="curtail"
      ;;
    false|0|no|off)
      target="$HA_PV_ACTIVE_POWER_LIMIT_DEFAULT_PERCENT"
      action="restore"
      ;;
    *)
      debug "PV active power limit: no decision available (price=${price_now:-?}, threshold=${threshold:-?}, source=${source:-?})"
      return 0
      ;;
  esac

  target_num="$(num_or_empty "$target")"
  if [ -z "$target_num" ]; then
    log "WARN: PV active power limit target for action=$action is not numeric: '$target'"
    return 1
  fi

  local pv_limit_entities=()
  list_to_array "$HA_PV_ACTIVE_POWER_LIMIT_NUMBER" pv_limit_entities

  if [ "${#pv_limit_entities[@]}" -eq 0 ]; then
    debug "PV active power limit: no ha_pv_active_power_limit_number configured; decision=$recommended target=${target_num}"
    return 0
  fi

  log "PV active power limit: action=$action target=${target_num} decision=$recommended price=${price_now:-?} threshold=${threshold:-?} source=${source:-?}"

  local pidx raw ent
  for ((pidx=0; pidx<${#pv_limit_entities[@]}; pidx++)); do
    raw="${pv_limit_entities[$pidx]}"
    ent="$(resolve_entity "$raw" active_power_limit_number "$pidx")"
    if [ -n "$ent" ]; then
      set_number_value "$ent" "$target_num" exact || true
    else
      log "WARN: empty PV active power limit entity at index $((pidx+1))"
    fi
  done
}

# ===== Parse configured entity lists =====
CMD_ENTITIES=()
DEF_ENTITIES=()
CTL_ENTITIES=()
TOUT_ENTITIES=()
CHG_LIMIT_ENTITIES=()
DIS_LIMIT_ENTITIES=()

list_to_array "$HA_CMD_MODE_SELECT" CMD_ENTITIES
list_to_array "$HA_DEFAULT_MODE_SELECT" DEF_ENTITIES
list_to_array "$HA_CONTROL_MODE_SELECT" CTL_ENTITIES
list_to_array "$HA_COMMAND_TIMEOUT_NUMBER" TOUT_ENTITIES
list_to_array "$HA_REMOTE_CHARGE_LIMIT_NUMBER" CHG_LIMIT_ENTITIES
list_to_array "$HA_REMOTE_DISCHARGE_LIMIT_NUMBER" DIS_LIMIT_ENTITIES

INVERTER_COUNT="$(array_max_len CMD_ENTITIES DEF_ENTITIES CTL_ENTITIES TOUT_ENTITIES CHG_LIMIT_ENTITIES DIS_LIMIT_ENTITIES)"
[ "$INVERTER_COUNT" -gt 0 ] || INVERTER_COUNT=1

# ================= 1) Read SOC/PV/GRID + current command mode(s) =================
SOC_JSON="$(ha_get "$HA_SOC_SENSOR" || true)"
PV_JSON="$(ha_get "$HA_PV_SENSOR" || true)"
GRID_JSON="$(ha_get "$HA_GRID_SENSOR" || true)"

SOC_STATE="$(echo "$SOC_JSON" | jq -r '.state // empty' 2>/dev/null || true)"
SOC_VAL="$(num_or_empty "$SOC_STATE")"
SOC_SHOW="$( [ -n "$SOC_VAL" ] && printf '%.1f' "$SOC_VAL" || echo '?' )"

PV_STATE="$(echo "$PV_JSON" | jq -r '.state // empty' 2>/dev/null || true)"
PV_UNIT="$(echo "$PV_JSON" | jq -r '.attributes.unit_of_measurement // empty' 2>/dev/null || true)"
PV_VAL="$(num_or_empty "$PV_STATE")"
PV_AC_W=""
if [ -n "$PV_VAL" ]; then
  if [ "$PV_UNIT" = "W" ]; then
    PV_AC_W="$(awk -v v="$PV_VAL" 'BEGIN { printf "%.1f", v }')"
  else
    PV_AC_W="$(awk -v v="$PV_VAL" 'BEGIN { printf "%.1f", v * 1000 }')"
  fi
fi

GRID_STATE="$(echo "$GRID_JSON" | jq -r '.state // empty' 2>/dev/null || true)"
GRID_UNIT="$(echo "$GRID_JSON" | jq -r '.attributes.unit_of_measurement // empty' 2>/dev/null || true)"
GRID_VAL="$(num_or_empty "$GRID_STATE")"
GRID_W="$GRID_VAL"
if [ -n "$GRID_VAL" ] && [ "$GRID_UNIT" = "kW" ]; then
  GRID_W="$(awk -v v="$GRID_VAL" 'BEGIN { printf "%.1f", v * 1000 }')"
elif [ -n "$GRID_VAL" ]; then
  GRID_W="$(awk -v v="$GRID_VAL" 'BEGIN { printf "%.1f", v }')"
fi

CMD_MODES_PRETTY=()
for ((idx=0; idx<INVERTER_COUNT; idx++)); do
  raw_cmd="$(pick_from_array CMD_ENTITIES "$idx")"
  ent="$(resolve_entity "$raw_cmd" command_select "$idx")"
  if [ -n "$ent" ]; then
    j="$(ha_get "$ent" || true)"
    st="$(echo "$j" | jq -r '.state // empty' 2>/dev/null || true)"
    if ha_entity_json_valid "$j" "$ent"; then
      CMD_MODES_PRETTY+=( "${ent}='${st:-?}'" )
    else
      CMD_MODES_PRETTY+=( "${ent}='<not found>'" )
    fi
  else
    CMD_MODES_PRETTY+=( "cmd[$((idx+1))]=<none>" )
  fi
 done

log "HA state: SOC=${SOC_SHOW}% cmd_modes=[${CMD_MODES_PRETTY[*]}] PV=${PV_AC_W:-n/a}W GRID=${GRID_W:-n/a}W"

# ================= 2) Heartbeat =================
HA_VERSION="$(ha_config_version || true)"
HB_JSON="$(jq -n \
  --argjson cid "$CLIENT_ID" \
  --argjson ts "$(date +%s)" \
  --arg soc "${SOC_VAL:-}" \
  --arg pvac "${PV_AC_W:-}" \
  --arg grid "${GRID_W:-}" \
  --arg agent_name "$AGENT_NAME" \
  --arg agent_type "$AGENT_TYPE" \
  --arg agent_version "$AGENT_VERSION" \
  --arg ha_version "${HA_VERSION:-}" \
  --arg backup_ok "${BACKUP_YAML_OK:-}" \
  --arg backup_path "${BACKUP_YAML_PATH:-}" \
  --arg backup_updated_at "${BACKUP_YAML_UPDATED_AT:-}" \
  '{
    client_id:$cid,
    reported_at:$ts,
    agent_name:$agent_name,
    agent_type:$agent_type,
    agent_version:$agent_version,
    ha_version:(if $ha_version == "" then null else $ha_version end),
    backup_yaml_ok:(if $backup_ok == "" then null elif (($backup_ok|ascii_downcase) == "true" or $backup_ok == "1") then true else false end),
    backup_yaml_path:(if $backup_path == "" then null else $backup_path end),
    backup_yaml_updated_at:(($backup_updated_at|tonumber?)),
    soc:(($soc|tonumber?)),
    pv_power_w:(($pvac|tonumber?)),
    grid_power_w:(($grid|tonumber?))
  }'
)"

CURL_ARGS=(-sS "${CURL_TLS[@]}" -H "Content-Type: application/json")
[ -n "$API_KEY" ] && CURL_ARGS+=(-H "X-API-Key: $API_KEY")

HTTP_TEL=$(curl -o /tmp/hb.out -w '%{http_code}' -X POST "${CURL_ARGS[@]}" -d "$HB_JSON" "$TEL_URL" || true)
[[ "$HTTP_TEL" =~ ^2 ]] && log "Heartbeat OK ($HTTP_TEL)" || log "WARN: Heartbeat HTTP $HTTP_TEL body=$(head -c 200 /tmp/hb.out 2>/dev/null || true)"

# ================= 3) Fetch next action =================
CURL_ACT_ARGS=(-o /tmp/act.out -w '%{http_code}' -sS "${CURL_TLS[@]}" -H "Accept: application/json")
[ -n "$API_KEY" ] && CURL_ACT_ARGS+=(-H "X-API-Key: $API_KEY")
HTTP_ACT=$(curl "${CURL_ACT_ARGS[@]}" "$API_URL" || true)
ACT_RAW="$(cat /tmp/act.out 2>/dev/null || true)"
if ! [[ "$HTTP_ACT" =~ ^2 ]]; then
  log "ERROR: next_action HTTP $HTTP_ACT body=$(echo "$ACT_RAW" | head -c 200)"
  exit 0
fi

MODE_NEW="$(echo "$ACT_RAW" | jq -r '.mode // empty')"
REASON="$(echo "$ACT_RAW" | jq -r '.reason // empty')"
PWR_NEW="$(echo "$ACT_RAW" | jq -r '.power_watt // empty')"

EPEX_PRICE_NOW="$(extract_action_epex_price "$ACT_RAW" "$REASON")"
PV_CURTAIL_THRESHOLD_API="$(extract_action_pv_curtail_threshold "$ACT_RAW")"
PV_CURTAIL_RECOMMENDED_API="$(extract_action_pv_curtail_recommended "$ACT_RAW")"

PV_CURTAIL_THRESHOLD="$(num_or_empty "$HA_PV_CURTAIL_BELOW_EUR_KWH")"
[ -n "$PV_CURTAIL_THRESHOLD" ] || PV_CURTAIL_THRESHOLD="$PV_CURTAIL_THRESHOLD_API"
[ -n "$PV_CURTAIL_THRESHOLD" ] || PV_CURTAIL_THRESHOLD="-0.12"

PV_CURTAIL_DECISION_LINE="$(decide_pv_curtail_from_action "$PV_CURTAIL_RECOMMENDED_API" "$EPEX_PRICE_NOW" "$PV_CURTAIL_THRESHOLD" "$REASON")"
PV_CURTAIL_RECOMMENDED="$(echo "$PV_CURTAIL_DECISION_LINE" | awk '{print $1}')"
PV_CURTAIL_SOURCE="$(echo "$PV_CURTAIL_DECISION_LINE" | cut -d' ' -f2-)"

LABEL_PRETTY="$(mode_to_label "$MODE_NEW")"
log "Action: mode=$MODE_NEW => '${LABEL_PRETTY:-?}' power=${PWR_NEW:-n/a}W | reason=${REASON:-n/a}"

apply_pv_active_power_limit_if_needed "$PV_CURTAIL_RECOMMENDED" "$EPEX_PRICE_NOW" "$PV_CURTAIL_THRESHOLD" "$PV_CURTAIL_SOURCE"

if ! [[ "$MODE_NEW" =~ ^[0-7]$ ]]; then
  log "No valid mode (0..7) in action; skip apply. mode='$MODE_NEW'"
  exit 0
fi

LABEL="$(mode_to_label "$MODE_NEW")"
if [ -z "$LABEL" ]; then
  log "WARN: No label mapping for mode=$MODE_NEW; skip apply."
  exit 0
fi

# ================= 4) Apply sequence per inverter =================
for ((idx=0; idx<INVERTER_COUNT; idx++)); do
  RAW_CMD_ENTITY="$(pick_from_array CMD_ENTITIES "$idx")"
  RAW_DEF_ENTITY="$(pick_from_array DEF_ENTITIES "$idx")"
  RAW_CTL_ENTITY="$(pick_from_array CTL_ENTITIES "$idx")"
  RAW_TOUT_ENTITY="$(pick_from_array TOUT_ENTITIES "$idx")"
  RAW_CHG_LIMIT_ENTITY="$(pick_from_array CHG_LIMIT_ENTITIES "$idx")"
  RAW_DIS_LIMIT_ENTITY="$(pick_from_array DIS_LIMIT_ENTITIES "$idx")"

  CMD_ENTITY="$(resolve_entity "$RAW_CMD_ENTITY" command_select "$idx")"
  DEF_ENTITY="$(resolve_entity "$RAW_DEF_ENTITY" default_select "$idx")"
  CTL_ENTITY="$(resolve_entity "$RAW_CTL_ENTITY" control_select "$idx")"
  TOUT_ENTITY="$(resolve_entity "$RAW_TOUT_ENTITY" timeout_number "$idx")"
  CHG_LIMIT_ENTITY="$(resolve_entity "$RAW_CHG_LIMIT_ENTITY" charge_limit_number "$idx")"
  DIS_LIMIT_ENTITY="$(resolve_entity "$RAW_DIS_LIMIT_ENTITY" discharge_limit_number "$idx")"

  log "Apply inverter[$((idx+1))]: cmd=${CMD_ENTITY:-<none>} def=${DEF_ENTITY:-<none>} ctl=${CTL_ENTITY:-<none>} timeout=${TOUT_ENTITY:-<none>} chargeLimit=${CHG_LIMIT_ENTITY:-<none>} dischargeLimit=${DIS_LIMIT_ENTITY:-<none>}"

  # A) Control mode -> Remote Control
  if [ -n "${CTL_ENTITY:-}" ]; then
    CTL_JSON="$(ha_get "$CTL_ENTITY" || true)"
    if ha_entity_json_valid "$CTL_JSON" "$CTL_ENTITY"; then
      CTL_STATE="$(echo "$CTL_JSON" | jq -r '.state // empty' 2>/dev/null || true)"
      if [ "$CTL_STATE" = "unavailable" ]; then
        log "WARN: $CTL_ENTITY unavailable; cannot set Remote Control"
        discover_helpers
      else
        CTL_OPT="$(remote_control_option "$CTL_JSON")"
        if [ -n "$CTL_OPT" ]; then
          set_select_option_if_needed "$CTL_ENTITY" "$CTL_OPT" || true
        else
          log "WARN: $CTL_ENTITY has no remote-like option. options=$(select_options_inline "$CTL_JSON")"
          discover_helpers
        fi
      fi
    else
      log "WARN: $CTL_ENTITY not found; cannot set Remote Control"
      discover_helpers
    fi
  else
    debug "no control_mode select configured/discovered for inverter[$((idx+1))]"
  fi

  # B) Command timeout -> raise it before remote command mode changes
  if [ -n "${TOUT_ENTITY:-}" ]; then
    set_number_value "$TOUT_ENTITY" "$HA_COMMAND_TIMEOUT_SEC" min || true
  else
    debug "no command_timeout number configured/discovered for inverter[$((idx+1))]"
  fi

  # C) Optional power limit from API response
  apply_power_limit_if_possible "$MODE_NEW" "$PWR_NEW" "$CHG_LIMIT_ENTITY" "$DIS_LIMIT_ENTITY"

  # D) Default mode (fallback)
  OPT_DEF=""
  if [ -n "${DEF_ENTITY:-}" ]; then
    DEF_JSON="$(ha_get "$DEF_ENTITY" || true)"
    if ha_entity_json_valid "$DEF_JSON" "$DEF_ENTITY"; then
      OPT_DEF="$(resolve_select_option "$DEF_JSON" "$LABEL")"
      if [ -n "$OPT_DEF" ]; then
        set_select_option_if_needed "$DEF_ENTITY" "$OPT_DEF" || true
      else
        log "WARN: $DEF_ENTITY has no option for '$LABEL'. options=$(select_options_inline "$DEF_JSON")"
      fi
    else
      log "WARN: $DEF_ENTITY not found; cannot set default mode"
      discover_helpers
    fi
  else
    debug "no default_mode select configured/discovered for inverter[$((idx+1))]"
  fi

  # E) Remote command mode (active)
  OPT_CMD=""
  if [ -n "${CMD_ENTITY:-}" ]; then
    CMD_JSON2="$(ha_get "$CMD_ENTITY" || true)"
    if ha_entity_json_valid "$CMD_JSON2" "$CMD_ENTITY"; then
      OPT_CMD="$(resolve_select_option "$CMD_JSON2" "$LABEL")"
      if [ -n "$OPT_CMD" ]; then
        set_select_option_if_needed "$CMD_ENTITY" "$OPT_CMD" || true
      else
        log "WARN: $CMD_ENTITY has no option for '$LABEL'. options=$(select_options_inline "$CMD_JSON2")"
      fi
    else
      log "WARN: $CMD_ENTITY not found; cannot set remote command mode"
      discover_helpers
    fi
  else
    debug "no command_mode select configured/discovered for inverter[$((idx+1))]"
  fi

  # Verify after the integration has had a moment to update states.
  sleep 5
  CMD_AFTER=""
  DEF_AFTER=""
  CTL_AFTER=""

  if [ -n "${CMD_ENTITY:-}" ]; then
    CMD_AFTER="$( (ha_get "$CMD_ENTITY" | jq -r '.state // empty' 2>/dev/null) || true )"
  fi
  if [ -n "${DEF_ENTITY:-}" ]; then
    DEF_AFTER="$( (ha_get "$DEF_ENTITY" | jq -r '.state // empty' 2>/dev/null) || true )"
  fi
  if [ -n "${CTL_ENTITY:-}" ]; then
    CTL_AFTER="$( (ha_get "$CTL_ENTITY" | jq -r '.state // empty' 2>/dev/null) || true )"
  fi

  ok=true
  if [ -n "$OPT_CMD" ] && [ "$CMD_AFTER" != "$OPT_CMD" ]; then ok=false; fi
  if [ -n "$OPT_DEF" ] && [ "$DEF_AFTER" != "$OPT_DEF" ]; then ok=false; fi
  if [ -n "$CTL_ENTITY" ] && [[ "${CTL_AFTER,,}" != *remote* ]]; then ok=false; fi

  if [ "$ok" = true ]; then
    log "Result inverter[$((idx+1))]: OK ctl='${CTL_AFTER:-?}' cmd='${CMD_AFTER:-n/a}' def='${DEF_AFTER:-n/a}'"
  else
    log "WARN: inverter[$((idx+1))] verify mismatch. wanted ctl~Remote got ctl='${CTL_AFTER:-?}' | wanted cmd='${OPT_CMD:-n/a}' got cmd='${CMD_AFTER:-?}' | wanted def='${OPT_DEF:-n/a}' got def='${DEF_AFTER:-?}'"
    log "HINT: If remote command/default mode keeps reverting, Storage Control Mode must be Remote Control and Storage Remote Command Timeout must be valid."
    discover_helpers
  fi
done

