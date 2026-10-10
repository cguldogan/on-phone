"""Voice activity detection and utterance endpointing.

SileroVAD wraps the Silero v5+ ONNX model (16 kHz, 512-sample windows with a
64-sample context, recurrent state). Segmenter turns per-window speech
probabilities into utterances with pre-roll and silence endpointing.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from .audio import RATE

WINDOW = 512           # samples per VAD window at 16 kHz (32 ms)
WINDOW_MS = WINDOW * 1000 // RATE
CONTEXT = 64


class VadModel(Protocol):
    def prob(self, window: np.ndarray) -> float: ...
    def reset(self) -> None: ...


class SileroVAD:
    def __init__(self, model_path: str):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        self._sr = np.array(RATE, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)

    def prob(self, window: np.ndarray) -> float:
        """window: float32 [-1, 1], exactly WINDOW samples."""
        x = np.concatenate([self._context, window.reshape(1, -1).astype(np.float32)], axis=1)
        out, self._state = self._session.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._context = x[:, -CONTEXT:]
        return float(out[0][0])


class SileroFactory:
    """Each call needs its own recurrent state; the ONNX file is read once per call (2 MB, ~10 ms)."""

    def __init__(self, model_path: str):
        self.model_path = model_path
        SileroVAD(model_path)  # fail fast if the model is missing

    def __call__(self) -> SileroVAD:
        return SileroVAD(self.model_path)


@dataclass
class Utterance:
    audio: np.ndarray                  # int16, 16 kHz, includes pre-roll
    speech_ms: int                     # windows classified as speech
    duration_ms: int
    started_at: float                  # time.monotonic()
    ended_at: float
    max_prob: float
    mean_prob: float
    forced_end: bool = False           # hit max utterance length
    started_during_playback: bool = False
    meta: dict = field(default_factory=dict)


class SegmenterListener(Protocol):
    def on_speech_start(self) -> None: ...
    def on_speech_progress(self, speech_ms: int) -> None: ...
    def on_utterance(self, utt: Utterance) -> None: ...
    def on_speech_abandoned(self) -> None: ...


class Segmenter:
    def __init__(self, vad: VadModel, listener: SegmenterListener, *, threshold: float = 0.5,
                 preroll_ms: int = 300, endpoint_ms: int = 900, min_speech_ms: int = 250,
                 start_ms: int = 64, max_utterance_s: int = 30):
        self.vad = vad
        self.listener = listener
        self.on_threshold = threshold
        self.off_threshold = max(threshold - 0.15, 0.05)
        self.endpoint_ms = endpoint_ms
        self.min_speech_ms = min_speech_ms
        self.start_ms = start_ms
        self.max_ms = max_utterance_s * 1000
        self._pending = np.zeros(0, dtype=np.int16)
        self._preroll: deque[np.ndarray] = deque(maxlen=max(1, preroll_ms // WINDOW_MS))
        self._candidate: list[tuple[np.ndarray, float]] = []
        self._in_speech = False
        self._chunks: list[np.ndarray] = []
        self._probs: list[float] = []
        self._speech_ms = 0
        self._silence_ms = 0
        self._started_at = 0.0
        self.playback_active = lambda: False  # set by the call; marks utterances overlapping playback
        self._started_during_playback = False
        self.dropped_short = 0

    @property
    def in_speech(self) -> bool:
        return self._in_speech or bool(self._candidate)

    def feed(self, pcm: np.ndarray) -> None:
        self._pending = np.concatenate([self._pending, pcm]) if self._pending.size else pcm
        while self._pending.size >= WINDOW:
            window, self._pending = self._pending[:WINDOW], self._pending[WINDOW:]
            self._process(window)

    def _process(self, window: np.ndarray) -> None:
        p = self.vad.prob(window.astype(np.float32) / 32768.0)
        if not self._in_speech:
            if p >= self.on_threshold:
                self._candidate.append((window, p))
                if len(self._candidate) * WINDOW_MS >= self.start_ms:
                    self._begin()
            else:
                for w, _ in self._candidate:
                    self._preroll.append(w)
                self._candidate.clear()
                self._preroll.append(window)
            return
        self._chunks.append(window)
        self._probs.append(p)
        if p >= self.off_threshold:
            self._speech_ms += WINDOW_MS
            self._silence_ms = 0
            self.listener.on_speech_progress(self._speech_ms)
        else:
            self._silence_ms += WINDOW_MS
        duration = len(self._chunks) * WINDOW_MS
        if self._silence_ms >= self.endpoint_ms:
            self._finish(forced=False)
        elif duration >= self.max_ms:
            self._finish(forced=True)

    def _begin(self) -> None:
        self._in_speech = True
        self._started_at = time.monotonic()
        self._started_during_playback = bool(self.playback_active())
        self._chunks = list(self._preroll) + [w for w, _ in self._candidate]
        self._probs = [p for _, p in self._candidate]
        self._speech_ms = len(self._candidate) * WINDOW_MS
        self._silence_ms = 0
        self._candidate.clear()
        self._preroll.clear()
        self.listener.on_speech_start()
        self.listener.on_speech_progress(self._speech_ms)

    def _finish(self, forced: bool) -> None:
        # Keep ~200 ms of the trailing silence; drop the rest.
        keep_tail = max(0, self._silence_ms - 200) // WINDOW_MS
        chunks = self._chunks[: len(self._chunks) - keep_tail] if keep_tail else self._chunks
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
        probs = self._probs or [0.0]
        utt = Utterance(
            audio=audio, speech_ms=self._speech_ms, duration_ms=len(audio) * 1000 // RATE,
            started_at=self._started_at, ended_at=time.monotonic(), max_prob=max(probs),
            mean_prob=float(np.mean(probs)), forced_end=forced,
            started_during_playback=self._started_during_playback)
        self._in_speech = False
        self._chunks, self._probs = [], []
        self._speech_ms = self._silence_ms = 0
        self.vad.reset()
        if utt.speech_ms < self.min_speech_ms:
            self.dropped_short += 1
            self.listener.on_speech_abandoned()
            return
        self.listener.on_utterance(utt)
