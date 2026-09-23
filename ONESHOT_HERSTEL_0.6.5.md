# DWARS OneShot 0.6.5 — sensorkoppeling bij een geladen GoodWe

23 september 2026. Basis: het volledige eerder geleverde GitHub-pakket 0.6.4.

## De fout

De aangeleverde diagnose meldt `stage=mapping`, `status=blocked` en
`Meer dan één passende entiteit voor active_power_total`. De omvormer is
`loaded`; de export bevat 194 entiteiten, waaronder een beschikbare batterij-SoC.
Het gaat dus niet om een nieuwe batterijdetectie of een mislukte omvormerinstallatie.

GoodWe levert op hetzelfde apparaat onder meer de registers `active_power_total`
en `meter_active_power_total`. De oude matcher gaf een exacte overeenkomst en
"eindigt op dezelfde woorden" dezelfde score. Daardoor werden deze registers als
twee gelijkwaardige keuzes behandeld, hoewel de bestaande aliastabel juist een
voorkeursvolgorde vastlegt. De originele 0.6.4-code reproduceert de melding met de
volledige diagnose-export.

## Correctie

De GoodWe-registersleutel wordt uit de unieke entiteits-ID gehaald, met behoud
van de koppeling aan het serienummer. Daarna wordt de volledige sleutel
vergeleken. `active_power_total` krijgt de bestaande eerste voorkeur;
`meter_active_power_total` is de volgende expliciete alias, niet opnieuw een
match van de eerste alias. Een generieke vertalingssleutel overrulet een bekende
GoodWe-registeridentiteit niet. Entiteitsnamen mogen door de gebruiker zijn
gewijzigd. Suffixherkenning voor overige integraties blijft als lagere fallback
beschikbaar.

Met de volledige gerapporteerde inventaris worden alle 13 GoodWe-opties gekoppeld.
De taak heet nu **Sensoren automatisch aan de agent koppelen**. Het interne
stap-ID `mapping` blijft gelijk; EMS en BMS hoeven hiervoor niet te veranderen.

De controles op de gekozen omvormer, actuele verplichte sensoren en aanwezige
bediening blijven bestaan. Echte dubbele meteridentiteiten of twee
batterij-omvormers zonder gekozen besturingsserienummer worden niet opgelost door
willekeurig één apparaat te besturen. Een ontbrekende of onbeschikbare SoC wordt
niet als een succesvolle installatie gemeld. Er worden in de koppelfase geen
laad-/ontlaadcommando's verstuurd en geen batterij-instellingen geschreven.

## Uitrollen op de bestaande 0.6.4-installatie

**Laat de werkende GoodWe-integratie, agent en API-key staan. Wis geen apparaten
of OneShot-voortgang.** Maak vooraf een Home Assistant-backup. Test op één
Raspberry; een gedeelde GitHub-branch kan door andere fleet-updaters worden gevolgd.

1. Stop de installer-app, niet de werkende GoodWe-agent. Publiceer de inhoud van
   `dwars-ems-homeassistant/` in de bestaande repositoryroot/ingestelde branch.
   Voeg geen extra buitenste map toe.
2. Vernieuw de appwinkel en voer de daadwerkelijke appupdate naar **0.6.5** uit.
   Alleen GitHub aanpassen en de 0.6.4-container herstarten verandert de ingebouwde
   Python-matcher niet.
3. Start de installer. De bestaande OneShot-taak met opgeslagen key wordt hervat.
   Een geblokkeerde taak wordt vanzelf opnieuw gecontroleerd. Gebruik zo nodig
   **Opnieuw controleren**. Gebruik niet **Zoek ontbrekende omvormers** voor deze
   reeds geladen omvormer.

Bij een normale hervatting van 0.6.4 op `mapping`, `agent` of `verify`, met de
bewaarde geldige payload, blijft de installer op die stap. De 0.6.4-componenten
zijn al correct en worden niet opnieuw geplaatst: **geen nieuwe scan en geen
geforceerde Home Assistant Core-herstart voor deze correctie**. De key, taak-ID,
serienummers, agentkoppelingen en herstartregistratie blijven behouden.

Ontbreekt de bewaarde payload, is deze onvolledig, of wordt vanaf een oudere
componentrelease opgewaardeerd, dan blijft de bestaande veilige vernieuwingsroute
van toepassing. Die kan componentbestanden opnieuw ophalen en Core herstarten;
zij verwijdert geen omvormerconfiguratie.

De reeds handmatig geïnstalleerde agent met dezelfde klantkey wordt hergebruikt.
Expliciete sensorkoppelingen en ingestelde vermogens-/veiligheidswaarden blijven
staan; ontbrekende of `auto`-koppelingen worden aangevuld. Wanneer opties of de
installatie-ID moeten worden aangevuld, wordt alleen de agent kort gestopt en
weer gestart. Een ongewijzigde reeds geregistreerde agent hoeft niet te herstarten.

Daarna wordt op een nieuwe, bij de installatie behorende BMS-telemetrieontvangst
gewacht. Geen ontvangst betekent niet "gereed".

## Afbakening

Dit is één volledig GitHub-pakket. **Geen nieuwe EMS/BMS-update of migratie.**
De eerder geleverde OneShot-serverupdate blijft wel een vereiste.

| Onderdeel | Versie | Gewijzigd t.o.v. 0.6.4 |
|---|---|---|
| DWARS OneShot Installer & Updater | 0.6.5 | Ja |
| GoodWe-integratie | 0.9.9.37 | Nee |
| DWARS Setup-brug | 1.2.0 | Nee |
| SolarEdge-integratie | 3.2.8 | Nee |
| GoodWe/SolarEdge/generieke agents | Bestaande versies | Nee |

Geen wijzigingen aan invertertransport, batterijsturing, prijsoptimalisatie,
SOC-reserves, netlimieten, serverconfiguratie of agentsource. De eerdere fixes
voor ontdekking, importdeadlock en handmatig herstel blijven behouden.

## Controle

Zie `ONESHOT_HERSTEL_0.6.5_TESTRAPPORT.md`,
`ONESHOT_HERSTEL_0.6.5_REPRODUCTIE.json` en
`ONESHOT_HERSTEL_0.6.5_WIJZIGINGEN.json`.
De nieuwe testfixture bevat de 194 letterlijke entiteitsidentiteiten en metingen
uit de diagnose, met vervangen serienummer, configuratie-ID, IP en MAC. De private
originele diagnose wordt niet in de GitHub-ZIP opgenomen.

De specifieke fout is met de originele en de nieuwe productiemethode
voor/na gecontroleerd. De volledige taak is lokaal getest met HTTP/WebSocket-
testservers, zowel als eerste installatie als bij een handmatig geïnstalleerde
agent. **Geen echte Home Assistant/Supervisor-runtime, fysieke Raspberry,
omvormer of ARM-Dockerbuild getest.**
