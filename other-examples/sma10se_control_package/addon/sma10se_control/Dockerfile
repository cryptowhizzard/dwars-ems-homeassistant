ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache \
    bash \
    curl \
    jq \
    chromium \
    chromium-chromedriver \
    xvfb \
    font-noto \
    ttf-freefont \
    py3-pip

RUN python3 -m pip install --no-cache-dir --break-system-packages selenium==4.27.0

COPY run.sh /root/run.sh
COPY app.py /root/app.py
COPY sma10se_browser.py /root/sma10se_browser.py

RUN chmod +x /root/run.sh /root/app.py /root/sma10se_browser.py

CMD ["/root/run.sh"]
