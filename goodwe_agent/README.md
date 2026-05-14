# GoodWe Agent v1.5.2 — Home Assistant control

Deze versie gebruikt geen Modbus meer voor de GoodWe besturing. De agent stuurt via Home Assistant services:

- EMS mode: `select.goodwe_ems_mode`
- EMS/eco power: `number.goodwe_eco_mode_power`
- Grid export limit: `number.goodwe_net_exportlimiet`
- Grid export limit switch: `switch.goodwe_grid_export_limit_switch`

## Mapping

De GoodWe select gebruikt interne option values, niet altijd de labels uit de UI. Daarom gebruikt deze versie standaard snake_case waarden:

| Server mode | Betekenis | GoodWe HA option |
|---:|---|---|
| 1 | Standby | `battery_standby` |
| 3 | AC laden | `import_ac` |
| 4 | AC export / ontladen | `export_ac` |
| 7 | Idle / MSC / PV volgen | `charge_pv` |

Hiermee is `mode=3` dus AC-laden en `mode=7` Charge PV.

## Multi-inverter support

De volgende opties accepteren één entity of meerdere entities. Scheiden mag met komma, puntkomma, spatie of newline:

- `ha_ems_mode_select`
- `ha_ems_power_number`
- `ha_grid_export_limit_number`
- `ha_grid_export_limit_switch`

Voorbeeld met twee omvormers:

```json
{
  "ha_ems_mode_select": "select.goodwe_ems_mode, select.goodwe_2_ems_mode",
  "ha_ems_power_number": "number.goodwe_eco_mode_power, number.goodwe_2_eco_mode_power",
  "ha_grid_export_limit_number": "number.goodwe_net_exportlimiet, number.goodwe_2_net_exportlimiet",
  "ha_grid_export_limit_switch": "switch.goodwe_grid_export_limit_switch, switch.goodwe_2_grid_export_limit_switch"
}
```

Dezelfde actie wordt dan op alle opgegeven entities toegepast. Bij `max` of `min` wordt de limiet per number entity afzonderlijk uit Home Assistant gelezen, zodat verschillende inverter-limieten correct blijven werken.

## Belangrijke opties

Vul minimaal in de add-on opties:

- `api_key`: BMS/MetDeZon API key
- `ha_token`: Home Assistant long-lived access token

Controleer deze entities:

- `ha_ems_mode_select`: standaard `select.goodwe_ems_mode`
- `ha_ems_power_number`: standaard `number.goodwe_eco_mode_power`
- `ha_grid_export_limit_number`: standaard `number.goodwe_net_exportlimiet`
- `ha_grid_export_limit_switch`: standaard `switch.goodwe_grid_export_limit_switch`

## EMS power

Standaard staat:

```text
ha_ems_power_value = max
```

Dat is bewust zo. Bij jouw GoodWe entity lijkt `number.goodwe_eco_mode_power` een 0..100 slider te zijn, geen wattage. De agent zet die daarom op de maximale waarde bij server modes `3` en `4`.

Wil je een echte watt-entity gebruiken, zet dan:

```text
ha_ems_power_value = server_power
```

## Grid export curtailment

Bij curtailment:

- elke opgegeven `ha_grid_export_limit_number` wordt op `0` gezet
- elke opgegeven `ha_grid_export_limit_switch` wordt `on`

Bij restore:

- elke opgegeven `ha_grid_export_limit_number` wordt op `max` gezet
- elke opgegeven `ha_grid_export_limit_switch` wordt `off`

De zip bevat geen secrets.

## DWARS repo-installatie

Plaats deze map als `goodwe_agent/` in de root van je Home Assistant add-on repository.
De map moet dus naast `repository.yaml` staan, niet onder `custom_components/`.

Vanaf versie `1.5.22` is `ha_token` optioneel. Als `ha_token` leeg blijft gebruikt de add-on automatisch de Home Assistant Supervisor Core API proxy via `SUPERVISOR_TOKEN`.

Fix in `1.5.22`: server mode `7` gebruikt nu correct `HA_EMS_MODE_7_OPTION` / `ha_ems_mode_7_option` met standaardwaarde `charge_pv`.
