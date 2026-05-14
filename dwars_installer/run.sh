#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="/data/options.json"
HA_CONFIG_DIR="/homeassistant_config"
LOCAL_PAYLOAD_DIR="/app/payload"
WORK_DIR="/tmp/dwars-installer"
SUPERVISOR_API="http://supervisor"
STATE_DIR="/data"

log() {
  printf '[DWARS Installer] %s\n' "$*" >&2
}

fail() {
  log "ERROR: $*"
  exit 1
}

get_opt() {
  local key="$1"
  local default="${2-}"
  jq -r --arg k "$key" --arg d "$default" '
    if has($k) then .[$k] else $d end // $d
  ' "$CONFIG_PATH"
}

get_bool() {
  local key="$1"
  local default="${2:-false}"
  local raw
  raw="$(get_opt "$key" "$default")"
  case "$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on|ja|aan) echo "true" ;;
    *) echo "false" ;;
  esac
}

selected_inverter_type() {
  get_opt inverter_type both | tr '[:upper:]' '[:lower:]'
}

should_handle() {
  local wanted="$1"
  local selected
  selected="$(selected_inverter_type)"
  [ "$selected" = "both" ] || [ "$selected" = "$wanted" ]
}

supervisor_curl() {
  local method="$1"
  local path="$2"
  local data="${3-}"

  if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
    fail "SUPERVISOR_TOKEN ontbreekt. Controleer hassio_api/homeassistant_api rechten in config.json."
  fi

  if [ -n "$data" ]; then
    curl -fsS \
      -X "$method" \
      -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
      -H "Content-Type: application/json" \
      -d "$data" \
      "${SUPERVISOR_API}${path}"
  else
    curl -fsS \
      -X "$method" \
      -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
      -H "Content-Type: application/json" \
      "${SUPERVISOR_API}${path}"
  fi
}

try_supervisor_curl() {
  local method="$1"
  local path="$2"
  local data="${3-}"
  supervisor_curl "$method" "$path" "$data" 2>/dev/null || true
}

dir_hash() {
  local dir="$1"
  if [ ! -d "$dir" ]; then
    echo "missing"
    return 0
  fi

  (
    cd "$dir"
    find . -type f ! -name '*.pyc' ! -path './__pycache__/*' -print0 | sort -z | xargs -0 sha256sum 2>/dev/null
  ) | sha256sum | awk '{print $1}'
}

prepare_payload_source() {
  local remote_enabled zip_url zip_file extract_dir top_dir
  remote_enabled="$(get_bool auto_update_from_github false)"

  if [ "$remote_enabled" != "true" ]; then
    printf '%s\n' "$LOCAL_PAYLOAD_DIR"
    return 0
  fi

  zip_url="$(get_opt github_repo_zip_url '')"
  if [ -z "$zip_url" ] || [ "$zip_url" = "null" ]; then
    log "auto_update_from_github=true maar github_repo_zip_url is leeg; embedded payload wordt gebruikt."
    printf '%s\n' "$LOCAL_PAYLOAD_DIR"
    return 0
  fi

  rm -rf "$WORK_DIR"
  mkdir -p "$WORK_DIR"
  zip_file="${WORK_DIR}/repo.zip"
  extract_dir="${WORK_DIR}/repo"

  log "Remote repo-ZIP downloaden voor custom component update."
  if ! curl -fsSL "$zip_url" -o "$zip_file"; then
    log "Download mislukt; embedded payload wordt gebruikt."
    printf '%s\n' "$LOCAL_PAYLOAD_DIR"
    return 0
  fi

  mkdir -p "$extract_dir"
  if ! unzip -q "$zip_file" -d "$extract_dir"; then
    log "Unzip mislukt; embedded payload wordt gebruikt."
    printf '%s\n' "$LOCAL_PAYLOAD_DIR"
    return 0
  fi

  top_dir="$(find "$extract_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [ -z "$top_dir" ] || [ ! -d "${top_dir}/custom_components" ]; then
    log "Remote ZIP bevat geen custom_components; embedded payload wordt gebruikt."
    printf '%s\n' "$LOCAL_PAYLOAD_DIR"
    return 0
  fi

  printf '%s\n' "$top_dir"
}

install_custom_component() {
  local source_root="$1"
  local component="$2"
  local src="${source_root}/custom_components/${component}"
  local dst="${HA_CONFIG_DIR}/custom_components/${component}"

  [ -d "$src" ] || fail "Payload ontbreekt: ${src}"
  mkdir -p "${HA_CONFIG_DIR}/custom_components"

  local src_hash dst_hash
  src_hash="$(dir_hash "$src")"
  dst_hash="$(dir_hash "$dst")"

  if [ "$src_hash" = "$dst_hash" ]; then
    log "${component}: custom component is al actueel (${src_hash})"
    return 1
  fi

  log "${component}: installeren/updaten naar ${dst}"
  rm -rf "$dst"
  cp -a "$src" "$dst"
  printf '%s\n' "$src_hash" > "${STATE_DIR}/${component}.payload.sha256"

  if [ -f "${dst}/manifest.json" ]; then
    log "${component}: manifest $(jq -r '.domain + " " + (.version // "no-version")' "${dst}/manifest.json")"
  fi

  return 0
}

find_addon_slug() {
  local base_slug="$1"
  local addons_json
  addons_json="$(supervisor_curl GET /addons)"

  printf '%s' "$addons_json" | jq -r --arg base "$base_slug" '
    (.addons // .data.addons // [])[]
    | select(
        .slug == $base
        or .slug == ("local_" + $base)
        or (.slug | endswith("_" + $base))
      )
    | .slug
  ' | head -n 1
}

ensure_store_reloaded() {
  log "Add-on store herladen."
  try_supervisor_curl POST /store/reload '{}' >/dev/null
  try_supervisor_curl POST /addons/reload '{}' >/dev/null
}

ensure_addon_installed() {
  local base_slug="$1"
  local label="$2"

  local addon_slug addon_info installed update_available
  addon_slug="$(find_addon_slug "$base_slug" || true)"
  if [ -z "$addon_slug" ] || [ "$addon_slug" = "null" ]; then
    log "${label}: add-on niet gevonden in /addons. Controleer of deze repo in de Add-on Store is toegevoegd."
    return 1
  fi

  log "${label}: slug gevonden: ${addon_slug}"
  addon_info="$(supervisor_curl GET "/addons/${addon_slug}/info")"
  installed="$(printf '%s' "$addon_info" | jq -r '.installed // .data.installed // false')"
  update_available="$(printf '%s' "$addon_info" | jq -r '.update_available // .data.update_available // false')"

  if [ "$installed" != "true" ]; then
    log "${label}: installeren via Store API."
    if ! supervisor_curl POST "/store/addons/${addon_slug}/install" '{"background": false}' >/dev/null; then
      log "${label}: Store API install faalde; fallback naar /addons/${addon_slug}/install."
      supervisor_curl POST "/addons/${addon_slug}/install" '{}' >/dev/null
    fi
  elif [ "$update_available" = "true" ]; then
    log "${label}: update beschikbaar, uitvoeren."
    supervisor_curl POST "/store/addons/${addon_slug}/update" '{"backup": false, "background": false}' >/dev/null \
      || supervisor_curl POST "/addons/${addon_slug}/update" '{}' >/dev/null \
      || true
  else
    log "${label}: is al geïnstalleerd en actueel."
  fi

  printf '%s' "$addon_slug"
}

set_addon_boot_auto_update() {
  local addon_slug="$1"
  local label="$2"
  if [ "$(get_bool enable_addon_auto_update true)" = "true" ]; then
    log "${label}: boot=auto en auto_update=true instellen."
    supervisor_curl POST "/addons/${addon_slug}/options" '{"boot":"auto","auto_update":true}' >/dev/null || true
  fi
}

configure_goodwe_agent() {
  local addon_slug="$1"
  [ "$(get_bool configure_agent_addons true)" = "true" ] || return 0

  local options_payload
  options_payload="$(jq -n \
    --arg api_url "$(get_opt goodwe_agent_api_url 'https://api.metdezon.nl/bms/api/next_action.php')" \
    --arg telemetry_url "$(get_opt goodwe_agent_telemetry_url 'https://api.metdezon.nl/bms/api/telemetry.php')" \
    --arg api_key "$(get_opt goodwe_agent_api_key '')" \
    --arg client_id "$(get_opt goodwe_agent_client_id '')" \
    --argjson poll_interval "$(get_opt goodwe_agent_poll_interval 60)" \
    --argjson power_watt "$(get_opt goodwe_agent_power_watt 5000)" \
    --argjson debug "$(get_bool goodwe_agent_debug true)" \
    --arg ems_mode_select "$(get_opt goodwe_agent_ha_ems_mode_select 'select.goodwe_ems_mode')" \
    --arg ems_power_number "$(get_opt goodwe_agent_ha_ems_power_number 'number.goodwe_eco_mode_power')" \
    --arg mode_0_option "$(get_opt goodwe_agent_ha_ems_mode_0_option 'auto')" \
    --arg grid_limit_number "$(get_opt goodwe_agent_ha_grid_export_limit_number 'number.goodwe_net_exportlimiet')" \
    --arg grid_limit_switch "$(get_opt goodwe_agent_ha_grid_export_limit_switch 'switch.goodwe_grid_export_limit_switch')" \
    '{
      boot: "auto",
      auto_update: true,
      options: {
        api_url: $api_url,
        telemetry_url: $telemetry_url,
        api_key: $api_key,
        client_id: $client_id,
        poll_interval: $poll_interval,
        power_watt: $power_watt,
        soc_entity: "sensor.goodwe_battery_state_of_charge",
        mode_entity: "",
        pv_entity: "sensor.goodwe_pv_power",
        grid_entity: "sensor.goodwe_active_power",
        debug: (if $debug then 1 else 0 end),
        ha_url: "",
        ha_token: "",
        ha_control_enabled: true,
        ha_ems_mode_select: $ems_mode_select,
        ha_ems_power_number: $ems_power_number,
        ha_ems_power_value: "max",
        ha_ems_set_power_modes: "3,4",
        ha_ems_set_power_before_mode: true,
        ha_ems_mode_0_option: $mode_0_option,
        ha_ems_mode_1_option: $mode_0_option,
        ha_ems_mode_3_option: "import_ac",
        ha_ems_mode_4_option: "export_ac",
        ha_ems_mode_7_option: $mode_0_option,
        ha_grid_export_limit_number: $grid_limit_number,
        ha_grid_export_limit_switch: $grid_limit_switch,
        ha_grid_export_limit_off_value: "0",
        ha_grid_export_limit_default_value: "max",
        ha_grid_export_limit_switch_curtail_state: "on",
        ha_grid_export_limit_switch_restore_state: "off",
        ha_pv_curtail_below_eur_kwh: "",
        ha_pv_curtail_enabled: true
      }
    }')"

  log "GoodWe Agent configureren."
  supervisor_curl POST "/addons/${addon_slug}/options" "$options_payload" >/dev/null
}

configure_solaredge_agent() {
  local addon_slug="$1"
  [ "$(get_bool configure_agent_addons true)" = "true" ] || return 0

  local options_payload
  options_payload="$(jq -n \
    --arg api_key "$(get_opt solaredge_agent_api_key '')" \
    --argjson client_id "$(get_opt solaredge_agent_client_id 1)" \
    --arg api_url "$(get_opt solaredge_agent_api_url 'https://api.metdezon.nl/bms/api/next_action.php')" \
    --arg telemetry_url "$(get_opt solaredge_agent_telemetry_url 'https://api.metdezon.nl/bms/api/heartbeat.php')" \
    --argjson interval_sec "$(get_opt solaredge_agent_interval_sec 60)" \
    --argjson debug "$(get_opt solaredge_agent_debug 1)" \
    --argjson verify_ssl "$(get_bool solaredge_agent_verify_ssl true)" \
    --arg grid_sensor "$(get_opt solaredge_agent_ha_grid_sensor 'sensor.electricity_meter_power_consumption')" \
    --arg pv_sensor "$(get_opt solaredge_agent_ha_pv_sensor 'sensor.solaredge_zonne_energie')" \
    --arg soc_sensor "$(get_opt solaredge_agent_ha_soc_sensor 'sensor.solaredge_storage_level')" \
    --arg cmd_mode_select "$(get_opt solaredge_agent_ha_cmd_mode_select 'select.solaredge_storage_remote_command_mode,select.solaredge2_storage_remote_command_mode,select.solaredge3_storage_remote_command_mode')" \
    --arg default_mode_select "$(get_opt solaredge_agent_ha_default_mode_select 'select.solaredge_storage_default_mode,select.solaredge2_storage_default_mode,select.solaredge3_storage_default_mode')" \
    --arg control_mode_select "$(get_opt solaredge_agent_ha_control_mode_select 'select.solaredge_storage_control_mode,select.solaredge2_storage_control_mode,select.solaredge3_storage_control_mode')" \
    --arg timeout_number "$(get_opt solaredge_agent_ha_command_timeout_number 'number.solaredge_storage_remote_command_timeout,number.solaredge2_storage_remote_command_timeout,number.solaredge3_storage_remote_command_timeout')" \
    --arg charge_limit "$(get_opt solaredge_agent_ha_remote_charge_limit_number 'number.solaredge_storage_remote_charge_limit,number.solaredge2_storage_remote_charge_limit,number.solaredge3_storage_remote_charge_limit')" \
    --arg discharge_limit "$(get_opt solaredge_agent_ha_remote_discharge_limit_number 'number.solaredge_storage_remote_discharge_limit,number.solaredge2_storage_remote_discharge_limit,number.solaredge3_storage_remote_discharge_limit')" \
    --argjson command_timeout_sec "$(get_opt solaredge_agent_ha_command_timeout_sec 21600)" \
    --argjson auto_discover "$(get_bool solaredge_agent_auto_discover_entities true)" \
    --arg pv_limit "$(get_opt solaredge_agent_ha_pv_active_power_limit_number 'number.solaredge2_active_power_limit')" \
    --argjson pv_limit_default "$(get_opt solaredge_agent_ha_pv_active_power_limit_default_percent 65)" \
    --argjson pv_limit_off "$(get_opt solaredge_agent_ha_pv_active_power_limit_off_percent 0)" \
    --argjson curtail_threshold "$(get_opt solaredge_agent_ha_pv_curtail_below_eur_kwh -0.12)" \
    --argjson curtail_enabled "$(get_bool solaredge_agent_ha_pv_curtail_enabled true)" \
    '{
      boot: "auto",
      auto_update: true,
      options: {
        api_key: $api_key,
        client_id: $client_id,
        api_url: $api_url,
        telemetry_url: $telemetry_url,
        interval_sec: $interval_sec,
        debug: $debug,
        verify_ssl: $verify_ssl,
        hass_url: "http://supervisor/core",
        hass_token: "",
        ha_grid_sensor: $grid_sensor,
        ha_pv_sensor: $pv_sensor,
        ha_soc_sensor: $soc_sensor,
        ha_cmd_mode_select: $cmd_mode_select,
        ha_default_mode_select: $default_mode_select,
        ha_control_mode_select: $control_mode_select,
        ha_command_timeout_number: $timeout_number,
        ha_remote_charge_limit_number: $charge_limit,
        ha_remote_discharge_limit_number: $discharge_limit,
        ha_command_timeout_sec: $command_timeout_sec,
        auto_discover_entities: $auto_discover,
        ha_pv_active_power_limit_number: $pv_limit,
        ha_pv_active_power_limit_default_percent: $pv_limit_default,
        ha_pv_active_power_limit_off_percent: $pv_limit_off,
        ha_pv_curtail_below_eur_kwh: $curtail_threshold,
        ha_pv_curtail_enabled: $curtail_enabled
      }
    }')"

  log "SolarEdge Agent configureren."
  supervisor_curl POST "/addons/${addon_slug}/options" "$options_payload" >/dev/null
}

start_addon_if_requested() {
  local addon_slug="$1"
  local label="$2"
  if [ "$(get_bool start_agent_addons false)" = "true" ]; then
    log "${label}: starten."
    supervisor_curl POST "/addons/${addon_slug}/start" '{}' >/dev/null \
      || supervisor_curl POST "/addons/${addon_slug}/restart" '{}' >/dev/null \
      || true
  else
    log "${label}: geïnstalleerd/geconfigureerd maar niet gestart. Zet start_agent_addons=true als de entities al bestaan."
  fi
}

configure_installer_self_update() {
  [ "$(get_bool enable_addon_auto_update true)" = "true" ] || return 0
  local slug
  slug="$(find_addon_slug dwars_installer || true)"
  if [ -n "$slug" ] && [ "$slug" != "null" ]; then
    set_addon_boot_auto_update "$slug" "DWARS Installer"
  fi
}

install_or_configure_agents() {
  [ "$(get_bool install_agent_addons true)" = "true" ] || return 0
  ensure_store_reloaded
  configure_installer_self_update

  if should_handle goodwe; then
    local goodwe_slug
    goodwe_slug="$(ensure_addon_installed goodwe_agent 'GoodWe Agent' || true)"
    if [ -n "$goodwe_slug" ]; then
      set_addon_boot_auto_update "$goodwe_slug" "GoodWe Agent"
      configure_goodwe_agent "$goodwe_slug"
      start_addon_if_requested "$goodwe_slug" "GoodWe Agent"
    fi
  fi

  if should_handle solaredge; then
    local se_slug
    se_slug="$(ensure_addon_installed solaredge_agent 'SolarEdge Agent' || true)"
    if [ -z "$se_slug" ]; then
      se_slug="$(ensure_addon_installed metdezon_bms_agent 'SolarEdge Agent' || true)"
    fi
    if [ -n "$se_slug" ]; then
      set_addon_boot_auto_update "$se_slug" "SolarEdge Agent"
      configure_solaredge_agent "$se_slug"
      start_addon_if_requested "$se_slug" "SolarEdge Agent"
    fi
  fi
}

restart_homeassistant_core() {
  log "Home Assistant Core herstarten zodat custom_components opnieuw geladen worden."
  supervisor_curl POST /core/restart '{}' >/dev/null
}

run_install_cycle() {
  local components_changed="false"
  local source_root
  source_root="$(prepare_payload_source)"

  if [ "$(get_bool install_custom_components true)" = "true" ]; then
    if should_handle goodwe; then
      if install_custom_component "$source_root" goodwe; then
        components_changed="true"
      fi
    fi

    if should_handle solaredge; then
      if install_custom_component "$source_root" solaredge_modbus_multi; then
        components_changed="true"
      fi
    fi
  fi

  install_or_configure_agents

  if [ "$components_changed" = "true" ] && [ "$(get_bool restart_homeassistant_after_custom_component true)" = "true" ]; then
    restart_homeassistant_core
  elif [ "$components_changed" = "true" ]; then
    log "Custom components zijn bijgewerkt, maar Home Assistant restart is overgeslagen. Herstart handmatig om de update te laden."
  else
    log "Geen custom component wijzigingen gevonden; restart niet nodig."
  fi
}

main() {
  [ -f "$CONFIG_PATH" ] || fail "Geen ${CONFIG_PATH} gevonden."

  log "Start install/update cycle: inverter_type=$(selected_inverter_type)"
  run_install_cycle
  log "Install/update cycle klaar."

  if [ "$(get_bool watch_for_embedded_updates true)" = "true" ] || [ "$(get_bool auto_update_from_github false)" = "true" ]; then
    local interval
    interval="$(get_opt update_check_interval_sec 21600)"
    log "Updater blijft actief en controleert iedere ${interval}s op custom component wijzigingen."
    while true; do
      sleep "$interval"
      run_install_cycle || log "Update cycle gaf een fout; volgende interval probeert opnieuw."
    done
  fi

  log "Updater loop staat uit; add-on blijft in idle mode actief."
  while true; do sleep 86400; done
}

main "$@"
