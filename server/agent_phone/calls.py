"""Call lifecycle, media pipeline and the speak/listen primitives.

One Call per phone call - outbound (agent dials the user) or inbound (the user dials
the agent; see inbound.py). Inbound audio flows RTP -> Segmenter (VAD) ->
transcription worker -> utterance queue. Outbound speech flows text ->
sentence chunks -> TTS -> paced RTP. Barge-in clears queued playback.
All waits are bounded; nothing here ever re-dials.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .audio import frames, split_sentences, wav_bytes
from .config import Settings
from .rtp import PortPool, RtpEndpoint, make_codec
from .sessions import PhoneError, PhoneSession
from .speech import Transcript, is_probable_hallucination
from .vad import Segmenter, Utterance

log = logging.getLogger(__name__)

MAX_SPEAK_CHARS = 600  # ~40 s of audio; keeps one speak inside the tool wait bound

# Q.850 causes seen before answer -> outcome
_PRE_ANSWER = {
    17: "busy", 21: "rejected", 18: "no_answer", 19: "no_answer",
    20: "unavailable", 1: "unavailable", 3: "unavailable", 27: "unavailable",
    34: "failed", 38: "failed", 41: "failed", 42: "failed", 47: "failed",
}
_RETRYABLE = {"busy", "no_answer", "unavailable", "failed", "destination_in_use"}


class Telephony(Protocol):
    host: str | None
    connected: asyncio.Event

    async def endpoint_state(self, tech: str, resource: str) -> str: ...
    async def originate(self, channel_id: str, endpoint: str, caller_id: str, timeout_s: int, app_args: str) -> None: ...
    async def answer(self, channel_id: str) -> None: ...
    async def hangup(self, channel_id: str, reason: str = "normal") -> None: ...
    async def setup_media(self, call_channel_id: str, media_channel_id: str, bridge_id: str,
                          external_host: str, codec: str = "g722") -> tuple[str, int]: ...
    async def teardown_media(self, media_channel_id: str, bridge_id: str) -> None: ...


class Speech(Protocol):
    async def transcribe(self, pcm: np.ndarray) -> Transcript: ...
    async def synthesize(self, text: str) -> tuple[np.ndarray, float]: ...


@dataclass
class HeardUtterance:
    seq: int
    text: str
    speech_ms: int
    duration_ms: int
    during_playback: bool
    barge_in: bool
    forced_end: bool
    quality: dict
    vad: dict
    endpoint_to_transcript_s: float
    started_at: float = 0.0           # time.monotonic() when speech began
    consumed: bool = False

    def public(self) -> dict:
        return {"seq": self.seq, "text": self.text, "speech_ms": self.speech_ms, "duration_ms": self.duration_ms,
                "during_playback": self.during_playback, "barge_in": self.barge_in,
                "truncated_at_max_length": self.forced_end, "stt": self.quality, "vad": self.vad,
                "endpoint_to_transcript_s": round(self.endpoint_to_transcript_s, 3)}


@dataclass
class Call:
    id: str
    session_id: str
    destination: str
    reason: str
    direction: str = "outbound"       # outbound | inbound
    channel_id: str = ""              # Asterisk channel; same as id for calls we originate
    claimed: bool = False             # an agent tool has touched this call (inbound hand-off)
    created_at: float = field(default_factory=time.time)
    created_mono: float = field(default_factory=time.monotonic)
    state: str = "dialing"            # dialing | ringing | answered | ended
    outcome: str | None = None        # answered | busy | rejected | unavailable | no_answer | disconnected | hung_up | idle_timeout | max_duration | session_ended | failed
    ringing_mono: float | None = None
    answered_mono: float | None = None
    ended_mono: float | None = None
    media_ready_mono: float | None = None
    hangup_cause: int | None = None
    hangup_request_cause: int | None = None
    cause_txt: str | None = None
    end_requested: str | None = None  # outcome we chose when we hung up
    error: str | None = None
    port: int | None = None
    rtp: RtpEndpoint | None = None
    segmenter: Segmenter | None = None
    heard: deque = field(default_factory=lambda: deque(maxlen=50))
    next_seq: int = 1
    stt_queue: asyncio.Queue | None = None
    stt_busy: bool = False
    speaking: bool = False
    spoken_once: bool = False
    speak_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    barge_in_active: bool = False     # current playback was interrupted
    utterance_barged: bool = False    # current utterance caused a barge-in
    recent_spoken: deque = field(default_factory=lambda: deque(maxlen=3))
    last_activity: float = field(default_factory=time.monotonic)
    version: int = 0
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    tasks: set = field(default_factory=set)
    stats: dict = field(default_factory=lambda: {
        "dropped_short": 0, "dropped_hallucination": 0, "dropped_echo": 0, "barge_ins": 0,
        "utterances": 0, "tts_chunks": 0})

    def __post_init__(self) -> None:
        self.channel_id = self.channel_id or self.id

    @property
    def media_id(self) -> str:
        return self.id + "-m"

    @property
    def bridge_id(self) -> str:
        return self.id + "-b"

    @property
    def active(self) -> bool:
        return self.state != "ended"

    def snapshot(self) -> dict:
        now = time.monotonic()
        t: dict[str, Any] = {}
        if self.ringing_mono:
            t["to_ringing_s"] = round(self.ringing_mono - self.created_mono, 3)
        if self.answered_mono:
            t["setup_s"] = round(self.answered_mono - self.created_mono, 3)
            t["talk_s"] = round((self.ended_mono or now) - self.answered_mono, 1)
        if self.media_ready_mono and self.answered_mono:
            t["media_ready_after_answer_s"] = round(self.media_ready_mono - self.answered_mono, 3)
        out = {
            "call_id": self.id, "session_id": self.session_id, "destination": self.destination,
            "direction": self.direction,
            "state": self.state, "outcome": self.outcome,
            "retryable": (self.outcome in _RETRYABLE) if self.outcome else None,
            "timings": t,
            "speaking": self.speaking,
            "speech_in_progress": bool(self.segmenter and self.segmenter.in_speech),
            "unread_utterances": sum(1 for h in self.heard if not h.consumed),
            "stats": dict(self.stats),
        }
        if self.rtp:
            out["rtp"] = {"codec": self.rtp.codec.name, "packets_in": self.rtp.packets_in, "packets_out": self.rtp.packets_out,
                          "silence_filled_frames": self.rtp.filled_frames, "bad_packets": self.rtp.bad_packets}
        if self.hangup_cause is not None or self.cause_txt:
            out["hangup_cause"] = {"q850": self.hangup_cause, "text": self.cause_txt}
        if self.error:
            out["error_detail"] = self.error
        return out

    async def notify(self) -> None:
        async with self.cond:
            self.version += 1
            self.cond.notify_all()

    def notify_soon(self) -> None:
        t = asyncio.get_running_loop().create_task(self.notify())
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def wait_change(self, timeout: float) -> bool:
        if timeout <= 0:
            return False
        start = self.version
        async with self.cond:
            try:
                await asyncio.wait_for(self.cond.wait_for(lambda: self.version != start), timeout)
                return True
            except asyncio.TimeoutError:
                return False


class _Listener:
    """Segmenter callbacks for one call."""

    def __init__(self, mgr: "CallManager", call: Call):
        self.mgr, self.call = mgr, call

    def on_speech_start(self) -> None:
        self.call.utterance_barged = False
        self.call.last_activity = time.monotonic()
        self.call.notify_soon()

    def on_speech_progress(self, speech_ms: int) -> None:
        c = self.call
        if c.speaking and c.rtp and c.rtp.playing and not c.utterance_barged \
                and speech_ms >= self.mgr.s.bargein_min_speech_ms:
            dropped = c.rtp.clear_playback()
            c.utterance_barged = True
            c.barge_in_active = True
            c.stats["barge_ins"] += 1
            log.info("call %s barge-in after %d ms speech (dropped %.1fs audio)", c.id, speech_ms, dropped * 0.02)
            c.notify_soon()

    def on_utterance(self, utt: Utterance) -> None:
        c = self.call
        if utt.started_during_playback and not c.utterance_barged:
            # Started while we were talking and never qualified as barge-in: echo or noise.
            c.stats["dropped_echo"] += 1
            c.notify_soon()
            return
        utt.meta["barge_in"] = c.utterance_barged
        if c.stt_queue is not None:
            try:
                c.stt_queue.put_nowait(utt)
            except asyncio.QueueFull:
                log.warning("call %s transcription queue full; dropping utterance", c.id)
        c.notify_soon()

    def on_speech_abandoned(self) -> None:
        self.call.stats["dropped_short"] += 1
        self.call.notify_soon()


class CallManager:
    def __init__(self, settings: Settings, telephony: Telephony, speech: Speech, vad_factory,
                 port_pool: PortPool | None = None, on_contact=None, on_inbound=None):
        self.s = settings
        self.tel = telephony
        self.speech = speech
        self.vad_factory = vad_factory
        self.ports = port_pool or PortPool(settings.rtp_port_min, settings.rtp_port_max)
        self.calls: dict[str, Call] = {}
        self.by_destination: dict[str, str] = {}
        self.by_channel: dict[str, str] = {}   # Asterisk channel id -> call id
        self._on_inbound = on_inbound          # (channel dict) -> coroutine; None = refuse inbound calls
        self._on_contact = on_contact or (lambda session_id: None)
        self._dest_lock = asyncio.Lock()
        self._inbound_tasks: set = set()

    # ---- lookup -------------------------------------------------------------
    def get_owned(self, session: PhoneSession, call_id: str) -> Call:
        c = self.calls.get(call_id)
        if not c or c.session_id != session.session_id:
            raise PhoneError("call_not_found", "no such call for this session")
        c.claimed = True
        return c

    def active_for_session(self, session_id: str) -> list[Call]:
        return [c for c in self.calls.values() if c.session_id == session_id and c.active]

    # ---- placing calls --------------------------------------------------------
    async def place_call(self, session: PhoneSession, reason: str, wait_s: float) -> dict:
        dest = session.destination
        if dest not in self.s.allowed_destinations:
            raise PhoneError("destination_not_allowed", f"extension {dest} is not in ALLOWED_DESTINATIONS")
        if not self.tel.connected.is_set():
            raise PhoneError("telephony_unavailable", "not connected to Asterisk ARI", retryable=True)
        async with self._dest_lock:
            holder = self.by_destination.get(dest)
            if holder and self.calls[holder].active:
                other = self.calls[holder]
                if other.session_id == session.session_id:
                    raise PhoneError("call_already_active", "this session already has a call to the destination",
                                     call_id=other.id)
                raise PhoneError("destination_in_use", "another coding session is on a call with this extension",
                                 retryable=True, retry_after_s=30)
            call = Call(id="ap-" + secrets.token_hex(6), session_id=session.session_id, destination=dest,
                        reason=reason[:200])
            self.calls[call.id] = call
            self.by_destination[dest] = call.id
            self.by_channel[call.channel_id] = call.id
        log.info("call %s: dialing %s for session %s", call.id, dest, session.session_id)
        # No endpoint-state pre-check: Asterisk's endpoint state lags a fresh registration.
        # Originate itself fails with "Allocation failed" when no contact is registered.
        try:
            # Asterisk's own dial timeout gets slack so our watchdog decides "no_answer" deterministically.
            await self.tel.originate(call.id, f"PJSIP/{dest}", self.s.caller_id, self.s.ring_timeout_s + 10, call.id)
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status", None) == 500 and "Allocation failed" in str(e):
                await self._finish(call, "unavailable", hangup_sent=True,
                                   cause_txt="extension not registered (no reachable contact)")
            else:
                call.error = str(e)[:300]
                await self._finish(call, "failed", hangup_sent=True)
            return call.snapshot()
        self._spawn(call, self._watchdog(call), "watchdog")
        await self.wait_for(call, lambda c: c.state in ("answered", "ended") and (c.state == "ended" or c.media_ready_mono),
                            wait_s)
        return call.snapshot()

    async def accept_inbound(self, channel_id: str, caller: str, session_id: str, wait_s: float = 10) -> Call:
        """Answer a call the user placed to the agent and bring up media. The caller must not be busy
        with another agent call (the desk checks that first)."""
        call = Call(id="ap-in-" + secrets.token_hex(5), session_id=session_id, destination=caller,
                    reason="inbound call", direction="inbound", channel_id=channel_id)
        async with self._dest_lock:
            holder = self.by_destination.get(caller)
            if holder and self.calls[holder].active:
                raise PhoneError("destination_in_use", "caller is already on a call with an agent")
            self.calls[call.id] = call
            self.by_destination[caller] = call.id
            self.by_channel[channel_id] = call.id
        log.info("call %s: inbound from %s (channel %s)", call.id, caller, channel_id)
        try:
            await self.tel.answer(channel_id)
        except Exception as e:  # noqa: BLE001
            call.error = f"answer failed: {e}"[:300]
            await self._finish(call, "failed")
            return call
        call.state = call.outcome = "answered"
        call.answered_mono = call.last_activity = time.monotonic()
        self._spawn(call, self._setup_media(call), "media-setup")
        self._spawn(call, self._watchdog(call), "watchdog")
        await self.wait_for(call, lambda c: c.state == "ended" or c.media_ready_mono, wait_s)
        return call

    def assign(self, call: Call, session_id: str) -> None:
        """Hand an inbound call to the phone session it was routed to."""
        call.session_id = session_id

    async def wait_for(self, call: Call, pred, wait_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, min(wait_s, self.s.max_tool_wait_s))
        while not pred(call):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await call.wait_change(remaining)
        return True

    # ---- ARI events ------------------------------------------------------------
    def handle_event(self, ev: dict) -> None:
        etype = ev.get("type")
        if etype == "AriDisconnected":
            return
        ch = ev.get("channel") or {}
        cid = ch.get("id", "")
        call = self.calls.get(self.by_channel.get(cid, ""))
        if call is None and etype == "StasisStart" and (ev.get("args") or [""])[0] == "inbound":
            self._start_inbound(ch)
            return
        if call is None or not call.active:
            return
        if etype == "ChannelStateChange":
            st = ch.get("state")
            if st == "Ringing" and call.state == "dialing":
                call.state = "ringing"
                call.ringing_mono = time.monotonic()
                call.notify_soon()
        elif etype == "StasisStart":
            if call.state in ("dialing", "ringing"):
                call.state = "answered"
                call.outcome = "answered"
                call.answered_mono = time.monotonic()
                call.last_activity = time.monotonic()
                self._on_contact(call.session_id)
                self._spawn(call, self._setup_media(call), "media-setup")
                call.notify_soon()
        elif etype == "StasisEnd" and call.state == "answered":
            # A channel that entered the app from the dialplan (inbound) gets no ChannelDestroyed once it
            # has left Stasis, so the caller hanging up shows up only as StasisEnd.
            t = asyncio.get_running_loop().create_task(
                self._finish(call, call.end_requested or "disconnected", hangup_sent=True))
            call.tasks.add(t)
        elif etype == "ChannelHangupRequest":
            if ev.get("cause"):
                call.hangup_request_cause = ev["cause"]
        elif etype == "ChannelDestroyed":
            cause = ev.get("cause") or call.hangup_request_cause
            call.hangup_cause = cause
            call.cause_txt = ev.get("cause_txt")
            if call.state in ("dialing", "ringing"):
                outcome = call.end_requested or _PRE_ANSWER.get(cause)
                if outcome is None:
                    rang_long = time.monotonic() - call.created_mono >= self.s.ring_timeout_s - 1
                    outcome = "no_answer" if rang_long else "failed"
            else:
                outcome = call.end_requested or "disconnected"
            t = asyncio.get_running_loop().create_task(self._finish(call, outcome, hangup_sent=True))
            call.tasks.add(t)

    def _start_inbound(self, ch: dict) -> None:
        if self._on_inbound is None:
            t = asyncio.get_running_loop().create_task(self.tel.hangup(ch.get("id", ""), "busy"))
        else:
            t = asyncio.get_running_loop().create_task(self._on_inbound(ch))
        self._inbound_tasks.add(t)
        t.add_done_callback(self._inbound_tasks.discard)

    async def _setup_media(self, call: Call) -> None:
        try:
            call.port = self.ports.acquire()
            seg_listener = _Listener(self, call)
            call.segmenter = Segmenter(
                self.vad_factory(), seg_listener, threshold=self.s.vad_threshold, preroll_ms=self.s.vad_preroll_ms,
                endpoint_ms=self.s.vad_endpoint_ms, min_speech_ms=self.s.vad_min_speech_ms,
                max_utterance_s=self.s.vad_max_utterance_s)
            call.rtp = RtpEndpoint(self.s.rtp_bind_host, call.port, call.segmenter.feed,
                                   codec=make_codec(self.s.media_codec))
            # Only the agent's speech counts as playback (not the silence primer).
            call.segmenter.playback_active = lambda: bool(call.speaking and call.rtp and call.rtp.playing)
            await call.rtp.start()
            call.stt_queue = asyncio.Queue(maxsize=8)
            self._spawn(call, self._stt_worker(call), "stt")
            host, port = await self.tel.setup_media(call.channel_id, call.media_id, call.bridge_id,
                                                    f"{self.s.rtp_advertise_host}:{call.port}", self.s.media_codec)
            call.rtp.remote = (host, port)
            # 200 ms of silence first: Asterisk's strict-RTP learning discards the first packets from a new
            # source, and the phone's jitter buffer settles - so the first spoken syllable isn't clipped.
            call.rtp.enqueue([np.zeros(320, dtype=np.int16)] * 10)
            call.media_ready_mono = time.monotonic()
            log.info("call %s: media ready (local %d <-> asterisk %s:%d)", call.id, call.port, host, port)
            await call.notify()
        except Exception as e:  # noqa: BLE001
            log.exception("call %s: media setup failed", call.id)
            call.error = f"media setup failed: {e}"[:300]
            await self.hangup(call, "failed")

    # ---- transcription -------------------------------------------------------------
    async def _stt_worker(self, call: Call) -> None:
        assert call.stt_queue is not None
        while True:
            utt: Utterance = await call.stt_queue.get()
            call.stt_busy = True
            try:
                tr = await self.speech.transcribe(utt.audio)
                reason = is_probable_hallucination(tr, utt.speech_ms)
                if reason is None and utt.started_during_playback and self._looks_like_echo(call, tr.text):
                    reason = "echo_of_assistant_speech"
                if reason:
                    key = "dropped_echo" if reason.startswith("echo") else "dropped_hallucination"
                    call.stats[key] += 1
                    log.info("call %s: dropped utterance (%s)", call.id, reason)
                else:
                    endpoint_s = time.monotonic() - utt.ended_at
                    h = HeardUtterance(
                        seq=call.next_seq, text=tr.text, speech_ms=utt.speech_ms, duration_ms=utt.duration_ms,
                        during_playback=utt.started_during_playback, barge_in=bool(utt.meta.get("barge_in")),
                        forced_end=utt.forced_end, quality=tr.quality(),
                        vad={"max_prob": round(utt.max_prob, 3), "mean_prob": round(utt.mean_prob, 3)},
                        endpoint_to_transcript_s=endpoint_s, started_at=utt.started_at)
                    call.next_seq += 1
                    call.heard.append(h)
                    call.stats["utterances"] += 1
                    call.last_activity = time.monotonic()
                    self._debug_capture(call, h, utt.audio)
            except Exception as e:  # noqa: BLE001
                log.warning("call %s: transcription failed: %s", call.id, e)
                call.error = f"transcription failed: {type(e).__name__}"
            finally:
                call.stt_busy = False
                await call.notify()

    @staticmethod
    def _looks_like_echo(call: Call, text: str) -> bool:
        t = text.lower().strip()
        return any(difflib.SequenceMatcher(None, t, s.lower()).ratio() > 0.75 or (len(t) > 12 and t in s.lower())
                   for s in call.recent_spoken)

    def _debug_capture(self, call: Call, h: HeardUtterance, audio: np.ndarray) -> None:
        if not self.s.debug_audio_dir:
            return
        try:
            d = Path(self.s.debug_audio_dir) / call.id
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{h.seq:03d}.wav").write_bytes(wav_bytes(audio))
            (d / f"{h.seq:03d}.json").write_text(json.dumps(h.public(), indent=1))
        except OSError as e:
            log.warning("debug capture failed: %s", e)

    # ---- agent operations -------------------------------------------------------------
    def _require_media(self, call: Call) -> None:
        if call.state == "ended":
            raise PhoneError("call_ended", f"call has ended ({call.outcome})", outcome=call.outcome)
        if call.state != "answered" or not call.media_ready_mono or not call.rtp:
            raise PhoneError("call_not_ready", f"call is {call.state}; wait with phone_status", retryable=True)

    async def speak(self, call: Call, text: str) -> dict:
        self._require_media(call)
        text = " ".join(text.split())
        if not text:
            raise PhoneError("empty_text", "text is empty")
        if len(text) > MAX_SPEAK_CHARS:
            raise PhoneError("text_too_long", f"keep each utterance under {MAX_SPEAK_CHARS} characters; split it")
        if call.speak_lock.locked():
            raise PhoneError("already_speaking", "another speak is in progress on this call", retryable=True)
        async with call.speak_lock:
            if not call.spoken_once:
                await self._await_pickup_greeting(call)
                call.spoken_once = True
            call.last_activity = time.monotonic()
            call.speaking = True
            call.barge_in_active = False
            call.recent_spoken.append(text)
            rtp = call.rtp
            assert rtp is not None
            sentences = split_sentences(text)
            t0 = time.monotonic()
            first_audio: float | None = None
            synth_s: list[float] = []
            audio_s = 0.0
            queued_sentences = 0
            produced = asyncio.Event()

            async def producer() -> None:
                nonlocal first_audio, audio_s, queued_sentences
                try:
                    for sentence in sentences:
                        if call.barge_in_active or not call.active:
                            return
                        pcm, took = await self.speech.synthesize(sentence)
                        synth_s.append(took)
                        if call.barge_in_active or not call.active:
                            return
                        rtp.enqueue(frames(pcm))
                        call.stats["tts_chunks"] += 1
                        queued_sentences += 1
                        audio_s += pcm.size / 16000
                        if first_audio is None:
                            first_audio = time.monotonic() - t0
                finally:
                    produced.set()

            task = asyncio.create_task(producer())
            budget = self.s.max_tool_wait_s
            try:
                while True:
                    if not call.active or call.barge_in_active:
                        break
                    if produced.is_set() and not rtp.playing:
                        break
                    if time.monotonic() - t0 > budget:
                        break
                    await call.wait_change(0.1)
            finally:
                if call.barge_in_active or not call.active:
                    rtp.clear_playback()
                if not task.done():  # interrupted, ended, or over the wait budget
                    task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception) as e:  # noqa: BLE001
                    if not isinstance(e, asyncio.CancelledError):
                        call.error = f"tts failed: {type(e).__name__}: {e}"[:300]
                call.speaking = False
                call.last_activity = time.monotonic()
                self._on_contact(call.session_id)
                await call.notify()
        result = {
            "ok": True, "call_id": call.id, "state": call.state,
            "interrupted": call.barge_in_active,
            "completed": not call.barge_in_active and call.active and queued_sentences == len(sentences),
            "sentences_total": len(sentences), "sentences_queued": queued_sentences,
            "audio_seconds": round(audio_s, 2),
            "tts_first_audio_s": round(first_audio, 3) if first_audio is not None else None,
            "tts_synthesis_s": [round(x, 3) for x in synth_s],
        }
        if not call.active:
            result["outcome"] = call.outcome
        if call.error and "tts failed" in call.error:
            result["ok"] = False
            result["error"] = {"code": "tts_failed", "message": call.error, "retryable": True}
        return result

    async def listen(self, call: Call, timeout_s: float, after_seq: int | None = None,
                     reply_after: float | None = None) -> dict:
        """Wait for the caller's next reply.

        Without `after_seq`, returns utterances not yet returned by an earlier listen.
        With `after_seq` (cursor from a previous result), returns everything newer than
        it, so retrying a listen whose response was lost returns the same transcript.
        """
        if call.state != "ended":
            self._require_media(call)
        call.last_activity = time.monotonic()
        timeout_s = max(0.5, min(float(timeout_s), self.s.max_tool_wait_s))
        t0 = time.monotonic()
        deadline = t0 + timeout_s

        def busy() -> bool:
            return bool((call.segmenter and call.segmenter.in_speech) or call.stt_busy
                        or (call.stt_queue and not call.stt_queue.empty()))

        during_prompt: list[HeardUtterance] = []

        def pending() -> list[HeardUtterance]:
            items = ([h for h in call.heard if not h.consumed] if after_seq is None
                     else [h for h in call.heard if h.seq > after_seq])
            if reply_after is None:
                return items
            # A reply to the prompt starts after the prompt ended - or interrupts it. Speech that began
            # earlier (a "hello?" on pickup, talking during the question) is reported, not treated as the answer.
            out = []
            for h in items:
                if h.barge_in or h.started_at >= reply_after:
                    out.append(h)
                elif h not in during_prompt:
                    during_prompt.append(h)
                    h.consumed = True
            return out

        while True:
            unread = pending()
            if unread and not busy():
                status = "speech"
                break
            if not call.active and not busy():
                status = "speech" if unread else "call_ended"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = "speech" if unread else "timeout"
                break
            await call.wait_change(min(remaining, 0.5))
        for h in unread:
            h.consumed = True
        call.last_activity = time.monotonic()
        out = {
            "ok": True, "call_id": call.id, "status": status, "state": call.state,
            "transcript": " ".join(h.text for h in unread).strip() or None,
            "utterances": [h.public() for h in unread],
            "interrupted_playback": any(h.barge_in for h in unread),
            "speech_still_in_progress": busy(),
            "waited_s": round(time.monotonic() - t0, 2),
            "cursor": max([h.seq for h in unread], default=after_seq if after_seq is not None else call.next_seq - 1),
        }
        if during_prompt:
            out["heard_during_prompt"] = [h.public() for h in during_prompt]
        if status == "timeout":
            out["note"] = "No speech heard. Silence is not consent; ask again or end the call."
        if not call.active:
            out["outcome"] = call.outcome
        if unread:
            self._on_contact(call.session_id)
        return out

    async def _await_pickup_greeting(self, call: Call) -> None:
        """Like a person: give the callee a moment after answering, and let a "hello?" finish first."""
        answered = call.answered_mono or time.monotonic()
        earliest = answered + self.s.answer_settle_ms / 1000
        latest = answered + self.s.answer_greeting_max_ms / 1000
        while call.active:
            now = time.monotonic()
            in_speech = bool(call.segmenter and call.segmenter.in_speech)
            if now >= latest or (now >= earliest and not in_speech):
                return
            await call.wait_change(0.05)

    async def speak_and_listen(self, call: Call, text: str, timeout_s: float) -> dict:
        t0 = time.monotonic()
        # Speech heard before this prompt is reported separately so the reply to *this* prompt is unambiguous.
        earlier = [h for h in call.heard if not h.consumed]
        for h in earlier:
            h.consumed = True
        spoken = await self.speak(call, text)
        if not spoken.get("ok"):
            return spoken
        before = [h.public() for h in earlier]
        if not call.active:
            return {"ok": True, "call_id": call.id, "speak": spoken, "listen": None, "state": call.state,
                    "outcome": call.outcome, "heard_before_prompt": before}
        remaining = self.s.max_tool_wait_s - (time.monotonic() - t0)
        prompt_end = time.monotonic() - 0.3  # small grace: people answer the moment the question ends
        heard = await self.listen(call, max(1.0, min(timeout_s, remaining)), reply_after=prompt_end)
        return {"ok": True, "call_id": call.id, "state": call.state, "status": heard.get("status"),
                "transcript": heard.get("transcript"),
                "interrupted": bool(spoken.get("interrupted") or heard.get("interrupted_playback")),
                "speak": spoken, "listen": heard, "heard_before_prompt": before,
                "heard_during_prompt": heard.get("heard_during_prompt", [])}

    async def hangup(self, call: Call, outcome: str = "hung_up") -> None:
        if not call.active:
            return
        call.end_requested = call.end_requested or outcome
        try:
            await self.tel.hangup(call.channel_id)
        except Exception as e:  # noqa: BLE001
            log.warning("call %s: hangup request failed: %s", call.id, e)
        # Don't depend on the ChannelDestroyed event arriving.
        await self._finish(call, call.end_requested, hangup_sent=True)

    async def _finish(self, call: Call, outcome: str, hangup_sent: bool = False, cause_txt: str | None = None) -> None:
        if call.state == "ended":
            return
        call.state = "ended"
        call.outcome = outcome
        if cause_txt:
            call.cause_txt = cause_txt
        call.ended_mono = time.monotonic()
        log.info("call %s ended: %s (cause=%s, rtp in/out=%s/%s, utterances=%d, dropped=%s)", call.id, call.outcome,
                 call.hangup_cause, call.rtp.packets_in if call.rtp else 0, call.rtp.packets_out if call.rtp else 0,
                 call.stats["utterances"], {k: v for k, v in call.stats.items() if k.startswith("dropped") and v})
        current = asyncio.current_task()
        for t in list(call.tasks):
            if t is not current:
                t.cancel()
        if call.rtp:
            await call.rtp.close()
        if call.port is not None:
            self.ports.release(call.port)
            call.port = None
        if call.media_ready_mono or call.answered_mono:
            try:
                await self.tel.teardown_media(call.media_id, call.bridge_id)
            except Exception:  # noqa: BLE001
                pass
        if not hangup_sent:
            try:
                await self.tel.hangup(call.channel_id)
            except Exception:  # noqa: BLE001
                pass
        if self.by_destination.get(call.destination) == call.id:
            del self.by_destination[call.destination]
        self.by_channel.pop(call.channel_id, None)
        await call.notify()

    # ---- background ---------------------------------------------------------------
    def _spawn(self, call: Call, coro, name: str) -> None:
        t = asyncio.create_task(coro, name=f"{call.id}-{name}")
        call.tasks.add(t)
        t.add_done_callback(call.tasks.discard)

    async def _watchdog(self, call: Call) -> None:
        ring_deadline = call.created_mono + self.s.ring_timeout_s
        while call.active:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            if call.state in ("dialing", "ringing") and now > ring_deadline:
                await self.hangup(call, "no_answer")
            elif call.state == "answered":
                if call.answered_mono and now - call.answered_mono > self.s.max_call_s:
                    await self.hangup(call, "max_duration")
                elif (now - call.last_activity > self.s.idle_timeout_s and not call.speaking
                      and not (call.segmenter and call.segmenter.in_speech)):
                    await self.hangup(call, "idle_timeout")

    async def end_session_calls(self, session_id: str, outcome: str = "session_ended") -> list[str]:
        ended = []
        for c in self.active_for_session(session_id):
            await self.hangup(c, outcome)
            ended.append(c.id)
        return ended

    def prune(self, keep_s: float = 3600) -> None:
        now = time.monotonic()
        for cid in [cid for cid, c in self.calls.items() if not c.active and c.ended_mono and now - c.ended_mono > keep_s]:
            del self.calls[cid]
