"""The user calls the agent: answer, find the session, take the instruction, hand the open call over.

Routing: the caller is identified by the SIP endpoint of the channel (digest-authenticated by
Asterisk, never the spoofable caller-ID). Eligible sessions are active phone sessions whose
extension is the caller and whose Claude Code session has a channel listener connected. The
instruction is pushed to that session and the call stays open; the agent continues it with the
normal phone tools. If no agent picks it up in time, the caller is told and the call ends.
"""
from __future__ import annotations

import difflib
import logging
import re
import time

from .calls import Call, CallManager
from .channel import ChannelHub
from .config import Settings
from .sessions import PhoneError, PhoneSession, SessionStore

log = logging.getLogger(__name__)

_ORDINALS = {"first": 0, "one": 0, "second": 1, "two": 1, "third": 2, "three": 2, "fourth": 3, "four": 3}


def caller_extension(channel: dict) -> str | None:
    """'PJSIP/1001-0000002a' -> '1001'."""
    m = re.fullmatch(r"PJSIP/([^-/]+)-[0-9a-f]+", channel.get("name", ""))
    return m.group(1) if m else None


def pick_session(transcript: str, sessions: list[PhoneSession]) -> PhoneSession | None:
    said = re.sub(r"[^a-z0-9 ]", " ", transcript.lower())
    words = said.split()
    best, score = None, 0.0
    for sess in sessions:
        label = re.sub(r"[^a-z0-9 ]", " ", sess.project_label.lower()).strip()
        s = 1.0 if label and label in " ".join(words) else difflib.SequenceMatcher(None, said.strip(), label).ratio()
        if s > score:
            best, score = sess, s
    if score >= 0.6:
        return best
    for w in words:
        if w in _ORDINALS and _ORDINALS[w] < len(sessions):
            return sessions[_ORDINALS[w]]
    return None


class InboundDesk:
    def __init__(self, s: Settings, store: SessionStore, calls: CallManager, hub: ChannelHub):
        self.s, self.store, self.calls, self.hub = s, store, calls, hub

    async def handle(self, channel: dict) -> None:
        chan_id = channel.get("id", "")
        caller = caller_extension(channel)
        if caller is None or caller not in self.s.allowed_destinations:
            log.info("inbound %s from %r refused (not an allowed extension)", chan_id, channel.get("name"))
            await self.calls.tel.hangup(chan_id, "rejected")
            return
        try:
            call = await self.calls.accept_inbound(chan_id, caller, session_id="")
        except PhoneError:
            await self.calls.tel.hangup(chan_id, "busy")
            return
        try:
            if call.media_ready_mono and call.active:
                await self._converse(call, caller)
        except PhoneError as e:
            log.info("inbound call %s: %s", call.id, e.code)
        except Exception:  # noqa: BLE001
            log.exception("inbound call %s failed", call.id)
        finally:
            if call.active and not call.claimed:
                await self.calls.hangup(call, "hung_up")

    async def _say_and_end(self, call: Call, text: str) -> None:
        await self.calls.speak(call, text)
        await self.calls.hangup(call, "hung_up")

    async def _ask(self, call: Call, text: str) -> str:
        r = await self.calls.speak_and_listen(call, text, self.s.inbound_listen_s)
        return " ".join((r.get("transcript") or "").split()) if call.active else ""

    async def _converse(self, call: Call, caller: str) -> None:
        sessions = [x for x in self.store.active_for_destination(caller) if self.hub.listening(x.cli_session_id)]
        if not sessions:
            log.info("inbound call %s from %s: no listening session", call.id, caller)
            await self._say_and_end(call, "Hi, this is the coding agent line. No coding session is listening for "
                                          "your calls right now. Start Claude Code with the phone channel, turn on "
                                          "phone mode, and call again. Goodbye.")
            return
        sess = sessions[0]
        if len(sessions) > 1:
            names = ", ".join(x.project_label for x in sessions[:-1]) + f", and {sessions[-1].project_label}"
            question = f"Hi. {len(sessions)} coding sessions are listening: {names}. Which project?"
            chosen = None
            for prompt in (question, "Sorry, which project?"):
                said = await self._ask(call, prompt)
                if not call.active:
                    return
                chosen = pick_session(said, sessions) if said else None
                if chosen:
                    break
            if not chosen:
                await self._say_and_end(call, "I couldn't tell which project. Please call again. Goodbye.")
                return
            sess = chosen
        self.calls.assign(call, sess.session_id)
        greeting = (f"Coding agent for {sess.project_label}. What would you like me to do?" if len(sessions) == 1
                    else f"{sess.project_label}. What would you like me to do?")
        text = await self._ask(call, greeting)
        if not text and call.active:
            text = await self._ask(call, "I'm listening. What should I do?")
        if not call.active:
            return
        if not text:
            await self._say_and_end(call, "I didn't hear anything. Goodbye.")
            return
        heard = next((h for h in reversed(call.heard) if h.text and " ".join(h.text.split()) in text), None)
        await self.calls.speak(call, "Got it. Passing that to the agent now.")
        delivered = self._push(sess, call, caller, text, heard.quality if heard else {})
        if not delivered:
            await self._say_and_end(call, "Sorry, I lost the connection to the agent. Please call again. Goodbye.")
            return
        log.info("inbound call %s: instruction handed to session %s", call.id, sess.session_id)
        deadline = time.monotonic() + self.s.inbound_claim_s
        while call.active and not call.claimed and time.monotonic() < deadline:
            await call.wait_change(0.5)
        if call.active and not call.claimed:
            log.info("inbound call %s: not picked up by the agent within %ss", call.id, self.s.inbound_claim_s)
            await self._say_and_end(call, "The agent is still busy with something else. It has your message and "
                                          "will call you back. Goodbye.")

    def _push(self, sess: PhoneSession, call: Call, caller: str, text: str, quality: dict) -> int:
        content = (
            f'The user phoned in from extension {caller} for project "{sess.project_label}" and said: "{text}"\n'
            f"The call is still open, waiting for you: call_id {call.id}. Continue it with phone_speak_and_listen "
            f"(your phone_enable session_token, call_id {call.id}). This is speech recognition, so ALWAYS confirm "
            "first, even for small changes: your first phone_speak_and_listen reads back what you understood and "
            "asks whether to go ahead. Act only after a clear yes to that question; anything else means ask again "
            "or don't act. Before long work, "
            "say you'll call back and phone_hangup. If the call has already ended, phone_call them back when you "
            "have something to say.")
        meta = {"call_id": call.id, "caller": caller, "project": sess.project_label}
        for k in ("no_speech_prob", "avg_logprob", "min_word_prob"):
            if quality.get(k) is not None:
                meta[f"stt_{k}"] = f"{quality[k]:.3f}"
        return self.hub.publish(sess.cli_session_id or "", content, meta)
