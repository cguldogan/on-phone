"""Live acceptance tests against the real stack (Pi Asterisk + Docker MCP + native STT/TTS).

Extension 1099 (test-only) is played by a scripted pjsua client on the Pi (tools/sip-test-client/run.sh),
so no human is needed. Measurements printed at the end are measured, not estimated.

  uv run --project server python tools/acceptance.py [scenario ...]
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import time
import uuid
import wave
from pathlib import Path

import httpx
import httpx2
import numpy as np
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8765"


def env() -> dict:
    out = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'")
    return out


ENV = env()
PI = httpx.URL(ENV["ARI_URL"]).host  # the Pi (Tailscale IP)
RESULTS: list[tuple[str, bool, str]] = []
METRICS: dict[str, list[float]] = {}


def metric(name: str, value) -> None:
    if value is not None:
        METRICS.setdefault(name, []).append(float(value))


def pi(cmd: str) -> str:
    r = subprocess.run(["ssh", PI, cmd], capture_output=True, text=True, timeout=90)
    return r.stdout + r.stderr


def client_mode(mode: str, wav: str = "reply-yes.wav", hangup_after: float = 5) -> None:
    """Set how the 1099 test bot (tools/sip-test-client/bot.py on the Pi) handles the next call."""
    body = json.dumps({"mode": mode, "wav": wav, "hangup_after": hangup_after})
    out = pi(f"curl -s -X POST 127.0.0.1:8099/mode -d '{body}'")
    st = json.loads(out)
    assert st.get("mode") == mode, out
    want = mode != "offline"
    for _ in range(30):  # wait for the (un)registration to settle in Asterisk
        contacts = pi("docker exec asterisk asterisk -rx 'pjsip show contacts'")
        present = "1099/" in contacts
        if want and present and "Avail" in [ln for ln in contacts.splitlines() if "1099/" in ln][0]:
            return
        if not want and not present:
            return
        if want and present:
            pi("docker exec asterisk asterisk -rx 'pjsip qualify 1099' >/dev/null")
        time.sleep(1)
    raise AssertionError(f"test bot not in state {mode}: {contacts!r}")


def far_end_heard() -> str:
    """Transcribe what the scripted client recorded (header may be unfinalised)."""
    raw = subprocess.run(["ssh", PI, "cat ~/sip-test-client/out/heard.wav"], capture_output=True, timeout=30).stdout
    tmp = ROOT / "models" / ".heard.wav"
    tmp.write_bytes(raw)
    r = httpx.post("http://127.0.0.1:8178/inference", files={"file": tmp.open("rb")},
                   data={"response_format": "json"}, timeout=60)
    tmp.unlink(missing_ok=True)
    return r.json().get("text", "").strip()


class Agent:
    """One MCP connection = one simulated coding session."""

    def __init__(self, label: str):
        self.label = label

    async def __aenter__(self):
        self._stack = contextlib.AsyncExitStack()
        http = await self._stack.enter_async_context(
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {ENV['MCP_BEARER_TOKEN']}"}, timeout=120))
        r, w = await self._stack.enter_async_context(streamable_http_client(URL + "/mcp", http_client=http))
        self.s = await self._stack.enter_async_context(ClientSession(r, w))
        await self.s.initialize()
        self.cli_id = f"accept-{uuid.uuid4().hex[:8]}"
        return self

    async def __aexit__(self, *a):
        await self._stack.aclose()

    async def tool(self, name, **args) -> dict:
        res = await self.s.call_tool(name, args)
        return res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)

    async def enable(self, dest="1099") -> str:
        en = await self.tool("phone_enable", destination_extension=dest, project_label=self.label,
                             cli_session_id=self.cli_id, ttl_minutes=10)
        assert en["ok"], en
        self.token = en["session_token"]
        return self.token

    async def call(self, key=None, wait=40) -> dict:
        return await self.tool("phone_call", session_token=self.token, reason="acceptance",
                               idempotency_key=key or uuid.uuid4().hex, wait_seconds=wait)

    async def turn(self, call_id, text, timeout=25) -> dict:
        return await self.tool("phone_speak_and_listen", session_token=self.token, call_id=call_id, text=text,
                               idempotency_key=uuid.uuid4().hex, timeout_seconds=timeout)

    async def hangup(self, call_id, goodbye=None) -> dict:
        return await self.tool("phone_hangup", session_token=self.token, call_id=call_id, goodbye_text=goodbye)


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}", flush=True)
    return bool(cond)


def record_turn(t: dict) -> None:
    sp, li = t.get("speak", {}), t.get("listen") or {}
    metric("tts_first_audio_s", sp.get("tts_first_audio_s"))
    for u in li.get("utterances", []):
        metric("stt_s", u["stt"].get("stt_seconds"))
        metric("endpoint_to_transcript_s", u.get("endpoint_to_transcript_s"))


# ---- scenarios ----------------------------------------------------------------------------

async def s_registration():
    async with httpx.AsyncClient() as h:
        d = (await h.get(URL + "/diag", headers={"Authorization": f"Bearer {ENV['MCP_BEARER_TOKEN']}"})).json()
    contacts = pi("docker exec asterisk asterisk -rx 'pjsip show contacts'")
    phone = [ln for ln in contacts.splitlines() if "1001/" in ln]
    check("1 Linphone (1001) registered over Tailscale", any(" 100." in ln.split("@", 1)[-1].replace(":", " ") and "Avail" in ln for ln in phone),
          phone[0].strip() if phone else "1001 not registered right now")
    check("diag: ARI + speech services", d["ari_connected"] and d["speech"]["stt"]["ok"] and d["speech"]["tts"]["ok"],
          json.dumps(d))


async def s_answer_speak_reply():
    client_mode("answer", "reply-yes.wav")
    async with Agent("demo") as a:
        await a.enable()
        t0 = time.monotonic()
        c = await a.call()
        metric("call_setup_s (auto-answer)", c.get("timings", {}).get("setup_s"))
        check("3 MCP call rings 1099 and is answered", c.get("state") == "answered", f"setup {c.get('timings')}")
        chans = httpx.get(ENV["ARI_URL"] + "/ari/channels", auth=(ENV["ARI_USER"], ENV["ARI_PASSWORD"])).json()
        ch = next((x for x in chans if x["id"] == c["call_id"]), {})
        check("3 caller identity is the assistant (700)", ch.get("caller", {}).get("number") == "700",
              f"caller={ch.get('caller')}")
        t = await a.turn(c["call_id"], "Demo project here. The feature is implemented and tests pass. "
                                       "Should I open a draft pull request?")
        record_turn(t)
        metric("turnaround_s (end of prompt playback -> transcript)", None)
        check("5 spoken reply returned as tool result", "screenshots" in (t.get("transcript") or "").lower(),
              repr(t.get("transcript")))
        h = await a.hangup(c["call_id"], "Okay, goodbye.")
        check("hangup outcome", h.get("outcome") == "hung_up", h.get("outcome"))
    await asyncio.sleep(1)
    heard = " ".join(far_end_heard().split())
    check("4 supplied text is audible at the far end",
          "demo project" in heard.lower() and "draft pull request" in heard.lower(), repr(heard))


async def s_multi_turn():
    client_mode("answer", "reply-two.wav")
    async with Agent("multi") as a:
        await a.enable()
        c = await a.call()
        t1 = await a.turn(c["call_id"], "Multi project. First question: should I include screenshots?")
        t2 = await a.turn(c["call_id"], "Got it. Second question: anything else?")
        record_turn(t1)
        record_turn(t2)
        ok = (t1.get("transcript") or "").startswith("Yes") and "tests again" in (t2.get("transcript") or "")
        check("7 several turns within one call", ok, f"{t1.get('transcript')!r} / {t2.get('transcript')!r}")
        await a.hangup(c["call_id"])


async def s_barge_in():
    client_mode("answer", "reply-bargein.wav")
    async with Agent("barge") as a:
        await a.enable()
        c = await a.call()
        long = ("Barge project. Here is a long status report about the migration, the tests, the build "
                "pipeline, and the deployment plan, which keeps going for quite a while so you can interrupt me.")
        t = await a.turn(c["call_id"], long)
        check("barge-in interrupts playback and returns the interruption",
              t.get("interrupted") and "staging" in (t.get("transcript") or "").lower(),
              f"interrupted={t.get('interrupted')} completed={t['speak'].get('completed')} {t.get('transcript')!r}")
        await a.hangup(c["call_id"])


async def s_silence():
    client_mode("answer", "reply-silence.wav")
    async with Agent("quiet") as a:
        await a.enable()
        c = await a.call()
        t = await a.turn(c["call_id"], "Quiet project. Say nothing, please.", timeout=12)
        st = (await a.tool("phone_status", session_token=a.token, call_id=c["call_id"]))
        check("silence/noise produces no fabricated reply", t.get("status") == "timeout" and not t.get("transcript"),
              f"status={t.get('status')} stats={st.get('stats')}")
        await a.hangup(c["call_id"])


async def s_outcomes():
    for mode, want in (("busy", "busy"), ("reject", "rejected")):
        client_mode(mode)
        async with Agent(mode) as a:
            await a.enable()
            c = await a.call()
            check(f"8 {mode} -> {want}", c.get("outcome") == want and c.get("state") == "ended",
                  f"{c.get('outcome')} cause={c.get('hangup_cause')}")
    client_mode("offline")
    async with Agent("offline") as a:
        await a.enable()
        c = await a.call()
        check("8 offline -> unavailable", c.get("outcome") in ("unavailable",), f"{c.get('outcome')} {c.get('hangup_cause')}")
    client_mode("noanswer")
    async with Agent("noanswer") as a:
        await a.enable()
        t0 = time.monotonic()
        c = await a.call(wait=45)
        if c.get("state") == "ringing":
            c = await a.tool("phone_status", session_token=a.token, call_id=c["call_id"], wait_seconds=45)
        check("8 no answer -> no_answer (no redial)", c.get("outcome") == "no_answer",
              f"{c.get('outcome')} after {time.monotonic() - t0:.1f}s")
    client_mode("hangup", "reply-silence.wav", hangup_after=5)
    async with Agent("hangup") as a:
        await a.enable()
        c = await a.call()
        li = await a.tool("phone_listen", session_token=a.token, call_id=c["call_id"], timeout_seconds=20)
        check("8 remote hangup -> clean disconnected outcome", li.get("status") == "call_ended" and li.get("outcome") == "disconnected",
              f"{li.get('status')} {li.get('outcome')} waited {li.get('waited_s')}s")
        chans = pi("docker exec asterisk asterisk -rx 'core show channels concise'")
        check("8 no leftover Asterisk channels after hangup", "ap-" not in chans, chans.strip()[:120])


async def s_idempotency_and_isolation():
    client_mode("answer", "reply-yes.wav")
    async with Agent("idem") as a, Agent("other") as b:
        await a.enable()
        key = uuid.uuid4().hex
        r1, r2 = await asyncio.gather(a.call(key), a.call(key))
        chans = pi("docker exec asterisk asterisk -rx 'core show channels concise'")
        n = sum(1 for ln in chans.splitlines() if ln.startswith("PJSIP/1099"))
        check("9 retried phone_call with same key -> one call", r1.get("call_id") == r2.get("call_id") and n == 1,
              f"{r1.get('call_id')} {r2.get('call_id')} pjsip_channels={n}")
        await b.enable()
        busy = await b.call()
        check("serialised: second session cannot ring the same extension",
              busy.get("error", {}).get("code") == "destination_in_use", json.dumps(busy.get("error")))
        steal = await b.tool("phone_listen", session_token=b.token, call_id=r1["call_id"], timeout_seconds=1)
        check("10 other session cannot read this call's feedback", steal.get("error", {}).get("code") == "call_not_found",
              json.dumps(steal.get("error")))
        d = await a.tool("phone_disable", session_token=a.token)
        after = await a.tool("phone_status", session_token=a.token)
        check("11 disable ends the call and revokes the token",
              d.get("ended_calls") == [r1["call_id"]] and after.get("error", {}).get("code") == "session_disabled",
              f"{d.get('ended_calls')} {after.get('error')}")


def bot_dial(wav: str, hangup_after: float) -> None:
    """Make the 1099 test bot phone the agent (extension 700)."""
    body = json.dumps({"dest": "700", "wav": wav, "hangup_after": hangup_after})
    assert '"error"' not in pi(f"curl -s -X POST 127.0.0.1:8099/dial -d '{body}'")


async def first_channel_event(cli_id: str, timeout: float) -> dict | None:
    """Act as the Claude Code channel bridge for one session and return the first pushed event."""
    async with httpx.AsyncClient(timeout=None) as h:
        async with h.stream("GET", URL + "/channel/events", params={"cli_session_id": cli_id},
                            headers={"Authorization": f"Bearer {ENV['MCP_BEARER_TOKEN']}"}) as r:
            async def read():
                async for line in r.aiter_lines():
                    if line.startswith("data:"):
                        return json.loads(line[5:])
            try:
                return await asyncio.wait_for(read(), timeout)
            except asyncio.TimeoutError:
                return None


async def s_phone_in():
    client_mode("answer", "reply-yes.wav")
    async with Agent("phone in") as a:
        await a.enable()
        bot_dial("reply-silence.wav", 20)  # phone mode on, but no channel listener yet
        await asyncio.sleep(16)
        heard = far_end_heard().lower()
        check("phone-in without a listening session is explained, not dropped", "no coding session" in heard, repr(heard))
        await asyncio.sleep(5)
        events = asyncio.create_task(first_channel_event(a.cli_id, 45))
        await asyncio.sleep(1)
        t0 = time.monotonic()
        bot_dial("instr-create-file.wav", 75)
        ev = await events
        metric("phone_in_dial_to_agent_event_s", time.monotonic() - t0 if ev else None)
        content = (ev or {}).get("content", "")
        call_id = (ev or {}).get("meta", {}).get("call_id")
        check("phone-in: spoken instruction pushed to the session's channel",
              ev is not None and "phonehello" in content.lower().replace(" ", "") and call_id, repr(content[:160]))
        if not call_id:
            return
        t = await a.turn(call_id, "You want me to create a file called phone hello dot t x t. Shall I go ahead?",
                         timeout=40)
        check("phone-in: the session continues the open call and gets the confirmation",
              "yes" in (t.get("transcript") or "").lower(), f"{t.get('status')} {t.get('transcript')!r}")
        h = await a.hangup(call_id, "Done. Goodbye.")
        check("phone-in: hangup", h.get("outcome") == "hung_up", h.get("outcome"))
    await asyncio.sleep(1)
    chans = pi("docker exec asterisk asterisk -rx 'core show channels concise'")
    check("phone-in: no leftover Asterisk channels", "PJSIP/1099" not in chans and "ap-" not in chans, chans.strip()[:120])


SCENARIOS = {"registration": s_registration, "answer": s_answer_speak_reply, "multi": s_multi_turn,
             "barge": s_barge_in, "silence": s_silence, "outcomes": s_outcomes, "idem": s_idempotency_and_isolation,
             "phonein": s_phone_in}


async def main(names: list[str]) -> int:
    for n in names or list(SCENARIOS):
        print(f"\n=== {n}", flush=True)
        try:
            await SCENARIOS[n]()
        except Exception as e:  # noqa: BLE001
            check(f"{n} (scenario crashed)", False, f"{type(e).__name__}: {e}")
    client_mode("answer", "reply-yes.wav")  # leave the test client in its default state
    print("\n=== measurements (this run)")
    for k, v in METRICS.items():
        print(f"{k:45s} n={len(v):2d}  median={np.median(v):.3f}s  min={min(v):.3f}s  max={max(v):.3f}s")
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
