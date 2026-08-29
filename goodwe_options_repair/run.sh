#!/usr/bin/with-contenv bashio
set -euo pipefail
CONFIG_PATH=/data/options.json
SUPERVISOR_API=http://supervisor

log() { printf '[GoodWe Options Repair] %s\n' "$*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

get_opt() {
  local key="$1" default="${2-}"
  jq -r --arg k "$key" --arg d "$default" '(.[$k] // $d) | if type == "boolean" then (if . then "true" else "false" end) else tostring end' "$CONFIG_PATH" 2>/dev/null || printf '%s' "$default"
}

supervisor_curl() {
  local method="$1" path="$2" data="${3-}"
  [ -n "${SUPERVISOR_TOKEN:-}" ] || fail "SUPERVISOR_TOKEN ontbreekt; hassio_api/hassio_role staat niet goed."
  if [ -n "$data" ]; then
    curl -fsS -X "$method" -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" -H 'Content-Type: application/json' -d "$data" "${SUPERVISOR_API}${path}"
  else
    curl -fsS -X "$method" -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" -H 'Content-Type: application/json' "${SUPERVISOR_API}${path}"
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
    | select(.slug == "goodwe_agent" or (.slug | endswith("_goodwe_agent")) or ((.name // "") | test("GoodWe Agent"; "i")))
    | .slug
  ' | head -n 1
}

slug="$(find_goodwe_slug)"
[ -n "$slug" ] && [ "$slug" != "null" ] || fail "GoodWe Agent add-on slug niet gevonden. Vul target_slug handmatig in, bijvoorbeeld 6a63497e_goodwe_agent."
log "Doel-add-on: ${slug}"

info="$(supervisor_curl GET "/addons/${slug}/info")"
schema_keys="$(printf '%s' "$info" | jq -c '((.data.schema // .schema // {}) | if type == "object" then keys else [] end)' 2>/dev/null || echo '[]')"
raw_options="$(printf '%s' "$info" | jq -c '(.data.options // .options // {})' 2>/dev/null || echo '{}')"
new_defaults="$(cat /app/default_goodwe_options.json)"
current_version="$(printf '%s' "$info" | jq -r '(.data.version // .version // "0.0.0")' 2>/dev/null || echo 0.0.0)"
preserve_only="$(get_opt preserve_only_api_key_and_ha_token true)"

# Eerste repair is schema-compatibel met de momenteel geïnstalleerde versie.
# Dit is nodig omdat Supervisor de huidige options valideert voordat hij naar
# de nieuwe versie mag updaten. Objecten/arrays worden dus verwijderd.
clean_current="$(jq -c -n \
  --argjson raw "$raw_options" \
  --argjson defaults "$new_defaults" \
  --argjson schema_keys "$schema_keys" \
  --arg preserve_only "$preserve_only" '
    def scalar: (type != "object" and type != "array");
    def as_string: if scalar then tostring else "" end;
    ($raw.options // {}) as $nested
    | ((if ($nested|type)=="object" then $nested else {} end) + (if ($raw|type)=="object" then $raw else {} end)) as $merged
    | if ($preserve_only | ascii_downcase) == "true" then
        # Legacy/foute GoodWe options resetten: alleen api_key en ha_token blijven behouden.
        $defaults
        | .api_key = (($merged.api_key // "") | as_string)
        | .ha_token = (($merged.ha_token // "") | as_string)
      else
        ($defaults + $merged)
        | del(.options)
        | with_entries(select(.value | scalar))
      end
    | del(.options)
    | if ($schema_keys | length) > 0 then with_entries(select(.key as $k | $schema_keys | index($k))) else . end
  ')"

payload="$(jq -c -n --argjson options "$clean_current" '{options:$options, boot:"auto", auto_update:true}')"
log "Schema-compatibele GoodWe-options opslaan vóór update; standaard worden alleen api_key en ha_token behouden."
if ! supervisor_curl POST "/addons/${slug}/options" "$payload" >/dev/null; then
  log "Normaal opslaan faalde; reset options=null en probeer opnieuw."
  supervisor_curl POST "/addons/${slug}/options" '{"options":null}' >/dev/null || true
  supervisor_curl POST "/addons/${slug}/options" "$payload" >/dev/null
fi

if [ "$(get_opt run_update true)" = "true" ]; then
  log "Add-on Store reload."
  supervisor_curl POST /store/reload '{}' >/dev/null || true
  supervisor_curl POST /addons/reload '{}' >/dev/null || true
  log "GoodWe Agent update proberen."
  supervisor_curl POST "/store/addons/${slug}/update" '{"backup":false,"background":false}' >/dev/null \
    || supervisor_curl POST "/addons/${slug}/update" '{}' >/dev/null \
    || log "Update endpoint faalde nog; probeer nu handmatig de GoodWe Agent update. De options zijn wel opgeschoond."
fi

# Na een geslaagde update kan de nieuwe schema extra velden accepteren. Als het
# lukt, vul dan direct de 1.8.x defaults aan zonder bestaande klantwaarden te wissen.
info2="$(supervisor_curl GET "/addons/${slug}/info" 2>/dev/null || echo '{}')"
schema2="$(printf '%s' "$info2" | jq -c '((.data.schema // .schema // {}) | if type == "object" then keys else [] end)' 2>/dev/null || echo '[]')"
current2="$(printf '%s' "$info2" | jq -c '(.data.options // .options // {})' 2>/dev/null || echo '{}')"
clean2="$(jq -c -n --argjson raw "$current2" --argjson defaults "$new_defaults" --argjson schema_keys "$schema2" --arg preserve_only "$preserve_only" '
  def scalar: (type != "object" and type != "array");
  def as_string: if scalar then tostring else "" end;
  ($raw.options // {}) as $nested
  | ((if ($nested|type)=="object" then $nested else {} end) + (if ($raw|type)=="object" then $raw else {} end)) as $merged
  | if ($preserve_only | ascii_downcase) == "true" then
      $defaults
      | .api_key = (($merged.api_key // "") | as_string)
      | .ha_token = (($merged.ha_token // "") | as_string)
    else
      ($defaults + $merged)
      | del(.options)
      | with_entries(select(.value | scalar))
    end
  | del(.options)
  | if ($schema_keys | length) > 0 then with_entries(select(.key as $k | $schema_keys | index($k))) else . end
')"
if [ "$clean2" != "{}" ]; then
  log "Nieuwe/default GoodWe 1.8.x options aanvullen waar de schema dit toestaat."
  supervisor_curl POST "/addons/${slug}/options" "$(jq -c -n --argjson options "$clean2" '{options:$options, boot:"auto", auto_update:true}')" >/dev/null || true
fi

if [ "$(get_opt restart_after_update false)" = "true" ]; then
  log "GoodWe Agent herstarten."
  supervisor_curl POST "/addons/${slug}/restart" '{}' >/dev/null || supervisor_curl POST "/addons/${slug}/start" '{}' >/dev/null || true
fi

log "Klaar. De GoodWe Agent options zijn naar defaults gereset met behoud van api_key en ha_token; de update is geprobeerd."
