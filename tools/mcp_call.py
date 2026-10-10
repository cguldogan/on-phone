"""Drive the phone MCP server like an agent would (used for acceptance tests and `phone-doctor --call`).

  uv run --project server python tools/mcp_call.py --dest 1099 --text "Hello" [--turns 2] [--listen-only]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


def load_env() -> dict:
    env = {}
    for line in (Path(__file__).resolve().parents[1] / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'")
    return env


async def tool(session, name, **args) -> dict:
    t0 = time.monotonic()
    res = await session.call_tool(name, args)
    sc = res.structured_content
    out = sc if sc is not None else json.loads(res.content[0].text)
    out["_tool_s"] = round(time.monotonic() - t0, 3)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default="1099")
    ap.add_argument("--label", default="phone test")
    ap.add_argument("--text", action="append", help="prompt(s); one per turn")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--url", default=os.environ.get("AGENT_PHONE_URL", "http://127.0.0.1:8765/mcp"))
    ap.add_argument("--goodbye", default="Thanks, goodbye.")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    prompts = args.text or ["This is a phone test. Please say something after the tone."]
    token = load_env()["MCP_BEARER_TOKEN"]
    show = (lambda *_: None) if args.quiet else (lambda label, d: print(f"--- {label}\n{json.dumps(d, indent=1)}", flush=True))

    client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=120)
    async with client, streamable_http_client(args.url, http_client=client) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        en = await tool(s, "phone_enable", destination_extension=args.dest, project_label=args.label,
                        cli_session_id=f"tools-{uuid.uuid4().hex[:8]}", ttl_minutes=10)
        if not en.get("ok"):
            show("enable", en)
            return 2
        st = en["session_token"]
        call = await tool(s, "phone_call", session_token=st, reason="manual test", idempotency_key=uuid.uuid4().hex)
        show("call", call)
        summary = {"call_outcome": call.get("outcome"), "setup_s": call.get("timings", {}).get("setup_s"), "turns": []}
        if call.get("state") != "answered":
            await tool(s, "phone_disable", session_token=st)
            print(json.dumps(summary))
            return 1
        for prompt in prompts:
            turn = await tool(s, "phone_speak_and_listen", session_token=st, call_id=call["call_id"], text=prompt,
                              idempotency_key=uuid.uuid4().hex, timeout_seconds=args.timeout)
            show("turn", turn)
            u = (turn.get("listen") or {}).get("utterances") or [{}]
            summary["turns"].append({
                "prompt": prompt, "status": turn.get("status"), "transcript": turn.get("transcript"),
                "interrupted": turn.get("interrupted"), "tool_s": turn["_tool_s"],
                "tts_first_audio_s": turn.get("speak", {}).get("tts_first_audio_s"),
                "audio_seconds": turn.get("speak", {}).get("audio_seconds"),
                "stt_s": u[-1].get("stt", {}).get("stt_seconds"),
                "endpoint_to_transcript_s": u[-1].get("endpoint_to_transcript_s"),
            })
            if turn.get("state") == "ended":
                break
        if call.get("call_id"):
            h = await tool(s, "phone_hangup", session_token=st, call_id=call["call_id"], goodbye_text=args.goodbye)
            summary["final_outcome"] = h.get("outcome")
        await tool(s, "phone_disable", session_token=st)
        print("SUMMARY " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
