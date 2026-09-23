# Herstel OneShot 0.6.4 — `discover` / `not_loaded`

Release: 23 september 2026. Basis: het volledige geleverde GitHub-pakket 0.6.3.

## Wat deze release herstelt

Een automatisch gevonden GoodWe-omvormer kon zichtbaar zijn in Home Assistant, met het juiste IP-adres en serienummer, maar op `not_loaded` blijven zonder entiteiten. De installer bleef dan bij ontdekken staan. Handmatig toevoegen na stoppen van de installer en herstarten van Core gebruikte een andere route.

In 0.6.3 werd de hele scan gestart als een **importflow**. Die startte voor elke omvormer nog een importflow en wachtte daarop. Bij de eerste configuratie van de integratie wacht Home Assistant met het laden van de integratie totdat de lopende imports geïnitialiseerd zijn. De bovenliggende scan was nog niet klaar, omdat deze juist op dat laden wachtte:

```
Bovenliggende importscan
  wacht op losse omvormerimport
    wacht op eerste integratielading
      wacht op bovenliggende importscan
```

Die wederzijdse afhankelijkheid is gereproduceerd met de daadwerkelijke 0.6.3-productiemethoden en een model van de officiële HA-importbarrière. De toestand in die proef is `running`, `not_loaded`, nul entiteiten. Een gelijksoortig probleem bestond bij het synchronisch herladen van een eigen mislukte entry binnen een nog onafgeronde import. Ook SolarEdge gebruikte dezelfde structuur voor de bulkscan.

Het oorspronkelijke diagnoseformaat bevatte geen Core-versie, actieve flows of traceback. Daarom is dit een aangetoonde codefout die het waargenomen patroon verklaart, geen bewijs dat iedere mogelijke verbindings- of laadfout op een specifieke Raspberry hiermee uitgesloten is.

## Gewijzigde werking

De bovenliggende scan gebruikt een **systemflow**. Iedere omvormer wordt nog steeds via een losse standaardimport toegevoegd. De bovenliggende scan houdt daardoor de importbarrière niet bezet. Een herlaadactie wordt met Home Assistants `async_schedule_reload` gepland, waarna de import kan terugkeren.

Een bestaande, ingeschakelde, door DWARS beheerde GoodWe-entry met `not_loaded` wordt geladen met zijn opgeslagen verbindingsgegevens. Die gegevens worden voor deze toestand niet opnieuw gedetecteerd en herschreven. Geladen of handmatig toegevoegde entries worden hergebruikt. Door de gebruiker uitgeschakelde entries worden niet opnieuw ingeschakeld; bij lopende setup/retry wordt niet tegelijk een herstelactie gestart. De bestaande verificatie van meetdata en het beleid voor daadwerkelijk mislukte DWARS-entries blijven van toepassing.

De agentcode, vermogensregeling, batterijgrenzen, EMS-profielen en BMS-aansturing zijn in deze release niet gewijzigd. Dit voegt geen gezamenlijke regeling van meerdere batterij-omvormers toe; de bestaande grenzen blijven gelden.

## Herstellen op de Raspberry

**Bestaande omvormerconfiguraties, GoodWe-agent en API-key behouden. Verwijder de integratie niet en wis `/data` niet.** Ook een inmiddels werkende handmatige configuratie mag blijven staan.

1. Maak een Home Assistant-backup. Stop de oude installer voor deze update. Publiceer de **inhoud** van `dwars-ems-homeassistant/` in de root van de ingestelde GitHub-repository/branch. Voeg geen extra buitenste map toe. Publiceer het volledige pakket, niet uitsluitend een gewijzigd versienummer of losse bridge. Een aparte testbranch/repository of eerst één test-Raspberry verdient de voorkeur; andere apparaten kunnen dezelfde branch automatisch volgen.
2. Vernieuw de app-/add-onwinkel en voer de echte update van **DWARS OneShot Installer & Updater naar 0.6.4** uit. Het opnieuw starten van de oude container vervangt de code niet. Zorg dat `github_repo_zip_url` naar de branch met de volledige nieuwe release wijst; de installer leest zijn payload uit deze ingestelde URL.
3. Start de installer en open de webinterface. Een lopende OneShot hervat met de opgeslagen key. Bij een afgeronde taak is er **Opnieuw controleren**; bij beschermde handmatige modus **Start met opgeslagen API-key**. Start niet handmatig een tweede besturingsagent.

Bij hervatten uit een eerdere release gaat de procedure eenmalig naar het plaatsen van de actuele software en vraagt zij een Core-herstart aan. Dat is nodig om een nog actieve, oude geblokkeerde Core-taak te beëindigen. **Alleen de installer-app herstarten beëindigt zo'n Core-taak niet.** De herstartintentie wordt ook bewaard wanneer een updater de nieuwe bestanden al op schijf heeft gezet. De normale limiet op herstartpogingen blijft gelden; de release-migratie reset die limiet niet bij iedere start.

Na de Core-herstart worden bestaande entries opnieuw gelezen. De procedure mag vervolgens naar batterij-/sensorcontrole, agent en telemetrieverificatie doorgaan. Een gevonden IP-adres op zichzelf geldt niet als geslaagde installatie.

## Versies die bij elkaar horen

| Onderdeel | Versie |
|---|---|
| Installer-app | 0.6.4 |
| DWARS Setup-integratie | 1.2.0 |
| GoodWe-integratie | 0.9.9.37 |
| SolarEdge Modbus Multi-integratie | 3.2.8 |

De GoodWe-configuratie blijft schema 2.3; de major-/minorconfiguratieversie wordt voor deze reparatie niet verhoogd. Geen apparaatverwijdering of nieuwe configuratie-ID vereist. Agentversies zijn niet verhoogd, omdat hun code niet verandert.

EMS/BMS hoeven voor deze correctie niet opnieuw vervangen of gemigreerd te worden. De eerder geleverde EMS/BMS-OneShot-pakketten met `20260908_oneshot.php` blijven een voorwaarde voor onboarding.

## Diagnose bij een resterend probleem

De knop **Diagnosebestand opslaan** bevat nu naast apparaten en entiteiten ook de HA-versie, draaiende brugversie, scanstatus/fase/duur, geladen integratiedomeinen en actieve flows (inclusief nog niet geïnitialiseerde flows). De export neemt alleen genoemde verbindingsvelden mee: IP, poort, protocol, familie, unit-ID, time-out, retries, keep-alive en interval. Geen complete opties of flowdata; geen API-key of Supervisor-token.

Een ontbrekende apparaattoewijzing staat als **nog niet bepaald**, niet meer ten onrechte als `monitoring`. Een time-out of annulering krijgt een zichtbare melding. Het huidige bereik/ontdekproces kan tijd vragen; een registratie zonder geladen entiteiten blijft geen succes.

Bewaar bij opnieuw vastlopen het nieuwe diagnosebestand en het Home Assistant Core-log van dezelfde poging, vóór verwijderen of opnieuw toevoegen. De nieuwe gegevens maken onderscheid tussen een importbarrière, echte verbindingsfout, migratieprobleem en ontbrekende sensoren. De export kan IP-adressen, serienummers en entiteitsnamen bevatten; deel deze niet in een publieke GitHub-issue zonder controle.

## Testgrenzen

237 lokale tests slagen, waarvan 24 nieuw voor deze release. De voor/na-proef gebruikt echte productiemethoden maar nagebootste HA-registries en apparaatuitlezing. **Er is geen echte Home Assistant/Supervisor, fysieke Raspberry, fysieke GoodWe/SolarEdge of ARM-Dockerbuild getest.** Eerst één installatie in de praktijk valideren. Zie het afzonderlijke testrapport en reproductiebestand.

## Bronnen voor de lifecycle

Home Assistant Core 2026.9.0, officiële broncode:

- https://raw.githubusercontent.com/home-assistant/core/2026.9.0/homeassistant/config_entries.py — `ConfigEntriesFlowManager.async_init`, `async_wait_import_flow_initialized`, `async_finish_flow`, `ConfigEntries.async_add`, `async_schedule_reload`.
- https://raw.githubusercontent.com/home-assistant/core/2026.9.0/homeassistant/setup.py — wachten op importinitialisatie vóór de configuratie-entries geladen worden.
- https://raw.githubusercontent.com/home-assistant/core/2026.9.0/homeassistant/data_entry_flow.py — `async_progress_by_handler(..., include_uninitialized=True)`.
