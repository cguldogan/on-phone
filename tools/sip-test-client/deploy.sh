#!/bin/zsh
# Copy the 1099 test bot to the Pi and (re)start it. Needs asterisk-pi/deploy.sh to have run first.
#   tools/sip-test-client/deploy.sh [pi-host]   (default: the host in ARI_URL from .env)
set -euo pipefail
HERE=${0:A:h}; ROOT=${HERE:h:h}
PI=${1:-$(grep '^ARI_URL=' $ROOT/.env | sed -E 's#^ARI_URL=https?://([^:/]+).*#\1#')}
PW=$(grep '^TEST_EXT_PASSWORD=' $ROOT/.env | cut -d= -f2- | tr -d "'")
ssh $PI 'mkdir -p ~/sip-test-client'
scp -q $HERE/bot.py $HERE/Dockerfile $HERE/run.sh $HERE/*.wav ${PI}:sip-test-client/
# The password goes over stdin, never on a command line.
print -r -- "$PW" | ssh $PI 'cd ~/sip-test-client && BOT_PASSWORD=$(cat) sh ./run.sh'
