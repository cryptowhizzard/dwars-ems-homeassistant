# SMA 10SE Browser Control

Deze zip bevat twee onderdelen:

1. `addon/sma10se_control` — een Home Assistant add-on die Chromium/Selenium gebruikt om de SMA Sunny Tripower 10.0 SE webinterface te bedienen.
2. `custom_components/sma10se_browser` — een Home Assistant custom integration die een select-entity, sensors, buttons en service aanmaakt om de add-on te bedienen.

De browser-automation zet de parameter **Fallback van de batterijregeling bij uitval van het meetpunt** op:

- `off` → `Uit`
- `charge` → `Accu opladen` / `Lading van de batterij voorkeur gegeven`
- `discharge` → `Accu ontladen` / `Ontlading van de batterij voorkeur gegeven`

De add-on bewaakt `min_state_change_s`, standaard 300 seconden. Bij een nieuwe andere modus binnen die periode wordt de modus als `pending_mode` opgeslagen en later automatisch toegepast.

## Installatie

### Add-on

Kopieer deze map:

```text
addon/sma10se_control
```

naar:

```text
/config/addons/sma10se_control
```

Ga daarna in Home Assistant naar **Instellingen → Add-ons → Add-on store → drie puntjes → Check for updates**. Open de lokale add-on **SMA 10SE Browser Battery Control**, bouw/installleer hem en configureer minimaal:

```yaml
url: "https://192.168.2.23/"
language: "Nederlands"
user_group: "Installateur"
password: "jouw_sma_wachtwoord"
port: 8099
min_state_change_s: 300
```

Start daarna de add-on.

Test vanaf SSH:

```bash
curl -s http://172.30.32.1:8099/status | jq
curl -s -X POST http://172.30.32.1:8099/set_mode -H 'Content-Type: application/json' -d '{"mode":"charge"}' | jq
```

Gebruik `off`, `charge` of `discharge`.

### Custom integration

Kopieer deze map:

```text
custom_components/sma10se_browser
```

naar:

```text
/config/custom_components/sma10se_browser
```

Herstart Home Assistant.

Ga naar **Instellingen → Apparaten & diensten → Integratie toevoegen → SMA 10SE Browser Control**.

Gebruik als API URL meestal:

```text
http://172.30.32.1:8099
```

Als dat niet werkt, probeer:

```text
http://homeassistant.local:8099
```

## EMS-aansturing

Via de custom integration kun je vanuit je EMS of scripts deze service aanroepen:

```yaml
service: sma10se_browser.set_mode
data:
  mode: charge
```

Of:

```yaml
service: sma10se_browser.set_mode
data:
  mode: discharge
```

Of:

```yaml
service: sma10se_browser.set_mode
data:
  mode: off
```

De 5-minuten blokkade zit in de add-on, niet in de Home Assistant automations.

## Debug

De add-on maakt screenshots in `/tmp` in de add-on-container met namen zoals:

```text
/tmp/sma10se_01_start.png
/tmp/sma10se_02_login_password_filled.png
...
```

Bij fouten zie je de details in de add-on logs.

## Belangrijke aannames

Deze module is gemaakt op basis van de meegestuurde screenshots. De SMA frontend kan per firmwareversie net andere labels gebruiken. Daarom zoekt het script op meerdere labels en opties, maar mogelijk moet bij een afwijkende firmware de doelparametertekst of optie-labels worden aangepast in `sma10se_browser.py`.
