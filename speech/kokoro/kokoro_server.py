"""Minimal native Kokoro-82M TTS service (kokoro-onnx).

POST /v1/audio/speech  {"input": str, "voice": str?, "speed": float?}
  -> raw PCM s16le mono; sample rate in the X-Sample-Rate header.
GET  /health
The model is loaded and warmed once at startup.
"""
import asyncio
import os
import time

import numpy as np
import uvicorn
from kokoro_onnx import Kokoro
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

MODEL_DIR = os.environ.get("KOKORO_MODEL_DIR", os.path.join(os.path.dirname(__file__), "../../models"))
DEFAULT_VOICE = os.environ.get("KOKORO_VOICE", "af_heart")
MAX_CHARS = int(os.environ.get("KOKORO_MAX_CHARS", "2000"))

kokoro = Kokoro(os.path.join(MODEL_DIR, "kokoro-v1.0.onnx"), os.path.join(MODEL_DIR, "voices-v1.0.bin"))
kokoro.create("Warm up.", voice=DEFAULT_VOICE, speed=1.0, lang="en-us")
VOICES = set(kokoro.get_voices())
_lock = asyncio.Lock()  # one synthesis at a time; ONNX session already uses all cores


async def speech(request: Request) -> Response:
    body = await request.json()
    text = str(body.get("input", "")).strip()
    voice = body.get("voice") or DEFAULT_VOICE
    speed = float(body.get("speed", 1.0))
    if not text:
        return JSONResponse({"error": "empty input"}, status_code=400)
    if len(text) > MAX_CHARS:
        return JSONResponse({"error": f"input longer than {MAX_CHARS} chars"}, status_code=400)
    if voice not in VOICES:
        return JSONResponse({"error": f"unknown voice {voice}"}, status_code=400)
    t0 = time.perf_counter()
    async with _lock:
        samples, rate = await asyncio.to_thread(kokoro.create, text, voice=voice, speed=speed, lang="en-us")
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    elapsed = time.perf_counter() - t0
    return Response(pcm, media_type="audio/pcm", headers={
        "X-Sample-Rate": str(rate),
        "X-Synthesis-Seconds": f"{elapsed:.3f}",
        "X-Audio-Seconds": f"{len(samples) / rate:.3f}",
    })


async def health(_: Request) -> Response:
    return JSONResponse({"ok": True, "default_voice": DEFAULT_VOICE})


app = Starlette(routes=[Route("/v1/audio/speech", speech, methods=["POST"]), Route("/health", health)])

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("KOKORO_HOST", "127.0.0.1"), port=int(os.environ.get("KOKORO_PORT", "8179")), log_level="warning")
