# DWARS OneShot 0.6.2 — installatie en uitrol

Voor herstel van de vastgelopen 0.6.1-onboarding en de onterechte handmatige route zie eerst **ONESHOT_HERSTEL_0.6.2.md**. De serverpakketten/migratie van 0.6.0 blijven ongewijzigd vereist.

Release: 8 september 2026. Dit is een testrelease; eerst op één test-Raspberry valideren, niet rechtstreeks over de hele vloot uitrollen.

## Wat deze release doet

Het bestaande `dwars_installer` is uitgebreid tot **DWARS OneShot Installer & Updater**. Het is geen tweede concurrerende installer. Op een nieuwe installatie opent de webinterface via Home Assistant Ingress en vraagt alleen om de bestaande DWARS/BMS API-key. De instellingen worden opgehaald bij BMS. De key, het klantprofiel en de voortgang worden in de eigen persistente `/data` van de installer opgeslagen.

Voor GoodWe worden de custom integratie en GoodWe-agent geplaatst. De netwerkdetectie gebruikt de bestaande detectie op de echte HA-netwerkinterfaces. Alle positief geïdentificeerde apparaten worden via de Home Assistant-configuratieflow toegevoegd. Een reeds bekend serienummer wordt niet dubbel toegevoegd; een bevestigd nieuw IP kan worden bijgewerkt. Voor SolarEdge worden SolarEdge Modbus Multi en de SolarEdge-agent geïnstalleerd; de detectie controleert zowel SunSpec als fabrikant SolarEdge en scant TCP 1502/502. Batterij- en opslagbediening worden ingeschakeld. Voor Anders wordt de generieke DWARS-agent gebruikt met vooraf ingevulde entiteitsmapping.

Sensoren worden op apparaat-/entiteitsregister en serienummer gekoppeld, niet door willekeurig de eerste gelijknamige sensor te kiezen. De installer controleert actuele numerieke meetwaarden en benodigde bedieningsentiteiten. Een netvermogen van 0 is geldig. Alleen door de integratie standaard uitgeschakelde benodigde entiteiten worden geactiveerd; een bewust door de gebruiker uitgeschakelde entiteit wordt niet geforceerd ingeschakeld.

Elke agent gebruikt haar **eigen Supervisor-token** via de interne Home Assistant-proxy. Een langlevend HA-gebruikerstoken is niet nodig en wordt niet aangemaakt. De installer kopieert zijn Supervisor-token niet naar een andere app.

## Eerst de serverzijde uitrollen

1. Maak een backup van de bestaande EMS-/BMS-bestanden en de database. Bewaar de actieve serverconfiguratie; de meegeleverde volledige codebases bevatten de configuratie uit de aangeleverde ZIPs, niet noodzakelijk de actuele productie-instellingen.
2. Plaats de inhoud van het BMS-pakket op de overeenkomstige locatie. De originele indeling `html/bms/` is behouden; kopieer niet blind een extra map `html` in de bestaande webroot.
3. Voer vanuit de BMS-applicatiemap uit:

   ```bash
   php migrations/20260908_oneshot.php
   ```

4. Plaats daarna de inhoud van het EMS-pakket op de overeenkomstige EMS-locatie. Voer daar dezelfde CLI-migratie uit wanneer EMS een andere database gebruikt. Bij een gedeelde database is de migratie al uitgevoerd; nogmaals uitvoeren is toegestaan.
5. Controleer de nieuwe onboardingkeuze GoodWe / SolarEdge / Anders. Kies voor een bestaande klant onder het klantenoverzicht **OneShot / installatieprofiel** het juiste platform. Er is geen nieuwe API-key nodig.

De migratie voegt `dwars_installation_settings`, `dwars_installations` en `dwars_telemetry_receipts` toe. Bestaande ENUM-waarden blijven behouden; zo nodig worden GoodWe, SolarEdge en Other toegevoegd. De migratie verwijdert geen klanten, bestaande metingen, apparaten of configuraties. Database-DDL is niet als geheel terug te draaien met een PDO-transactie: daarom eerst de databasebackup maken. Bij een onderbreking kan de migratie opnieuw worden uitgevoerd.

De BMS-endpoints `api/install_profile.php` en `api/install_status.php` authenticeren met de bestaande `X-API-Key`. Installatiegegevens van een andere klant zijn niet via die key bereikbaar. Het profiel komt uit de reguliere `clients`-velden en de nieuwe installatietabel, niet uit de parallelle oude energy-settings-route. De bestaande decision-endpoints en energiebeslislogica zijn niet vervangen.

## GitHub en eerste praktijktest

Publiceer de **inhoud** van `dwars-ems-homeassistant/` in de repositoryroot. Niet de buitenste map nogmaals als extra submap toevoegen. Publiceer het volledige pakket: de installer heeft ook `custom_components/dwars_setup` nodig. De oude `.git`-geschiedenis en Python-cachebestanden zijn niet opgenomen. Het aanwezige `goodwe_agent.zip` is opnieuw opgebouwd uit de meegeleverde agent en bevat geen oude agentversie meer.

**Let op de bestaande fleet-updaters:** wanneer zij `main` volgen, kan publiceren op `main` ook bestaande apparaten laten updaten. Gebruik eerst een aparte testrepository en pas op de test-Raspberry `github_repo_zip_url` aan naar het repository-ZIP-adres van die testrepository. De normale productie-default blijft:

```text
https://github.com/cryptowhizzard/dwars-ems-homeassistant/archive/refs/heads/main.zip
```

Na een geslaagde praktijktest kan de geteste release naar de productierepository. Maak vooraf een volledige Home Assistant-backup. Alleen het GitHub-pakket hoort in de HA-repository; EMS en BMS bevatten private serverconfiguratie en horen daar niet bij.

## Werkwijze op een nieuwe Raspberry

Home Assistant OS moet al draaien, de eerste Home Assistant-onboarding moet afgerond zijn en het netwerk/internet moet werken. Deze release flasht geen SD-kaart en installeert geen Tailscale.

1. Voeg de DWARS-apprepository toe en vernieuw de appwinkel.
2. Installeer en start **DWARS OneShot Installer & Updater** (slug blijft `dwars_installer`). Laat automatisch starten aan.
3. Open de webinterface van die app. Laat `installation_mode: auto` staan op een nieuwe installatie.
4. Vul de DWARS API-key in en klik op **Installatie starten**.

De normale volgorde is: klantprofiel → bestanden → integraties → Core-herstart → setupbrug → ontdekking → sensorkoppeling → agent → bevestigde telemetrie. Het merk en de klantinstellingen worden niet opnieuw op de Raspberry gevraagd. De standaard BMS-basis is `https://api.metdezon.nl/bms/api/`; alleen bij een andere BMS-host moet de beheerder vooraf `oneshot_api_base_url` aanpassen.

**Sluit het venster gerust tijdens installatie.** De werkzaamheden lopen in de app, niet in JavaScript in de browser. Na een Core-herstart kan de webinterface tijdelijk niet bereikbaar zijn. Na een echte apparaat-/appherstart wordt de opgeslagen stap hervat. De API-key hoeft niet opnieuw ingevoerd te worden. De installatie wist geen `configuration.yaml` en schrijft niet rechtstreeks in HA-configuratieregisters.

Normaal is één Core-herstart nodig wanneer de integratiebestanden veranderen. In de uitzonderlijke situatie dat een herstartverzoek verloren gaat wordt eerst de daadwerkelijke beschikbaarheid gecontroleerd. Er zijn maximaal twee OneShot-herstartverzoeken per installatie; het bestaande begrensde herstelmechanisme kan een gestopte Core starten. Er wordt geen oneindige herstartlus gemaakt.

**Gereed** verschijnt pas na actuele benodigde sensoren, een gestarte agent en een nieuwe BMS-ontvangstbevestiging met het installatie-ID. Bij GoodWe moet ook het omvormerserienummer overeenkomen. Een open TCP-poort of alleen een draaiende container geldt niet als bewijs dat de installatie gereed is.

## Meerdere omvormers en grenzen

Alle ontdekte ondersteunde omvormers worden in Home Assistant toegevoegd. De agent bestuurt in deze release **één batterij-omvormer**. Een PV-omvormer plus één batterij-omvormer kan daardoor automatisch worden gekoppeld. Bij meerdere batterij-omvormers kiest de installer niet willekeurig: leg vooraf in EMS het te besturen serienummer vast. Overige apparaten blijven in HA beschikbaar; deze release bouwt geen aparte BMS-telemetriestroom per extra omvormer en geen vermogens-/SoC-aggregatie over meerdere batterijsystemen.

Het klantvermogen moet passen bij het werkelijk bestuurde batterijsysteem. Een gezamenlijk vermogen van meerdere installaties niet als setpoint voor één geselecteerde omvormer gebruiken. Bij meerdere gelijkwaardige meters of batterijsensoren moet een eenduidige mapping vooraf zijn vastgelegd. De installer blokkeert in plaats van te gokken.

SolarEdge Modbus/TCP moet op de omvormer bereikbaar en ingeschakeld zijn. Een lokaal vereiste installateursinstelling kan de Raspberry niet via een gesloten Modbus-poort oplossen. Standaard wordt unit-ID 1 gezocht; extra Modbus-adressen kunnen vooraf in EMS onder `unit_ids` worden ingesteld. Een nieuwe SolarEdge-agent krijgt zonder expliciete afwijking 100% als PV-herstelpercentage; de locatiespecifieke 65%-default uit de oude agent wordt niet blind op nieuwe klanten toegepast. Een al gekoppelde bestaande agent behoudt zijn instelling.

**Anders is geen universele auto-detectiedriver.** De passende HA-integratie en entiteiten moeten aanwezig zijn. Vul het apparaatprofiel vooraf in EMS in. Deze OneShot-route vereist een SoC-sensor, netsensor, modus-select en één vermogens-number of afzonderlijke laad-/ontlaad-numbers. Voorbeeld:

```json
{
  "agent_options": {
    "soc_entity": "sensor.batterij_soc",
    "grid_entity": "sensor.netvermogen",
    "ha_mode_select": "select.batterijmodus",
    "ha_mode_idle_option": "auto",
    "ha_mode_charge_option": "charge",
    "ha_mode_discharge_option": "discharge",
    "ha_power_number": "number.batterijvermogen"
  }
}
```

Vervang deze voorbeeldentiteiten door werkelijk bestaande entiteiten. SoC moet in procenten zijn; vermogens moeten aansluiten op de eenheden en tekenconventies die de bestaande agent verwacht. Een JSON-modemap voor de generieke agent blijft een **string**, geen genest object. Voer geen API-keys, tokens of willekeurige URLs in een apparaatprofiel in.

Optionele netwerkinstructies voor GoodWe/SolarEdge, normaal niet nodig:

```json
{"hosts":["192.168.20.45"],"unit_ids":[1,2]}
```

Gebruik het veld **verwacht aantal omvormers** wanneer de installer niet mag afronden voordat een vooraf bekend aantal is gevonden. 0 betekent onbekend. Bij een onbereikbaar apparaat kan geen software garanderen dat alle fysiek aanwezige omvormers gevonden zijn.

## Bestaande installaties en beheer

`installation_mode: auto` beschouwt een API-key, component-hash of updaterrestant niet als bewijs dat onboarding klaar is. Een eenduidige bestaande installerkey wordt bij een onafgeronde installatie hergebruikt. Actieve agents en al ingerichte omvormers worden conservatief beschermd; ook een bewust gestopte ingerichte installatie blijft behouden. Start daar bewust vanuit de webinterface met **Start met opgeslagen API-key** om OneShot te gebruiken. Een API-storing tijdens de voorcontrole start niet de oude `both`-installatieroute. Zie het 0.6.2-herstelrapport voor de precieze controles.

Expliciete bestaande sensormappings worden behouden. Komen zij niet overeen met het geselecteerde apparaat, dan wordt de installatie geblokkeerd; ze worden niet stilzwijgend vervangen. Een agent met een andere API-key of een ander installatie-ID wordt niet overgenomen. Een tweede reeds actieve DWARS-besturingsagent voorkomt dat er nog een concurrerende controller wordt gestart. Bewaar of controleer bestaande lokale HA-automatiseringen: de installer kan niet alle mogelijke externe regelingen herkennen.

De eerste agentsnapshot staat beveiligd in `/data/oneshot_original_<agent>.json`. De API-key staat in `/data/oneshot_credentials.json`; de voortgang in `/data/oneshot_state.json`. Deze bestanden en backups daarvan zijn gevoelig. Maak een golden image **vóór** invoer van een klantkey, niet van een reeds gekoppelde Raspberry.

Na gereed gaat de bestaande updater verder met dezelfde instelbare update-/backuplogica. OneShot en onderhoud delen één lock. **Opnieuw controleren** gebruikt de opgeslagen key. Bij een actieve onderhoudstaak wordt geen tweede taak ertussendoor gestart. De knop is geen functie om een Raspberry naar een andere klant over te dragen; gebruik daarvoor een bewust voorbereide, schone installatie.

Bij `waiting` wordt opnieuw geprobeerd met opgeslagen voortgang. Bij `blocked` moet de aangegeven configuratievoorwaarde worden opgelost (bijvoorbeeld verkeerd merk, meerdere batterijsystemen, ontbrekende entiteiten). Een gewijzigd wachtend EMS-profiel wordt bij een volgende poging opgehaald. Bij `complete` wijzigt een later aangepast profiel niet stilzwijgend de installatie; laat die bewust opnieuw controleren. Een platformwissel midden in de installatie wordt niet automatisch uitgevoerd.

## Rollback en verificatie

Behoud de oorspronkelijke ZIPs en een actuele database-/HA-backup. Serverrollback: herstel de voorgaande bestanden; de nieuwe tabellen mogen blijven staan. Herstel voor een volledige HA-rollback de HA-backup met de vorige appconfiguratie en integraties. Verwijder niet willekeurig `/data`, tokens of `.storage`-bestanden om een retry af te dwingen.

Het bestand `ONESHOT_TESTRAPPORT.md` beschrijft precies wat lokaal is getest en wat niet. Live Home Assistant, een daadwerkelijke ARM-Dockerbuild, live MySQL/MariaDB en fysieke GoodWe/SolarEdge-omvormers zijn niet in deze werkomgeving getest. De werking op jouw hardware moet daarom in de praktijktest worden bevestigd voordat je de release breed publiceert.
