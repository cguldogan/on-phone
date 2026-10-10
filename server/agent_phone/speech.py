"""Clients for the native speech services (whisper.cpp server, Kokoro service)."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import httpx
import numpy as np

from .audio import RATE, le_bytes_to_pcm, resample, wav_bytes

# Phrases Whisper commonly invents on noise/silence. Only dropped when the
# decoder itself also signals weak evidence (see is_probable_hallucination).
_HALLUCINATIONS = {
    "thank you", "thanks for watching", "thank you for watching", "you", "bye", "okay",
    "thank you very much", "please subscribe", "so", "uh", "um", "hmm",
}
_NON_SPEECH_RE = re.compile(r"^\s*[\[\(][^\]\)]*[\]\)]\s*$")  # "[BLANK_AUDIO]", "(music)"


@dataclass
class Transcript:
    text: str
    stt_seconds: float
    no_speech_prob: float | None = None      # max over segments, from whisper.cpp
    avg_logprob: float | None = None         # duration-weighted mean over segments
    min_word_prob: float | None = None
    mean_word_prob: float | None = None
    raw: dict = field(default_factory=dict)

    def quality(self) -> dict:
        """Only metrics whisper.cpp actually reported. No synthetic confidence."""
        q = {"stt_seconds": round(self.stt_seconds, 3)}
        for k in ("no_speech_prob", "avg_logprob", "min_word_prob", "mean_word_prob"):
            v = getattr(self, k)
            if v is not None:
                q[k] = round(v, 4)
        return q


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z' ]+", "", text.lower()).strip()


def is_probable_hallucination(t: Transcript, speech_ms: int) -> str | None:
    """Return a reason string if the transcript should be discarded."""
    text = t.text.strip()
    if not text or _NON_SPEECH_RE.match(text) or not re.search(r"[A-Za-z0-9]", text):
        return "empty_or_non_speech"
    weak = (t.no_speech_prob is not None and t.no_speech_prob > 0.5) or (
        t.avg_logprob is not None and t.avg_logprob < -1.0)
    if weak and (t.no_speech_prob or 0) > 0.8:
        return "decoder_no_speech"
    if _normalise(text) in _HALLUCINATIONS and (weak or speech_ms < 600):
        return "known_hallucination_low_evidence"
    return None


class SpeechClients:
    def __init__(self, stt_url: str, tts_url: str, voice: str, timeout: float = 30.0, speed: float = 1.0):
        self.stt_url = stt_url
        self.tts_url = tts_url
        self.voice = voice
        self.speed = speed
        self._http = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._http.aclose()

    async def transcribe(self, pcm: np.ndarray) -> Transcript:
        t0 = time.perf_counter()
        r = await self._http.post(
            self.stt_url,
            files={"file": ("utt.wav", wav_bytes(pcm, RATE), "audio/wav")},
            data={"response_format": "verbose_json", "temperature": "0.0", "language": "en"},
        )
        r.raise_for_status()
        d = r.json()
        elapsed = time.perf_counter() - t0
        segs = d.get("segments") or []
        nsp = [s["no_speech_prob"] for s in segs if "no_speech_prob" in s]
        lp, w = [], []
        word_probs: list[float] = []
        for s in segs:
            if "avg_logprob" in s:
                lp.append(s["avg_logprob"])
                w.append(max(float(s.get("end", 0)) - float(s.get("start", 0)), 0.01))
            word_probs += [x["probability"] for x in (s.get("words") or []) if "probability" in x]
        return Transcript(
            text=(d.get("text") or "").strip(),
            stt_seconds=elapsed,
            no_speech_prob=max(nsp) if nsp else None,
            avg_logprob=float(np.average(lp, weights=w)) if lp else None,
            min_word_prob=min(word_probs) if word_probs else None,
            mean_word_prob=float(np.mean(word_probs)) if word_probs else None,
        )

    async def synthesize(self, text: str) -> tuple[np.ndarray, float]:
        """Return (16 kHz int16 pcm, synthesis seconds)."""
        t0 = time.perf_counter()
        r = await self._http.post(self.tts_url, json={"input": text, "voice": self.voice, "speed": self.speed})
        r.raise_for_status()
        rate = int(r.headers.get("X-Sample-Rate", "24000"))
        pcm = resample(le_bytes_to_pcm(r.content), rate, RATE)
        return pcm, time.perf_counter() - t0

    async def health(self) -> dict:
        out = {}
        base_stt = self.stt_url.rsplit("/", 1)[0]
        base_tts = self.tts_url.split("/v1/")[0]
        for name, url in (("stt", base_stt + "/"), ("tts", base_tts + "/health")):
            try:
                r = await self._http.get(url, timeout=3)
                out[name] = {"ok": r.status_code < 500, "status": r.status_code}
            except Exception as e:  # noqa: BLE001
                out[name] = {"ok": False, "error": type(e).__name__}
        return out
