# DWARS Generic EMS Add-on

Gebruik deze add-on voor een omvormer die niet GoodWe of SolarEdge is, maar wel via Home Assistant entities bestuurd kan worden.

## Belangrijkste configuratie

- `ha_mode_select`: de Home Assistant `select.*` entity die de modus van de omvormer/batterij bestuurt.
- `ha_mode_idle_option`: exacte optie voor idle/self-use/auto. Gebruik hier de modus waarbij PV het huis ondersteunt en de batterij volgens de normale inverterlogica werkt.
- `ha_mode_charge_option`: exacte optie voor laden.
- `ha_mode_discharge_option`: exacte optie voor ontladen.
- `ha_power_number`: optionele algemene `number.*` entity voor laad-/ontlaadvermogen.
- `ha_charge_power_number`, `ha_discharge_power_number`, `ha_idle_power_number`: optionele aparte power entities per mode.
- `ha_charge_power_value`, `ha_discharge_power_value`, `ha_idle_power_value`: `server_power`, `max`, `min`, `skip` of een vast getal.

## Standaard server-mode mapping

```text
1,7 -> idle
3   -> charge
4   -> discharge
```

Je kunt dit aanpassen met:

```text
ha_server_modes_idle
ha_server_modes_charge
ha_server_modes_discharge
```

Of volledig overschrijven met `ha_mode_map_json`, bijvoorbeeld:

```json
{"1":"Self Use","3":"Charge from grid","4":"Discharge","7":"Self Use"}
```

De opties moeten exact overeenkomen met de opties die Home Assistant in de betreffende select-entity aanbiedt. De add-on probeert hoofdletter-/spatieverschillen automatisch te normaliseren.
