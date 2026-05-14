# DWARS Installer & Updater

Installeert en update de DWARS Home Assistant custom components en inverter agents.

## Belangrijkste opties

- `inverter_type`: `goodwe`, `solaredge` of `both`.
- `install_custom_components`: kopieert de gekozen custom components naar `/config/custom_components`.
- `install_agent_addons`: installeert de gekozen agent add-ons uit dezelfde repo.
- `configure_agent_addons`: zet de agent opties vanuit de installer-configuratie.
- `start_agent_addons`: standaard `false`, omdat de Home Assistant entities pas bestaan nadat de custom integration is toegevoegd en geladen.
- `enable_addon_auto_update`: zet agents en installer op `auto_update: true`.
- `restart_homeassistant_after_custom_component`: herstart Home Assistant Core als custom components zijn gewijzigd.
- `auto_update_from_github`: downloadt periodiek `github_repo_zip_url` en gebruikt die versie van `custom_components` als update-bron.

## Updategedrag

De installer draait als service. Bij start vergelijkt hij de payload met `/config/custom_components`. Alleen als er verschil is, kopieert hij de nieuwe componenten en herstart hij Home Assistant Core.

Laat `auto_update_from_github` standaard `false` als je alleen via Home Assistant add-on versies wilt updaten. Zet deze op `true` als je wilt dat wijzigingen op de GitHub branch automatisch worden opgehaald zonder eerst de installer add-on te verhogen. De installer vergelijkt checksums en herstart Home Assistant Core alleen bij echte componentwijzigingen.
