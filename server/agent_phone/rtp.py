"""Minimal RTP endpoint for an Asterisk ExternalMedia channel.

Receive: parse RTP, decode, hand 16 kHz int16 frames to a callback.
Send:    a paced playback queue (20 ms frames on a monotonic schedule), encoded.

Codec choice matters on Asterisk <= 20.11: chan_rtp's UnicastRTP channel has no
receive payload mapping, and Asterisk only falls back to the static table for
payload types <= 34. slin16 (dynamic PT 118) therefore works Asterisk -> bridge
but is silently dropped bridge -> Asterisk. G.722 (static PT 9) is wideband
16 kHz and works in both directions, so it is the default.
"""
from __future__ import annotations

import asyncio
import logging
import random
import struct
import time
from collections import deque
from collections.abc import Callable

import numpy as np

from .audio import FRAME_MS, FRAME_SAMPLES, be_bytes_to_pcm, pcm_to_be_bytes, resample

log = logging.getLogger(__name__)


class Slin16Codec:
    """Signed linear 16 kHz, network byte order. Inbound only on Asterisk <= 20.11 (see module doc)."""
    name, pt, ts_per_frame = "slin16", 118, FRAME_SAMPLES

    def encode(self, pcm: np.ndarray) -> bytes:
        return pcm_to_be_bytes(pcm)

    def decode(self, payload: bytes) -> np.ndarray:
        return be_bytes_to_pcm(payload)


class G722Codec:
    """G.722 at 64 kbit/s: 16 kHz audio, RTP clock 8 kHz (RFC 3551 quirk), 160 bytes per 20 ms."""
    name, pt, ts_per_frame = "g722", 9, FRAME_SAMPLES // 2

    def __init__(self):
        from G722 import G722
        self._enc = G722(16000, 64000)  # codec state is per direction
        self._dec = G722(16000, 64000)

    def encode(self, pcm: np.ndarray) -> bytes:
        return self._enc.encode(pcm.astype(np.int16))

    def decode(self, payload: bytes) -> np.ndarray:
        return np.asarray(self._dec.decode(payload), dtype=np.int16)


def _ulaw_encode(x: np.ndarray) -> bytes:
    bias, clip = 0x84, 32635
    s = x.astype(np.int32)
    sign = (s < 0).astype(np.int32) << 7
    mag = np.minimum(np.abs(s), clip) + bias
    exp = np.clip(np.floor(np.log2(mag)).astype(np.int32) - 7, 0, 7)
    mant = (mag >> (exp + 3)) & 0x0F
    return (~(sign | (exp << 4) | mant) & 0xFF).astype(np.uint8).tobytes()


def _ulaw_decode(b: bytes) -> np.ndarray:
    u = ~np.frombuffer(b, dtype=np.uint8).astype(np.int32) & 0xFF
    sign, exp, mant = u & 0x80, (u >> 4) & 0x07, u & 0x0F
    mag = ((mant << 3) + 0x84) << exp
    return np.where(sign, 0x84 - mag, mag - 0x84).astype(np.int16)


class UlawCodec:
    """G.711 mu-law, 8 kHz narrowband (resampled to/from 16 kHz). Fallback only."""
    name, pt, ts_per_frame = "ulaw", 0, FRAME_SAMPLES // 2

    def encode(self, pcm: np.ndarray) -> bytes:
        return _ulaw_encode(resample(pcm, 16000, 8000))

    def decode(self, payload: bytes) -> np.ndarray:
        return resample(_ulaw_decode(payload), 8000, 16000)


CODECS = {"g722": G722Codec, "slin16": Slin16Codec, "ulaw": UlawCodec}


def make_codec(name: str):
    try:
        return CODECS[name]()
    except KeyError:
        raise ValueError(f"unsupported external media codec {name!r}; use one of {sorted(CODECS)}") from None


def parse_rtp(packet: bytes) -> tuple[int, int, int, bytes] | None:
    """Return (payload_type, seq, timestamp, payload) or None if not RTP v2."""
    if len(packet) < 12:
        return None
    b0, b1, seq, ts, _ssrc = struct.unpack("!BBHII", packet[:12])
    if b0 >> 6 != 2:
        return None
    offset = 12 + 4 * (b0 & 0x0F)
    if b0 & 0x10:  # header extension
        if len(packet) < offset + 4:
            return None
        ext_len = struct.unpack("!H", packet[offset + 2:offset + 4])[0]
        offset += 4 + 4 * ext_len
    end = len(packet)
    if b0 & 0x20:  # padding
        end -= packet[-1]
    if offset > end:
        return None
    return b1 & 0x7F, seq, ts, packet[offset:end]


def build_rtp(pt: int, seq: int, ts: int, ssrc: int, payload: bytes, marker: bool = False) -> bytes:
    return struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc) + payload


class PortPool:
    def __init__(self, lo: int, hi: int):
        self._free = deque(range(lo, hi + 1))
        self._used: set[int] = set()

    def acquire(self) -> int:
        if not self._free:
            raise RuntimeError("no free RTP ports")
        port = self._free.popleft()
        self._used.add(port)
        return port

    def release(self, port: int) -> None:
        if port in self._used:
            self._used.remove(port)
            self._free.append(port)

    @property
    def in_use(self) -> int:
        return len(self._used)


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, endpoint: "RtpEndpoint"):
        self.endpoint = endpoint

    def datagram_received(self, data: bytes, addr) -> None:
        self.endpoint._on_packet(data, addr)


class RtpEndpoint:
    """One UDP socket per call. Thread-free; everything runs on the event loop."""

    MAX_QUEUE_FRAMES = 50 * 120  # 2 minutes of audio; bounded

    def __init__(self, bind_host: str, port: int, on_audio: Callable[[np.ndarray], None], codec=None):
        self.bind_host = bind_host
        self.port = port
        self.on_audio = on_audio
        self.codec = codec or G722Codec()
        self.remote: tuple[str, int] | None = None  # where Asterisk listens (from ARI)
        self.pt = self.codec.pt
        self._transport: asyncio.DatagramTransport | None = None
        self._queue: deque[np.ndarray] = deque()
        self._ssrc = random.getrandbits(32)
        self._seq = random.getrandbits(16)
        self._ts_base = random.getrandbits(32)
        self._t0 = time.monotonic()
        self._sender: asyncio.Task | None = None
        self._wakeup = asyncio.Event()
        self._talkspurt = True
        self.packets_in = 0
        self.packets_out = 0
        self.bad_packets = 0
        self.first_packet_at: float | None = None
        self.drain_waiters: list[asyncio.Future] = []

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Proto(self), local_addr=(self.bind_host, self.port))
        self._sender = asyncio.create_task(self._send_loop(), name=f"rtp-send-{self.port}")
        self._gap_filler = asyncio.create_task(self._gap_fill_loop(), name=f"rtp-gap-{self.port}")

    def _on_packet(self, data: bytes, addr) -> None:
        parsed = parse_rtp(data)
        if parsed is None:
            self.bad_packets += 1
            return
        pt, _seq, _ts, payload = parsed
        if pt != self.codec.pt:  # e.g. comfort noise (13): the gap filler covers it as silence
            self.other_pt_packets += 1
            return
        now = time.monotonic()
        if self.packets_in == 0:
            self.first_packet_at = now
        self.packets_in += 1
        self._last_audio_at = now
        if payload:
            self.on_audio(self.codec.decode(payload))

    other_pt_packets: int = 0

    _last_audio_at: float = 0.0
    _gap_filler: asyncio.Task | None = None
    GAP_S = 0.12  # treat >120 ms without packets as silence (DTX / silence suppression upstream)

    async def _gap_fill_loop(self) -> None:
        """Feed silence while the far end sends nothing, so VAD endpointing keeps a real-time clock."""
        silence = np.zeros(FRAME_SAMPLES, dtype=np.int16)
        while True:
            await asyncio.sleep(FRAME_MS / 1000)
            if not self._last_audio_at:
                continue
            gap = time.monotonic() - self._last_audio_at
            if gap >= self.GAP_S:
                n = min(int(gap / (FRAME_MS / 1000)), 50)
                for _ in range(n):
                    self.on_audio(silence)
                self.filled_frames += n
                self._last_audio_at += n * FRAME_MS / 1000

    filled_frames: int = 0

    # ---- playback -------------------------------------------------------
    def enqueue(self, frames: list[np.ndarray]) -> None:
        room = self.MAX_QUEUE_FRAMES - len(self._queue)
        if room < len(frames):
            log.warning("playback queue full; dropping %d frames", len(frames) - room)
            frames = frames[:max(room, 0)]
        self._queue.extend(frames)
        self._wakeup.set()

    def clear_playback(self) -> int:
        dropped = len(self._queue)
        self._queue.clear()
        self._resolve_drain()
        return dropped

    @property
    def playing(self) -> bool:
        return bool(self._queue)

    @property
    def queued_seconds(self) -> float:
        return len(self._queue) * FRAME_MS / 1000

    async def wait_drained(self) -> None:
        if not self._queue:
            return
        fut = asyncio.get_running_loop().create_future()
        self.drain_waiters.append(fut)
        await fut

    def _resolve_drain(self) -> None:
        waiters, self.drain_waiters = self.drain_waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)

    async def _send_loop(self) -> None:
        interval = FRAME_MS / 1000
        next_at = time.monotonic()
        while True:
            if not self._queue:
                self._resolve_drain()
                self._talkspurt = True
                self._wakeup.clear()
                await self._wakeup.wait()
                next_at = time.monotonic()
            frame = self._queue.popleft() if self._queue else None
            if frame is not None and self.remote and self._transport:
                # Timestamp follows wall clock so gaps between talkspurts stay consistent.
                n_frames = int((time.monotonic() - self._t0) / interval)
                ts = self._ts_base + n_frames * self.codec.ts_per_frame
                pkt = build_rtp(self.pt, self._seq, ts, self._ssrc, self.codec.encode(frame), marker=self._talkspurt)
                self._transport.sendto(pkt, self.remote)
                self._seq = (self._seq + 1) & 0xFFFF
                self._talkspurt = False
                self.packets_out += 1
            next_at += interval
            delay = next_at - time.monotonic()
            if delay < -0.1:  # fell far behind (e.g. loop stall): resync instead of bursting
                next_at = time.monotonic()
                delay = 0
            await asyncio.sleep(max(delay, 0))

    async def close(self) -> None:
        self.clear_playback()
        for task in (self._sender, self._gap_filler):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self._transport:
            self._transport.close()
            self._transport = None
