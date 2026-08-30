#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG_PATH="${CONFIG_PATH:-/data/options.json}"
SUPERVISOR_API="${SUPERVISOR_API:-http://supervisor}"
CREDENTIAL_BACKUP_PATH="${CREDENTIAL_BACKUP_PATH:-/data/goodwe-credentials-backup.json}"
MIN_MIGRATED_VERSION="${MIN_MIGRATED_VERSION:-1.7.0}"
SHARED_MARKER_PATH="${SHARED_MARKER_PATH:-/config/.dwars_goodwe_defaults_only.json}"

log() {
  printf '[GoodWe Legacy Options Migrator] %s\n' "$*" >&2
}

fail() {
  log "ERROR: $*"
  exit 1
}

get_opt() {
  local key="$1" default="${2-}"
  if [ ! -f "$CONFIG_PATH" ]; then
    printf '%s' "$default"
    return 0
  fi
  jq -r --arg key "$key" --arg default "$default" '
    (.[$key] // $default) as $value
    | if ($value | type) == "boolean" then (if $value then "true" else "false" end)
      elif (($value | type) == "object" or ($value | type) == "array") then $default
      else ($value | tostring)
      end
  ' "$CONFIG_PATH" 2>/dev/null || printf '%s' "$default"
}

is_true() {
  case "$(printf '%s' "${1-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on|ja|aan) return 0 ;;
    *) return 1 ;;
  esac
}

supervisor_curl() {
  local method="$1" path="$2"
  local data_is_set="${3+x}" data="${3-}"

  [ -n "${SUPERVISOR_TOKEN:-}" ] \
    || fail "SUPERVISOR_TOKEN ontbreekt; hassio_api=true en hassio_role=manager zijn vereist."

  if [ -n "$data_is_set" ]; then
    curl -fsS \
      -X "$method" \
      -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
      -H 'Content-Type: application/json' \
      -d "$data" \
      "${SUPERVISOR_API}${path}"
  else
    curl -fsS \
      -X "$method" \
      -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
      -H 'Content-Type: application/json' \
      "${SUPERVISOR_API}${path}"
  fi
}

find_goodwe_slug() {
  local configured addons
  configured="$(get_opt target_slug auto)"
  if [ -n "$configured" ] && [ "$configured" != "auto" ]; then
    printf '%s\n' "$configured"
    return 0
  fi

  addons="$(supervisor_curl GET /addons)"
  printf '%s' "$addons" | jq -r '
    (.data.addons // .addons // [])[]
    | select(
        .slug == "goodwe_agent"
        or (.slug | endswith("_goodwe_agent"))
        or ((.name // "") | test("GoodWe Agent"; "i"))
      )
    | .slug
  ' | head -n 1
}

version_lt() {
  local left="${1:-0}" right="${2:-0}"
  local l1 l2 l3 r1 r2 r3
  left="${left%%-*}"; right="${right%%-*}"
  IFS=. read -r l1 l2 l3 _ <<<"$left"
  IFS=. read -r r1 r2 r3 _ <<<"$right"
  l1="${l1:-0}"; l2="${l2:-0}"; l3="${l3:-0}"
  r1="${r1:-0}"; r2="${r2:-0}"; r3="${r3:-0}"
  [[ "$l1" =~ ^[0-9]+$ ]] || l1=0
  [[ "$l2" =~ ^[0-9]+$ ]] || l2=0
  [[ "$l3" =~ ^[0-9]+$ ]] || l3=0
  [[ "$r1" =~ ^[0-9]+$ ]] || r1=0
  [[ "$r2" =~ ^[0-9]+$ ]] || r2=0
  [[ "$r3" =~ ^[0-9]+$ ]] || r3=0
  (( l1 < r1 )) && return 0
  (( l1 > r1 )) && return 1
  (( l2 < r2 )) && return 0
  (( l2 > r2 )) && return 1
  (( l3 < r3 ))
}

extract_secret_recursive() {
  local json="$1" key="$2"
  printf '%s' "$json" | jq -r --arg key "$key" '
    [
      .. | objects | .[$key]?
      | select(type == "string")
      | select(length > 0)
    ][0] // ""
  ' 2>/dev/null || true
}

addon_info() {
  supervisor_curl GET "/addons/$1/info"
}

addon_version() {
  printf '%s' "$1" | jq -r '(.data.version // .version // "0.0.0")' 2>/dev/null || printf '0.0.0'
}

addon_options() {
  printf '%s' "$1" | jq -c '
    (.data.options // .options // {})
    | if type == "object" then . else {} end
  ' 2>/dev/null || printf '{}'
}

validate_stored_options() {
  local slug="$1" response valid message
  response="$(supervisor_curl POST "/addons/${slug}/options/validate")"
  valid="$(printf '%s' "$response" | jq -r '(.data.valid // .valid // true) | tostring' 2>/dev/null || printf true)"
  if [ "$valid" != "true" ]; then
    message="$(printf '%s' "$response" | jq -r '(.data.message // .message // "onbekende validatiefout")' 2>/dev/null || true)"
    log "Options-validatie faalde: ${message}"
    return 1
  fi
  return 0
}

save_credentials_only() {
  local slug="$1" api_key="$2" ha_token="$3" payload
  payload="$(jq -c -n \
    --arg api_key "$api_key" \
    --arg ha_token "$ha_token" \
    '{options:{api_key:$api_key, ha_token:$ha_token}}')"
  supervisor_curl POST "/addons/${slug}/options" "$payload" >/dev/null
}

restore_old_defaults_with_credentials() {
  local slug="$1" api_key="$2" ha_token="$3"
  log "Update is niet gelukt; oude add-on-defaults met uitsluitend api_key en ha_token herstellen."
  if save_credentials_only "$slug" "$api_key" "$ha_token" && validate_stored_options "$slug"; then
    log "Oude GoodWe-versie is weer valide geconfigureerd."
  else
    log "WAARSCHUWING: automatisch herstel faalde; credentialbackup staat in ${CREDENTIAL_BACKUP_PATH}."
  fi
}

wait_for_update() {
  local slug="$1" old_version="$2" target_version="$3" timeout_sec="${4:-600}"
  local started now info version
  started="$(date +%s)"
  while true; do
    info="$(addon_info "$slug" 2>/dev/null || true)"
    version="$(addon_version "$info")"
    if [ -n "$version" ] && [ "$version" != "$old_version" ] && ! version_lt "$version" "$MIN_MIGRATED_VERSION"; then
      if [ -z "$target_version" ] || [ "$target_version" = "null" ] || [ "$version" = "$target_version" ] || ! version_lt "$version" "$target_version"; then
        printf '%s\n' "$version"
        return 0
      fi
    fi
    now="$(date +%s)"
    if (( now - started >= timeout_sec )); then
      return 1
    fi
    sleep 5
  done
}

run_update() {
  local slug="$1"
  supervisor_curl POST /store/reload '{}' >/dev/null || true
  supervisor_curl POST /addons/reload '{}' >/dev/null || true

  if supervisor_curl POST "/store/addons/${slug}/update" '{"backup":false,"background":false}' >/dev/null; then
    return 0
  fi
  supervisor_curl POST "/addons/${slug}/update" '{}' >/dev/null
}

main() {
  local slug info old_version raw_options api_key ha_token store_info target_version new_version

  slug="$(find_goodwe_slug)"
  [ -n "$slug" ] && [ "$slug" != "null" ] \
    || fail "GoodWe Agent slug niet gevonden. Vul target_slug in, bijvoorbeeld 23c0ae60_goodwe_agent."

  info="$(addon_info "$slug")"
  old_version="$(addon_version "$info")"
  raw_options="$(addon_options "$info")"
  api_key="$(extract_secret_recursive "$raw_options" api_key)"
  ha_token="$(extract_secret_recursive "$raw_options" ha_token)"

  umask 077
  mkdir -p "$(dirname "$CREDENTIAL_BACKUP_PATH")"
  jq -n \
    --arg slug "$slug" \
    --arg version "$old_version" \
    --arg api_key "$api_key" \
    --arg ha_token "$ha_token" \
    '{slug:$slug, source_version:$version, api_key:$api_key, ha_token:$ha_token}' \
    >"$CREDENTIAL_BACKUP_PATH"
  chmod 600 "$CREDENTIAL_BACKUP_PATH" 2>/dev/null || true

  log "Doel-add-on=${slug}; geïnstalleerd=${old_version}; api_key lengte=${#api_key}; ha_token lengte=${#ha_token}."

  if ! version_lt "$old_version" "$MIN_MIGRATED_VERSION"; then
    log "Versie ${old_version} is niet lager dan ${MIN_MIGRATED_VERSION}; geen destructieve legacy-reset uitgevoerd."
    if is_true "$(get_opt run_update true)"; then
      run_update "$slug" || fail "Normale GoodWe-update mislukte."
    fi
    rm -f "$CREDENTIAL_BACKUP_PATH"
    exit 0
  fi

  is_true "$(get_opt preserve_only_api_key_and_ha_token true)" \
    || fail "Deze migrator ondersteunt bewust alleen reset met behoud van api_key en ha_token."

  log "Legacy-upgrade: ALLE opgeslagen GoodWe-options worden eerst verwijderd met options=null."
  log "Vóór de update wordt geen enkele 1.8.x-optie tegen de 1.5.x-schema opgeslagen."
  supervisor_curl POST "/addons/${slug}/options" '{"options":null}' >/dev/null \
    || fail "Supervisor kon de legacy-options niet resetten. Er is niets geüpdatet."

  validate_stored_options "$slug" \
    || fail "De huidige ${old_version}-defaults zijn na options=null niet geldig; update wordt niet gestart."

  if ! is_true "$(get_opt run_update true)"; then
    save_credentials_only "$slug" "$api_key" "$ha_token" \
      || fail "Credentials konden niet op de oude defaults worden teruggezet."
    validate_stored_options "$slug" \
      || fail "Oude defaults met credentials zijn niet geldig."
    log "run_update=false: reset uitgevoerd en uitsluitend api_key/ha_token teruggezet."
    rm -f "$CREDENTIAL_BACKUP_PATH"
    exit 0
  fi

  store_info="$(supervisor_curl GET "/store/addons/${slug}" 2>/dev/null || true)"
  target_version="$(printf '%s' "$store_info" | jq -r '(.data.version // .data.version_latest // .version // .version_latest // "")' 2>/dev/null || true)"
  log "Persisted options zijn leeg. GoodWe-update wordt nu gestart${target_version:+ naar ${target_version}}."

  if ! run_update "$slug"; then
    restore_old_defaults_with_credentials "$slug" "$api_key" "$ha_token"
    fail "GoodWe-updateendpoint faalde."
  fi

  if ! new_version="$(wait_for_update "$slug" "$old_version" "$target_version" 600)"; then
    restore_old_defaults_with_credentials "$slug" "$api_key" "$ha_token"
    fail "GoodWe-versie veranderde niet binnen 600 seconden."
  fi

  supervisor_curl POST /addons/reload '{}' >/dev/null || true
  log "GoodWe-update geslaagd: ${old_version} -> ${new_version}. Nu worden uitsluitend api_key en ha_token als custom options opgeslagen."

  save_credentials_only "$slug" "$api_key" "$ha_token" \
    || fail "Credentials konden niet op de nieuwe defaults worden opgeslagen; backup staat in ${CREDENTIAL_BACKUP_PATH}."
  validate_stored_options "$slug" \
    || fail "Nieuwe defaults met api_key/ha_token zijn niet geldig."

  supervisor_curl POST "/addons/${slug}/options" '{"boot":"auto","auto_update":true}' >/dev/null || true

  if is_true "$(get_opt restart_after_update true)"; then
    supervisor_curl POST "/addons/${slug}/restart" '{}' >/dev/null \
      || supervisor_curl POST "/addons/${slug}/start" '{}' >/dev/null \
      || true
  fi

  if [ -d "$(dirname "$SHARED_MARKER_PATH")" ]; then
    jq -n --arg slug "$slug" --arg version "$new_version" \
      '{slug:$slug, migrated_version:$version, defaults_only:true}' >"$SHARED_MARKER_PATH"
    chmod 600 "$SHARED_MARKER_PATH" 2>/dev/null || true
  fi

  rm -f "$CREDENTIAL_BACKUP_PATH"
  log "Klaar: oude options volledig verwijderd; nieuwe versie draait op defaults met alleen api_key en ha_token als overrides."
}

main "$@"
