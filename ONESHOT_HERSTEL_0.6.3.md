# DWARS OneShot 0.6.3 — GoodWe-configuratie en veilig hervatten

Release: 9 september 2026. Alleen het GitHub/Raspberry-pakket wijzigt. De eerder geleverde OneShot-versies van EMS/BMS en hun databasemigratie blijven nodig, maar worden voor deze correctie niet aangepast.

## Voor de installatie die inmiddels handmatig werkt

**Laat de werkende GoodWe-integratie en GoodWe-agent staan. Verwijder geen apparaat, app, API-key of installatievoortgang.** De update is bedoeld om die configuratie over te nemen, niet om opnieuw te beginnen.

1. Maak een Home Assistant-backup. Publiceer eerst naar een aparte testrepository/branch of test op één Raspberry. Apparaten die dezelfde branch via fleet-updates volgen kunnen ook de nieuwe componenten ophalen.
2. Plaats de **inhoud** van `dwars-ems-homeassistant/` uit de ZIP in de repositoryroot. De map `dwars_installer/` en `custom_components/` staan rechtstreeks in de root. Houd de ingestelde repository-/ZIP-URL van de testinstaller gelijk aan de plek waar deze release gepubliceerd wordt.
3. Vernieuw de appwinkel en voer de appupdate van **DWARS OneShot Installer & Updater naar 0.6.3** daadwerkelijk uit. Alleen oude appcode herstarten is niet voldoende. De complete repository bevat ook **GoodWe-integratie 0.9.9.36** en **DWARS Setup-brug 1.1.0**; alleen `config.json` vervangen werkt niet.
4. Start de installer en open de webinterface. Bij een lopende/onderbroken OneShot worden opgeslagen API-key en installatie-ID hergebruikt. Is de vorige taak `complete`, kies **Opnieuw controleren**. Is het een beschermde oude handmatige installatie, kies **Start met opgeslagen API-key**. Niet opnieuw het apparaat toevoegen en niet `Zoek ontbrekende omvormers` gebruiken wanneer alles al aanwezig is.

### Wat er bij deze update hoort te gebeuren

De installer ververst een oude installatiesnapshot eenmalig zodat de nieuwe GoodWe-code en installatiebrug werkelijk naar `/config/custom_components` gaan. Daarom kan de voortgang eerst weer **componenten installeren** tonen en kan één Home Assistant **Core-herstart** nodig zijn. Dat is een software-update, geen verwijdering of herinstallatie van de fysieke omvormerconfiguratie.

Na het laden controleert de installer de actuele HA-configuraties. Een geladen handmatige configuratie wordt hergebruikt. De reeds aanwezige sensoren worden opnieuw op basis van het serienummer en hun actuele apparaatkoppeling gelezen. Het oude interne HA-entry-ID is niet leidend.

Een bestaande GoodWe-agent met dezelfde klantkey behoudt zijn ingevulde entiteiten, vermogens-/batterijinstellingen en veiligheidsinstellingen. Als alleen de OneShot-installatie-ID ontbreekt, wordt die toegevoegd voor de bestaande BMS-ontvangstcontrole; hiervoor kan de agent één keer worden gestopt en gestart. Er is geen herinstallatie nodig. Een agent met een ander klantnummer, andere klantkey of andere installatie-ID wordt niet overgenomen.

Ontbrekende of onbeschikbare sensoren houden de taak bij **Batterij en sensoren controleren**. De concrete ontbrekende entiteit of HA-laadfout wordt getoond. Die fout leidt niet meer tot opnieuw ontdekken/importeren van een al aanwezige omvormer. Alleen een werkelijk ontbrekend apparaat, een nog niet bereikt verwacht aantal, of de aparte actie **Zoek ontbrekende omvormers** geeft aanleiding voor een nieuwe scan.

## Concrete codecorrecties

### 1. Consistente GoodWe-configuratieversie

De oude flow erfde `VERSION=1`, terwijl de migratie `version=2` opsloeg. Home Assistant accepteert een entry met een hogere major-versie dan de flow niet. Flow en migratie zijn nu `VERSION=2`, `MINOR_VERSION=3`. Dit is de configuratieschemaversie; niet de appversie of componentversie.

De migratie vult ontbrekende/null defaults aan zonder netwerkdetectie. Geldige bestaande waarden blijven staan. De verbindingsopbouw neemt timeout, retries en Modbus-unit-ID uit opgeslagen data over, tenzij een expliciete optie die overschrijft. Ook de pollinterval en keep-alive blijven consistent. De vorige setup viel voor enkele velden terug op defaults ondanks opgeslagen data.

### 2. Niet alleen identificatie, ook echte meetdata vóór automatisch opslaan

Automatische toevoeging verifieert het serienummer en vraagt vervolgens runtime-meetwaarden op. Pas na een geslaagde, niet-lege uitlezing worden model/familie, protocol, poort en verbindingsinstellingen opgeslagen via dezelfde gegevensopbouw als handmatig toevoegen. Het geteste timeoutniveau wordt ook bij de latere setup gebruikt. Als identificatie lukt maar uitlezen niet, wordt geen schijnbaar geslaagde configuratie aangemaakt. Een alternatief transport wordt begrensd geprobeerd.

Bestaande geladen configuraties worden door OneShot-import niet herverbonden, niet aangepast en niet herladen. Ook uitgeschakelde of niet-geladen **handmatige** entries worden niet stilzwijgend gerepareerd. Alleen herkenbaar door DWARS aangemaakte, niet-geladen entries kunnen na succesvolle verificatie gericht worden hersteld. Verwijderen van dubbele/problematische handmatige entries gebeurt nooit automatisch.

### 3. Hervatten zonder de werkende installatie te overschrijven

De installer leest de actuele configuratie- en entiteitenregisters bij ontdekken, koppelen, agentconfiguratie en eindcontrole. Na handmatig verwijderen/toevoegen verandert alleen de opgeslagen koppeling naar hetzelfde serienummer. Een tijdelijk `unavailable` SoC, ontbrekende control of HA-`setup_error` blijft een laad-/sensorprobleem, niet een verzoek om een nieuw apparaat te installeren.

Bestaande handmatige agentkoppelingen krijgen voorrang boven naamherkenning. De toewijzing controleert wel of batterij/control-entiteiten echt bij het gekozen apparaat horen. Een externe netvermogensensor mag extern blijven. Er wordt geen willekeurig ander batterijserienummer gekozen. Ook GoodWe-switch-unique-ID's zonder `goodwe_`-prefix worden herkend.

### 4. Oude softwarecache niet als nieuwe release gebruiken

Een update midden in een onderbroken taak maakt de oude payloadcache eenmalig ongeldig, met een private lokale voortgangssnapshot. De API-key, klantbinding en fysieke apparaattoewijzing blijven behouden. Downloads met een te oude GoodWe-component of installatiebrug worden afgewezen voordat componenten worden gewijzigd. De installer controleert of brug 1.1.0 daadwerkelijk in Core geladen is.

## Diagnose zonder geheimen

De webinterface toont per apparaat de HA-laadstatus. **Diagnosebestand opslaan** levert `dwars-oneshot-diagnose.json`, met installatieversie, fase, laadstatus/foutreden, serienummers, IP's en entiteitskoppelingen. API-keys, Supervisor-token en volledige appopties worden niet opgenomen; bekende sleutels worden ook uit foutteksten verwijderd. Het bestand bevat wel installatiegegevens zoals lokale IP's en serienummers: behandel het als interne technische informatie.

**Opnieuw controleren** en **Zoek ontbrekende omvormers** zijn afzonderlijke acties. Alle bedieningsroutes lopen via Home Assistant Ingress; schrijfacties vereisen daarnaast de pagina-CSRF-token.

## Wat deze correctie niet beweert of toevoegt

Het exacte protocol/poort/familieverschil bij de gemelde eerste mislukte installatie is niet uit een Core-log of configuratie-export vastgesteld. De versie-, optie- en hervattingsdefecten hierboven zijn wel in de code gevonden en met de oude en nieuwe methode-uitvoering vergeleken.

De nieuwe tests zijn lokaal. Geen fysieke Raspberry, echte Home Assistant/Supervisor, ARM-containerbuild of echte GoodWe/SolarEdge-omvormer is hier uitgevoerd. Het installeren van de werkelijke HA- en goodwe-testafhankelijkheden was in deze omgeving niet mogelijk; de integratietests gebruiken daarom gecontroleerde testobjecten aan die grenzen. Zie het testrapport voor de precieze dekking.

Meerdere omvormers toevoegen blijft ondersteund, maar gezamenlijke regeling/SoC-aggregatie over meerdere batterij-omvormers is niet toegevoegd. Bij meerdere besturingskandidaten blijft een eenduidig besturingsserienummer vereist. Andere veiligheids- of batterijregeling is niet gewijzigd.

## Terugzetten

Stop bij onverwacht gedrag eerst de installer. Verwijder niet de handmatige integratie of agent. Gebruik een volledige HA-backup om componentbestanden én configuratiegegevens consistent terug te zetten. Alleen de oude GoodWe-flow terugzetten kan het oude major-versieconflict opnieuw veroorzaken. De installer bewaart daarnaast zijn eigen pre-upgrade-voortgang en componentbackup; dat vervangt geen complete HA-backup.

## Bestanden en testbewijs

- `ONESHOT_HERSTEL_0.6.3_TESTRAPPORT.md`: uitgevoerde controles en grenzen.
- `ONESHOT_HERSTEL_0.6.3_WIJZIGINGEN.json`: wijzigingen met hashes ten opzichte van 0.6.2.
- `dwars_installer/tests/063_before_after.json`: resultaten van dezelfde gecontroleerde gevallen op oude en nieuwe productiemethoden.
- `dwars_installer/tests/063_test_output.txt`: volledige lokale testsamenvatting.
