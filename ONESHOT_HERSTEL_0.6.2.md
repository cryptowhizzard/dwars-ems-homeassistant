# DWARS OneShot 0.6.2 — herstel van de vastgelopen onboarding

Datum: 8 september 2026. Alleen de GitHub/Raspberry-code wijzigt. Gebruik de eerder geleverde EMS/BMS OneShot-serverpakketten en hun migratie. Dit pakket bevat geen nieuwe server- of databasewijzigingen.

## Aangetoonde fouten in 0.6.1

Een API-key in `goodwe_agent_api_key`, `solaredge_agent_api_key` of `dwars_addon_api_key` selecteerde in auto-modus direct de oude handmatige route. De installer deed dan geen automatische configflow/omvormerontdekking en `inverter_type: both` kon beide agents plaatsen. Met `start_agent_addons: false` bleven die gestopt.

De handmatige SolarEdge-configuratie stuurde bovendien een gedeeltelijke optieset. De verplichte `backup_yaml_check_enabled`, `backup_yaml_path` en `backup_yaml_overwrite` ontbraken in die aanvraag. Supervisor valideert de hele aangeboden optieset, vóór opslag. De HTTP-400-respons zelf bevatte in het aangeleverde log geen verdere details, omdat curl de responsebody wegliet. De gedeeltelijke aanvraag is een aantoonbare fout; de precieze servermelding van die productieaanvraag is niet beschikbaar.

Bash `set -e` voorkwam het doorgaan niet: de hele cyclus werd in een `if`/`||`-context uitgevoerd, waarin de impliciete foutafbreking ook binnen aangeroepen functies niet geldt. De configuratiefout werd overschreven door de daaropvolgende logregel. Het log noemde daardoor configuratie terwijl de aanvraag was afgewezen.

## Gedrag in 0.6.2

In auto-modus is een ingevulde key invoer voor de installatie, geen bewijs van een afgeronde installatie. Bij de geteste beginsituatie — bestaande key, gestopte GoodWe-agent, ongeconfigureerde gestopte SolarEdge-agent en geen HA-omvormerconfiguratie — wordt de key uit de installeropties naar de persistente OneShot-opslag overgenomen en begint OneShot automatisch. Het merk komt uit het BMS-installatieprofiel, niet uit `inverter_type: both` of de naam van het oude API-key-veld.

Verschillende keys in de oude velden worden niet willekeurig gekozen. Reeds opgeslagen OneShot-credentials worden niet vervangen. Een actieve DWARS-agent of een geconfigureerde oude omvormer blijft beschermd tegen automatische herconfiguratie. Ook een bewust gestopte, al ingerichte installatie blijft beschermd. Deze bescherming is conservatief: een actieve container bewijst niet dat de omvormer werkt. De webinterface kan OneShot dan expliciet starten, met de bestaande key, zonder YAML-wijziging of tweede key-invoer. Bij een actieve onderhoudslock wordt geen lopende update onderbroken.

De knop **Start met opgeslagen API-key** is beschikbaar wanneer één eenduidige key al in de installeropties staat. Een expliciete `installation_mode: manual` blijft gerespecteerd totdat je via deze webinterface OneShot start. Deze UI-actie slaat de modus op via Supervisor, niet door handmatig in zijn `options.json` te schrijven.

Voor het plaatsen van integraties worden bestaande agentkeys en installatie-ID's gecontroleerd. Een andere klant of concurrerende actieve agent blokkeert de procedure vóór de integratiebestanden worden gewijzigd. Een ongebruikte, gestopte agent van een ander platform uit dezelfde repository, zonder key of installatie-ID, krijgt `boot: manual` en watchdog uit. Hij wordt niet verwijderd. Zijn oorspronkelijke instellingen worden in `/data/oneshot_unused_<slug>.json` bewaard. Zo start de achtergebleven ongeconfigureerde SolarEdge-agent niet alsnog bij een latere Raspberry-reboot.

De normale OneShot-route voegt de ondersteunde gevonden omvormers toe, koppelt de sensoren en start alleen de bij het BMS-profiel horende agent. Ze gebruikt niet de oude optie `start_agent_addons`. Werkelijk bestaande beschermde legacy-installaties behouden onderhoud zonder dat er door de `both`-default nog een tweede agent wordt geïnstalleerd of hun opties worden overschreven. Hun bestaande apps blijven onder de normale systeemupdater vallen.

In de handmatige route worden volledige SolarEdge-/generieke opties aangeboden, inclusief bestaande verplichte defaults. Een mislukte configuratie of start wordt expliciet doorgegeven. Er wordt geen succes of start gemeld na een afgewezen configuratie. HTTP-status en foutomschrijving zijn zichtbaar, met afscherming van bekende API-keys en HA-/Supervisor-tokens. Een JSON-fout onder HTTP 200 is eveneens een fout.

## Installeren op de probleem-Raspberry

1. Bewaar een HA-backup en de huidige repositoryversie. Plaats de inhoud van de map `dwars-ems-homeassistant/` in de repositoryroot. Voeg geen extra buitenste map toe. Let op andere Raspberry's die dezelfde branch automatisch volgen; test eerst op één systeem.
2. Vernieuw de Home Assistant-appwinkel en **werk DWARS OneShot Installer & Updater daadwerkelijk bij naar 0.6.2**. Alleen appherstart of het ophalen van de remote custom components vervangt de Python-installercode in de app niet.
3. Start de installer en open zijn webinterface. Bij de beschreven startsituatie wordt de bestaande key automatisch hergebruikt. Is bestaand beheer beschermd of expliciet handmatig gekozen, gebruik **Start met opgeslagen API-key**. Een nieuwe key kun je op dezelfde pagina invoeren wanneer nog geen klant gebonden is.

Verwijder de app niet. Wis `/data`, `.storage`, de key of de voortgang niet. Zet niet zomaar beide agents handmatig aan. EMS en BMS hoeven voor 0.6.2 niet opnieuw gewijzigd te worden; hun eerdere OneShot-update en migratie moeten wel al aanwezig zijn.

Bij daadwerkelijk begonnen onboarding staat in de log `mode=oneshot`. Je ziet vervolgens de stappen `profile`, `payload`, `components`, `restart`, `bridge`, `discover`, `mapping`, `agent` en `verify`. De agent start pas nadat de benodigde koppeling is gecontroleerd. `complete` blijft afhankelijk van nieuwe telemetriebevestiging vanuit BMS. Een ongeldige key, ontbrekend serverendpoint of onbereikbare omvormer leidt niet tot een geslaagde installatie.

## Tests en begrenzing van de resultaten

Uitgevoerd in deze release: **154 lokale installer-Python-tests** (126 bestaande, met gewijzigde verwachtingen voor de gerepareerde moduskeuze, en 28 nieuwe regressietests). Commando:

```bash
python3 -m unittest discover -s dwars_installer/tests -q
```

De nieuwe tests gebruiken echte Bash/curl- en aiohttp/HTTP/WebSocket-aanroepen tegen lokale testservers. Ze voeren de echte OneShot-worker uit vanaf de oude API-key-optie. Getest zijn onder andere beide gestopte agents zonder omvormerconfiguratie, een verse GoodWe-installatie, SolarEdge geselecteerd vanuit BMS ondanks het oude GoodWe-key-veld, eigendomsconflicten, de UI-overgang zonder YAML, afwijzingen, volledige opties, afgeschermde foutmeldingen en onderbreking direct na het bevestigde Core-herstartverzoek. De tweede installerinstantie hervat met hetzelfde installatie-ID en dezelfde key, zonder tweede herstartverzoek.

De testservers bootsen de API-contracten na. De schemafixture controleert verplichte velden en JSON-typen uit de meegeleverde agentmanifesten, maar is niet de echte Supervisor-validator. Netwerkdetectie, HA-registers, Core-herstart en BMS-telemetrie zijn gesimuleerd. De GitHub-payload wordt in de workertest als lokale cache aangeboden en het externe Core-herstelproces is vervangen door een testdubbel. Bestaande unit-tests voeren de echte import-/probe-methoden met testdubbels uit; ze zijn geen Modbus-hardwaretest.

De ongewijzigde 0.6.1-shellcode is afzonderlijk opnieuw uitgevoerd: een afgewezen gedeeltelijke SolarEdge-configuratie gaf HTTP 400 maar `CYCLE_SUCCESS`. De gecorrigeerde code slaat in dezelfde lokale testopstelling een volledige optieset op. Met een geforceerde HTTP 400 geeft zij `CYCLE_FAILED` en doet zij geen startaanvraag. Zie `ONESHOT_HERSTEL_0.6.2_REPRODUCTIE.json`.

Ook Python-syntax (inclusief Python 3.11-grammatica voor de installer-runtime), Bash-syntax, JavaScript-syntax, JSON en ZIP-integriteit zijn gecontroleerd. EMS/BMS/PHP en de database zijn in deze release niet opnieuw getest of gewijzigd.

**Niet uitgevoerd:** een fysieke Raspberry, een echte Home Assistant/Supervisor, een ARM-Dockerbuild, echte GoodWe/SolarEdge-communicatie, een productie-API-key, live EMS/BMS of een live database/migratie. Deze tests bewijzen niet dat jouw fysieke omvormer al correct werkt. Bevestig de volledige werking op één installatie vóór brede uitrol.

De bestaande beperking blijft gelden: alle gevonden ondersteunde apparaten kunnen aan HA worden toegevoegd, maar de agent bestuurt één gekozen batterij-omvormer. Meerdere batterij-omvormers vereisen vooraf het besturingsserienummer; gezamenlijke vermogensverdeling/SoC-aggregatie is niet toegevoegd.

## Technische referentie

De officiële Supervisor-implementatie valideert de volledige aangeboden `options` vóór opslag (`APIApps.options`); een losse optiesaanvraag is geen automatische patch/merge van alle huidige velden:

```text
https://raw.githubusercontent.com/home-assistant/supervisor/main/supervisor/api/apps.py
https://developers.home-assistant.io/docs/api/supervisor/endpoints/
```
