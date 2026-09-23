# Testrapport — DWARS OneShot 0.6.3

Datum: 9 september 2026. Basis: het in deze conversatie aangeleverde volledige `dwars-github-oneshot-0.6.2.zip`.

## Resultaat

De oorspronkelijke suite op 0.6.2: **154 tests geslaagd**. Dit bewees de gemelde praktijkgevallen niet; de oude mocks controleerden onvolledige aspecten van GoodWe-configuratie en hervatten.

De suite in deze release: **213 tests geslaagd**, waaronder **59 nieuwe tests**. Daarnaast rapporteert pytest 22 geslaagde unittest-subtests (geen 22 extra top-level tests). Er zijn 41 niet-fatale `aiohttp`-deprecationwaarschuwingen over de bestaande WebSocket-timeoutparameter. Deze waarschuwingen zijn niet als functionele validatieproblemen weggedrukt; ze staan in de meegeleverde uitvoer.

Opdracht vanuit repositoryroot:

```sh
python -m pytest dwars_installer/tests -q
```

Omgeving: Python 3.13.5, lokale aiohttp HTTP-/WebSocket-servers, shell/curl-tests, pytest. De productiefuncties voor gegevensopbouw, import, migratie en verbinding worden uit de broncode uitgevoerd met testobjecten voor HA-registratie en invertercommunicatie. De UI- en worker-tests gebruiken echte lokale HTTP-/WebSocket-uitwisseling, maar de antwoorden komen van testservers, niet van Supervisor of BMS-productie.

## Oude en nieuwe code daadwerkelijk vergeleken

De uitkomst staat ook machineleesbaar in `dwars_installer/tests/063_before_after.json`.

| Gecontroleerd geval | 0.6.2 | 0.6.3 |
|---|---|---|
| Flowmajor versus bestaande entrymajor 2 | Flow 1; HA-versievoorwaarde wijst entry af | Flow 2; versievoorwaarde klopt |
| Alleen identificatie bereikbaar, runtime-uitlezing leeg | Entry aangemaakt, nul runtime-reads | Geen entry, fout `cannot_read_runtime` |
| Reeds geladen handmatige UDP-configuratie komt in TCP-scan terug | Config gewijzigd, reload ingepland | Geen wijziging, geen reload |
| Persistente timeout 4 / unit-ID 247 in data | Setup gebruikt timeout 1 / unit-ID 0 | Setup gebruikt timeout 4 / unit-ID 247 |
| Bestaande omvormer, SoC `unavailable` | Fase terug naar `discover` | Fase blijft `mapping` |

Dit is geen bewijs dat exact dezelfde poort-/timeoutwaarden op de Raspberry van de gebruiker stonden. De testwaarden zijn doelbewust gekozen om het codegedrag te onderscheiden. Het HA-versiegedrag is gecontroleerd tegen de primaire Core-broncode; de volledige HA-configentrymanager is hier niet uitgevoerd.

## Nieuwe dekking

`test_063_goodwe_configuration.py` — 23 tests. Flow/migratieversies; volledige opgeslagen verbindingsdata; gedeelde handmatige/automatische gegevensopbouw; runtime-uitlezing vereist; fallback naar alternatief transport; serienummeridentiteit; behouden van geladen handmatige en geïmporteerde entries; niet aanraken van gebruikersmatig uitgeschakelde/niet-geladen entries; alleen eigen mislukte imports begrensd herstellen; deduplicatie; offline migratie; null-defaults; versiebeveiliging; data/options-prioriteit.

`test_063_resume.py` — 27 tests. Actuele geladen configuratie hergebruiken; alleen ontbrekende of expliciet gevraagde ontdekking; letterlijk gemodelleerde GoodWe-unique-ID's; unavailable/missing-control/setup-error blijft mapping; herstart en handmatige re-add; serienummer vastzetten; onderscheid tussen door gebruiker en door integratie uitgeschakelde entiteiten; bestaande agentinstellingen en mappings behouden; vreemde klant/entiteit weigeren; verify terug naar mapping bij gewijzigde HA-ID's; oude bridge/payload weigeren; eenmalige softwarecacheverversing.

`test_063_http_recovery.py` — 9 tests. Volledige worker na handmatig toevoegen van integratie én agent; hervatten voltooit zonder ontdekking, apparaatinstallatie of Core-herstart in die testopstelling; alleen ontbrekende installatie-ID wordt aan de reeds werkende agent toegevoegd. Aanvullend diagnose-export, geheimenredactie, Ingress-beperking, CSRF, scheiding opnieuw controleren/ontdekken, gelijktijdigheidsbeperking en ingress-prefix in UI.

Een oudere wrong-serial-test is aangescherpt: het fysieke geselecteerde apparaat blijft aanwezig en de **BMS-ontvangst** bevat een verkeerd serienummer. Daardoor test hij weer het bedoelde telemetrieconflict, niet eerst het ontbreken van een HA-apparaat. Eerdere bronmocks zijn bijgewerkt met expliciete laadstatus, bridgeversie en runtime-uitlezing zodat ze het nieuwe contract niet omzeilen.

## Syntax, pakket en scope

Alle Python-bronnen worden gecompileerd, shellbestanden worden met `bash -n` gecontroleerd en JSON-bestanden worden geparsed. De aantallen en ZIP-/bronintegriteitscontrole staan in `ONESHOT_HERSTEL_0.6.3_CONTROLES.json`. Er zijn geen wijzigingen aan EMS/BMS, geen nieuwe servermigratie en geen wijziging aan de GoodWe-agentbroncode zelf. De nieuwe installer bewaart bestaande agentinstellingen in plaats van ze opnieuw met profieldefaults te vullen.

## Niet uitgevoerd / beperkingen

- Geen fysieke Raspberry, GoodWe-/SolarEdge-omvormer of echte Modbus-uitlezing.
- Geen live HA/Supervisor-configentrymanager en geen echte Core- of hostherstart.
- Geen Docker-/ARM-build, geen netwerkdownload vanuit de doel-Raspberry.
- Geen live BMS-telemetrie of databaserun nodig/uitgevoerd voor deze GitHub-correctie.
- De HA- en goodwe-afhankelijkheden konden niet in de testomgeving worden geïnstalleerd wegens ontbrekende externe netwerktoegang. De testgrenzen worden daarom uitdrukkelijk gesimuleerd.

De praktijkacceptatie moet op één Raspberry gebeuren voordat dezelfde branch door de fleet wordt gevolgd: nieuwe toevoeging met runtime-sensoren; app stoppen/starten tijdens setup; handmatige re-add; behoud huidige agentinstellingen; nieuwe BMS-ontvangst; geen terugval bij tijdelijk ontbrekende SoC.

## Primaire referenties voor het configuratiecontract

- Home Assistant Core 2026.9.0, `ConfigEntry.async_migrate`, major-versiecontrole vóór integratiemigratie: https://github.com/home-assistant/core/blob/2026.9.0/homeassistant/config_entries.py#L1072-L1126
- Home Assistant configflow-versies: https://developers.home-assistant.io/docs/core/integration/config_flow/
- Configentry-lifecycle en laadstatus: https://developers.home-assistant.io/docs/config_entries_index/

De primaire bronnen ondersteunen het API-/versiecontract, niet een bewering dat deze release op fysieke hardware is gevalideerd.
