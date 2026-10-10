# on-phone

Lets a running **Claude Code** session phone you on a SIP extension (Linphone over Tailscale), say
something, listen to your spoken reply and get the transcript back as an MCP tool result, in the same
session. The coding agent does all the reasoning; this project only provides speech in/out and call
control. No extra LLM, no paid STT/TTS. You can also **phone the agent**: dial 700 and your instruction lands in
the running session.

![How on-phone works: the agent calls you, and you call the agent](docs/how-it-works.svg)

## Install with an AI agent

Paste this into Claude Code (or any coding agent with a shell) on the Mac:

```text
Install https://github.com/cguldogan/on-phone on this Mac by following docs/AGENT_INSTALL.md in that repo
exactly. Ask me before each step marked [ASK], never print or commit secrets, and never ring my real phones
unless I say so.
```

[docs/AGENT_INSTALL.md](docs/AGENT_INSTALL.md) is the step-by-step runbook it follows: a check after every
step, a confirmation before anything changes outside the repo, and secrets kept in files.

### Architecture

```
 Android/iPhone (Linphone)                       Raspberry Pi (Tailscale 100.x.y.z)
 ext 1001 / 1002 ──SIP/TCP+RTP over Tailscale──▶ Asterisk 20 (Docker, host net)
                                                  ├─ PJSIP 5060, RTP 10000-10999
                                                  └─ ARI :8088 (Tailscale IP only)
                                                        │  ARI REST + events (WS)
                                                        │  ExternalMedia RTP, G.722 16 kHz
 Mac (Tailscale 100.a.b.c)                              ▼
 ┌─ Docker Desktop ───────────────────────────────────────────────────────┐
 │ phone-mcp  :8765 (127.0.0.1 only)  MCP Streamable HTTP + hook routes    │
 │            RTP 40000-40019/udp (published on the Tailscale IP only)     │
 │            ARI client · RTP bridge · Silero VAD · barge-in · sessions   │
 └───────────────▲──────────────────────────┬─────────────────────────────┘
                 │ MCP (bearer token)        │ host.docker.internal
   Claude Code ──┘                           ├─▶ whisper-server :8178  (native, Metal, large-v3-turbo)
   /on-phone skill + Stop/Notification hooks └─▶ Kokoro-82M      :8179  (native, kokoro-onnx)
```

MCP is the control interface only; audio never goes through it.

## Install (exact commands)

Prerequisites: Docker Desktop, Tailscale on the Mac and phones, Homebrew, `uv`, Claude Code, and SSH access
to the Pi over Tailscale (`ssh <pi-tailscale-ip>`). Put the Pi's and the Mac's Tailscale IPs in `.env`
(`ARI_URL`, `MAC_TAILSCALE_IP`); everything else reads them from there.

```sh
git clone https://github.com/cguldogan/on-phone agent-phone && cd agent-phone
scripts/gen-env.sh                      # .env with fresh secrets (skip if .env exists)
$EDITOR .env                            # check MAC_TAILSCALE_IP, ARI_URL, ALLOWED_DESTINATIONS

asterisk-pi/deploy.sh                   # Asterisk + ARI on the Pi (keeps existing SIP passwords)
speech/setup.sh                         # whisper.cpp (brew) + models (~2 GB) + Kokoro venv
speech/install-launchd.sh               # whisper :8178 and Kokoro :8179 as launchd agents (127.0.0.1)
docker compose up -d --build            # MCP server + audio bridge
integrations/claude/install.sh 1001     # /on-phone, /off-phone, MCP registration, hooks (default ext 1001)
tools/sip-test-client/deploy.sh         # optional: the 1099 test bot on the Pi
bin/phone-doctor                        # check every hop; add `--call 1099` for a live test call
```

`integrations/claude/install.sh` changes your user-level Claude Code config:
- It registers MCP server `agent-phone` (user scope) with a `headersHelper`, so the token isn't stored in `~/.claude.json`.
- It writes the token to `~/.config/agent-phone/token` (mode 600).
- It installs `~/.claude/skills/{on-phone,off-phone}`.
- It adds `mcp__agent-phone` to `permissions.allow` in `~/.claude/settings.json`, keeping a timestamped backup, so phone tools never wait on a permission prompt while you're away.

Undo it with `integrations/claude/uninstall.sh`.

**Phones:** each phone runs Linphone with its own extension (by default 1001 for Android and 1002 for iPhone). Use transport TCP,
with the Pi's Tailscale IP as the domain; the SIP passwords are in the Pi's `~/asterisk/config/pjsip.conf`. **1099** is a
scripted test bot on the Pi for automated tests (`tools/sip-test-client/deploy.sh`); don't give it to a person.

## Use

In any Claude Code session:

```
/on-phone                 # phone mode on: calls ext 1001, project label = repo name
/on-phone 1002 billing    # explicit extension and label
... give it a task, walk away ...
/off-phone                # phone mode off (also hangs up)
```

With phone mode on, the agent:
- Calls when the task is done, when it's blocked, or when it needs a decision. It doesn't call for routine progress.
- Opens with the project name, speaks two or three sentences, and asks one question.
- Gets your reply as the `transcript` of `phone_speak_and_listen` and continues in the same context, with
  normal permissions. Spoken "yes" is not an approval bypass.
- Doesn't redial after `no_answer`, `busy`, `rejected` or `unavailable`. Silence is never consent.

### Phone in: call the agent and tell it what to do

Start the session with the phone channel, turn on phone mode, then call **extension 700** from your phone:

```
cld   (or claude-phone)      # = claude ... --dangerously-load-development-channels server:agent-phone-channel
/on-phone                    # phone mode on for 1001
... later, from Linphone: dial 700 ...
```

1. The server answers, works out who is calling from the authenticated SIP endpoint (not the caller ID),
   and finds the phone-mode sessions for that extension whose channel is connected. If several are
   listening, it asks which project.
2. "Coding agent for <project>. What would you like me to do?" Your instruction is transcribed and
   pushed into that Claude Code session as a channel message (`<channel source="agent-phone-channel"
   call_id=...>`). This works even when the session is idle at the prompt; if the session is busy, the
   message is delivered at its next turn.
3. The call stays open and is handed to the session. The agent continues it with the normal tools: it
   reads back what it understood, acts only on a clear yes, then hangs up before long work and calls
   you back with the result.
4. If nobody is listening, you hear that and the call ends. If the agent doesn't pick the call up within
   30 s (`INBOUND_CLAIM_S`), you're told it will call back, and the message stays queued in the session.

How it plugs in:
- `agent-phone-channel` is a small stdio MCP server (Python standard library only) that Claude Code starts
  for every session. It holds an SSE stream to `GET /channel/events?cli_session_id=…` open only when the
  parent `claude` process was started with it as a channel. Otherwise Claude Code would silently drop the
  messages, so the bridge stays idle and callers hear that nobody is listening.
- Channels are a Claude Code research preview. Custom channels need `--dangerously-load-development-channels`,
  which shows a warning screen at each start, and they don't work with `claude -p`.
- **Other models.** Phone-in also works when Claude Code talks to a local or other non-Anthropic model through a
  gateway (`ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`); tested with GLM-5.3 through LiteLLM. Two conditions:
  you're still signed in to claude.ai, and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` / `DISABLE_TELEMETRY` are
  not set. Claude Code checks with Anthropic whether channels are enabled, and with that traffic off it reports
  "Channels are not currently available". Prompts and code still go only to your gateway.
- Spoken instructions are user input. They never answer permission prompts. `cld` also passes
  `--dangerously-skip-permissions`, so in those sessions the agent's spoken read-back and your "yes" are
  the only check before it acts.

Phone mode is per Claude Code session. It's stored server-side (SQLite) with destination, project label,
the CLI session ID (`${CLAUDE_SESSION_ID}`) and an expiry (default 4 h, max 24 h).

### What actually drives a call (be clear about this)

| Situation | What happens |
|---|---|
| Session active, agent working | The agent calls through MCP tools whenever the skill's rules say so. This is the main mechanism. |
| Agent is about to end its turn | The skill's **Stop hook** asks the server whether phone mode is on and whether the agent talked to you this turn. If not, Claude Code continues once, with a reminder to call if there's something meaningful. It never loops (`stop_hook_active`), and the turn that ran `/on-phone` isn't nagged. |
| Agent blocked on a permission prompt | The skill's **Notification(permission_prompt) hook** has the server place a short one-way call ("…waiting for your approval at the terminal"). Rate-limited to one per 5 min per session. You still have to approve at the terminal. |
| Turn has ended, session idle | If the session was started with the phone channel, **call 700**: your instruction wakes the session (see "Phone in"). Otherwise nothing can resume it from the phone. |
| CLI exited | Phone mode stays stored until it expires. `claude --resume <id>` continues the session and the token in context still works. If the token is lost (compaction, for example), the skill re-runs `phone_enable`, which replaces the old session. |

There's no keystroke injection or MCP sampling. Phone-in uses Claude Code's channel mechanism.

Codex isn't integrated yet. The server is CLI-agnostic: a Codex skill would live in
`~/.agents/skills`, be invoked as `$on-phone`, and pass `CODEX_THREAD_ID`.

## MCP tools

Every tool except `phone_enable` takes the secret `session_token` returned by `phone_enable`. Calls are bound to the
session that made them: another session gets `call_not_found`.

| Tool | Purpose |
|---|---|
| `phone_enable(destination_extension, project_label, cli_session_id?, ttl_minutes?)` | Start phone mode, returning `session_token` |
| `phone_disable(session_token)` | Stop phone mode and hang up any open call |
| `phone_call(session_token, reason, idempotency_key, wait_seconds≤50)` | Ring and wait for answered, busy, rejected, unavailable or no_answer |
| `phone_status(session_token, call_id?, wait_seconds≤50)` | Session or call state; can wait for the next state change |
| `phone_speak(session_token, call_id, text≤600, idempotency_key)` | Speak, returning on completion or barge-in |
| `phone_listen(session_token, call_id, timeout_seconds≤50, after_seq?)` | Next reply; `after_seq` cursor makes retries lossless |
| `phone_speak_and_listen(…, text, idempotency_key, timeout_seconds)` | One conversational turn |
| `phone_hangup(session_token, call_id, goodbye_text?)` | Say goodbye and hang up |

Results include:
- state and outcome (`answered`, `busy`, `rejected`, `unavailable`, `no_answer`, `disconnected`, `hung_up`, `idle_timeout`, `max_duration`, `session_ended`, `failed`), plus `retryable`, the Q.850 cause, and timings
- the transcript, with what whisper.cpp actually reports: `no_speech_prob`, `avg_logprob`, word probabilities, and STT seconds. No synthetic confidence numbers.
- VAD probabilities, `interrupted` / `barge_in` flags, and RTP counters

**Idempotency:** a retry with the same key awaits the original operation. It never dials twice or repeats speech, and
reusing a key with different arguments is an error. Every wait is bounded at 50 s, under Claude Code's HTTP
per-request timer; the server is registered with a 120 s tool timeout. Calls to the same extension are
serialized: a second session gets `destination_in_use` (retryable).

## Telephone behaviour

- **Allowed destinations:** only `ALLOWED_DESTINATIONS`. No PSTN, and no free-form numbers.
- **Caller ID:** the agent calls as `"Coding Agent" <700>` and never impersonates you. Dialing 700 from a phone just plays a notice.
- **Timeouts:** ring 30 s, idle 180 s (no speak/listen activity), max call 900 s. All configurable.
- **Pickup:** like a person, the agent waits at least 0.8 s after answer, and if you say "hello?" it lets you finish (up to 3 s) before speaking. A pickup greeting is reported in `heard_during_prompt`, not taken as the answer.
- **Endpointing:** Silero VAD (v6, ONNX) with 300 ms pre-roll and 900 ms of end-of-utterance silence. If the far end stops sending packets (DTX or silence suppression), a gap filler supplies silence, so endpointing still fires.
- **Barge-in:** 600 ms of caller speech (a quick "hello?" on pickup doesn't count) while the agent is talking clears queued playback. The reply comes back with `interrupted: true`.
- **Echo guard:** speech that starts during playback and never qualifies as barge-in is dropped. So are transcripts that fuzzy-match the agent's own recent words.
- **Hallucination guard:** empty or bracketed output is dropped, as are known Whisper filler phrases ("Thank you.", …) unless the decoder shows strong evidence.
- **Cleanup:** on hangup, error or session end, the server releases the RTP port, the per-destination lock, the ARI bridge and the media channel, and cancels tasks. Orphaned `ap-*` channels are removed when ARI reconnects.

### Media path detail (why G.722)

Asterisk 20.11's `chan_rtp` (ExternalMedia) has no receive payload mapping, and Asterisk only falls back to the
static payload table for PT ≤ 34. So **slin16 (PT 118) is one-way**: you would hear nothing from the agent.
G.722 (static PT 9) is 16 kHz wideband in both directions and is the default (`MEDIA_CODEC`). The bridge encodes
and decodes it with `g722` (libg722).

### Networking

- Phone ↔ Asterisk: Tailscale only. A PJSIP ACL allows 100.64.0.0/10 and localhost.
- Asterisk ↔ bridge: RTP over Tailscale to the Mac's Tailscale IP. Docker Desktop publishes 40000–40019/udp on that IP only.
  Docker Desktop rewrites the source port of the bridge's outgoing RTP. Asterisk's strict RTP handles that (verified with `strictrtp=yes`).
- Bridge ↔ speech: `host.docker.internal` to services bound to 127.0.0.1 on the Mac.
- MCP: 127.0.0.1:8765 with a bearer token, constant-time compare, and DNS-rebinding protection. ARI is on the Pi's
  Tailscale IP with its own password. AMI stays disabled. No router port forwarding.

## Privacy

- Audio isn't recorded and transcripts aren't persisted by default. Transcripts live only in memory with the call (calls are pruned 1 h after ending) and in the idempotency cache (1 h).
- `DEBUG_AUDIO_DIR` (off by default) saves per-utterance WAV and JSON files, deleted after `DEBUG_RETENTION_HOURS`.
- Tokens and passwords are redacted from logs (filter plus no access log). Session tokens are stored only as SHA-256 hashes.
- The phone server has no shell, file or repository tools.

## Tests

```sh
cd server && uv run pytest              # 34 unit/integration tests (fake Asterisk with real UDP RTP; real MCP client)
cd .. && uv run --project server python tools/acceptance.py   # live: Pi + Docker + native speech, calls 1099 only
```

The live suite uses the 1099 test bot (`tools/sip-test-client/bot.py`, pjsua2, controlled over local HTTP on the Pi).
It never rings 1001 or 1002.

## Acceptance status

| # | Test | Status |
|---|---|---|
| 1 | Linphone registers over Tailscale | ✔ seen live (1001 over TCP from its Tailscale IP); re-check with `phone-doctor` |
| 2 | 1001 ↔ 1002 two-way audio | ✔ verified by hand with two Linphone phones |
| 3 | MCP call rings the extension as 700 | ✔ live on 1099; caller-ID display on a real phone is a manual check |
| 4 | Supplied text audible | ✔ far-end recording transcribed back verbatim |
| 5 | Spoken reply returned as a tool result | ✔ |
| 6 | Same coding session acts on it | ✔ `claude -p` + `--resume` (same session_id) wrote NOTES.md and, after "Yes, and include screenshots", added the section |
| 7 | Several turns in one call | ✔ |
| 8 | No-answer, busy, rejected, offline, remote hangup | ✔ each gives a clean outcome; no redial; no leftover channels |
| 9 | Retries don't duplicate calls | ✔ one Asterisk channel for two concurrent retries |
| 10 | Sessions can't read each other's feedback | ✔ |
| 11 | Disable / expiry | ✔ disable live; expiry sweep in tests |
| 12 | CLI-specific activation | ✔ `/on-phone` skill, `${CLAUDE_SESSION_ID}` binding, skill Stop hook fired in a real session |
| 13 | Phone in to a running session | ✔ live: test bot dials 700. In a real interactive session (`--dangerously-load-development-channels`), the idle agent got the instruction, read it back on the same call, acted after "yes, go ahead", and reported back. Without a listener, the caller is told. Scripted as `acceptance.py phonein`. |

**Still manual (needs you and a real phone):**
1. Take a real agent call on 1001 and talk back for a few turns.
2. Barge-in with Linphone's own echo canceller.
3. Check the caller-ID display.
4. Check behaviour on mobile data.
5. Phone in from 1001: start with `cld`, run `/on-phone`, dial 700.

## Measurements (measured on this hardware, Oct 2026)

| Metric | Result |
|---|---|
| Call setup, ARI originate → answered (bot auto-answers) | 0.02–0.11 s. A human picking up adds their own ring time. |
| Media ready after answer | 0.05–0.14 s |
| TTS time to first audio (first sentence) | median 0.24 s (0.18–0.27) |
| Kokoro synthesis of a 5.3 s sentence | native 0.72 s · Docker CPU profile 0.93 s |
| STT (whisper large-v3-turbo, Metal) for a ~2 s reply | median 0.92 s (0.90–1.08) |
| End of speech → transcript in tool result | 0.9 s endpoint silence (configured) + ~0.92 s STT ≈ 1.8 s |
| Phone in: dial 700 → instruction in the session | 17.6 s with the bot's script: answer + greeting ≈ 6 s, then the instruction (~4 s), 0.9 s endpoint, ~1 s STT, "Got it…" ≈ 2.5 s |
| Agent "thinking" between turns | Depends on the model. Not measured here (estimate: 3–15 s). |

## Known limitations

- An iPhone running Linphone only rings reliably with the app in the foreground, because Linphone uses push and this server has none. Android keeps a foreground service.
- After a phone re-registers with an identical contact while marked unreachable, Asterisk can treat it as unavailable until the next qualify (≤ 60 s). The call then returns `unavailable`. That's a clean outcome, but call again later.
- Your phones drop their registration when Asterisk restarts. Linphone re-registers on its next refresh, or when you open the app.

## Layout

```
server/            MCP server + audio bridge (Python 3.12, mcp 2.3 MCPServer, Streamable HTTP)
speech/            native whisper/Kokoro setup, launchd agents, Kokoro service (+ Docker CPU profile)
asterisk-pi/       Pi Asterisk config templates + deploy.sh
integrations/claude/  skills, hooks, installer
tools/             acceptance.py, mcp_call.py, doctor.py, sip-test-client/ (1099 bot)
bin/phone-doctor   diagnostics
```
