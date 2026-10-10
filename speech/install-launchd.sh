#!/bin/zsh
# Install/refresh launchd agents for whisper-server (:8178) and Kokoro (:8179), both bound to 127.0.0.1.
set -euo pipefail
ROOT=${0:A:h:h}
LA=~/Library/LaunchAgents; LOGS=~/Library/Logs/agent-phone
mkdir -p $LA $LOGS
for name in whisper kokoro; do
  sed -e "s|__ROOT__|$ROOT|g" -e "s|__LOGS__|$LOGS|g" $ROOT/speech/launchd/com.agentphone.$name.plist > $LA/com.agentphone.$name.plist
  launchctl bootout gui/$(id -u)/com.agentphone.$name 2>/dev/null || true
  launchctl bootstrap gui/$(id -u) $LA/com.agentphone.$name.plist
done
for i in {1..60}; do
  curl -sf -o /dev/null 127.0.0.1:8178/ && curl -sf -o /dev/null 127.0.0.1:8179/health && { echo "whisper + kokoro up"; exit 0; }
  sleep 1
done
echo "services did not come up; see $LOGS" >&2; exit 1
