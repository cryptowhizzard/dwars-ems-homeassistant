# DWARS Installer

Deze add-on installeert de GoodWe custom component naar `/config/custom_components/goodwe` en kan de `goodwe_agent` add-on uit dezelfde repository installeren en configureren.

## Gebruik

1. Voeg deze GitHub repository toe aan de Home Assistant Add-on Store.
2. Installeer `DWARS Installer`.
3. Vul minimaal de GoodWe Agent API-opties in als je de agent direct wilt configureren.
4. Start de installer één keer.
5. Home Assistant Core wordt herstart als `restart_homeassistant_after_custom_component` aan staat.

De GoodWe Agent wordt standaard wel geïnstalleerd/geconfigureerd, maar niet gestart. Zet `start_goodwe_agent_addon` op `true` als je hem direct wilt starten.
