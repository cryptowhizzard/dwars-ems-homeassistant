FROM ghcr.io/home-assistant/amd64-base:latest

RUN apk add --no-cache python3 py3-pip

COPY run.sh /run.sh
COPY gotion_agent.py /gotion_agent.py

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
