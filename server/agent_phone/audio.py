"""Sample-format helpers.

Internal format everywhere is 16 kHz mono int16 (numpy, native byte order).
On the wire, Asterisk's slin16 RTP payload is big-endian (network order);
Kokoro returns 24 kHz little-endian PCM; whisper.cpp wants a WAV file.
"""
from __future__ import annotations

import io
import re
import wave

import numpy as np
import soxr

RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000  # 320 samples = 640 bytes


def be_bytes_to_pcm(payload: bytes) -> np.ndarray:
    """slin16 RTP payload (big-endian int16) -> native int16 array."""
    if len(payload) % 2:
        payload = payload[:-1]
    return np.frombuffer(payload, dtype=">i2").astype(np.int16)


def pcm_to_be_bytes(pcm: np.ndarray) -> bytes:
    return pcm.astype(">i2").tobytes()


def le_bytes_to_pcm(data: bytes) -> np.ndarray:
    if len(data) % 2:
        data = data[:-1]
    return np.frombuffer(data, dtype="<i2").astype(np.int16)


def resample(pcm: np.ndarray, src_rate: int, dst_rate: int = RATE) -> np.ndarray:
    if src_rate == dst_rate or pcm.size == 0:
        return pcm.astype(np.int16, copy=False)
    out = soxr.resample(pcm.astype(np.float32) / 32768.0, src_rate, dst_rate, quality="HQ")
    return np.clip(np.round(out * 32768.0), -32768, 32767).astype(np.int16)


def to_float(pcm: np.ndarray) -> np.ndarray:
    return pcm.astype(np.float32) / 32768.0


def frames(pcm: np.ndarray, frame_samples: int = FRAME_SAMPLES) -> list[np.ndarray]:
    """Split into fixed frames, zero-padding the last one."""
    if pcm.size == 0:
        return []
    pad = (-pcm.size) % frame_samples
    if pad:
        pcm = np.concatenate([pcm, np.zeros(pad, dtype=np.int16)])
    return [pcm[i:i + frame_samples] for i in range(0, pcm.size, frame_samples)]


def wav_bytes(pcm: np.ndarray, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype("<i2").tobytes())
    return buf.getvalue()


def rms_dbfs(pcm: np.ndarray) -> float:
    if pcm.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(pcm.astype(np.float64)))))
    return 20 * np.log10(max(rms, 1e-9) / 32768.0)


_SENTENCE_RE = re.compile(r"(?<=[.!?;:])\s+")


def split_sentences(text: str, max_chars: int = 220) -> list[str]:
    """Sentence chunks for incremental TTS; long sentences are split on commas/spaces."""
    out: list[str] = []
    for sentence in _SENTENCE_RE.split(text.strip()):
        sentence = sentence.strip()
        while len(sentence) > max_chars:
            cut = sentence.rfind(",", 0, max_chars)
            if cut < max_chars // 2:
                cut = sentence.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            out.append(sentence[: cut + 1].strip())
            sentence = sentence[cut + 1:].strip()
        if sentence:
            out.append(sentence)
    return out
