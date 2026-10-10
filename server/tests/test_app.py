"""End-to-end through the real ASGI app + official MCP client over Streamable HTTP (fake telephony)."""
import asyncio
import contextlib
import json

import httpx
import httpx2
import numpy as np
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_phone.app import Runtime, build_app
from agent_phone.sessions import SessionStore

from .fakes import EnergyVAD, FakeAsterisk, FakeSpeech, free_ports, settings, tone


@contextlib.asynccontextmanager
async def running_server(replies=None):
    port = free_ports(1)[0]
    s = settings(http_port=port, allowed_hosts=(f"127.0.0.1:{port}",))
    tel, speech = FakeAsterisk(), FakeSpeech(replies)
    rt = Runtime(s, telephony=tel, speech=speech, vad_factory=EnergyVAD, store=SessionStore(":memory:"))
    rt.calls.ports._free.extend(free_ports(2))
    tel.bind(rt.calls)
    server = uvicorn.Server(uvicorn.Config(build_app(s, rt), host="127.0.0.1", port=port, log_level="warning",
                                           lifespan="on"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", s, tel, rt
    finally:
        server.should_exit = True
        await task


@contextlib.asynccontextmanager
async def mcp_session(base, token):
    client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60)
    async with client, streamable_http_client(base + "/mcp", http_client=client) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            yield session


async def call(session, name, **args) -> dict:
    res = await session.call_tool(name, args)
    if res.structured_content is not None:
        sc = res.structured_content
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    return json.loads(res.content[0].text)


async def test_auth_required_and_health_open():
    async with running_server() as (base, s, tel, rt):
        async with httpx.AsyncClient() as h:
            assert (await h.get(base + "/health")).status_code == 200
            assert (await h.post(base + "/mcp", json={})).status_code == 401
            assert (await h.get(base + "/diag", headers={"Authorization": "Bearer nope"})).status_code == 401
            ok = await h.get(base + "/diag", headers={"Authorization": f"Bearer {s.mcp_bearer_token}"})
            assert ok.status_code == 200 and ok.json()["ari_connected"] is True


async def test_full_conversation_over_mcp_and_stop_hook():
    async with running_server(replies=["Yes, and include screenshots."]) as (base, s, tel, rt):
        hook_client = httpx.AsyncClient(headers={"Authorization": f"Bearer {s.mcp_bearer_token}"}, timeout=10)
        hook = lambda body: hook_client.post(base + "/hooks/claude/stop", json=body)  # noqa: E731
        async with hook_client, mcp_session(base, s.mcp_bearer_token) as session:
            names = {t.name for t in (await session.list_tools()).tools}
            assert names == {"phone_enable", "phone_disable", "phone_call", "phone_status", "phone_speak",
                             "phone_listen", "phone_speak_and_listen", "phone_hangup"}

            en = await call(session, "phone_enable", destination_extension="1001", project_label="demo",
                            cli_session_id="claude-sess-1")
            token = en["session_token"]
            assert en["binding"] == "cli_session_id+token"

            # The turn that enabled phone mode is not nagged; the next turn without contact is.
            assert (await hook({"session_id": "claude-sess-1", "stop_hook_active": False})).json() == {}
            r = (await hook({"session_id": "claude-sess-1", "stop_hook_active": False})).json()
            assert r["decision"] == "block" and "demo" in r["reason"]
            # Unrelated CLI session: no opinion.
            assert (await hook({"session_id": "someone-else", "stop_hook_active": False})).json() == {}

            c = await call(session, "phone_call", session_token=token, reason="done", idempotency_key="c1")
            assert c["state"] == "answered", c
            again = await call(session, "phone_call", session_token=token, reason="done", idempotency_key="c1")
            assert again["call_id"] == c["call_id"] and again["idempotent_replay"] and len(tel.originated) == 1

            async def reply():
                await asyncio.sleep(0.8)
                await tel.send_audio(np.concatenate([tone(0.6), np.zeros(9600, dtype=np.int16)]))
            asyncio.create_task(reply())
            turn = await call(session, "phone_speak_and_listen", session_token=token, call_id=c["call_id"],
                              text="Demo: the feature is done. Open a draft PR?", idempotency_key="t1",
                              timeout_seconds=10)
            assert turn["transcript"] == "Yes, and include screenshots.", turn

            # Open call: hook insists on finishing it.
            r = (await hook({"session_id": "claude-sess-1", "stop_hook_active": False})).json()
            assert r["decision"] == "block" and "still open" in r["reason"]

            # A different session's token cannot touch this call.
            other = await call(session, "phone_enable", destination_extension="1002", project_label="other")
            denied = await call(session, "phone_listen", session_token=other["session_token"], call_id=c["call_id"])
            assert denied["ok"] is False and denied["error"]["code"] == "call_not_found"

            h = await call(session, "phone_hangup", session_token=token, call_id=c["call_id"])
            assert h["state"] == "ended" and h["outcome"] == "hung_up"

            # Contacted this turn and nothing open: allowed to stop.
            assert (await hook({"session_id": "claude-sess-1", "stop_hook_active": False})).json() == {}
            # Next turn without contact: block again; but never loop when stop_hook_active.
            assert (await hook({"session_id": "claude-sess-1", "stop_hook_active": False})).json()["decision"] == "block"
            assert (await hook({"session_id": "claude-sess-1", "stop_hook_active": True})).json() == {}

            d = await call(session, "phone_disable", session_token=token)
            assert d["disabled"]
            after = await call(session, "phone_status", session_token=token)
            assert after["error"]["code"] == "session_disabled"
            assert (await hook({"session_id": "claude-sess-1", "stop_hook_active": False})).json() == {}


async def test_session_expiry_sweep_hangs_up():
    async with running_server() as (base, s, tel, rt):
        token, sess = rt.store.enable("1001", "demo", "claude", "x", 600)
        snap = await rt.calls.place_call(sess, "r", 5)
        rt.store._db.execute("UPDATE sessions SET expires_at=0 WHERE session_id=?", (sess.session_id,))
        await rt.sweep_once()
        assert rt.calls.calls[snap["call_id"]].outcome == "session_ended"
        with pytest.raises(Exception):
            rt.store.authenticate(token)


async def test_permission_prompt_notification_places_one_announce_call():
    async with running_server() as (base, s, tel, rt):
        token, sess = rt.store.enable("1001", "demo", "claude", "sess-n", 600)
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {s.mcp_bearer_token}"}) as h:
            url = base + "/hooks/claude/notify"
            assert (await h.post(url, json={"session_id": "sess-n", "notification_type": "idle_prompt"})).json() == {}
            assert (await h.post(url, json={"session_id": "nobody", "notification_type": "permission_prompt"})).json() == {}
            r = await h.post(url, json={"session_id": "sess-n", "notification_type": "permission_prompt"})
            assert r.json() == {"scheduled": True}
            again = await h.post(url, json={"session_id": "sess-n", "notification_type": "permission_prompt"})
            assert again.json() == {"skipped": "rate_limited"}
        for _ in range(100):
            if any(c.state == "ended" for c in rt.calls.calls.values()):
                break
            await asyncio.sleep(0.05)
        calls = list(rt.calls.calls.values())
        assert len(calls) == 1 and calls[0].outcome == "hung_up" and len(tel.originated) == 1
