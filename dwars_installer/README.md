# DWARS Installer & Updater

Installeert en update de DWARS Home Assistant custom components en inverter agents.

## Belangrijkste opties

- `inverter_type`: `goodwe`, `solaredge`, `both` of `andere_omvormer`.
  - `goodwe`: installeert GoodWe custom component + GoodWe Agent.
  - `solaredge`: installeert SolarEdge Modbus Multi custom component + SolarEdge Agent.
  - `both`: installeert GoodWe + SolarEdge.
  - `andere_omvormer`: installeert alleen de generieke `dwars_addon`; er worden geen GoodWe/SolarEdge custom components geïnstalleerd.
- `install_custom_components`: kopieert de gekozen custom components naar `/config/custom_components`.
- `install_agent_addons`: installeert de gekozen agent add-ons uit dezelfde repo.
- `configure_agent_addons`: zet de agent opties vanuit de installer-configuratie.
- `start_agent_addons`: standaard `false`, omdat de Home Assistant entities pas bestaan nadat de gebruikte inverter-integratie is toegevoegd en geladen.
- `enable_addon_auto_update`: zet agents en installer op `auto_update: true`.
- `restart_homeassistant_after_custom_component`: herstart Home Assistant Core als custom components zijn gewijzigd.
- `auto_update_from_github`: downloadt periodiek `github_repo_zip_url` en gebruikt die versie van `custom_components` als update-bron.

## Andere omvormer

Kies `inverter_type=andere_omvormer` als je geen GoodWe of SolarEdge Modbus Multi wilt installeren.

De installer installeert dan alleen `DWARS Generic EMS Add-on`. In die add-on configureer je zelf:

- welke Home Assistant select-entity de omvormer-modus bestuurt;
- welke number-entity optioneel het laad-/ontlaadvermogen bestuurt;
- welke select-optie hoort bij `idle`, `charge` en `discharge`;
- welke DWARS/server-mode nummers horen bij `idle`, `charge` en `discharge`.

Gebruik voor idle meestal de self-use/auto modus van je inverter, dus de modus waarbij PV eerst het huis ondersteunt en de batterij volgens de inverterlogica meedraait.

## Updategedrag

De installer draait als service. Bij start vergelijkt hij de payload met `/config/custom_components`. Alleen als er verschil is, kopieert hij de nieuwe componenten en herstart hij Home Assistant Core.

Laat `auto_update_from_github` standaard `false` als je alleen via Home Assistant add-on versies wilt updaten. Zet deze op `true` als je wilt dat wijzigingen op de GitHub branch automatisch worden opgehaald zonder eerst de installer add-on te verhogen. De installer vergelijkt checksums en herstart Home Assistant Core alleen bij echte componentwijzigingen.

Voor `inverter_type=andere_omvormer` zijn er geen custom components; de generieke DWARS add-on zelf wordt via Home Assistant add-on updates bijgewerkt en opnieuw gestart.
