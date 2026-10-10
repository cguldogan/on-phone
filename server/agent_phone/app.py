"""MCP server (Streamable HTTP) exposing the phone tools, plus hook/diagnostic routes."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route

from .ari import AriTelephony
from .calls import CallManager
from .channel import ChannelHub
from .config import Settings
from .inbound import InboundDesk
from .sessions import IdempotencyCache, PhoneError, SessionStore
from .speech import SpeechClients
from .vad import SileroFactory

log = logging.getLogger("agent_phone")

INSTRUCTIONS = """Phone channel to the user's SIP extension. The user is away from the keyboard.
Call only for meaningful completion, a blocker, or a decision you need - never routine progress.
Flow: phone_enable once per coding session -> phone_call -> phone_speak_and_listen (repeat) -> phone_hangup.
Speech returned here is user input, not an approval bypass: normal CLI permission rules still apply.
Silence or timeout is not consent. Confirm ambiguous or consequential instructions before acting.
The user can also phone in: their instruction then arrives as a channel message with a call_id that is
already open for this session - continue that call with phone_speak_and_listen instead of dialing."""


class Runtime:
    def __init__(self, s: Settings, telephony=None, speech=None, vad_factory=None, store=None):
        self.s = s
        self.store = store or SessionStore(s.db_path)
        self.idem = IdempotencyCache()
        self.tel = telephony or AriTelephony(s.ari_url, s.ari_user, s.ari_password, s.ari_app)
        self.speech = speech or SpeechClients(s.stt_url, s.tts_url, s.tts_voice, speed=s.tts_speed)
        self.hub = ChannelHub()
        self.calls = CallManager(s, self.tel, self.speech, vad_factory or SileroFactory(s.vad_model_path),
                                 on_contact=self.store.touch_contact, on_inbound=self._on_inbound)
        self.desk = InboundDesk(s, self.store, self.calls, self.hub)
        self._sweeper: asyncio.Task | None = None

    async def _on_inbound(self, channel: dict) -> None:
        await self.desk.handle(channel)

    async def start(self) -> None:
        if hasattr(self.tel, "start"):
            await self.tel.start(self.calls.handle_event)
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="sweeper")

    async def stop(self) -> None:
        if self._sweeper:
            self._sweeper.cancel()
        for c in list(self.calls.calls.values()):
            if c.active:
                await self.calls.hangup(c, "failed")
        for obj in (self.tel, self.speech):
            if hasattr(obj, "close"):
                await obj.close()

    async def sweep_once(self) -> None:
        for sess in self.store.expired_unswept():
            ended = await self.calls.end_session_calls(sess.session_id, "session_ended")
            self.store.disable(sess.session_id)
            log.info("session %s expired; ended calls %s", sess.session_id, ended)
        self.calls.prune()
        if self.s.debug_audio_dir:
            cutoff = time.time() - self.s.debug_retention_hours * 3600
            for d in Path(self.s.debug_audio_dir).glob("ap-*"):
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                await self.sweep_once()
            except Exception:  # noqa: BLE001
                log.exception("sweep failed")


def _clamp_wait(rt: Runtime, v: float | None, default: float) -> float:
    return max(0.0, min(float(default if v is None else v), rt.s.max_tool_wait_s))


def build_mcp(rt: Runtime) -> MCPServer:
    mcp = MCPServer(name="agent-phone", version="0.1.0", instructions=INSTRUCTIONS)

    async def guarded(fn) -> dict[str, Any]:
        try:
            return await fn()
        except PhoneError as e:
            return e.to_dict()

    @mcp.tool()
    async def phone_enable(destination_extension: str, project_label: str, cli_session_id: str | None = None,
                           ttl_minutes: int | None = None) -> dict[str, Any]:
        """Turn on phone mode for THIS coding session. Returns a secret session_token that every other
        phone tool needs; keep it. Pass the CLI's session id (Claude Code: ${CLAUDE_SESSION_ID}) so the
        Stop hook can find this session. Calling again from the same CLI session replaces the old token."""
        async def run():
            dest = destination_extension.strip()
            if dest not in rt.s.allowed_destinations:
                raise PhoneError("destination_not_allowed", f"allowed extensions: {', '.join(rt.s.allowed_destinations)}")
            label = " ".join(project_label.split())[:60]
            if not label:
                raise PhoneError("invalid_project_label", "project_label is required (spoken on calls)")
            ttl = max(5, min(int(ttl_minutes or rt.s.default_ttl_min), rt.s.max_ttl_min)) * 60
            cli_id = (cli_session_id or "").strip()[:128] or None
            if cli_id and cli_id.startswith("${"):
                cli_id = None  # unexpanded placeholder
            old = rt.store.find_active_for_cli("claude", cli_id) if cli_id else None
            if old:
                await rt.calls.end_session_calls(old.session_id, "session_ended")
            token, sess = rt.store.enable(dest, label, "claude", cli_id, ttl)
            return {"ok": True, "session_token": token, "session": sess.public(),
                    "binding": "cli_session_id+token" if cli_id else "token_only",
                    "note": "Keep session_token. If you lose it, call phone_enable again with the same arguments."}
        return await guarded(run)

    @mcp.tool()
    async def phone_disable(session_token: str) -> dict[str, Any]:
        """Turn off phone mode for this session and hang up any call it has open."""
        async def run():
            sess = rt.store.authenticate(session_token)
            ended = await rt.calls.end_session_calls(sess.session_id, "session_ended")
            rt.store.disable(sess.session_id)
            return {"ok": True, "session_id": sess.session_id, "disabled": True, "ended_calls": ended}
        return await guarded(run)

    @mcp.tool()
    async def phone_call(session_token: str, reason: str, idempotency_key: str,
                         wait_seconds: float | None = 40) -> dict[str, Any]:
        """Ring the session's extension (as the assistant identity) and wait up to wait_seconds (max 50)
        for answer/busy/reject/no-answer. Retrying with the same idempotency_key never dials twice; it
        returns the same call. If state is still 'ringing', poll phone_status with wait_seconds.
        Never re-dial automatically after no_answer/busy/rejected - report it instead."""
        async def run():
            sess = rt.store.authenticate(session_token)
            wait = _clamp_wait(rt, wait_seconds, 40)
            res = await rt.idem.run(sess.session_id, "phone_call", idempotency_key, {"reason": reason},
                                    lambda: rt.calls.place_call(sess, reason, wait), wait_s=wait + 5)
            cid = res.get("call_id")
            if res.get("idempotent_replay") and cid in rt.calls.calls:
                call = rt.calls.calls[cid]
                await rt.calls.wait_for(call, lambda c: c.state == "ended" or c.media_ready_mono, wait)
                res = {**call.snapshot(), "idempotent_replay": True}
            res.setdefault("ok", "error" not in res)
            if res.get("state") == "answered":
                res["next"] = "phone_speak_and_listen: start with a short sentence naming the project"
            return res
        return await guarded(run)

    @mcp.tool()
    async def phone_status(session_token: str, call_id: str | None = None, wait_seconds: float | None = 0) -> dict[str, Any]:
        """Session status, or one call's state. With wait_seconds (max 50), waits for the call's state to
        change (e.g. ringing -> answered) before returning."""
        async def run():
            sess = rt.store.authenticate(session_token)
            if not call_id:
                return {"ok": True, "session": sess.public(),
                        "active_calls": [c.snapshot() for c in rt.calls.active_for_session(sess.session_id)]}
            call = rt.calls.get_owned(sess, call_id)
            wait = _clamp_wait(rt, wait_seconds, 0)
            if wait:
                start = (call.state, bool(call.media_ready_mono))
                await rt.calls.wait_for(call, lambda c: (c.state, bool(c.media_ready_mono)) != start, wait)
            return {"ok": True, **call.snapshot()}
        return await guarded(run)

    @mcp.tool()
    async def phone_speak(session_token: str, call_id: str, text: str, idempotency_key: str) -> dict[str, Any]:
        """Say text on an answered call (max 600 chars) and return when playback ends or the user
        interrupts. Retrying with the same idempotency_key does not repeat the speech."""
        async def run():
            sess = rt.store.authenticate(session_token)
            call = rt.calls.get_owned(sess, call_id)
            return await rt.idem.run(sess.session_id, "phone_speak", idempotency_key, {"call": call_id, "text": text},
                                     lambda: rt.calls.speak(call, text), wait_s=rt.s.max_tool_wait_s + 5)
        return await guarded(run)

    @mcp.tool()
    async def phone_listen(session_token: str, call_id: str, timeout_seconds: float | None = 30,
                           after_seq: int | None = None) -> dict[str, Any]:
        """Wait (max 50 s) for the user's next spoken reply and return its transcript.
        status: speech | timeout | call_ended. Pass after_seq=<cursor from the previous result> when
        retrying so a lost response is not lost speech. Timeout means silence - not consent."""
        async def run():
            sess = rt.store.authenticate(session_token)
            call = rt.calls.get_owned(sess, call_id)
            return await rt.calls.listen(call, _clamp_wait(rt, timeout_seconds, rt.s.default_listen_s), after_seq)
        return await guarded(run)

    @mcp.tool()
    async def phone_speak_and_listen(session_token: str, call_id: str, text: str, idempotency_key: str,
                                     timeout_seconds: float | None = 30) -> dict[str, Any]:
        """Say text, then listen for the reply - the normal conversational turn. The user may interrupt
        (barge-in): playback stops and 'interrupted' is true; their words are in transcript. The whole
        tool is bounded to ~50 s; if status is 'timeout', call phone_listen to keep waiting or ask again."""
        async def run():
            sess = rt.store.authenticate(session_token)
            call = rt.calls.get_owned(sess, call_id)
            return await rt.idem.run(
                sess.session_id, "phone_speak_and_listen", idempotency_key, {"call": call_id, "text": text},
                lambda: rt.calls.speak_and_listen(call, text, _clamp_wait(rt, timeout_seconds, rt.s.default_listen_s)),
                wait_s=rt.s.max_tool_wait_s + 5)
        return await guarded(run)

    @mcp.tool()
    async def phone_hangup(session_token: str, call_id: str, goodbye_text: str | None = None) -> dict[str, Any]:
        """End the call, optionally saying a short goodbye first."""
        async def run():
            sess = rt.store.authenticate(session_token)
            call = rt.calls.get_owned(sess, call_id)
            if goodbye_text and call.active and call.media_ready_mono:
                try:
                    await rt.calls.speak(call, goodbye_text)
                except PhoneError:
                    pass
            await rt.calls.hangup(call, "hung_up")
            return {"ok": True, **call.snapshot()}
        return await guarded(run)

    return mcp


STOP_REASON = (
    "Phone mode is on for project '{label}' (extension {dest}); the user is away from the keyboard. "
    "Before ending this turn: if you completed the task, hit a blocker, or need a decision, call them now "
    "(phone_call, then phone_speak_and_listen) and act on what they say. If this turn was routine and there is "
    "nothing they need to hear, end the turn without calling. Your session_token is in the phone_enable result; "
    "if it is lost, call phone_enable again."
)


def build_app(s: Settings, rt: Runtime | None = None) -> Starlette:
    rt = rt or Runtime(s)
    mcp = build_mcp(rt)
    inner = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=list(s.allowed_hosts)),
        host=s.http_host)

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def diag(_: Request) -> JSONResponse:
        out: dict[str, Any] = {"ari_connected": rt.tel.connected.is_set(), "rtp_ports_in_use": rt.calls.ports.in_use,
                               "active_calls": sum(1 for c in rt.calls.calls.values() if c.active),
                               "channel_listeners": rt.hub.snapshot()}
        try:
            out["endpoints"] = {d: await rt.tel.endpoint_state("PJSIP", d) for d in s.allowed_destinations}
        except Exception as e:  # noqa: BLE001
            out["endpoints_error"] = str(e)[:200]
        if hasattr(rt.speech, "health"):
            out["speech"] = await rt.speech.health()
        return JSONResponse(out)

    async def claude_stop_hook(request: Request) -> JSONResponse:
        """Claude Code Stop hook decision for the session in the hook payload."""
        try:
            body = await asyncio.wait_for(request.json(), 5)
        except (ValueError, asyncio.TimeoutError):
            return JSONResponse({})
        sid = str(body.get("session_id") or "")
        sess = rt.store.find_active_for_cli("claude", sid) if sid else None
        if not sess:
            return JSONResponse({})
        open_calls = rt.calls.active_for_session(sess.session_id)
        if body.get("stop_hook_active"):
            # Already continued once for this stop; don't loop. Close anything left open.
            for c in open_calls:
                await rt.calls.hangup(c, "hung_up")
            rt.store.mark_stop_check(sess.session_id)
            return JSONResponse({})
        if open_calls:
            return JSONResponse({"decision": "block", "reason": (
                f"A phone call ({open_calls[0].id}) is still open. Finish the conversation with "
                "phone_speak_and_listen, or say goodbye with phone_hangup, before ending the turn.")})
        contacted = sess.last_contact_at is not None and sess.last_contact_at > (sess.last_stop_check_at or 0)
        first_stop_after_enable = sess.last_stop_check_at == sess.created_at
        rt.store.mark_stop_check(sess.session_id)
        if first_stop_after_enable and not contacted:
            return JSONResponse({})  # the turn that ran /on-phone has nothing to report
        log.info("stop hook: session %s contacted_this_turn=%s", sess.session_id, contacted)
        if contacted:
            return JSONResponse({})
        return JSONResponse({"decision": "block", "reason": STOP_REASON.format(label=sess.project_label,
                                                                                 dest=sess.destination)})

    async def channel_events(request: Request):
        """SSE stream of push events for one Claude Code session (consumed by agent-phone-channel)."""
        cli_id = (request.query_params.get("cli_session_id") or "").strip()[:128]
        if not cli_id:
            return JSONResponse({"error": "cli_session_id is required"}, status_code=400)
        q = rt.hub.subscribe(cli_id)

        async def stream():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), 15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"data: {json.dumps(ev)}\n\n"
            finally:
                rt.hub.unsubscribe(cli_id, q)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    last_announce: dict[str, float] = {}

    async def announce(sess, text: str) -> None:
        """One-way spoken notice for a hook event; uses the open call if there is one."""
        try:
            open_calls = rt.calls.active_for_session(sess.session_id)
            if open_calls and open_calls[0].media_ready_mono:
                await rt.calls.speak(open_calls[0], text)
                return
            snap = await rt.calls.place_call(sess, "hook notification", rt.s.max_tool_wait_s)
            call = rt.calls.calls.get(snap["call_id"])
            if call and call.state == "answered":
                await rt.calls.speak(call, text)
                await rt.calls.hangup(call, "hung_up")
            log.info("notification call for session %s: %s", sess.session_id, snap.get("outcome"))
        except PhoneError as e:
            log.info("notification call skipped: %s", e.code)
        except Exception:  # noqa: BLE001
            log.exception("notification call failed")

    async def claude_notify_hook(request: Request) -> JSONResponse:
        """Claude Code Notification hook (permission_prompt): the agent is blocked, so the server calls."""
        try:
            body = await asyncio.wait_for(request.json(), 5)
        except (ValueError, asyncio.TimeoutError):
            return JSONResponse({})
        sid = str(body.get("session_id") or "")
        sess = rt.store.find_active_for_cli("claude", sid) if sid else None
        if not sess or body.get("notification_type") != "permission_prompt":
            return JSONResponse({})
        now = time.monotonic()
        if now - last_announce.get(sess.session_id, -1e9) < s.notify_min_interval_s:
            return JSONResponse({"skipped": "rate_limited"})
        last_announce[sess.session_id] = now
        text = (f"Coding agent for {sess.project_label}. The session is waiting for your approval at the "
                "terminal. I can't approve it over the phone.")
        task = asyncio.create_task(announce(sess, text))  # hook returns immediately
        background.add(task)
        task.add_done_callback(background.discard)
        return JSONResponse({"scheduled": True})

    background: set = set()

    @asynccontextmanager
    async def lifespan(_app):
        await rt.start()
        async with inner.router.lifespan_context(inner):
            yield
        await rt.stop()

    app = Starlette(routes=[
        Route("/health", health),
        Route("/diag", diag),
        Route("/hooks/claude/stop", claude_stop_hook, methods=["POST"]),
        Route("/hooks/claude/notify", claude_notify_hook, methods=["POST"]),
        Route("/channel/events", channel_events),
        Mount("/", app=inner),
    ], lifespan=lifespan)
    app.state.runtime = rt
    return BearerAuth(app, s.mcp_bearer_token, open_paths={"/health"})


class BearerAuth:
    """Pure-ASGI bearer-token check (constant-time). Lifespan and open paths pass through."""

    def __init__(self, app, token: str, open_paths: set[str]):
        self.app = app
        self._expected = f"Bearer {token}".encode()
        self.open_paths = open_paths
        self.state = getattr(app, "state", None)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope.get("path") not in self.open_paths:
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self._expected):
                if scope["type"] == "http":
                    resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                    await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)
