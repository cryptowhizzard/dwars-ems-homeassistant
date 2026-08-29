FROM ghcr.io/home-assistant/base:latest

RUN apk add --no-cache jq curl

COPY run.sh /run.sh
COPY default_goodwe_options.json /app/default_goodwe_options.json
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
