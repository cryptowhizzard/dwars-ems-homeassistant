#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="/data/options.json"
HA_CONFIG_DIR="/homeassistant_config"
PAYLOAD_DIR="/app/payload"
GOODWE_COMPONENT_SRC="${PAYLOAD_DIR}/custom_components/goodwe"
GOODWE_COMPONENT_DST="${HA_CONFIG_DIR}/custom_components/goodwe"
SUPERVISOR_API="http://supervisor"

log() {
  printf '[DWARS Installer] %s\n' "$*"
}

fail() {
  log "ERROR: $*"
  exit 1
}

get_opt() {
  local key="$1"
  local default="${2-}"
  jq -r --arg k "$key" --arg d "$default" '
    if has($k) then
      .[$k]
    else
      $d
    end // $d
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

install_goodwe_custom_component() {
  [ -d "$GOODWE_COMPONENT_SRC" ] || fail "Payload ontbreekt: ${GOODWE_COMPONENT_SRC}"
  mkdir -p "${HA_CONFIG_DIR}/custom_components"

  if [ -d "$GOODWE_COMPONENT_DST" ]; then
    log "Bestaande GoodWe custom component verwijderen: ${GOODWE_COMPONENT_DST}"
    rm -rf "$GOODWE_COMPONENT_DST"
  fi

  log "GoodWe custom component installeren naar ${GOODWE_COMPONENT_DST}"
  cp -a "$GOODWE_COMPONENT_SRC" "$GOODWE_COMPONENT_DST"

  if [ -f "${GOODWE_COMPONENT_DST}/manifest.json" ]; then
    log "Geïnstalleerde manifest: $(jq -r '.domain + " " + (.version // "no-version")' "${GOODWE_COMPONENT_DST}/manifest.json")"
  fi
}

find_goodwe_agent_slug() {
  local addons_json
  addons_json="$(supervisor_curl GET /addons)"

  printf '%s' "$addons_json" | jq -r '
    (.addons // .data.addons // [])[]
    | select(
        .slug == "goodwe_agent"
        or .slug == "local_goodwe_agent"
        or (.slug | test("_goodwe_agent$"))
      )
    | .slug
  ' | head -n 1
}

install_or_configure_goodwe_agent_addon() {
  log "Add-on store herladen"
  supervisor_curl POST /store/reload '{}' >/dev/null || supervisor_curl POST /addons/reload '{}' >/dev/null || true

  local addon_slug
  addon_slug="$(find_goodwe_agent_slug || true)"
  if [ -z "$addon_slug" ] || [ "$addon_slug" = "null" ]; then
    log "GoodWe Agent add-on niet gevonden in /addons. Controleer of deze repo in de Add-on Store is toegevoegd."
    return 1
  fi

  log "GoodWe Agent slug gevonden: ${addon_slug}"

  local addon_info installed
  addon_info="$(supervisor_curl GET "/addons/${addon_slug}/info")"
  installed="$(printf '%s' "$addon_info" | jq -r '.installed // .data.installed // false')"

  if [ "$installed" != "true" ]; then
    log "GoodWe Agent installeren via Store API"
    if ! supervisor_curl POST "/store/addons/${addon_slug}/install" '{"background": false}' >/dev/null; then
      log "Store API install faalde; fallback naar /addons/${addon_slug}/install"
      supervisor_curl POST "/addons/${addon_slug}/install" '{}' >/dev/null
    fi
  else
    log "GoodWe Agent is al geïnstalleerd"
  fi

  if [ "$(get_bool configure_goodwe_agent_addon true)" = "true" ]; then
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
          ha_ems_mode_1_option: "battery_standby",
          ha_ems_mode_3_option: "import_ac",
          ha_ems_mode_4_option: "export_ac",
          ha_ems_mode_7_option: "charge_pv",
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

    log "GoodWe Agent configureren: boot=auto, auto_update=true"
    supervisor_curl POST "/addons/${addon_slug}/options" "$options_payload" >/dev/null
  fi

  if [ "$(get_bool start_goodwe_agent_addon false)" = "true" ]; then
    log "GoodWe Agent starten"
    supervisor_curl POST "/addons/${addon_slug}/start" '{}' >/dev/null
  else
    log "GoodWe Agent is geïnstalleerd/geconfigureerd maar nog niet gestart. Zet start_goodwe_agent_addon=true als je hem direct wilt starten."
  fi
}

restart_homeassistant_core() {
  log "Home Assistant Core herstarten zodat custom_components opnieuw geladen worden"
  supervisor_curl POST /core/restart '{}' >/dev/null
}

main() {
  [ -f "$CONFIG_PATH" ] || fail "Geen ${CONFIG_PATH} gevonden"

  if [ "$(get_bool install_goodwe_custom_component true)" = "true" ]; then
    install_goodwe_custom_component
  fi

  if [ "$(get_bool install_goodwe_agent_addon true)" = "true" ]; then
    install_or_configure_goodwe_agent_addon || log "GoodWe Agent automatische installatie overgeslagen door fout hierboven"
  fi

  if [ "$(get_bool restart_homeassistant_after_custom_component true)" = "true" ]; then
    restart_homeassistant_core
  else
    log "Home Assistant restart overgeslagen. Herstart handmatig om de custom component te laden."
  fi

  log "Klaar."
}

main "$@"
