#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="/data/options.json"
HA_CONFIG_DIR="${HA_CONFIG_DIR:-/homeassistant_config}"
if [ ! -d "$HA_CONFIG_DIR" ] && [ -d /config ]; then
  HA_CONFIG_DIR=/config
fi
LOCAL_PAYLOAD_DIR="/app/payload"
WORK_DIR="/tmp/dwars-installer"
SUPERVISOR_API="http://supervisor"
STATE_DIR="/data"
SYSTEM_UPDATES_APPLIED="false"

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
    def root: if ((.options? | type) == "object") and (has("inverter_type") | not) then .options else . end;
    (root[$k] // null) as $v
    | if $v == null then $d
      elif ($v | type) == "object" or ($v | type) == "array" then $d
      elif ($v | type) == "boolean" then (if $v then "true" else "false" end)
      else ($v | tostring)
      end
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

  case "$selected" in
    both)
      # 'both' betekent GoodWe + SolarEdge. De generieke DWARS add-on wordt
      # alleen geïnstalleerd bij inverter_type=andere_omvormer.
      [ "$wanted" = "goodwe" ] || [ "$wanted" = "solaredge" ]
      ;;
    other|andere|andere_omvormer)
      [ "$wanted" = "other" ]
      ;;
    *)
      [ "$selected" = "$wanted" ]
      ;;
  esac
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

custom_components_required() {
  [ "$(get_bool install_custom_components true)" = "true" ] || return 1

  if should_handle goodwe || should_handle solaredge; then
    return 0
  fi

  return 1
}

local_payload_has_required_components() {
  local root="$1"

  if should_handle goodwe && [ ! -d "${root}/custom_components/goodwe" ]; then
    return 1
  fi

  if should_handle solaredge && [ ! -d "${root}/custom_components/solaredge_modbus_multi" ]; then
    return 1
  fi

  return 0
}

download_repo_payload_source() {
  local zip_url zip_file extract_dir top_dir

  zip_url="$(get_opt github_repo_zip_url '')"
  if [ -z "$zip_url" ] || [ "$zip_url" = "null" ]; then
    return 1
  fi

  rm -rf "$WORK_DIR"
  mkdir -p "$WORK_DIR"
  zip_file="${WORK_DIR}/repo.zip"
  extract_dir="${WORK_DIR}/repo"

  log "Remote repo-ZIP downloaden voor custom component payload."
  if ! curl -fsSL "$zip_url" -o "$zip_file"; then
    log "Remote download mislukt: ${zip_url}"
    return 1
  fi

  mkdir -p "$extract_dir"
  if ! unzip -q "$zip_file" -d "$extract_dir"; then
    log "Remote ZIP uitpakken mislukt."
    return 1
  fi

  top_dir="$(find "$extract_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [ -z "$top_dir" ] || [ ! -d "${top_dir}/custom_components" ]; then
    log "Remote ZIP bevat geen custom_components map."
    return 1
  fi

  printf '%s\n' "$top_dir"
}

prepare_payload_source() {
  local remote_enabled remote_root
  remote_enabled="$(get_bool auto_update_from_github false)"

  mkdir -p "$LOCAL_PAYLOAD_DIR"

  # Bij inverter_type=andere_omvormer worden geen custom_components geïnstalleerd.
  # Dan is een lege /app/payload voldoende en hoeft de Docker build geen payload-map te bevatten.
  if ! custom_components_required; then
    printf '%s\n' "$LOCAL_PAYLOAD_DIR"
    return 0
  fi

  if [ "$remote_enabled" != "true" ] && local_payload_has_required_components "$LOCAL_PAYLOAD_DIR"; then
    printf '%s\n' "$LOCAL_PAYLOAD_DIR"
    return 0
  fi

  if [ "$remote_enabled" = "true" ]; then
    log "auto_update_from_github=true; remote payload wordt gebruikt."
  else
    log "Embedded payload ontbreekt of is incompleet; remote repo-ZIP wordt als fallback gebruikt."
  fi

  if remote_root="$(download_repo_payload_source)"; then
    if local_payload_has_required_components "$remote_root"; then
      printf '%s\n' "$remote_root"
      return 0
    fi
    log "Remote payload mist één of meer geselecteerde custom components."
  fi

  if local_payload_has_required_components "$LOCAL_PAYLOAD_DIR"; then
    log "Remote payload niet bruikbaar; embedded payload wordt gebruikt."
    printf '%s\n' "$LOCAL_PAYLOAD_DIR"
    return 0
  fi

  fail "Geen bruikbare custom component payload gevonden. Zet auto_update_from_github=true of voeg custom_components/goodwe en/of custom_components/solaredge_modbus_multi toe aan de repo."
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

json_field() {
  local json="$1"
  local key="$2"
  local default="${3-}"
  printf '%s' "$json" | jq -r --arg k "$key" --arg d "$default" '((.data // .)[$k] // $d) | tostring' 2>/dev/null || printf '%s' "$default"
}

get_addon_info_json() {
  local addon_slug="$1"
  supervisor_curl GET "/addons/${addon_slug}/info" 2>/dev/null || true
}

get_store_addon_info_json() {
  local addon_slug="$1"
  supervisor_curl GET "/store/addons/${addon_slug}" 2>/dev/null || true
}

restart_agent_after_addon_change() {
  local addon_slug="$1"
  local label="$2"
  local previous_state="$3"

  [ "$(get_bool restart_agent_addons_after_update true)" = "true" ] || return 0

  if [ "$previous_state" = "started" ] || [ "$(get_bool start_agent_addons false)" = "true" ]; then
    log "${label}: herstart/start na add-on update."
    supervisor_curl POST "/addons/${addon_slug}/restart" '{}' >/dev/null \
      || supervisor_curl POST "/addons/${addon_slug}/start" '{}' >/dev/null \
      || true
  fi
}

rebuild_addon_if_repo_source_changed() {
  local addon_slug="$1"
  local base_slug="$2"
  local label="$3"
  local source_root="${4-}"
  local previous_state="$5"
  local version_update_done="$6"

  [ "$(get_bool rebuild_agent_addons_when_repo_changed true)" = "true" ] || return 0
  [ -n "$source_root" ] || return 0
  [ -d "${source_root}/${base_slug}" ] || return 0

  local source_hash state_file old_hash
  source_hash="$(dir_hash "${source_root}/${base_slug}")"
  state_file="${STATE_DIR}/${base_slug}.addon_source.sha256"
  old_hash=""
  [ -f "$state_file" ] && old_hash="$(cat "$state_file" 2>/dev/null || true)"

  if [ "$old_hash" = "$source_hash" ]; then
    log "${label}: add-on source is ongewijzigd (${source_hash})."
    return 0
  fi

  if [ "$version_update_done" = "true" ]; then
    log "${label}: add-on source hash gewijzigd en versie-update is al uitgevoerd (${source_hash})."
    printf '%s\n' "$source_hash" > "$state_file"
    return 0
  fi

  log "${label}: add-on source gewijzigd op GitHub; rebuild wordt geprobeerd (${old_hash:-geen} -> ${source_hash})."
  if supervisor_curl POST "/addons/${addon_slug}/rebuild" '{"force": true}' >/dev/null; then
    printf '%s\n' "$source_hash" > "$state_file"
    restart_agent_after_addon_change "$addon_slug" "$label" "$previous_state"
    return 0
  fi

  log "${label}: rebuild faalde; fallback naar Store update endpoint."
  if supervisor_curl POST "/store/addons/${addon_slug}/update" '{"backup": false, "background": false}' >/dev/null; then
    printf '%s\n' "$source_hash" > "$state_file"
    restart_agent_after_addon_change "$addon_slug" "$label" "$previous_state"
    return 0
  fi

  log "${label}: kon add-on niet rebuilden/updaten. Verhoog anders de version in ${base_slug}/config.json."
  return 1
}


goodwe_version_lt_1_7() {
  local version="${1:-0.0.0}" major minor rest
  version="${version%%-*}"
  major="${version%%.*}"
  rest="${version#*.}"
  if [ "$rest" = "$version" ]; then
    minor=0
  else
    minor="${rest%%.*}"
  fi
  case "$major" in ''|*[!0-9]*) return 0 ;; esac
  case "$minor" in ''|*[!0-9]*) minor=0 ;; esac
  if [ "$major" -lt 1 ]; then
    return 0
  fi
  if [ "$major" -eq 1 ] && [ "$minor" -lt 7 ]; then
    return 0
  fi
  return 1
}

repair_goodwe_options_for_current_schema() {
  local addon_slug="$1"
  local addon_info schema_keys raw_options clean_options payload current_version legacy_reset

  addon_info="$(get_addon_info_json "$addon_slug")"
  if [ -z "$addon_info" ]; then
    log "GoodWe Agent: kan huidige addon-info niet lezen; options-repair overgeslagen."
    return 0
  fi

  current_version="$(json_field "$addon_info" version '0.0.0')"
  legacy_reset="false"
  if [ "$(get_bool reset_legacy_goodwe_options_below_1_7 true)" = "true" ] && goodwe_version_lt_1_7 "$current_version"; then
    legacy_reset="true"
  fi

  # Belangrijk: dit draait VOOR de update. Gebruik daarom uitsluitend velden
  # die in de NU geïnstalleerde schema bestaan. Anders weigert Supervisor de
  # repair nog vóórdat de nieuwe versie geïnstalleerd is.
  schema_keys="$(printf '%s' "$addon_info" | jq -c '((.data.schema // .schema // {}) | if type == "object" then keys else [] end)' 2>/dev/null || echo '[]')"
  raw_options="$(printf '%s' "$addon_info" | jq -c '(.data.options // .options // {})' 2>/dev/null || echo '{}')"

  if [ "$legacy_reset" = "true" ]; then
    log "GoodWe Agent: versie ${current_version:-onbekend} is lager dan 1.7; reset alle opties naar defaults en behoud alleen api_key en ha_token."
  fi

  clean_options="$(jq -c -n \
    --argjson raw "$raw_options" \
    --argjson schema_keys "$schema_keys" \
    --arg legacy_reset "$legacy_reset" '
      def scalar: (type != "object" and type != "array");
      def as_string: if scalar then tostring else "" end;
      def legacy_defaults: {
        api_url: "https://api.metdezon.nl/bms/api/next_action.php",
        telemetry_url: "https://api.metdezon.nl/bms/api/telemetry.php",
        api_key: "",
        client_id: "",
        poll_interval: 60,
        safety_interval: 10,
        standalone_interval: 30,
        defaults_interval: 60,
        entity_discovery_interval: 60,
        power_watt: 5000,
        soc_entity: "auto",
        mode_entity: "",
        pv_entity: "auto",
        grid_entity: "auto",
        battery_power_entity: "auto",
        goodwe_serial_number: "",
        goodwe_serial_entity: "auto",
        goodwe_phase_entity: "auto",
        goodwe_ip_entity: "auto",
        goodwe_mac_entity: "auto",
        goodwe_last_seen_entity: "auto",
        ha_auto_entity_discovery: true,
        debug: 1,
        ha_url: "http://supervisor/core",
        ha_token: "",
        ha_control_enabled: true,
        ha_ems_mode_select: "auto",
        ha_ems_power_number: "auto",
        ha_ems_power_value: "server_power",
        ha_ems_set_power_modes: "3,4",
        ha_ems_set_power_before_mode: true,
        ha_ems_mode_0_option: "auto",
        ha_ems_mode_1_option: "battery_standby",
        ha_ems_mode_3_option: "charge_battery",
        ha_ems_mode_4_option: "discharge_battery",
        ha_ems_mode_7_option: "auto",
        override_default_values: false,
        goodwe_default_dod: 90,
        goodwe_default_dod_on_grid: 90,
        goodwe_default_dod_holding: "off",
        goodwe_default_backup_supply: "on",
        goodwe_default_operation_mode: "general",
        ha_dod_holding_switch: "auto",
        ha_backup_supply_switch: "auto",
        ha_dod_number: "auto",
        ha_dod_on_grid_number: "auto",
        ha_operation_mode_select: "auto",
        ha_charge_block_enabled: true,
        ha_charge_block_sensor: "auto",
        ha_charge_block_below_w: "auto",
        ha_charge_block_release_above_w: "auto",
        ha_charge_block_duration_sec: 300,
        ha_charge_block_modes: "3",
        ha_charge_block_fallback_option: "auto",
        ha_grid_export_limit_number: "auto",
        ha_grid_export_limit_switch: "auto",
        ha_grid_export_limit_off_value: "0",
        ha_grid_export_limit_default_value: "auto",
        ha_grid_export_limit_switch_curtail_state: "on",
        ha_grid_export_limit_switch_restore_state: "on",
        ha_pv_curtail_below_eur_kwh: "",
        ha_pv_curtail_enabled: true,
        standalone_enabled: false,
        standalone_pv_entity: "",
        standalone_grid_entity: "auto",
        standalone_deadband_w: 150,
        standalone_max_charge_w: 0,
        backup_yaml_check_enabled: true,
        backup_yaml_path: "/config/backup.yaml",
        backup_yaml_overwrite: false
      };
      # Ondersteun zowel een corrupte root {options:{...}} als een root met
      # daarnaast een corrupte options-key. Scalars uit root winnen van nested.
      ($raw.options // {}) as $nested
      | ((if ($nested|type) == "object" then $nested else {} end) + (if ($raw|type)=="object" then $raw else {} end)) as $merged
      | if $legacy_reset == "true" then
          # Voor upgrades vanuit <1.7: GEEN oude entitynamen/limieten/standalone/fasewaarden meenemen.
          # Alleen credentials blijven staan. Dit bootst praktisch een verse installatie na.
          legacy_defaults
          | .api_key = (($merged.api_key // "") | as_string)
          | .ha_token = (($merged.ha_token // "") | as_string)
        else
          (legacy_defaults + $merged)
          | del(.options)
          | with_entries(select(.value | scalar))
        end
      | del(.options)
      | if ($schema_keys | length) > 0 then with_entries(select(.key as $k | $schema_keys | index($k))) else . end
    ' 2>/dev/null || echo '{}')"

  if [ "$clean_options" = "{}" ] || [ -z "$clean_options" ]; then
    log "GoodWe Agent: lege clean options-map; repair overgeslagen."
    return 0
  fi

  payload="$(jq -c -n --argjson options "$clean_options" '{options:$options}')"
  if [ "$legacy_reset" = "true" ]; then
    log "GoodWe Agent: legacy reset-options opslaan vóór update."
  else
    log "GoodWe Agent: opgeslagen options normaliseren vóór update, zodat Supervisor-validatie niet op een oude nested map faalt."
  fi

  if supervisor_curl POST "/addons/${addon_slug}/options" "$payload" >/dev/null; then
    return 0
  fi

  log "GoodWe Agent: normale repair faalde; reset naar null en herhaal met schema-compatibele platte options."
  supervisor_curl POST "/addons/${addon_slug}/options" '{"options":null}' >/dev/null || true
  supervisor_curl POST "/addons/${addon_slug}/options" "$payload" >/dev/null || true
}

update_addon_if_available() {
  local addon_slug="$1"
  local base_slug="$2"
  local label="$3"
  local source_root="${4-}"

  [ "$(get_bool force_update_agent_addons true)" = "true" ] || return 0

  local addon_info store_info installed current latest update_available state backup update_payload version_update_done
  addon_info="$(get_addon_info_json "$addon_slug")"
  store_info="$(get_store_addon_info_json "$addon_slug")"

  installed="$(json_field "$addon_info" installed false)"
  current="$(json_field "$addon_info" version '')"
  latest="$(json_field "$store_info" version_latest '')"
  if [ -z "$latest" ] || [ "$latest" = "null" ]; then
    latest="$(json_field "$addon_info" version_latest '')"
  fi
  update_available="$(json_field "$store_info" update_available '')"
  if [ -z "$update_available" ] || [ "$update_available" = "null" ]; then
    update_available="$(json_field "$addon_info" update_available false)"
  fi
  state="$(json_field "$addon_info" state '')"
  version_update_done="false"

  if [ "$installed" != "true" ]; then
    return 0
  fi

  if [ "$base_slug" = "goodwe_agent" ]; then
    repair_goodwe_options_for_current_schema "$addon_slug" || true
  fi

  if [ -n "$current" ] && [ "$current" != "null" ] && [ -n "$latest" ] && [ "$latest" != "null" ]; then
    if [ "$current" != "$latest" ]; then
      update_available="true"
    fi
  fi

  if [ "$update_available" = "true" ]; then
    backup="$(get_bool addon_update_backup false)"
    update_payload="$(jq -n --argjson backup "$backup" '{backup: $backup, background: false}')"
    log "${label}: add-on update beschikbaar: ${current:-onbekend} -> ${latest:-latest}."
    if supervisor_curl POST "/store/addons/${addon_slug}/update" "$update_payload" >/dev/null; then
      version_update_done="true"
      try_supervisor_curl POST /addons/reload '{}' >/dev/null
      restart_agent_after_addon_change "$addon_slug" "$label" "$state"
    elif supervisor_curl POST "/addons/${addon_slug}/update" '{}' >/dev/null; then
      version_update_done="true"
      try_supervisor_curl POST /addons/reload '{}' >/dev/null
      restart_agent_after_addon_change "$addon_slug" "$label" "$state"
    else
      log "${label}: add-on update endpoint faalde."
    fi
  else
    log "${label}: add-on versie actueel (${current:-onbekend})."
  fi

  rebuild_addon_if_repo_source_changed "$addon_slug" "$base_slug" "$label" "$source_root" "$state" "$version_update_done" || true
}

ensure_addon_installed() {
  local base_slug="$1"
  local label="$2"
  local source_root="${3-}"

  local addon_slug addon_info installed current latest
  addon_slug="$(find_addon_slug "$base_slug" || true)"
  if [ -z "$addon_slug" ] || [ "$addon_slug" = "null" ]; then
    log "${label}: add-on niet gevonden in /addons. Controleer of deze repo in de Add-on Store is toegevoegd."
    return 1
  fi

  log "${label}: slug gevonden: ${addon_slug}"
  addon_info="$(supervisor_curl GET "/addons/${addon_slug}/info")"
  installed="$(json_field "$addon_info" installed false)"
  current="$(json_field "$addon_info" version '')"
  latest="$(json_field "$addon_info" version_latest '')"

  if [ "$installed" != "true" ]; then
    log "${label}: installeren via Store API."
    if ! supervisor_curl POST "/store/addons/${addon_slug}/install" '{"background": false}' >/dev/null; then
      log "${label}: Store API install faalde; fallback naar /addons/${addon_slug}/install."
      supervisor_curl POST "/addons/${addon_slug}/install" '{}' >/dev/null
    fi
  else
    log "${label}: geïnstalleerd (${current:-onbekend}, latest=${latest:-onbekend})."
  fi

  update_addon_if_available "$addon_slug" "$base_slug" "$label" "$source_root" || true
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

  local addon_info current_options desired_options merged_options options_payload api_key installer_key client_id installer_client_id ha_token
  addon_info="$(supervisor_curl GET "/addons/${addon_slug}/info" 2>/dev/null || echo '{}')"
  current_options="$(printf '%s' "$addon_info" | jq -c '
    (.data.options // .options // {})
    | if ((.options? | type) == "object") and ((.api_url? | type) != "string") then .options else . end
    | with_entries(select((.value|type) != "object" and (.value|type) != "array"))
  ' 2>/dev/null || echo '{}')"

  installer_key="$(get_opt goodwe_agent_api_key '')"
  installer_client_id="$(get_opt goodwe_agent_client_id '')"
  api_key="$(printf '%s' "$current_options" | jq -r '.api_key // ""')"
  client_id="$(printf '%s' "$current_options" | jq -r '.client_id // ""')"
  ha_token="$(printf '%s' "$current_options" | jq -r '.ha_token // ""')"
  [ -n "$installer_key" ] && api_key="$installer_key"
  [ -n "$installer_client_id" ] && client_id="$installer_client_id"

  desired_options="$(jq -n \
    --arg api_url "$(get_opt goodwe_agent_api_url 'https://api.metdezon.nl/bms/api/next_action.php')" \
    --arg telemetry_url "$(get_opt goodwe_agent_telemetry_url 'https://api.metdezon.nl/bms/api/telemetry.php')" \
    --arg api_key "$api_key" \
    --arg client_id "$client_id" \
    --arg ha_token "$ha_token" \
    --argjson poll_interval "$(get_opt goodwe_agent_poll_interval 60)" \
    --argjson power_watt "$(get_opt goodwe_agent_power_watt 5000)" \
    --argjson debug "$(get_bool goodwe_agent_debug true)" \
    --arg main_fuse "$(get_opt goodwe_agent_main_fuse_profile auto)" \
    '{
      api_url: $api_url,
      telemetry_url: $telemetry_url,
      api_key: $api_key,
      client_id: $client_id,
      poll_interval: $poll_interval,
      safety_interval: 10,
      standalone_interval: 30,
      defaults_interval: 60,
      entity_discovery_interval: 60,
      power_watt: $power_watt,
      main_fuse_profile: $main_fuse,
      soc_entity: "auto",
      mode_entity: "",
      pv_entity: "auto",
      grid_entity: "auto",
      battery_power_entity: "auto",
      goodwe_serial_number: "",
      goodwe_serial_entity: "auto",
      goodwe_phase_entity: "auto",
      goodwe_ip_entity: "auto",
      goodwe_mac_entity: "auto",
      goodwe_last_seen_entity: "auto",
      ha_auto_entity_discovery: true,
      ha_registry_discovery: true,
      publish_diagnostic_entities: true,
      debug: (if $debug then 1 else 0 end),
      ha_url: "http://supervisor/core",
      ha_token: $ha_token,
      ha_control_enabled: true,
      ha_ems_mode_select: "auto",
      ha_ems_power_number: "auto",
      ha_ems_power_value: "server_power",
      ha_ems_set_power_modes: "3,4",
      ha_ems_set_power_before_mode: true,
      ha_ems_mode_0_option: "auto",
      ha_ems_mode_1_option: "battery_standby",
      ha_ems_mode_3_option: "charge_battery",
      ha_ems_mode_4_option: "discharge_battery",
      ha_ems_mode_7_option: "auto",
      override_default_values: false,
      goodwe_default_dod: 90,
      goodwe_default_dod_on_grid: 90,
      goodwe_default_dod_holding: "off",
      goodwe_default_backup_supply: "on",
      goodwe_default_operation_mode: "general",
      ha_dod_holding_switch: "auto",
      ha_backup_supply_switch: "auto",
      ha_dod_number: "auto",
      ha_dod_on_grid_number: "auto",
      ha_operation_mode_select: "auto",
      ha_charge_block_enabled: true,
      ha_charge_block_sensor: "auto",
      ha_charge_block_below_w: "auto",
      ha_charge_block_release_above_w: "auto",
      ha_charge_block_duration_sec: 300,
      ha_charge_block_modes: "3",
      ha_charge_block_fallback_option: "auto",
      ha_grid_export_limit_number: "auto",
      ha_grid_export_limit_switch: "auto",
      ha_grid_export_limit_off_value: "0",
      ha_grid_export_limit_default_value: "auto",
      ha_grid_export_limit_switch_curtail_state: "on",
      ha_grid_export_limit_switch_restore_state: "on",
      ha_pv_curtail_below_eur_kwh: "",
      ha_pv_curtail_enabled: true,
      standalone_enabled: false,
      standalone_pv_entity: "",
      standalone_grid_entity: "auto",
      standalone_deadband_w: 150,
      standalone_max_charge_w: 0,
      backup_yaml_check_enabled: true,
      backup_yaml_path: "/config/backup.yaml",
      backup_yaml_overwrite: false
    }')"

  # Preserve bestaande handmatige klantinstellingen, maar vul nieuwe defaults aan.
  # Bij een corrupte nested options-map wordt die hierboven eerst platgetrokken.
  merged_options="$(jq -c -n --argjson current "$current_options" --argjson desired "$desired_options" '
    ($desired + $current)
    | .api_url = $desired.api_url
    | .telemetry_url = $desired.telemetry_url
    | .api_key = (if ($desired.api_key // "") != "" then $desired.api_key else (.api_key // "") end)
    | .client_id = (if ($desired.client_id // "") != "" then $desired.client_id else (.client_id // "") end)
    | .ha_token = (if ($desired.ha_token // "") != "" then $desired.ha_token else (.ha_token // "") end)
    | .poll_interval = $desired.poll_interval
    | .power_watt = $desired.power_watt
    | .main_fuse_profile = (if ($desired.main_fuse_profile // "auto") != "auto" then $desired.main_fuse_profile else (.main_fuse_profile // "auto") end)
  ')"
  options_payload="$(jq -c -n --argjson options "$merged_options" '{boot:"auto", auto_update:true, options:$options}')"

  log "GoodWe Agent configureren met gevalideerde platte options-map."
  if ! supervisor_curl POST "/addons/${addon_slug}/options" "$options_payload" >/dev/null; then
    log "GoodWe Agent options-save faalde; corrupte opgeslagen options worden gereset en daarna opnieuw toegepast."
    supervisor_curl POST "/addons/${addon_slug}/options" '{"options":null}' >/dev/null || true
    supervisor_curl POST "/addons/${addon_slug}/options" "$options_payload" >/dev/null
  fi
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

configure_dwars_addon() {
  local addon_slug="$1"
  [ "$(get_bool configure_agent_addons true)" = "true" ] || return 0

  local options_payload
  options_payload="$(jq -n \
    --arg api_url "$(get_opt dwars_addon_api_url 'https://api.metdezon.nl/bms/api/next_action.php')" \
    --arg telemetry_url "$(get_opt dwars_addon_telemetry_url 'https://api.metdezon.nl/bms/api/telemetry.php')" \
    --arg api_key "$(get_opt dwars_addon_api_key '')" \
    --arg client_id "$(get_opt dwars_addon_client_id '')" \
    --argjson poll_interval "$(get_opt dwars_addon_poll_interval 60)" \
    --argjson power_watt "$(get_opt dwars_addon_power_watt 5000)" \
    --argjson debug "$(get_bool dwars_addon_debug true)" \
    --arg soc_entity "$(get_opt dwars_addon_soc_entity '')" \
    --arg pv_entity "$(get_opt dwars_addon_pv_entity '')" \
    --arg grid_entity "$(get_opt dwars_addon_grid_entity '')" \
    --arg battery_power_entity "$(get_opt dwars_addon_battery_power_entity '')" \
    --arg inverter_mode_entity "$(get_opt dwars_addon_inverter_mode_entity '')" \
    --arg mode_select "$(get_opt dwars_addon_ha_mode_select '')" \
    --arg mode_map_json "$(get_opt dwars_addon_ha_mode_map_json '')" \
    --arg idle_option "$(get_opt dwars_addon_ha_mode_idle_option 'auto')" \
    --arg charge_option "$(get_opt dwars_addon_ha_mode_charge_option 'charge')" \
    --arg discharge_option "$(get_opt dwars_addon_ha_mode_discharge_option 'discharge')" \
    --arg idle_modes "$(get_opt dwars_addon_ha_server_modes_idle '1,7')" \
    --arg charge_modes "$(get_opt dwars_addon_ha_server_modes_charge '3')" \
    --arg discharge_modes "$(get_opt dwars_addon_ha_server_modes_discharge '4')" \
    --arg power_number "$(get_opt dwars_addon_ha_power_number '')" \
    --arg charge_power_number "$(get_opt dwars_addon_ha_charge_power_number '')" \
    --arg discharge_power_number "$(get_opt dwars_addon_ha_discharge_power_number '')" \
    --arg idle_power_number "$(get_opt dwars_addon_ha_idle_power_number '')" \
    --arg charge_power_value "$(get_opt dwars_addon_ha_charge_power_value 'server_power')" \
    --arg discharge_power_value "$(get_opt dwars_addon_ha_discharge_power_value 'server_power')" \
    --arg idle_power_value "$(get_opt dwars_addon_ha_idle_power_value 'skip')" \
    --argjson set_power_before_mode "$(get_bool dwars_addon_ha_set_power_before_mode true)" \
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
        debug: $debug,
        ha_url: "",
        ha_token: "",
        soc_entity: $soc_entity,
        pv_entity: $pv_entity,
        grid_entity: $grid_entity,
        battery_power_entity: $battery_power_entity,
        inverter_mode_entity: $inverter_mode_entity,
        ha_control_enabled: true,
        ha_mode_select: $mode_select,
        ha_mode_map_json: $mode_map_json,
        ha_mode_idle_option: $idle_option,
        ha_mode_charge_option: $charge_option,
        ha_mode_discharge_option: $discharge_option,
        ha_server_modes_idle: $idle_modes,
        ha_server_modes_charge: $charge_modes,
        ha_server_modes_discharge: $discharge_modes,
        ha_power_number: $power_number,
        ha_charge_power_number: $charge_power_number,
        ha_discharge_power_number: $discharge_power_number,
        ha_idle_power_number: $idle_power_number,
        ha_charge_power_value: $charge_power_value,
        ha_discharge_power_value: $discharge_power_value,
        ha_idle_power_value: $idle_power_value,
        ha_set_power_before_mode: $set_power_before_mode
      }
    }')"

  log "DWARS Generic EMS Add-on configureren."
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
  local source_root="${1-}"
  [ "$(get_bool install_agent_addons true)" = "true" ] || return 0
  ensure_store_reloaded
  configure_installer_self_update

  if should_handle goodwe; then
    local goodwe_slug
    goodwe_slug="$(ensure_addon_installed goodwe_agent 'GoodWe Agent / BMS' "$source_root" || true)"
    if [ -n "$goodwe_slug" ]; then
      set_addon_boot_auto_update "$goodwe_slug" "GoodWe Agent / BMS"
      configure_goodwe_agent "$goodwe_slug"
      start_addon_if_requested "$goodwe_slug" "GoodWe Agent / BMS"
    fi
  fi

  if should_handle solaredge; then
    local se_slug
    se_slug="$(ensure_addon_installed solaredge_agent 'SolarEdge Agent / BMS' "$source_root" || true)"
    if [ -z "$se_slug" ]; then
      se_slug="$(ensure_addon_installed metdezon_bms_agent 'SolarEdge Agent / BMS' "$source_root" || true)"
    fi
    if [ -n "$se_slug" ]; then
      set_addon_boot_auto_update "$se_slug" "SolarEdge Agent / BMS"
      configure_solaredge_agent "$se_slug"
      start_addon_if_requested "$se_slug" "SolarEdge Agent / BMS"
    fi
  fi

  if should_handle other; then
    local dwars_slug
    dwars_slug="$(ensure_addon_installed dwars_addon 'DWARS Generic EMS Add-on' "$source_root" || true)"
    if [ -n "$dwars_slug" ]; then
      set_addon_boot_auto_update "$dwars_slug" "DWARS Generic EMS Add-on"
      configure_dwars_addon "$dwars_slug"
      start_addon_if_requested "$dwars_slug" "DWARS Generic EMS Add-on"
    fi
  fi
}

restart_homeassistant_core() {
  log "Home Assistant Core herstarten zodat custom_components opnieuw geladen worden."
  supervisor_curl POST /core/restart '{}' >/dev/null
}


ha_curl() {
  local method="$1"
  local path="$2"
  local data="${3-}"
  if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
    log "SUPERVISOR_TOKEN ontbreekt; HA-updatecyclus wordt overgeslagen."
    return 1
  fi
  if [ -n "$data" ]; then
    curl -fsS -X "$method" -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" -H "Content-Type: application/json" -d "$data" "http://supervisor/core/api${path}"
  else
    curl -fsS -X "$method" -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" -H "Content-Type: application/json" "http://supervisor/core/api${path}"
  fi
}

is_upstream_goodwe_update_entity() {
  local entity_json="$1"
  local haystack
  haystack="$(printf '%s' "$entity_json" | jq -r '[.entity_id, .state, (.attributes.friendly_name // ""), (.attributes.title // ""), (.attributes.installed_version // ""), (.attributes.latest_version // ""), (.attributes.release_url // ""), (.attributes.entity_picture // "")] | join(" ")' 2>/dev/null | tr '[:upper:]' '[:lower:]')"
  case "$haystack" in
    *goodwe*)
      case "$haystack" in
        *dwars*|*dcent*|*metdezon*|*cryptowhizzard*) return 1 ;;
        *) return 0 ;;
      esac
      ;;
  esac
  return 1
}

install_ha_update_entities() {
  [ "$(get_bool auto_full_system_update true)" = "true" ] || return 0
  local states skip_goodwe backup entities entity_json entity_id
  backup="$(get_bool auto_full_system_update_backup true)"
  skip_goodwe="$(get_bool skip_upstream_goodwe_updates true)"
  log "Home Assistant update-entities controleren."
  states="$(ha_curl GET /states 2>/dev/null || true)"
  [ -n "$states" ] || { log "Kon /api/states niet lezen; HA update-entities overgeslagen."; return 0; }
  entities="$(printf '%s' "$states" | jq -c '.[] | select(.entity_id|startswith("update.")) | select(.state == "on")')"
  [ -n "$entities" ] || { log "Geen HA update-entities met state=on."; return 0; }
  while IFS= read -r entity_json; do
    [ -n "$entity_json" ] || continue
    entity_id="$(printf '%s' "$entity_json" | jq -r '.entity_id')"
    if [ "$skip_goodwe" = "true" ] && is_upstream_goodwe_update_entity "$entity_json"; then
      log "${entity_id}: upstream/originele GoodWe update overgeslagen; DWARS-versie blijft leidend."
      continue
    fi
    log "${entity_id}: update.install uitvoeren."
    if ha_curl POST /services/update/install "$(jq -n --arg e "$entity_id" --argjson backup "$backup" '{entity_id:$e, backup:$backup}')" >/dev/null; then
      SYSTEM_UPDATES_APPLIED="true"
    else
      log "${entity_id}: update.install faalde."
    fi
    sleep 5
  done <<< "$entities"
}

update_all_installed_addons() {
  [ "$(get_bool auto_full_system_update true)" = "true" ] || return 0
  local backup addons slug update_available name
  backup="$(get_bool addon_update_backup false)"
  addons="$(supervisor_curl GET /addons 2>/dev/null || true)"
  [ -n "$addons" ] || return 0
  while IFS=$'\t' read -r slug name update_available; do
    [ -n "$slug" ] || continue
    if [ "$update_available" = "true" ]; then
      log "Add-on ${name} (${slug}) updaten."
      if supervisor_curl POST "/store/addons/${slug}/update" "$(jq -n --argjson backup "$backup" '{backup:$backup, background:false}')" >/dev/null; then
        SYSTEM_UPDATES_APPLIED="true"
        supervisor_curl POST "/addons/${slug}/restart" '{}' >/dev/null || true
      else
        log "Add-on ${name} (${slug}) update faalde."
      fi
    fi
  done < <(printf '%s' "$addons" | jq -r '(.addons // .data.addons // [])[] | select(.installed == true) | [.slug, (.name // .slug), (.update_available // false)] | @tsv')
}

supervisor_update_if_available() {
  local info_path="$1"
  local update_path="$2"
  local label="$3"
  local info update_available
  info="$(supervisor_curl GET "$info_path" 2>/dev/null || true)"
  if [ -z "$info" ]; then
    log "${label}: info niet beschikbaar; update overgeslagen."
    return 0
  fi
  update_available="$(printf '%s' "$info" | jq -r '(.data.update_available // .update_available // false)' 2>/dev/null || echo false)"
  if [ "$update_available" != "true" ]; then
    log "${label}: geen update beschikbaar."
    return 0
  fi
  log "${label}: update beschikbaar; update uitvoeren."
  if supervisor_curl POST "$update_path" '{}' >/dev/null; then
    SYSTEM_UPDATES_APPLIED="true"
  else
    log "${label}: update faalde."
  fi
}

update_supervisor_core_os_best_effort() {
  [ "$(get_bool auto_full_system_update true)" = "true" ] || return 0
  log "Supervisor/Core/OS updates controleren."
  supervisor_update_if_available /supervisor/info /supervisor/update "Supervisor"
  supervisor_update_if_available /core/info /core/update "Home Assistant Core"
  supervisor_update_if_available /os/info /os/update "Home Assistant OS"
}

run_full_system_update_cycle() {
  [ "$(get_bool auto_full_system_update true)" = "true" ] || return 0
  SYSTEM_UPDATES_APPLIED="false"
  log "Volledige automatische updatecyclus gestart."
  install_ha_update_entities || true
  update_all_installed_addons || true
  update_supervisor_core_os_best_effort || true
  if [ "$SYSTEM_UPDATES_APPLIED" = "true" ] && [ "$(get_bool auto_full_system_update_reboot true)" = "true" ]; then
    log "Minimaal één update toegepast; host reboot aanvragen."
    try_supervisor_curl POST /host/reboot '{}' >/dev/null
  else
    log "Geen toegepaste updates of reboot uitgeschakeld; geen host reboot."
  fi
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

  install_or_configure_agents "$source_root"

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
  run_full_system_update_cycle || true
  log "Install/update cycle klaar."

  if [ "$(get_bool watch_for_embedded_updates true)" = "true" ] || [ "$(get_bool auto_update_from_github false)" = "true" ] || [ "$(get_bool auto_full_system_update true)" = "true" ]; then
    local interval full_interval last_full now_ts
    interval="$(get_opt update_check_interval_sec 900)"
    full_interval="$(get_opt auto_full_system_update_interval_sec 21600)"
    last_full="$(date +%s)"
    log "Updater blijft actief: repo/add-ons iedere ${interval}s, volledige systeemupdates iedere ${full_interval}s."
    while true; do
      sleep "$interval"
      run_install_cycle || log "Update cycle gaf een fout; volgende interval probeert opnieuw."
      now_ts="$(date +%s)"
      if [ $((now_ts - last_full)) -ge "$full_interval" ]; then
        run_full_system_update_cycle || true
        last_full="$now_ts"
      fi
    done
  fi

  log "Updater loop staat uit; add-on blijft in idle mode actief."
  while true; do sleep 86400; done
}

main "$@"
