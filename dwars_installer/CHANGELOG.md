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
