# DWARS OneShot-release 0.6.3

**Herstel voor GoodWe-configuratie en hervatten na handmatige installatie.** Flow-/migratieversies zijn consistent; automatisch toevoegen vereist echte meetdata; een werkende handmatige omvormer en agent worden hergebruikt. Sensorproblemen leiden niet meer tot opnieuw installeren van een aanwezige omvormer.

Begin bij [ONESHOT_HERSTEL_0.6.3.md](ONESHOT_HERSTEL_0.6.3.md) voor herstel op een reeds werkende Raspberry. Verwijder geen integratie, agent of opgeslagen key. Alleen het GitHub-pakket verandert; de eerdere EMS/BMS-OneShot-update blijft vereist.

Versies: installer **0.6.3**, GoodWe-integratie **0.9.9.36**, DWARS Setup **1.1.0**. Zie het [testrapport](ONESHOT_HERSTEL_0.6.3_TESTRAPPORT.md) voor 213 lokale tests en de grenzen: geen fysieke HA/Supervisor-/omvormertest. Eerst één Raspberry valideren.

Voor een nieuwe installatie: open de OneShot-webinterface en voer de API-key in. Voor serververeisten en platformbeperkingen: [ONESHOT_INSTALLATIE.md](ONESHOT_INSTALLATIE.md).

## Bestaande projectdocumentatie

# DWARS EMS Home Assistant

Home Assistant add-on repository voor DWARS EMS componenten, GoodWe en SolarEdge.

## Inhoud

- `dwars_installer` — installeert en update de custom components en agent add-ons.
- `goodwe_agent` — GoodWe EMS bridge die GoodWe Home Assistant entities aanstuurt.
- `solaredge_agent` — SolarEdge BMS/EMS bridge die SolarEdge Modbus Multi entities aanstuurt.
- `dwars_addon` — generieke DWARS EMS bridge voor andere omvormers via zelf gekozen Home Assistant entities.
- `custom_components/goodwe` — aangepaste GoodWe custom integration met discovery, MAC-opslag en IP-herstel.
- `custom_components/solaredge_modbus_multi` — SolarEdge Modbus Multi met DWARS DHCP/MAC/IP-herstel.

## Historische handmatige installatie (niet de OneShot-route)

1. Voeg deze repository toe in Home Assistant:

   `Settings → Add-ons → Add-on Store → Repositories`

2. Installeer `DWARS Installer & Updater`.
3. Kies bij `inverter_type`:
   - `goodwe`
   - `solaredge`
   - `both`
   - `andere_omvormer`
4. Vul de API/client velden in.
5. Start de installer.
6. Home Assistant Core wordt automatisch herstart als er custom components zijn geïnstalleerd of bijgewerkt.
7. Voeg daarna de gewenste integratie toe via `Settings → Devices & services → Add integration`.

## Automatische updates

De installer zet de inverter agents op `boot: auto` en `auto_update: true`. De installer zelf kan zichzelf ook op `auto_update: true` zetten.

Voor custom components zijn er twee routes:

- Normaal: verhoog bij repo-wijzigingen de versie van `dwars_installer/config.json`. Zodra de add-on door Home Assistant is geüpdatet, kopieert de installer de embedded payload opnieuw naar `/config/custom_components` en herstart Home Assistant Core als er daadwerkelijk verschil is.
- Direct vanaf GitHub: zet `auto_update_from_github` op `true`. De installer downloadt dan periodiek `github_repo_zip_url`, vergelijkt checksums en herstart Home Assistant Core alleen als een geselecteerde custom component echt gewijzigd is.

## Andere omvormer

Kies in de installer `inverter_type=andere_omvormer` als je geen GoodWe of SolarEdge wilt installeren. Dan installeert hij alleen `DWARS Generic EMS Add-on`. In die add-on stel je zelf de Home Assistant entities en mode mapping in:

- `ha_mode_select`: de select-entity die de inverter-modus bestuurt.
- `ha_mode_idle_option`: exacte HA select-optie voor idle/self-use/auto.
- `ha_mode_charge_option`: exacte HA select-optie voor laden.
- `ha_mode_discharge_option`: exacte HA select-optie voor ontladen.
- `ha_power_number`: optionele number-entity voor laad-/ontlaadvermogen.

Idle is bedoeld als de modus waarin PV het huis ondersteunt en de inverter/batterij volgens de normale self-use-logica werkt.

## GoodWe mapping

De GoodWe agent gebruikt bewust deze DWARS mapping:

```python
EMS_MODE_OPTIONS: dict[int, str] = {
    1: os.environ.get("HA_EMS_MODE_0_OPTION", "auto"),
    3: os.environ.get("HA_EMS_MODE_3_OPTION", "import_ac"),
    4: os.environ.get("HA_EMS_MODE_4_OPTION", "export_ac"),
    7: os.environ.get("HA_EMS_MODE_0_OPTION", "auto"),
}
```

## SolarEdge IP/MAC herstel

SolarEdge Modbus Multi slaat het MAC-adres op wanneer dit via DHCP of ARP beschikbaar is. Bij een IP-wissel probeert de integration het nieuwe IP te vinden via DHCP discovery en een lokale ARP/Modbus scan.

## Licenties

De oorspronkelijke licentieteksten van de vendored integrations staan in `licenses/`.
