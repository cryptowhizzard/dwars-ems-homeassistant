ARG BUILD_ARCH=aarch64
ARG BUILD_VERSION=0.1.0
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.19

ARG BUILD_ARCH
ARG BUILD_VERSION

LABEL   io.hass.version="${BUILD_VERSION}"   io.hass.type="app"   io.hass.arch="${BUILD_ARCH}"   org.opencontainers.image.title="DWARS Generic EMS Add-on"   org.opencontainers.image.description="Generic DWARS EMS bridge for inverter control through Home Assistant entities."   org.opencontainers.image.source="https://github.com/cryptowhizzard/dwars-ems-homeassistant"   org.opencontainers.image.version="${BUILD_VERSION}"

RUN apk add --no-cache     bash     python3     py3-requests     jq     ca-certificates   && update-ca-certificates

WORKDIR /app
COPY run.sh /app/run.sh
COPY dwars_agent.py /app/dwars_agent.py
COPY config.json /app/config.json
RUN chmod +x /app/run.sh

CMD [ "/app/run.sh" ]
