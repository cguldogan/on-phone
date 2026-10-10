"""phone-doctor: one-shot health check of every hop. Exit code 0 only if every required check passes."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import httpx2
import numpy as np
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
FAILED: list[str] = []


def env() -> dict:
    out = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'")
    return out


def ok(name: str, cond: bool, detail: str = "", required: bool = True) -> bool:
    mark = "✔" if cond else ("✘" if required else "!")
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    if not cond and required:
        FAILED.append(name)
    return cond


def sh(*cmd: str, timeout: int = 15) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return f"ERR {e}"


async def main() -> int:
    ap = argparse.ArgumentParser(prog="phone-doctor")
    ap.add_argument("--call", metavar="EXT", help="also place a live test call to this extension")
    ap.add_argument("text", nargs="?", default="This is a phone doctor test call. Please say anything.")
    args = ap.parse_args()
    E = env()
    tok = E["MCP_BEARER_TOKEN"]
    H = {"Authorization": f"Bearer {tok}"}

    print("Tailscale")
    ts_ip = sh("tailscale", "ip", "-4").splitlines()[:1]
    ok("Mac Tailscale IP matches MAC_TAILSCALE_IP", ts_ip == [E["MAC_TAILSCALE_IP"]], f"{ts_ip} vs {E['MAC_TAILSCALE_IP']}")
    rtp_bound = sh("lsof", "-nP", f"-iUDP@{E['MAC_TAILSCALE_IP']}:40000")
    ok("RTP ports published on the Tailscale IP", "40000" in rtp_bound, "Docker Desktop binds 40000-40019/udp")

    print("Asterisk (Pi) via ARI")
    async with httpx.AsyncClient(auth=(E["ARI_USER"], E["ARI_PASSWORD"]), timeout=5) as ari:
        try:
            info = (await ari.get(E["ARI_URL"] + "/ari/asterisk/info")).json()
            ok("ARI reachable over Tailscale", True, f"Asterisk {info['system']['version']}")
        except Exception as e:  # noqa: BLE001
            ok("ARI reachable over Tailscale", False, f"{type(e).__name__}: {e}")
        for ext in E.get("ALLOWED_DESTINATIONS", "").split(","):
            try:
                st = (await ari.get(f"{E['ARI_URL']}/ari/endpoints/PJSIP/{ext}")).json().get("state")
            except Exception:  # noqa: BLE001
                st = "error"
            ok(f"extension {ext} registered", st == "online", st, required=False)

    print("MCP server (Docker)")
    ps = sh("docker", "compose", "--project-directory", str(ROOT), "ps", "--format", "{{.Service}} {{.Status}}")
    ok("phone-mcp container running", "phone-mcp Up" in ps, ps.replace("\n", "; "))
    async with httpx.AsyncClient(timeout=10) as h:
        try:
            d = (await h.get("http://127.0.0.1:8765/diag", headers=H)).json()
            ok("server connected to ARI", d.get("ari_connected"), json.dumps(d.get("endpoints")))
            ok("server reaches whisper + kokoro", d["speech"]["stt"]["ok"] and d["speech"]["tts"]["ok"])
        except Exception as e:  # noqa: BLE001
            ok("/diag", False, f"{type(e).__name__}: {e}")
        ok("unauthenticated requests rejected", (await h.post("http://127.0.0.1:8765/mcp", json={})).status_code == 401)
    try:
        async with httpx2.AsyncClient(headers=H, timeout=30) as hc, \
                streamable_http_client("http://127.0.0.1:8765/mcp", http_client=hc) as (r, w), ClientSession(r, w) as s:
            t0 = time.monotonic()
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            ok("MCP initialize + list_tools", len(tools) == 8, f"{len(tools)} tools in {time.monotonic() - t0:.2f}s")
    except Exception as e:  # noqa: BLE001
        ok("MCP initialize + list_tools", False, f"{type(e).__name__}: {e}")

    print("Speech services (native)")
    for label in ("whisper", "kokoro"):
        st = sh("launchctl", "print", f"gui/{os.getuid()}/com.agentphone.{label}")
        ok(f"launchd com.agentphone.{label}", "state = running" in st, required=False)
    async with httpx.AsyncClient(timeout=60) as h:
        try:
            t0 = time.monotonic()
            r = await h.post("http://127.0.0.1:8179/v1/audio/speech", json={"input": "Testing one two three."})
            tts_s = time.monotonic() - t0
            pcm = np.frombuffer(r.content, dtype="<i2")
            import io
            import wave
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wv:
                wv.setnchannels(1)
                wv.setsampwidth(2)
                wv.setframerate(int(r.headers.get("X-Sample-Rate", 24000)))
                wv.writeframes(pcm.tobytes())
            t1 = time.monotonic()
            tr = (await h.post("http://127.0.0.1:8178/inference", files={"file": ("t.wav", buf.getvalue(), "audio/wav")},
                               data={"response_format": "json"})).json()["text"].strip()
            stt_s = time.monotonic() - t1
            ok("TTS -> STT round trip", "testing" in tr.lower(), f"{tr!r}; TTS {tts_s:.2f}s, STT {stt_s:.2f}s")
        except Exception as e:  # noqa: BLE001
            ok("TTS -> STT round trip", False, f"{type(e).__name__}: {e}")

    print("Claude Code integration")
    mcp = sh("claude", "mcp", "get", "agent-phone", timeout=40)
    ok("MCP server 'agent-phone' registered and connected", "Connected" in mcp, required=False)
    for skill in ("on-phone", "off-phone"):
        ok(f"skill /{skill} installed", (Path.home() / f".claude/skills/{skill}/SKILL.md").exists(), required=False)
    tf = Path.home() / ".config/agent-phone/token"
    ok("hook token file private", tf.exists() and (tf.stat().st_mode & 0o077) == 0, required=False)

    if args.call:
        print(f"Live call to {args.call}")
        r = subprocess.run([sys.executable, str(ROOT / "tools/mcp_call.py"), "--quiet", "--dest", args.call,
                            "--text", args.text], capture_output=True, text=True, timeout=180)
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("SUMMARY ")), "")
        summary = json.loads(line[8:]) if line else {}
        ok("call answered", summary.get("call_outcome") == "answered", json.dumps(summary)[:300])

    print("\nOK" if not FAILED else f"\n{len(FAILED)} problem(s): " + "; ".join(FAILED))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
