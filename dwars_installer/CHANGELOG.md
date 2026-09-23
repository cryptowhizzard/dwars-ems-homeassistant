# 0.6.4 — 2026-09-23

- Herstelt een circulaire afhankelijkheid bij de eerste integratielading: de DWARS-bulkscan is een `system`-flow en geen bovenliggende `import`-flow. Losse omvormers blijven standaardimports. GoodWe en SolarEdge gebruiken dezelfde herstelde structuur.
- Oude bruggen die een bulkimport starten krijgen een expliciete afwijzing; geen stille terugval naar de geblokkeerde route.
- Herladen van een bestaande DWARS-GoodWe-entry wordt via `async_schedule_reload` gepland; de import wordt niet meer opgehouden door haar eigen herlaadactie.
- Een bestaande ingeschakelde, DWARS-beheerde `not_loaded`-entry wordt met ongewijzigde opgeslagen verbinding geladen. Geladen, handmatige, uitgeschakelde of reeds ladende configuraties worden niet omgezet.
- Releasehervatting ververst de payload en vraagt een persistente Core-herstart aan, ook als de nieuwe bestanden al op schijf staan. API-key, serienummers, configuratie-ID's en agentkoppelingen worden niet gewist.
- Controle op brug 1.2.0 en minimaal GoodWe 0.9.9.37 / SolarEdge 3.2.8 voorkomt onvolledige publicatie.
- Diagnose voegt HA-versie, component-laadstatus, ook nog niet geïnitialiseerde flows, scanfase, duur en een beperkte lijst verbindingsinstellingen toe. Geen volledige opties/context of sleutels geëxporteerd.
- Time-out en annulering worden zichtbaar; een nog niet gekoppelde omvormer wordt niet als definitief monitoring-only aangemerkt.
- 24 nieuwe tests; 237 lokale tests totaal. De oude code loopt in de nieuwe importbarrière-proef vast, de nieuwe code niet. Geen volledige HA-runtime of fysieke hardware getest.

Zie `ONESHOT_HERSTEL_0.6.4.md` en `ONESHOT_HERSTEL_0.6.4_TESTRAPPORT.md` in de repositoryroot.

# 0.6.3 — 2026-09-09

- GoodWe flow/migratie beide schema 2.3; ontbrekende/default-null velden offline herstellen zonder geldige gebruikersinstellingen te vervangen.
- Setup respecteert persistente timeout, retries, Modbus-unit-ID, pollinterval en keep-alive.
- Automatische import vereist runtime-meetdata, slaat de geteste verbindingsparameters op en laat geladen/handmatige/uitgeschakelde entries ongemoeid.
- Hervatten leest actuele HA-laadstatus en serienummer/entiteiten. Sensorfouten betekenen geen herinstallatie; handmatige re-add wijzigt alleen de interne koppeling.
- Bestaande agententiteiten en batterij-/veiligheidsinstellingen behouden; alleen ontbrekende installatie-ID registreren indien nodig.
- Oude payload eenmalig ongeldig maken bij releaseovergang; brug 1.1.0 en GoodWe 0.9.9.36 controleren.
- Afzonderlijke herstel-/ontdekknoppen en Ingress-diagnose-export zonder sleutels.
- 59 nieuwe lokale tests, 213 totaal; echte HA/Supervisor/hardware en ARM-build niet uitgevoerd.

Zie `ONESHOT_HERSTEL_0.6.3.md` en `ONESHOT_HERSTEL_0.6.3_TESTRAPPORT.md` in de repositoryroot.

# 0.6.2 — 2026-09-08

- API-key in oude installeropties blokkeert auto-onboarding niet meer. Eenduidige bestaande key wordt persistent overgenomen; een bestaande OneShot-key blijft behouden.
- Read-only controle onderscheidt onafgeronde onboarding van actieve of al ingerichte legacy-installaties. Geen terugval naar de oude `both`-installatieroute bij een mislukte opstartcontrole.
- OneShot kan vanuit Ingress met de opgeslagen key gestart worden, zonder YAML-wijziging/herstart/key-herhaling.
- Vroege controle op andere klantkeys, installatie-ID's en concurrerende agents; snapshots en boot-beveiliging van ongebruikte gestopte agents zonder key.
- Beschermd legacy-onderhoud installeert geen tweede platform en overschrijft geen agentconfiguratie.
- Volledige SolarEdge-/generieke options-map, controle van verplichte velden en expliciete foutpropagatie. Geen fictieve configuratie-/startsuccesmelding na HTTP 400.
- API-foutdetails zichtbaar met secret-redactie; JSON-fouten onder HTTP 200 tellen ook als fout.
- 28 nieuwe regressietests; 154 lokale tests slagen. Echte Raspberry, Supervisor, ARM-build en omvormers niet getest. Serverpakketten blijven ongewijzigd.

Zie `ONESHOT_HERSTEL_0.6.2.md` voor herstelstappen en testgrenzen.

# 0.6.1 — 2026-09-08

- Repareert het zoeken/installeren van nog niet geïnstalleerde agents via de storecatalogus in plaats van alleen `/addons`.
- Herkent appdetails zonder optionele `installed`-boolean en geeft installatiefouten door.
- Modus `auto` onderscheidt updaterrestanten van een daadwerkelijk geconfigureerde oude agent; bestaande handmatige installaties blijven beschermd.
- Duidelijke modusreden, instructie voor `installation_mode: oneshot` en voortgangslogs; behoud opgeslagen sleutel/state.
- 23 nieuwe regressietests met onder meer lokale HTTP-contracttests. Geen wijzigingen aan EMS/BMS of batterijregeling.

Zie `ONESHOT_HERSTEL_0.6.1.md` in de repositoryroot voor installatie en testgrenzen.

# 0.6.0 — 2026-09-08

API-key-only Ingress UI; hervatbare installatie; automatische configflows; apparaatgebonden mapping; BMS-installatieprofiel/status/telemetriecontrole; behoud handmatige updater.

Zie `ONESHOT_INSTALLATIE.md` in de repositoryroot. Eerst testen op één Raspberry.

# Changelog

## 0.5.1

- Gebruikt het officiële `/reload_updates`-endpoint met compatibiliteitsfallback naar `/supervisor/reload`.
- Expliciete HTTP 4xx-responses falen direct in plaats van 30–60 minuten te blijven wachten.
- Een door Supervisor geweigerde HAOS-update veroorzaakt geen hostreboot meer.
- De dagelijkse systeemupdater start ook als de eerste GitHub-/Store-componentsynchronisatie tijdelijk mislukt.
- Extra regressietests voor refresh-fallback, afwijzingen en OS-rebootveiligheid.
- De uitsluiting geldt alleen voor de originele GoodWe software/HACS-update; fysieke GoodWe-firmware blijft updatebaar.

## 0.5.0

- Volledige updater herschreven als persistente Python state-machine.
- Dagelijks schema standaard 04:00 lokale tijd met vaste fleet-jitter van maximaal 15 minuten.
- Eerste gemiste ochtendrun wordt tot 10:00 ingehaald.
- Eén volledige backup per run; geen per-update backups.
- Geen backup wanneer de voorcontrole vaststelt dat alles al actueel is.
- Store-, Supervisor- en update-entitycatalogi worden direct vóór de run ververst.
- Retentie voor uitsluitend automatisch gemaakte DWARS-backups; standaard drie exemplaren.
- Veilige volgorde: add-ons, HACS/update-entities, retries, systeemplugins, Supervisor, OS/reboot, Core als laatste.
- Hervatten na Core/Supervisor-restart, add-on self-update en HAOS-reboot.
- Alle geïnstalleerde add-ons worden meegenomen, waaronder Tailscale.
- Supervisor CLI, DNS, Audio, Multicast en Observer worden eveneens gecontroleerd.
- Upstream/originele GoodWe-update wordt uitgesloten; DWARS GoodWe blijft leidend.
- Periodieke GitHub-synchronisatie standaard en via `fleet_managed_updates` afgedwongen.
- Onderhoudslock voorkomt dat repository-installatie en systeemupdate door elkaar lopen.
- Retrylogica, eindverificatie, statusentity en één eindnotificatie toegevoegd.
- Reboot-timeout voorkomt een permanent vastgelopen OS-updatestage.
