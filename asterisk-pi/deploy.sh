#!/bin/zsh
# Deploy/refresh the Asterisk config on the Pi. Idempotent; preserves existing SIP and ARI passwords.
#   asterisk-pi/deploy.sh [pi-host]      (default: the host in ARI_URL from .env, i.e. the Pi's Tailscale IP;
#                                         needs ssh access and Docker on the Pi)
set -euo pipefail
HERE=${0:A:h}; ROOT=${HERE:h}
PI_IP=$(grep '^ARI_URL=' $ROOT/.env | sed -E 's#^ARI_URL=https?://([^:/]+).*#\1#')
[[ $PI_IP == 100.* ]] || { echo "ARI_URL in .env must point at the Pi's Tailscale IP" >&2; exit 1; }
PI=${1:-$PI_IP}
TMP=$(mktemp -d); trap 'rm -rf $TMP' EXIT
cp -R $HERE/config $TMP/config
cp $HERE/docker-compose.yml $TMP/
# The Pi's Tailscale IP: ARI/provisioning bind to it only.
sed -i '' "s|__PI_IP__|$PI_IP|g" $TMP/config/http.conf $TMP/config/entrypoint.sh $TMP/docker-compose.yml
gen() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20; }
live=$(ssh $PI 'cat ~/asterisk/config/pjsip.conf 2>/dev/null' || true)
for ext in 1001 1002 1003 1004 1099; do
  pw=$(print -r -- "$live" | awk -v e="[$ext](auth)" '$0==e{f=1} f&&/^password=/{sub("password=","");print;exit}')
  [[ -n $pw ]] || pw=$(gen)
  sed -i '' "s|__PW_${ext}__|$pw|" $TMP/config/pjsip.conf.template
done
mv $TMP/config/pjsip.conf.template $TMP/config/pjsip.conf
ARI_PASSWORD=$(grep '^ARI_PASSWORD=' $ROOT/.env | cut -d= -f2-)
sed "s|__ARI_PASSWORD__|$ARI_PASSWORD|" $TMP/config/ari.conf.template > $TMP/config/ari.conf && rm $TMP/config/ari.conf.template
ssh $PI 'mkdir -p ~/asterisk/config ~/asterisk/provision'
scp -q $HERE/Dockerfile $TMP/docker-compose.yml $PI:~/asterisk/
scp -q $TMP/config/* $PI:~/asterisk/config/
ssh $PI 'cd ~/asterisk && chmod 644 config/*.conf config/entrypoint.sh && chmod 640 config/ari.conf && sudo chgrp 101 config/ari.conf \
  && docker compose up -d --build 2>&1 | tail -2'
echo "deployed to $PI"
