#!/bin/zsh
# Install the agent-phone integration for Claude Code (user scope). Idempotent.
#   ./install.sh [default-extension]      (default 1001)
set -euo pipefail
HERE=${0:A:h}; ROOT=${HERE:h:h}
EXT=${1:-1001}
CFG=~/.config/agent-phone; BIN=$CFG/bin
mkdir -p $BIN && chmod 700 $CFG
cp $HERE/bin/agent-phone-hook $HERE/bin/agent-phone-headers $HERE/bin/agent-phone-channel $HERE/bin/claude-phone $BIN/ \
  && chmod 755 $BIN/*
mkdir -p ~/.local/bin && ln -sf $BIN/claude-phone ~/.local/bin/claude-phone
TOKEN=$(grep '^MCP_BEARER_TOKEN=' $ROOT/.env | cut -d= -f2-)
( umask 077; print -r -- $TOKEN > $CFG/token )

for skill in on-phone off-phone; do
  mkdir -p ~/.claude/skills/$skill
  sed -e "s|__BIN__|$BIN|g" -e "s|__DEFAULT_EXT__|$EXT|g" $HERE/skills/$skill/SKILL.md > ~/.claude/skills/$skill/SKILL.md
done

claude mcp remove -s user agent-phone >/dev/null 2>&1 || true
claude mcp add-json -s user agent-phone \
  "{\"type\":\"http\",\"url\":\"http://127.0.0.1:8765/mcp\",\"headersHelper\":\"$BIN/agent-phone-headers\",\"timeout\":120000}"

# Phone-in bridge: a stdio channel server; it only listens in sessions started with `claude-phone`.
claude mcp remove -s user agent-phone-channel >/dev/null 2>&1 || true
claude mcp add -s user agent-phone-channel -- /usr/bin/python3 $BIN/agent-phone-channel

# Pre-approve the phone tools so a call is never stuck behind a permission prompt while you're away.
python3 - <<PY
import json, os, shutil, time
p = os.path.expanduser("~/.claude/settings.json")
d = json.load(open(p)) if os.path.exists(p) else {}
rule = "mcp__agent-phone"
allow = d.setdefault("permissions", {}).setdefault("allow", [])
if rule not in allow:
    if os.path.exists(p):
        shutil.copy(p, p + f".bak-agent-phone-{int(time.time())}")
    allow.append(rule)
    json.dump(d, open(p, "w"), indent=2)
    print("added permission allow rule:", rule)
else:
    print("permission rule already present")
PY
echo "installed: /on-phone and /off-phone skills, MCP servers 'agent-phone' + 'agent-phone-channel' (user scope),"
echo "hooks in $BIN, launcher ~/.local/bin/claude-phone (start sessions you want to phone in to with it)"
