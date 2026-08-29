ARG BUILD_ARCH=aarch64
ARG BUILD_VERSION=1.8.5
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.19

ARG BUILD_ARCH
ARG BUILD_VERSION

LABEL \
  io.hass.version="1.8.5" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}" \
  org.opencontainers.image.title="GoodWe Agent" \
  org.opencontainers.image.description="Bridge central server mode voor GoodWe via Home Assistant entities." \
  org.opencontainers.image.source="https://github.com/cryptowhizzard/dwars-ems-homeassistant" \
  org.opencontainers.image.version="${BUILD_VERSION}"

# Python + requests + jq + CA certs voor HTTPS en add-on options parsing.
RUN apk add --no-cache \
    bash \
    python3 \
    py3-requests \
    jq \
    ca-certificates \
  && update-ca-certificates

WORKDIR /app
COPY run.sh /app/run.sh
COPY goodwe_agent.py /app/goodwe_agent.py
COPY config.json /app/config.json
RUN chmod +x /app/run.sh

CMD [ "/app/run.sh" ]
