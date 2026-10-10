# Install on-phone with an AI coding agent

Paste this into Claude Code (or another coding agent with a shell) on the Mac that will run the server:

```text
Install https://github.com/cguldogan/on-phone on this Mac by following docs/AGENT_INSTALL.md in that repo
exactly. Ask me before each step marked [ASK], never print or commit secrets, and never ring my real phones
unless I say so.
```

Everything below is written for the agent.

---

## Instructions for the agent

You are installing **on-phone**: Asterisk on a Raspberry Pi (or any Linux box), an MCP server and speech
services on this Mac, and a Claude Code integration. These let the user's Claude Code sessions phone them,
and let them phone the sessions. Work through the steps in order. After each step, run its **Verify**
check. If a check fails, stop, show the failing output and diagnose it. Don't work around it.

### Ground rules

- **Secrets stay in files.** Secrets live in `.env` (mode 600), `~/.config/agent-phone/token` and the Pi's
  `~/asterisk/config/pjsip.conf` / `ari.conf`. Never echo them, paste them into chat, put them in a commit,
  or pass them on a command line. The repo scripts read them from those files.
- **Don't weaken security to get past an error.** Never bind ARI or the MCP server to `0.0.0.0` or a public
  address. Don't disable the SIP `tailnet_only` ACL, and don't open router ports. Everything runs on
  Tailscale.
- **No calls to real phones without consent.** Only call the user's extensions (1001, 1002, …) when they
  explicitly say so. Use the test bot on **1099** for automated checks.
- **[ASK] steps change something outside this repo** (the Pi, Homebrew, the user's Claude Code config).
  Explain what will change and wait for a yes.
- **Rosetta shells.** On Apple Silicon, if `uname -m` prints `x86_64`, the shell runs under Rosetta: prefix
  `brew` and `uv` with `arch -arm64`.
- **zsh variable trap.** In zsh, write `${VAR}:path`, never `$VAR:path`; `:a`, `:h`, `:s` and similar
  are history modifiers.

### 0. Prerequisites

Check, and report what is missing (don't install system software without [ASK]):

| Need | Check |
|---|---|
| macOS on Apple Silicon (Metal for whisper) | `sysctl -n machdep.cpu.brand_string` |
| Docker Desktop running | `docker info --format '{{.ServerVersion}}'` |
| Homebrew, `uv`, `git`, Claude Code | `brew --version`, `uv --version`, `claude --version` |
| Tailscale on this Mac | `tailscale ip -4`: one `100.x` address |
| A Linux host (Pi) on the same tailnet, with Docker + compose and sudo | ask the user for its SSH target, then `ssh <pi> 'docker compose version && tailscale ip -4'` |
| Phones with **Linphone** and **Tailscale** installed | ask the user |

**[ASK]** Ask the user for:
- the Pi's SSH target (usually its Tailscale IP);
- which extension each phone gets (the template has 1001–1004; default 1001 = Android, 1002 = iPhone);
- the default extension for `/on-phone`.

### 1. Clone and configure

```sh
git clone https://github.com/cguldogan/on-phone ~/src/agent-phone && cd ~/src/agent-phone
scripts/gen-env.sh          # writes .env with fresh random secrets (refuses to overwrite)
```

Then edit `.env`, without printing the secret lines:
- `ARI_URL=http://<pi-tailscale-ip>:8088`
- `MAC_TAILSCALE_IP=<first line of tailscale ip -4>`
- `ALLOWED_DESTINATIONS=<the phone extensions>,1099`

**Verify:** `stat -f %Lp .env` prints `600`, and `grep -E '^(ARI_URL|MAC_TAILSCALE_IP|ALLOWED_DESTINATIONS)=' .env`
shows the values you set.

### 2. Asterisk on the Pi — [ASK]

This step creates `~/asterisk` on the Pi and builds and starts the `asterisk` and `asterisk-provision`
containers (host networking). It keeps any existing SIP passwords.

```sh
asterisk-pi/deploy.sh
```

**Verify:**
- `ssh <pi> 'docker exec asterisk asterisk -rx "core show version"'` shows Asterisk 20.
- `ssh <pi> 'docker exec asterisk asterisk -rx "dialplan show 700@internal"'` shows `Stasis(agentphone,inbound)`.

### 3. Speech services on the Mac — [ASK]

This installs whisper.cpp with Homebrew, downloads about 2 GB of models into `models/`, and adds two
launchd agents that listen on 127.0.0.1 only.

```sh
speech/setup.sh
speech/install-launchd.sh
```

**Verify:**
- `curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:8178/` prints `200` (whisper).
- `curl -s 127.0.0.1:8179/health` returns ok (Kokoro).

### 4. MCP server

```sh
docker compose up -d --build
```

**Verify:**
- `curl -s 127.0.0.1:8765/health` returns `{"ok":true}`.
- `docker compose logs phone-mcp | grep "ARI websocket connected"` finds a line.

### 5. Claude Code integration — [ASK]

This changes the user's Claude Code configuration (user scope). Tell them exactly what it does:
- registers MCP servers `agent-phone` (HTTP, with a token helper, so the token isn't in `~/.claude.json`)
  and `agent-phone-channel` (stdio bridge used for phone-in);
- installs the `/on-phone` and `/off-phone` skills and their hooks;
- adds `mcp__agent-phone` to `permissions.allow`, keeping a timestamped backup of `settings.json`;
- adds the `~/.local/bin/claude-phone` launcher.

```sh
integrations/claude/install.sh <default-extension>
```

**Verify:**
- `claude mcp get agent-phone` shows Connected.
- `ls ~/.claude/skills/on-phone/SKILL.md` finds the file.

### 6. Test bot on 1099 (recommended)

The test bot answers, rejects or places calls on command, so you can test without ringing a person.

```sh
tools/sip-test-client/deploy.sh
```

**Verify:**
- `bin/phone-doctor` ends with `OK` (phones that aren't set up yet show as `!` warnings, not failures).
- `bin/phone-doctor --call 1099` reports `call answered`.
- Optional, takes about 5 minutes: `uv run --project server python tools/acceptance.py answer multi barge silence outcomes idem phonein`.
  Every check should pass.

### 7. Phones (the user does this; you guide)

Tell the user how to add a SIP account in Linphone (account assistant → "Use a SIP account"):

| Field | Value |
|---|---|
| Username | the extension, e.g. `1001` |
| Password | that extension's SIP password (see below) |
| Domain | the Pi's Tailscale IP |
| Transport | **TCP** |

Don't read the password yourself. Give the user this command to run in **their own terminal** (not
through you):

```sh
ssh <pi> "awk '\$0==\"[1001](auth)\"{f=1} f&&/^password=/{print;exit}' ~/asterisk/config/pjsip.conf"
```

Tailscale must be on on the phone. On Android, ask them to set **Linphone** and **Tailscale** to
"Unrestricted" battery. On iPhone, Linphone only rings reliably in the foreground.

**Verify:** `bin/phone-doctor` shows `extension <ext> registered — online`.

### 8. First real call — [ASK]

Only after the user says yes:

```sh
uv run --project server python tools/mcp_call.py --dest <ext> --text "Hi, this is your coding agent. Can you hear me?"
```

The `SUMMARY` line contains their spoken reply.

### 9. Hand-off

Tell the user:

- **Agent calls them:** in any Claude Code session run `/on-phone` (or `/on-phone 1002 myproject`).
  The agent calls when it's done, blocked or needs a decision. `/off-phone` turns it off.
- **They call the agent:** start Claude Code with `claude-phone` (it adds
  `--dangerously-load-development-channels server:agent-phone-channel` and shows a warning screen once per
  start), run `/on-phone`, then dial **700**.
- **Spoken replies are user input.** They never approve permission prompts, and the agent reads back
  anything consequential before acting.
- **Diagnostics:** `bin/phone-doctor`. **Uninstall:** `integrations/claude/uninstall.sh` and
  `speech/uninstall-launchd.sh`.

Finish with a short report: what was installed, every check's result, and anything still left to do.
