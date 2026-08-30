ARG BUILD_ARCH=aarch64
ARG BUILD_VERSION=0.2.1
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.19

ARG BUILD_ARCH
ARG BUILD_VERSION

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}" \
  org.opencontainers.image.title="GoodWe Legacy Options Migrator" \
  org.opencontainers.image.description="One-shot reset en migratie voor GoodWe Agent versies lager dan 1.7." \
  org.opencontainers.image.source="https://github.com/cryptowhizzard/dwars-ems-homeassistant" \
  org.opencontainers.image.version="${BUILD_VERSION}"

RUN apk add --no-cache bash curl jq ca-certificates \
  && update-ca-certificates

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
