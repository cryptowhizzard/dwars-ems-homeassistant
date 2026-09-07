ARG BUILD_ARCH=aarch64
ARG BUILD_VERSION=0.5.3
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.19

ARG BUILD_ARCH
ARG BUILD_VERSION

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}" \
  org.opencontainers.image.title="DWARS Installer & Fleet Updater" \
  org.opencontainers.image.description="Installs DWARS components and performs resumable full Home Assistant updates." \
  org.opencontainers.image.source="https://github.com/cryptowhizzard/dwars-ems-homeassistant" \
  org.opencontainers.image.version="${BUILD_VERSION}"

RUN apk add --no-cache \
    bash \
    curl \
    jq \
    unzip \
    coreutils \
    ca-certificates \
    python3 \
    tzdata \
    util-linux \
  && update-ca-certificates

WORKDIR /app
COPY run.sh /app/run.sh
COPY auto_updater.py /app/auto_updater.py
COPY core_recovery.py /app/core_recovery.py
RUN mkdir -p /app/payload \
  && chmod +x /app/run.sh /app/auto_updater.py

CMD [ "/app/run.sh" ]
