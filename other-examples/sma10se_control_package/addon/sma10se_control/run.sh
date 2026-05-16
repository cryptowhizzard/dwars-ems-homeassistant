#!/usr/bin/env bash
set -euo pipefail

export DISPLAY=:99
rm -f /tmp/.X99-lock || true
rm -rf /tmp/.X11-unix/X99 || true

Xvfb :99 -screen 0 2048x1152x24 >/tmp/sma10se_xvfb.log 2>&1 &
sleep 2

exec python3 /root/app.py
