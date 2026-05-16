DOMAIN = "sma10se_browser"
PLATFORMS = ["select", "sensor", "button"]

CONF_API_URL = "api_url"
CONF_API_TOKEN = "api_token"
CONF_NAME = "name"

DEFAULT_NAME = "SMA 10SE"
DEFAULT_API_URL = "http://172.30.32.1:8099"

MODE_OFF = "off"
MODE_CHARGE = "charge"
MODE_DISCHARGE = "discharge"
VALID_MODES = [MODE_OFF, MODE_CHARGE, MODE_DISCHARGE]
MODE_LABELS = {
    MODE_OFF: "Uit / Standby",
    MODE_CHARGE: "Accu opladen",
    MODE_DISCHARGE: "Accu ontladen",
}
