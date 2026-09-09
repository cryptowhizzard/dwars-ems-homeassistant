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
