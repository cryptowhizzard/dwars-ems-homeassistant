# DWARS OneShot — testrapport

Datum: 8 september 2026. Release installer 0.6.0; installatieprotocol 1.

## Geslaagde gerichte controles

- **103 Python-tests**, inclusief de 50 bestaande installer/hersteltests en 53 nieuwe OneShot-/import-/probecontroles. De bestaande pakketversiecontrole is bijgewerkt van 0.5.3 naar 0.6.0; overige herstelasserties zijn behouden.
- **43 EMS-onboardingscenario's met 405 controles** via een in-memory PDO-testdubbel: bestaande ENUM/VARCHAR-varianten, 4 hoofdzekeringprofielen, 3 fasekeuzes, transactionele rollback, nieuwe platformkeuze en installatietabel. De testfixture is uitgebreid met het nu verplichte platform en de installatietabel; bestaande veiligheidsasserties zijn behouden.
- **26 EMS-profielvalidatiecontroles** en **44 BMS-profielvalidatiecontroles**. Totaal 475 gerichte PHP-controles.
- **124 PHP-bestanden**: `php -l`, geen syntaxfouten.
- **8 shellbestanden**: `bash -n`, geen syntaxfouten.
- **51 Python-bestanden**: parsercontrole, geen syntaxfouten. Daarnaast 7 container-runtimebestanden gecontroleerd op Python 3.11-grammatica.
- **31 JSON-bestanden**: JSON-parsercontrole, geen fouten.

## Wat is gemodelleerd/getest

API-profielvalidatie, drie platformroutes, bewaakte installatievolgorde, herladen van een nieuwe installerinstantie na iedere stap, behoud van de key, begrensde herstartverzoeken, verloren herstartantwoord, geen extra reboot bij werkende nieuwe brug, geen overschrijven van een andere klant/agent, geen tweede actieve DWARS-controller, veilige ZIP-extractie, atomische statusopslag met 0600-rechten, apparaatgebonden sensorkoppeling, ambiguïteit bij meerdere batterij-omvormers, expliciet serienummer, verwacht apparaatcount, handmatige mappings behouden, 0 W geldig, NaN/verouderde waarden afgewezen, telemetrie van een verkeerd GoodWe-serienummer afgewezen, Ingress-toegangscontrole, CSRF-controle en geen sleutel in de statusrespons.

De echte import-/probe-methoden zijn daarnaast als unit met kleine HA-/pymodbus-testdubbels uitgevoerd: alle gevonden GoodWe-apparaten importeren, bestaande serial/IP behouden/bijwerken zonder een nieuwe entry, veranderde identiteit weigeren, SolarEdge-batterijopties activeren en beide pymodbus-argumentvarianten (`slave`/`device_id`) afhandelen. Een andere SunSpec-fabrikant wordt niet als SolarEdge aangenomen.

## Twee bestaande smoke-tests falen al op de aangeleverde basis

Deze zijn zowel op de ongewijzigde input als op de nieuwe code uitgevoerd en geven dezelfde fout:

- EMS `tests/smoke_frontend.php`: `FAIL: battery standby setting exists`.
- BMS `tests/smoke_helpers.php`: `FAIL: battery standby is GoodWe-only`.

Het zijn onder meer broncodemarker-controles; de verwachte marker ontbreekt al in de aangeleverde bron. Deze tests zijn niet verwijderd en de energiebeslislogica is niet gewijzigd om ze kunstmatig groen te maken. De twee failures zijn dus geen nieuw verschil door OneShot, maar ook geen bewijs dat de onderliggende bestaande functie correct is.

## Niet uitgevoerd

Er was geen draaiende Home Assistant/Supervisor, fysieke Raspberry of omvormer beschikbaar. Er is geen echte ARM-Dockerbuild uitgevoerd, geen echte API-key naar jouw servers gestuurd, geen live database geopend en geen migratie op jouw database toegepast. De PDO-controles gebruiken testdubbels, geen MySQL/MariaDB-server. De Python-routetests gebruiken gesimuleerde remote API's; zij zijn geen end-to-end hardwarecertificering.

Nog te bevestigen in de praktijktest: bouwen/installeren van de apps, laden van de custom integraties in jouw HA-versie, werkelijke netwerkontdekking en Modbus-identificatie, correcte apparaatregisterstructuur/sensoreenheden, juiste regeling op de omvormer, echte Core-/hostherstart met hervatting en daadwerkelijke ontvangst bij BMS. Test ook de specifieke aanwezige meter-/batterijcombinatie. Multi-batterijaggregatie is niet geïmplementeerd.

## Zelf opnieuw uitvoeren

Vanuit de GitHub-repositoryroot (Python met aiohttp):

```bash
python3 -m unittest discover -s dwars_installer/tests -v
```

Vanuit EMS:

```bash
php tests/onboarding_connection_regression.php
php tests/oneshot_validation.php
```

Vanuit BMS:

```bash
php tests/oneshot_validation.php
```

Deze commando's wijzigen geen productieklanten en benaderen geen fysieke omvormers. Publiceer de testbestanden bij voorkeur niet als openbare webpagina's; de nieuwe PHP-tests en migraties zijn CLI-only.
