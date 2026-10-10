#!/bin/zsh
set -u
claude mcp remove -s user agent-phone 2>/dev/null
claude mcp remove -s user agent-phone-channel 2>/dev/null
rm -f ~/.local/bin/claude-phone
rm -rf ~/.claude/skills/on-phone ~/.claude/skills/off-phone ~/.config/agent-phone
python3 - <<PY
import json, os
p = os.path.expanduser("~/.claude/settings.json")
if os.path.exists(p):
    d = json.load(open(p)); a = d.get("permissions", {}).get("allow", [])
    if "mcp__agent-phone" in a:
        a.remove("mcp__agent-phone"); json.dump(d, open(p, "w"), indent=2)
PY
echo "agent-phone Claude Code integration removed"
