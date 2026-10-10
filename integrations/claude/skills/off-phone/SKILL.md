---
name: off-phone
description: Turn off phone mode for this session (stop calling the user's phone). Only when the user runs /off-phone.
disable-model-invocation: true
allowed-tools: mcp__agent-phone__phone_disable mcp__agent-phone__phone_enable
---

Turn phone mode off for this session:

1. Call `phone_disable` with the `session_token` from this session's `phone_enable` result. This also hangs up
   any open call.
2. If you don't have the token, call `phone_enable` with `destination_extension` from earlier (or `__DEFAULT_EXT__`),
   the same project label and `cli_session_id`: `${CLAUDE_SESSION_ID}`, then `phone_disable` with the new token.
3. Confirm in one line that phone mode is off. From now on, don't call the user unless they turn it on again.
