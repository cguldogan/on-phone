#!/bin/zsh
for name in whisper kokoro; do
  launchctl bootout gui/$(id -u)/com.agentphone.$name 2>/dev/null || true
  rm -f ~/Library/LaunchAgents/com.agentphone.$name.plist
done
echo "removed"
