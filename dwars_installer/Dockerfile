ARG BUILD_ARCH=aarch64
ARG BUILD_VERSION=0.4.0
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.19

ARG BUILD_ARCH
ARG BUILD_VERSION

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}" \
  org.opencontainers.image.title="DWARS Installer" \
  org.opencontainers.image.description="Installs and updates DWARS, GoodWe, SolarEdge and generic inverter Home Assistant components." \
  org.opencontainers.image.source="https://github.com/cryptowhizzard/dwars-ems-homeassistant" \
  org.opencontainers.image.version="${BUILD_VERSION}"

RUN apk add --no-cache \
    bash \
    curl \
    jq \
    python3 \
    unzip \
    coreutils \
    ca-certificates \
  && update-ca-certificates

WORKDIR /app
COPY run.sh /app/run.sh
COPY system_updater.py /app/system_updater.py
RUN mkdir -p /app/payload
RUN chmod +x /app/run.sh /app/system_updater.py

CMD [ "/app/run.sh" ]
