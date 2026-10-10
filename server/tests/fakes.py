"""Test doubles: a fake Asterisk (ARI events + a real UDP RTP peer), fake speech, energy VAD."""
from __future__ import annotations

import asyncio
import random
import socket

import numpy as np

from agent_phone.ari import AriError
from agent_phone.audio import FRAME_SAMPLES, pcm_to_be_bytes
from agent_phone.calls import CallManager
from agent_phone.config import Settings
from agent_phone.rtp import PortPool, build_rtp, make_codec, parse_rtp
from agent_phone.sessions import SessionStore
from agent_phone.speech import Transcript


def settings(**kw) -> Settings:
    base = dict(mcp_bearer_token="test-token", ari_password="x", rtp_advertise_host="127.0.0.1",
                rtp_bind_host="127.0.0.1", rtp_port_min=1, rtp_port_max=0, ring_timeout_s=3, idle_timeout_s=30,
                max_call_s=60, max_tool_wait_s=8, default_listen_s=3, db_path=":memory:",
                vad_endpoint_ms=320, vad_min_speech_ms=96, bargein_min_speech_ms=160, vad_preroll_ms=96,
                answer_settle_ms=0, answer_greeting_max_ms=0)
    base.update(kw)
    return Settings(**base)


def free_ports(n: int) -> list[int]:
    socks, ports = [], []
    for _ in range(n):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        socks.append(s)
    for s in socks:
        s.close()
    return ports


class EnergyVAD:
    def prob(self, window: np.ndarray) -> float:
        return 1.0 if float(np.sqrt(np.mean(window ** 2))) > 0.05 else 0.0

    def reset(self) -> None:
        pass


def tone(seconds: float, amp: float = 0.3, freq: float = 440.0) -> np.ndarray:
    t = np.arange(int(16000 * seconds)) / 16000
    return (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)


class FakeSpeech:
    def __init__(self, replies: list[str] | None = None, tts_seconds_per_char: float = 0.02):
        self.replies = list(replies or [])
        self.spc = tts_seconds_per_char
        self.synth_calls: list[str] = []
        self.transcribe_calls = 0

    async def transcribe(self, pcm: np.ndarray) -> Transcript:
        self.transcribe_calls += 1
        await asyncio.sleep(0.01)
        text = self.replies.pop(0) if self.replies else "default reply"
        return Transcript(text=text, stt_seconds=0.01, no_speech_prob=0.01, avg_logprob=-0.2)

    async def synthesize(self, text: str):
        self.synth_calls.append(text)
        await asyncio.sleep(0.005)
        return tone(len(text) * self.spc, amp=0.2, freq=300), 0.005


class FakeAsterisk:
    """Pretends to be ARI. Behaviour per destination: 'answer' | 'busy' | 'reject' | 'noanswer' | 'offline'."""

    def __init__(self, behaviour: dict[str, str] | None = None, answer_delay: float = 0.05):
        self.host = "127.0.0.1"
        self.connected = asyncio.Event()
        self.connected.set()
        self.behaviour = behaviour or {}
        self.answer_delay = answer_delay
        self.handler = None
        self.originated: list[str] = []
        self.hungup: list[str] = []
        self.answered: list[str] = []
        self.torn_down: list[str] = []
        self.rtp_sock: socket.socket | None = None
        self.bridge_addr: tuple[str, int] | None = None
        self.received_frames = 0

    def bind(self, mgr: CallManager) -> None:
        self.handler = mgr.handle_event

    def _emit(self, ev: dict) -> None:
        self.handler(ev)

    async def endpoint_state(self, tech, resource):
        return "offline" if self.behaviour.get(resource) == "offline" else "online"

    async def originate(self, channel_id, endpoint, caller_id, timeout_s, app_args):
        self.originated.append(channel_id)
        dest = endpoint.split("/")[1]
        mode = self.behaviour.get(dest, "answer")
        if mode == "offline":
            raise AriError(500, "Allocation failed")

        async def script():
            await asyncio.sleep(0.01)
            self._emit({"type": "ChannelStateChange", "channel": {"id": channel_id, "state": "Ringing"}})
            await asyncio.sleep(self.answer_delay)
            if mode == "answer":
                self._emit({"type": "StasisStart", "channel": {"id": channel_id}, "args": [app_args]})
            elif mode == "busy":
                self._emit({"type": "ChannelDestroyed", "channel": {"id": channel_id}, "cause": 17, "cause_txt": "User busy"})
            elif mode == "reject":  # real Asterisk sometimes reports the cause only on the hangup request
                self._emit({"type": "ChannelHangupRequest", "channel": {"id": channel_id}, "cause": 21})
                self._emit({"type": "ChannelDestroyed", "channel": {"id": channel_id}, "cause": 0, "cause_txt": "Unknown"})
        asyncio.get_running_loop().create_task(script())

    async def answer(self, channel_id):
        self.answered.append(channel_id)

    def ring_in(self, channel_id: str, endpoint: str = "1001"):
        """The user dials the agent (extension 700 -> Stasis(agentphone,inbound))."""
        self._emit({"type": "StasisStart", "args": ["inbound"],
                    "channel": {"id": channel_id, "name": f"PJSIP/{endpoint}-0000002a", "state": "Ring"}})

    async def hangup(self, channel_id, reason="normal"):
        self.hungup.append(channel_id)

    async def setup_media(self, call_channel_id, media_channel_id, bridge_id, external_host, codec="g722"):
        self.codec = make_codec(codec)
        host, port = external_host.rsplit(":", 1)
        self.bridge_addr = (host, int(port))
        self.rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp_sock.bind(("127.0.0.1", 0))
        self.rtp_sock.setblocking(False)
        asyncio.get_running_loop().create_task(self._drain())
        return "127.0.0.1", self.rtp_sock.getsockname()[1]

    async def _drain(self):
        loop = asyncio.get_running_loop()
        while self.rtp_sock is not None:
            try:
                data = await asyncio.wait_for(loop.sock_recv(self.rtp_sock, 2048), 0.2)
            except (asyncio.TimeoutError, OSError):
                continue
            if parse_rtp(data):
                self.received_frames += 1

    async def teardown_media(self, media_channel_id, bridge_id):
        self.torn_down.append(bridge_id)
        if self.rtp_sock:
            self.rtp_sock.close()
            self.rtp_sock = None

    async def send_audio(self, pcm: np.ndarray, realtime: bool = True):
        """Send caller audio to the bridge as slin16 RTP, 20 ms per packet."""
        seq, ts = random.getrandbits(16), 0
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for i in range(0, pcm.size, FRAME_SAMPLES):
                frame = pcm[i:i + FRAME_SAMPLES]
                if frame.size < FRAME_SAMPLES:
                    frame = np.concatenate([frame, np.zeros(FRAME_SAMPLES - frame.size, dtype=np.int16)])
                sock.sendto(build_rtp(self.codec.pt, seq, ts, 1234, self.codec.encode(frame)), self.bridge_addr)
                seq, ts = seq + 1, ts + self.codec.ts_per_frame
                await asyncio.sleep(0.02 if realtime else 0.001)
        finally:
            sock.close()

    def remote_hangup(self, channel_id: str):
        self._emit({"type": "ChannelDestroyed", "channel": {"id": channel_id}, "cause": 16, "cause_txt": "Normal Clearing"})


def make_manager(behaviour=None, replies=None, **kw):
    s = settings(**kw)
    tel = FakeAsterisk(behaviour)
    speech = FakeSpeech(replies)
    ports = free_ports(4)
    pool = PortPool(0, -1)
    pool._free.extend(ports)
    store = SessionStore(":memory:")
    mgr = CallManager(s, tel, speech, EnergyVAD, port_pool=pool, on_contact=store.touch_contact)
    tel.bind(mgr)
    return s, tel, speech, mgr, store
