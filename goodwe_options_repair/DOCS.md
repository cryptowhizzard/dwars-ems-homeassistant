# GoodWe Legacy Options Migrator

Gebruik deze one-shot app uitsluitend wanneer een geïnstalleerde GoodWe Agent
lager dan 1.7 niet kan worden bijgewerkt door incompatibele of corrupte options.

De migrator bewaart tijdelijk `api_key` en `ha_token`, reset alle persisted
GoodWe-options via Supervisor, voert de update uit en zet daarna uitsluitend de
twee credentials terug. Alle andere instellingen gebruiken de defaults van de
nieuwe GoodWe Agent.

Klik niet eerst zelf op **Update** bij GoodWe Agent. Start deze migrator één keer.
Laat `target_slug` op `auto`, of vul de volledige slug in wanneer autodetectie niet
werkt.
