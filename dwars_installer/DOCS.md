# DWARS OneShot 0.6.1

Nieuwe installatie: start de app, open de webinterface en voer de DWARS API-key in. BMS en EMS moeten eerst zijn bijgewerkt en de migratie moet zijn uitgevoerd. Laat `installation_mode: auto` staan op een nieuwe Raspberry. Geen langlevend HA-token nodig. Voortgang en key blijven bij een herstart bewaard.

Een bestaande geconfigureerde installatie blijft in auto-modus de handmatige updater gebruiken. Alleen oude updaterbestanden zijn niet langer voldoende om OneShot te blokkeren. Bij `mode=manual`: zet voor automatische onboarding `installation_mode` op `oneshot`, sla op en herstart deze app; open daarna de webinterface. Verwijder de app of zijn `/data` niet. Zie `ONESHOT_HERSTEL_0.6.1.md` voor deze correctie. De volledige uitrolhandleiding en grenzen staan in `ONESHOT_INSTALLATIE.md` in de repositoryroot. Deze versie eerst op een test-Raspberry controleren.

## Bestaande handmatige installer- en updaterdocumentatie

De volgende opties blijven van toepassing op de handmatige route en het onderhoud na OneShot. De eerste-installatiebediening hierboven vervangt de oude handmatige agentconfiguratiestappen voor nieuwe OneShot-installaties.

# DWARS Installer & Fleet Updater 0.5.1

Deze add-on houdt de DWARS-repository en de Home Assistant-installatie automatisch actueel.
Na installatie van versie 0.5.1 is geen aparte blueprint, automation of helper nodig.

## Standaardgedrag

- DWARS-componenten worden iedere 15 minuten vanuit de ingestelde GitHub-repository gecontroleerd.
- De volledige systeemupdate start dagelijks om **04:00 lokale Home Assistant-tijd**.
- Per Raspberry wordt een vaste spreiding van 0–15 minuten toegepast, zodat alle systemen niet exact tegelijk GitHub en de Home Assistant-infrastructuur benaderen.
- Krijgt een Raspberry de nieuwe Installer pas na 04:00 maar vóór 10:00, dan wordt de gemiste eerste run binnen ongeveer één minuut ingehaald.
- Vlak vóór de run worden de add-onstore, de officiële Supervisor `reload_updates`-catalogus en alle
  `update.*`-entities ververst, zodat ook kort daarvoor verschenen updates
  worden meegenomen.
- Alleen wanneer werkelijk updates beschikbaar zijn, wordt één volledige
  Home Assistant-backup gemaakt.
- Alleen automatisch gemaakte backups met naam `DWARS auto-update ...` vallen
  onder retentie; standaard worden de nieuwste drie bewaard. Handmatige en
  beschermde backups worden nooit verwijderd.
- De originele/upstream GoodWe software/HACS-update wordt overgeslagen. De DWARS/DCENT/MetDeZon/cryptowhizzard-versie blijft leidend en wordt periodiek opnieuw vanuit deze repository geplaatst. Een fysieke GoodWe-firmware-update blijft wel updatebaar wanneer device-updates zijn ingeschakeld.

## Updatevolgorde

1. Eén volledige backup.
2. Alle geïnstalleerde add-ons, waaronder Tailscale wanneer een update beschikbaar is.
3. HACS, custom integrations en overige `update.*`-entities.
4. Eén Home Assistant Core-herstart om bijgewerkte custom integrations te laden.
5. Retryfase voor mislukte add-on- en entity-updates.
6. Supervisor-systeemplugins: CLI, DNS, Audio, Multicast en Observer.
7. Home Assistant Supervisor.
8. Home Assistant OS, gevolgd door de verplichte hostreboot.
9. Home Assistant Core als laatste.
10. Eindcontrole en één samenvattende persistent notification.

De voortgang staat in `/data/dwars_auto_update_state.json`. Daardoor hervat de add-on automatisch na een Core-restart, Supervisor-restart, update van de DWARS Installer zelf of een hostreboot.

## Belangrijkste opties

| Optie | Standaard | Betekenis |
|---|---:|---|
| `fleet_managed_updates` | `true` | Dwingt periodieke synchronisatie van DWARS-componenten vanaf GitHub af. |
| `auto_full_system_update` | `true` | Schakelt de dagelijkse volledige updater in. |
| `auto_full_system_update_time` | `04:00` | Starttijd in de ingestelde/lokale tijdzone. |
| `auto_full_system_update_timezone` | `auto` | Leest de tijdzone uit HA/Supervisor; fallback `Europe/Amsterdam`. |
| `auto_full_system_update_days` | alle dagen | Komma-gescheiden Engelse dagcodes. |
| `auto_full_system_update_jitter_minutes` | `15` | Vaste spreiding per Raspberry. |
| `auto_full_system_update_first_start_catchup_until` | `10:00` | Eerste gemiste ochtendrun alsnog uitvoeren tot dit tijdstip. |
| `auto_full_system_update_backup` | `true` | Maakt één volledige pre-run backup. |
| `auto_full_system_update_backup_keep` | `3` | Bewaart maximaal dit aantal onbeschermde `DWARS auto-update`-backups. |
| `auto_full_system_update_abort_on_backup_failure` | `false` | Bij `true` stopt de run als de backup mislukt. |
| `auto_full_system_update_refresh_catalogs` | `true` | Ververst Store, Supervisor en `update.*`-entities vóór inventarisatie. |
| `auto_full_system_update_max_retries` | `2` | Aantal retries voor add-ons en update-entities. |
| `auto_full_system_update_reboot` | `true` | Staat de voor HAOS vereiste reboot toe. |
| `auto_full_system_update_reboot_timeout_sec` | `1800` | Voorkomt dat de state-machine permanent blijft wachten op een mislukte reboot. |
| `skip_upstream_goodwe_updates` | `true` | Slaat de originele GoodWe-repository over. |
| `auto_full_system_update_include_device_updates` | `true` | Neemt ook firmware/device update-entities mee. |
| `auto_full_system_update_exclude_entities` | leeg | Optionele komma-gescheiden entity-ID's/globs om uit te sluiten. |

`auto_full_system_update_interval_sec` blijft om compatibiliteitsredenen in de add-onconfiguratie staan, maar de dagelijkse scheduler gebruikt voortaan `auto_full_system_update_time`.

## Diagnose

De add-on publiceert, wanneer Home Assistant bereikbaar is:

```text
sensor.dwars_auto_update_status
```

De volledige voortgang staat in de add-onlog. Na afloop verschijnt één persistente Home Assistant-notificatie met het aantal geslaagde, mislukte en nog openstaande updates.

Handmatig de persisted state bekijken vanuit de container:

```bash
python3 /app/auto_updater.py --show-state
```

Een handmatige run vanuit de container:

```bash
python3 /app/auto_updater.py --run-now
```

## Afbakening

Op Home Assistant OS worden Core, Supervisor, OS, systeemplugins, add-ons en beschikbare update-entities bijgewerkt. Willekeurige Debian/Raspberry Pi OS `apt`-pakketten bestaan op HAOS niet als zelfstandig beheerde hostpakketten. Op Home Assistant Supervised wordt de OS-stap overgeslagen wanneer `/os/info` niet beschikbaar is.

## Foutafhandeling 0.5.1

- Een expliciete HTTP 4xx bij een add-on-, update-entity-, Supervisor-, OS- of Core-update wordt direct als afwijzing behandeld; de updater wacht dan niet onnodig op een update die nooit gestart is.
- Een afgewezen HAOS-update veroorzaakt nadrukkelijk geen blinde hostreboot. Alleen een geaccepteerde update of een aannemelijke transport/gateway-onderbreking gaat door naar post-rebootverificatie.
- Een tijdelijke GitHub- of Store-fout tijdens de initiële DWARS-componentsynchronisatie verhindert niet dat de dagelijkse systeemupdater start.
