# Testrapport OneShot 0.6.4

Datum: 23 september 2026. Baseline: geleverde volledige ZIP 0.6.3. Runtime van de lokale tests: Python 3.13.5.

## Resultaat en beperkingen

**237 lokale unittests/contracttests geslaagd: de bestaande 213 plus 24 nieuwe.** De distributie-ZIP wordt opnieuw uitgepakt en vanuit die uitgepakte bestanden getest. Tevens controle op Python-syntax, shell-syntax, JSON, manifestversies en ZIP-integriteit.

**Niet uitgevoerd:** echte Home Assistant-runtime, echte Supervisor, fysieke Raspberry, fysieke GoodWe-/SolarEdge-uitlezing, ARM-Dockerbuild en live server/database. Er zijn geen wijzigingen uitgerold naar een klantinstallatie of GitHub. Het feit dat tests slagen is geen volledige hardware- of productieacceptatie.

De 213 eerdere tests slaagden ook op 0.6.3. Ze bootsten echter niet na dat HA bij de eerste integratielading op alle lopende importflows wacht. Daardoor dekten ze precies de circulaire afhankelijkheid van de bulkimport niet af. De nieuwe proef modelleert dit gedrag expliciet, met afzonderlijke pending-importfutures per domein, in plaats van elke `async_init` onmiddellijk als succesvol te laten terugkeren.

## Voor/na-proef met oorspronkelijke productiemethoden

`dwars_installer/tests/test_064_lifecycle.py` haalt de relevante werkelijke Python-methoden uit de bronbestanden, ook die uit de apart uitgepakte 0.6.3-baseline. Alleen de HA-lifecycleomgeving/registries en invertertransporten zijn doubles. Het model volgt de importinitialisatiebarrière uit de officiële HA Core 2026.9.0-broncode, die in de herstelhandleiding is gelinkt.

| Geval | 0.6.3 | 0.6.4 |
|---|---|---|
| Eerste GoodWe-integratie, automatische bulkscan | Circulaire wachtcyclus; `running`, `not_loaded`, geen entiteiten | Scan rondt af; model-entry geladen |
| Eerste SolarEdge-integratie, automatische bulkscan | Zelfde circulaire wachtcyclus | Scan rondt af; model-entry geladen |
| Herstel van mislukte GoodWe-entry vóór eerste domeinlading | Import wacht op zijn eigen herlaadactie | Import keert terug; geplande laadactie rondt af |

Het model voegt na succesvolle setup **één testentiteit** toe. Dit is een lifecycle-marker, geen nabootsing of acceptatie van de complete set werkelijke GoodWe-sensoren. De JSON met `entity_counts: [1]` betekent dus niet dat een fysieke inverter is uitgelezen. De tijdslimiet in deze deterministische lokale proef is kort; de productie-scan behoudt zijn eigen ruime tijdslimiet.

Het bestand `ONESHOT_HERSTEL_0.6.4_REPRODUCTIE.json` bevat de resultaten, inclusief de verschillen in bron van de ouderflow (`import` versus `system`).

## Nieuwe tests

`test_064_lifecycle.py`: 11 tests voor eerste domeinlading GoodWe/SolarEdge, meerdere omvormers, herhaald scannen, bestaande `not_loaded`-configuratie met behoud van verbindingsgegevens, geplande herstelactie, werkende handmatige configuratie, bewust uitgeschakelde configuratie, lopende setup/retry, afwijzen van de oude bulkimport en een ongeldige systemflow.

`test_064_diagnostics_release.py`: 13 tests voor system-bron, zichtbare time-out, annulering, legacyfout, diagnose met ook ongeïnitialiseerde flows, uitsluitend beheerderstoegang, whitelist van verbindingsgegevens zonder sleutels, persistente herstart bij releaseovergang ook als bestanden al nieuw zijn, versieblokkade voor een oude draaiende bridge of oude payload, acceptatie van de nieuwe payload en een nog onbepaalde apparaatrol.

De bestaande fixtures zijn bijgewerkt naar de nieuwe componentversies en de nieuwe systemflow voor bulkscans. Tests voor losse imports blijven imports. De hersteltest controleert nu dat herladen wordt ingepland en niet meer synchronisch afgewacht. Deze wijzigingen verbergen geen mislukte fysieke tests: fysieke tests waren niet aanwezig en zijn niet uitgevoerd.

## Herhalen

Vanuit de repositoryroot, in een Python-omgeving met aiohttp:

```bash
python -m unittest discover -s dwars_installer/tests -p 'test_*.py'
```

De negatieve controle vereist daarnaast de originele ZIP 0.6.3, afzonderlijk uitgepakt. Vervang het voorbeeldpad door de werkelijke repositorymap:

```bash
python dwars_installer/tests/test_064_lifecycle.py   --baseline /pad/naar/uitgepakte-0.6.3/dwars-ems-homeassistant   --output /tmp/oneshot-064-before-after.json
```

Deze proef controleert met assertions dat de oude code de bedoelde blokkering reproduceert en de nieuwe code niet. Geef de baseline dus niet dezelfde map als de nieuwe release. De onafhankelijke regressiesuite heeft geen baseline nodig.

## Afbakening

Geen wijzigingen aan GoodWe-agent, SolarEdge-agent, generieke agent, EMS/BMS-optimalisatie, batterijregeling of vermogensgrenzen. Geen live databasemigratie nodig voor deze release. Voor de minimale serververeisten en bestaande multi-invertergrenzen blijft de oorspronkelijke installatiehandleiding gelden.
