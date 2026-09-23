# DWARS OneShot 0.6.1 — herstel modusherkenning en agentinstallatie

Datum: 8 september 2026. Dit is een gerichte correctie op het eerder geleverde GitHub-pakket 0.6.0. EMS en BMS blijven ongewijzigd; de serverrelease en migratie van 0.6.0 blijven vereist.

## Diagnose van de gemelde log

`[DWARS OneShot] 0.6.0; mode=manual; stage=profile` betekent dat de oude handmatige installer/updater is gestart, niet de automatische onboarding. `stage=profile` is dan de initiële statuswaarde en geen bewijs dat een profiel is opgehaald. De bestaande modusherkenning kon manual kiezen door een expliciete instelling, een API-key in de oude installeropties of alleen achtergebleven updaterbestanden. Uit deze log alleen is niet vast te stellen welke van deze redenen van toepassing was.

Daarnaast zocht `run.sh` de GoodWe-/SolarEdge-agent uitsluitend met `GET /addons`. Die API somt geïnstalleerde apps op. Een nog niet geïnstalleerde agent hoort in `GET /store/addons` te worden gevonden. De melding “Controleer of deze repo is toegevoegd” was daarom misleidend en werd gevolgd door een onterechte succesvolle cyclusmelding.

De s6-opstartregels zijn op zichzelf geen fout. De `versie=0.5.3`-melding komt van de afzonderlijke interne systeemupdater; deze patch wijzigt die engineversie niet.

## Installeren op deze Raspberry

1. Maak een backup. Publiceer de inhoud van `dwars-ems-homeassistant/` uit deze ZIP in dezelfde GitHub-repository als de al geïnstalleerde installer. Voeg geen extra buitenste map toe. Test eerst op één Raspberry; een publicatie op een door de vloot gevolgde branch kan door andere updaters worden opgehaald.
2. Laat Home Assistant de add-onwinkel vernieuwen en werk **DWARS OneShot Installer & Updater** bij naar **0.6.1**. Alleen een repositoryupload, Core-herstart of restart van de oude container vervangt niet automatisch de ingebouwde installer-Pythoncode; de appupdate is nodig.
3. Zet op het tabblad Configuratie van de installer alleen `installation_mode` op `oneshot`, sla op en herstart **deze app**. Laat de overige instellingen intact. De complete Raspberry hoeft hiervoor niet opnieuw geïnstalleerd of gereset te worden.
4. Open de webinterface. Een via OneShot opgeslagen API-key en voortgang worden hergebruikt. Wanneer er nog geen via OneShot opgeslagen key is, voer deze op de webpagina in en klik op **Installatie starten**. Een key die alleen in de oude handmatige configuratie staat is niet hetzelfde als een al gestarte OneShot-installatie; die wordt niet stilzwijgend overgenomen.
5. Controleer de log op `0.6.1; mode=oneshot` en daarna de voortgang `profile`, `payload`, `components`, `restart`, `bridge`, `discover`, `mapping`, `agent` en `verify`. De procedure installeert de agent na ontdekking en sensorkoppeling. Een ontbrekende meetwaarde kan hem dus vóór de agentfase laten wachten; de webinterface en log geven de reden.

Verwijder de app niet en wis `/data` of de OneShot-statusbestanden niet. Normaal bijwerken bewaart de opgeslagen key en voortgang. Er hoeft geen HA long-lived token gemaakt te worden. GoodWe hoeft niet vooraf handmatig als agent te worden geïnstalleerd.

De expliciete instelling `installation_mode: oneshot` kan ook in 0.6.0 de automatische route activeren. Deze 0.6.1-update repareert daarnaast de oude agentinstallatieroute, de te brede modusherkenning en de ontbrekende diagnostiek.

## Wijzigingen

- Achtergebleven payloadhashes of updaterstatus kiezen niet langer zonder meer handmatige modus. Bij zo'n upgrade controleert de app eerst, uitsluitend lezend, of er daadwerkelijk een geconfigureerde DWARS-agent bestaat. Een geconfigureerde oude installatie houdt de bestaande updater. Bij een onbereikbare Supervisor wordt de bestaande updater eveneens behouden; expliciet `oneshot` kiezen omzeilt die upgradeherkenning.
- Expliciet `manual` en oude klantkeys in de installeropties blijven beschermd. Een bestaande OneShot-status/key wordt bij `auto` hervat. Er worden geen klantkeys van andere apps naar de installer gekopieerd.
- De log toont de reden van de moduskeuze en wat nodig is om OneShot te openen. Handmatige modus toont geen misleidende OneShot-stappenlijst meer.
- Agentzoeken gebruikt de geïnstalleerde lijst én de storecatalogus. De agent uit dezelfde repository als de installer krijgt voorrang; er wordt niet willekeurig een gelijknamige agent uit een andere repository gekozen.
- Een niet-geïnstalleerde agent wordt via `/store/addons/<slug>/install` geïnstalleerd voordat zijn geïnstalleerde `/info` wordt gelezen. Appdetails met `version` maar zonder `installed`-boolean worden correct herkend. Installatiefouten worden niet langer als geslaagde agentcyclus doorgegeven.
- OneShot logt fase/statuswijzigingen en agentinstallatie/-start, zonder API-key of Supervisor-token te loggen.
- Geen wijzigingen aan EMS, BMS, de omvormerdrivers, discoveryalgoritmen, batterijregeling of database. Geen migratie van het OneShot-stateformaat.

## Reproductie en tests

De oorspronkelijke `run.sh` uit de 0.6.0-ZIP is uitgevoerd met echte `curl`-verzoeken tegen een lokale HTTP-testserver die de Supervisor-API nabootst. De winkel bevatte een GoodWe-agent; `/addons` bevatte alleen de installer. De oorspronkelijke code stopte met exitcode 1 en exact de melding `add-on niet gevonden in /addons`. De gecorrigeerde code vond en installeerde onder dezelfde voorwaarden de agent en stopte met exitcode 0. Zie `ONESHOT_HERSTEL_0.6.1_REPRODUCTIE.json`.

Alle **126 installer-Python-tests** slagen: 103 eerder aanwezige tests plus 23 nieuwe regressiecontroles. De nieuwe controles gebruiken onder meer echte `curl`- en `aiohttp`-verzoeken tegen deze lokale testserver, met een geïnstalleerde-appcatalogus en de gedocumenteerde store-array in een Supervisor-envelope. Ook geverifieerd: geen herinstallatie van een aanwezige agent, geen overname van een andere klant, geen keuze uit de verkeerde repository, foutdoorgifte, behoud opgeslagen key/stap, bescherming van bestaande geconfigureerde installaties en geen geheimen in nieuwe logs/status.

Uitvoeren vanuit de repositoryroot (Python met aiohttp, bash, curl en jq):

```bash
python3 -m unittest discover -s dwars_installer/tests -v
bash -n dwars_installer/run.sh
```

De ene bestaande modustest is aangepast omdat een schedulerbestand zonder klant/agent bewust niet langer voldoende is voor `manual`. De pakketversieassertie is gewijzigd naar 0.6.1. Overige bestaande assertions zijn niet verwijderd. Het eerdere `ONESHOT_TESTRAPPORT.md` blijft daarnaast de historische tests en beperkingen van 0.6.0 documenteren.

Dit is geen praktijktest op een Raspberry of echte Home Assistant/Supervisor. Er is geen ARM-Dockerbuild uitgevoerd en geen echte omvormer, klantkey, database of productieserver benaderd. De volledige route moet nog op de test-Raspberry worden bevestigd. Alle eerder beschreven grenzen bij meerdere batterijsystemen en SolarEdge/Anders blijven gelden.

## API-contract

Officiële documentatie: https://developers.home-assistant.io/docs/api/supervisor/endpoints/ — GET `/addons`, GET `/store/addons`, POST `/store/addons/<addon>/install`, GET `/addons/<addon>/info`.
