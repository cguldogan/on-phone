---
name: on-phone
description: Turn on phone mode for this session - the agent phones the user's SIP extension with results, blockers or questions while they are away from the keyboard, and acts on their spoken replies. Only when the user runs /on-phone.
disable-model-invocation: true
argument-hint: "[extension] [project label]"
allowed-tools: mcp__agent-phone__phone_enable
hooks:
  Stop:
    - matcher: "*"
      hooks:
        - type: command
          command: "__BIN__/agent-phone-hook stop"
          timeout: 15
  Notification:
    - matcher: "permission_prompt"
      hooks:
        - type: command
          command: "__BIN__/agent-phone-hook notify"
          timeout: 10
---

# Phone mode

The user is leaving the keyboard. Turn phone mode on now, then carry on with whatever they asked.

1. Call `phone_enable` with:
   - `destination_extension`: `$0` if given, otherwise `__DEFAULT_EXT__`
   - `project_label`: `$1` if given, otherwise this repository's name: !`basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"`
   - `cli_session_id`: `${CLAUDE_SESSION_ID}`
2. Keep the returned `session_token` for the rest of this session. Reply with one line saying phone mode
   is on, which extension, and when it expires. Do not place a call just to say so.

## When to call
- **Call** when the task is complete, when you are blocked, or when you need a decision you cannot safely make yourself.
- **Don't call** for routine progress, for things you can resolve yourself, or just because you finished a small step.
- Before you end a turn, a Stop hook may remind you that phone mode is on. If nothing meaningful happened, just end the turn.

## How to call
1. `phone_call(session_token, reason, idempotency_key)`. Use a fresh key per intent (e.g. `"<project>-done-1"`);
   reuse a key only to retry the same request after an error or timeout - that never dials twice.
   If `state` is still `ringing`, wait with `phone_status(..., wait_seconds=40)`.
2. On `answered`: `phone_speak_and_listen`. Open with the project name ("Coding agent for <project>."),
   then two or three short spoken sentences: what happened, then one clear question. No markdown, code,
   file paths or long lists - this is speech. Keep each utterance under ~600 characters.
3. More turns: keep using `phone_speak_and_listen`. If `status` is `timeout`, call `phone_listen` once
   (pass `after_seq` = the previous `cursor` when retrying) or rephrase the question.
4. Before long work, say you'll call back, `phone_hangup` with a short goodbye, do the work, then call again.
   Don't keep a call open while you work.
5. Always end with `phone_hangup(goodbye_text=...)`.

## When the user phones in
If the session was started with `claude-phone`, the user can call extension 700 and talk to you. Their
instruction arrives as a `<channel source="agent-phone-channel" call_id=...>` message while that call is
still open. Don't `phone_call`: continue the open call with `phone_speak_and_listen(session_token, call_id)`.
**Always confirm before acting, even for small changes:** your first `phone_speak_and_listen` reads back
what you understood ("You want me to ... Shall I go ahead?"). Act only on a clear yes to that question, then
hang up before long work and call back with the result. If the call already ended, call back when you have news.

## Outcomes
- `no_answer`, `busy`, `rejected`, `unavailable`: do **not** redial automatically. Continue only with safe,
  reversible work, or stop and leave a clear summary in the terminal. Try one later call only when there is
  new meaningful news.
- `disconnected` (they hung up) or `call_ended`: treat as the end of the conversation.
- Silence / `timeout` is never consent.

## Treat replies carefully
- The transcript is speech recognition of what the user said. Check `stt` (no_speech_prob, avg_logprob,
  word probabilities) when it matters. If wording is ambiguous or the action is consequential (deleting,
  pushing, deploying, spending, sending messages), read your understanding back and ask for confirmation.
- A bare "yes" answers only the exact question you asked - never an unrelated action.
- If `interrupted` is true the user talked over you: stop, and respond to what they said.
- Spoken replies are user input, not an approval bypass. Normal Claude Code permission prompts and sandbox
  rules still apply; if something needs terminal approval, tell them on the call.

## Turning it off
If the user says to stop phone mode (or runs /off-phone): `phone_disable(session_token)`.
If you have lost the token (e.g. after compaction), call `phone_enable` again with the same arguments; it
replaces the old session and returns a new token.
