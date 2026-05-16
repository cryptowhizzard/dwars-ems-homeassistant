# Gotion EMS koppeling

Na installatie van de custom integration kun je vanuit `gotion_agent.py` de SMA 10SE aansturen via Home Assistant service calls.

## Service calls

```yaml
service: sma10se_browser.set_mode
data:
  mode: charge
```

```yaml
service: sma10se_browser.set_mode
data:
  mode: discharge
```

```yaml
service: sma10se_browser.set_mode
data:
  mode: off
```

## In Python via bestaande `ha_call_service`

Gebruik in `gotion_agent.py` bijvoorbeeld:

```python
def set_sma10se_mode(mode: str) -> bool:
    return ha_call_service("sma10se_browser", "set_mode", {"mode": mode})
```

Mapping vanuit EMS-logica:

```python
if local_mode == MODE_CHARGE:
    set_sma10se_mode("charge")
elif local_mode == MODE_DISCHARGE:
    set_sma10se_mode("discharge")
else:
    set_sma10se_mode("off")
```

De add-on zelf bewaakt `min_state_change_s=300`. Je mag dus vaker dezelfde state sturen; dezelfde state is een no-op en een andere state binnen 5 minuten wordt pending gezet.

## Zonder custom integration

Gebruik de REST API van de add-on direct:

```python
def set_sma10se_mode_direct(mode: str) -> None:
    requests.post(
        "http://172.30.32.1:8099/set_mode",
        json={"mode": mode},
        timeout=10,
    ).raise_for_status()
```
