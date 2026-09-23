# Testrapport OneShot 0.6.5

Datum: 23 september 2026. Basis: de volledige geleverde ZIP 0.6.4.

## Resultaten

De bestaande 237 tests slagen op de ongewijzigde 0.6.4-baseline. Toch blokkeert die
productiematcher met de werkelijke diagnose-export. De fout was niet afgedekt:
het grootste deel van de oude mappingfixtures werd uit de aliastabel opgebouwd
en bevatte niet beide total-power-registers van dit werkelijke apparaat.

De nieuwe release heeft **268 geslaagde lokale tests**: dezelfde 237 plus **31
nieuwe tests** in `dwars_installer/tests/test_065_mapping.py`. Dit zijn unittests,
regressies en contracttests met nagebootste externe services, geen 268 fysieke
installaties. De suite wordt ook vanuit de opnieuw uitgepakte distributie-ZIP
gedraaid. Python-, JSON- en shellsyntax en ZIP-integriteit worden apart gecontroleerd.

## Voor/na op de gerapporteerde inventaris

De originele `oneshot_common.py` uit ZIP 0.6.4 en de nieuwe productiemethode
`bind_device` zijn beide uitgevoerd op de complete private diagnose-export.
Dezelfde proef is uitgevoerd met de meegeleverde geanonimiseerde kopie van de
apparaatinventaris (194 entiteiten).

| Geval | 0.6.4 | 0.6.5 |
|---|---|---|
| Volledige inventaris met beide total-power-registers | `Blocked: Meer dan één passende entiteit voor active_power_total` | Alle 13 agentkoppelingen gevonden |
| Netmeting automatisch gekozen | Geen keuze; taak stopt | Register `active_power_total` volgens de bestaande aliasvoorkeur |
| Alternatief `meter_active_power_total` expliciet handmatig ingesteld | Wordt als expliciete koppeling behandeld | Blijft behouden |

De JSON-reproductie bevat bronhashes en de geanonimiseerde gekozen entiteits-ID's.
De private diagnose met oorspronkelijke identificatoren zit niet in deze release.

## Nieuwe afdekking

De nieuwe tests gebruiken een vaste, letterlijke inventaris, niet een lijst
sensoren gegenereerd uit `GOODWE_MAP`. Afgedekt zijn de twee overlappende totals,
alle 13 opties, schudden van entiteiten, gewijzigde entity-ID's, generieke
translation-keys, fallback naar expliciete alternatieven, niet verwarren met
fasemeting/reactief/schijnbaar vermogen/tweede meter, nul en onbeschikbare waarden,
echte dubbele identiteiten, meerdere fysieke batterij-omvormers, expliciete
handmatige mappings, ontbrekende bediening en SolarEdge-meterafbakening.

Releasehervatting vanaf 0.6.4 behoudt het stadium, de payload, sleutel en
herstartregistratie. De tests controleren ook herhalen van deze migratie,
ontbrekende cache/defaults en oudere componentversies die wel moeten vernieuwen.

De nieuwe volledige-workertests gebruiken lokale aiohttp HTTP-/WebSocket-servers
met de werkelijke productie-`request`, `ws`, `mapping_stage`, `agent_stage`,
`verify_stage` en taaklus. De externe diensten worden nagebootst. Gecontroleerd:

- Eerste installatie met de letterlijke inventaris, gevolgd door agentinstallatie
  en bevestiging van een test-telemetrieontvangst.
- Hervatten van de exacte blokkadesituatie zonder nieuwe ontdekking,
  configuratieflow, componentkopie of Core-herstart; alleen ontbrekende agent
  installeren en starten.
- Bestaande draaiende agent met alleen API-key en automatische sensoropties:
  hergebruiken, ontbrekende koppelingen/registratie aanvullen en ingestelde
  niet-automatische opties behouden.
- Bestaande expliciete alternate-meterkoppeling behouden; alleen ontbrekende
  installatie-ID wijzigen bij een verder volledig geconfigureerde agent.
- Onbeschikbare verplichte sensor en echte dubbele identiteit starten geen agent.
- Uitblijvende BMS-ontvangst wordt geen succesmelding.

De diagnose-export bevat geen volledige HA-state-attributen. De tests voegen
**gesimuleerde select-opties** toe en verversen de state-tijdstempels. De waarden,
registeridentiteiten en disabled-flags blijven die van de geanonimiseerde
inventaris. Dit is een test van de koppeling/taaklus, geen bewijs dat de fysieke
omvormer iedere bedieningsmodus ondersteunt.

## Beperkingen

**Niet uitgevoerd:** echte Home Assistant/Supervisor-runtime, fysieke Raspberry,
Modbus-uitlezing of batterijbesturing op hardware, ARM-Dockerbuild, live
EMS/BMS/database, echte telemetrieaflevering, GitHub-publicatie of uitrol op
klantapparatuur. Downloads, Core-recovery-subprocess en onderhoudsdaemon zijn in
de volledige-workertests vervangen; de HTTP-/WebSocket-contracten zijn lokale
testimplementaties.

Bestaande tests met vaste releaseverwijzingen zijn aangepast om hun fixture op
de huidige release te houden en de huidige snapshotnaam te controleren. De
inhoudelijke veiligheidsasserties zijn niet verwijderd.

## Herhalen

Vanuit de repositoryroot, met Python en aiohttp beschikbaar:

```bash
python -m unittest discover -s dwars_installer/tests -p 'test_*.py' -v
```

De nieuwe tests afzonderlijk:

```bash
python -m unittest discover -s dwars_installer/tests -p 'test_065_mapping.py' -v
```

Voor/na-reproductie met de originele, vertrouwde lokale ZIP 0.6.4:

```bash
python dwars_installer/tests/reproduce_065_mapping.py \
  --baseline /pad/naar/dwars-github-oneshot-0.6.4.zip \
  --output /tmp/oneshot-065-reproductie.json
```

Optioneel kan `--diagnostic /pad/naar/eigen-diagnose.json` de eigen lokale export
gebruiken. Die uitvoer bevat dan de eigen serienummers/entity-ID's en hoort niet
zonder controle in een publieke repository. De proef doet geen netwerkverzoeken.
