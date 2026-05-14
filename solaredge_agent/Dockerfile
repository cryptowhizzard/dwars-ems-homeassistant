ARG BUILD_ARCH=aarch64
ARG BUILD_VERSION=3.4.3
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.19

ARG BUILD_VERSION

# Minimal deps: bash, curl and jq for HA/API calls.
RUN apk add --no-cache bash curl jq

WORKDIR /app
COPY run.sh /run.sh
COPY se-agent-bms.sh /app/se-agent-bms.sh
COPY config.json /app/config.json
RUN chmod +x /run.sh /app/se-agent-bms.sh

CMD ["/run.sh"]
