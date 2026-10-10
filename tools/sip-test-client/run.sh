#!/bin/sh
# (Re)start the persistent test bot on the Pi:  BOT_PASSWORD=... ./run.sh
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
docker build -q -t local/sip-test-bot "$DIR" >/dev/null
docker rm -f sipbot >/dev/null 2>&1 || true
mkdir -p "$DIR/out"
docker run -d --name sipbot --restart unless-stopped --network host -e BOT_PASSWORD="$BOT_PASSWORD" \
  -v "$DIR":/work local/sip-test-bot >/dev/null
i=0; until curl -s 127.0.0.1:8099/state | grep -q '"registered": true'; do
  i=$((i+1)); [ $i -gt 30 ] && echo "bot did not register" >&2 && docker logs sipbot | tail -5 >&2 && exit 1; sleep 1; done
docker exec asterisk asterisk -rx "pjsip qualify 1099" >/dev/null 2>&1 || true
echo "sipbot ready"
