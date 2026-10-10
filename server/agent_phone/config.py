"""Runtime settings, read once from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    # MCP / HTTP
    mcp_bearer_token: str
    http_host: str = "0.0.0.0"
    http_port: int = 8765
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:8765", "localhost:8765")

    # Asterisk ARI (on the Pi, over Tailscale)
    ari_url: str = ""  # e.g. http://<pi-tailscale-ip>:8088 (ARI_URL)
    ari_user: str = "agentphone"
    ari_password: str = ""
    ari_app: str = "agentphone"

    # RTP between Asterisk and this bridge
    rtp_advertise_host: str = ""  # address Asterisk sends RTP to: the Mac's Tailscale IP (MAC_TAILSCALE_IP)
    rtp_bind_host: str = "0.0.0.0"
    rtp_port_min: int = 40000
    rtp_port_max: int = 40019
    media_codec: str = "g722"  # g722 | ulaw | slin16 (see rtp.py for why not slin16)

    # Speech services (native on the macOS host)
    stt_url: str = "http://host.docker.internal:8178/inference"
    tts_url: str = "http://host.docker.internal:8179/v1/audio/speech"
    tts_voice: str = "af_heart"
    tts_speed: float = 0.9  # Kokoro speaking rate; 1.0 sounded rushed on a phone

    # Telephone policy
    allowed_destinations: tuple[str, ...] = ("1001", "1002")
    caller_id: str = '"Coding Agent" <700>'
    ring_timeout_s: int = 30
    idle_timeout_s: int = 180
    max_call_s: int = 900
    notify_min_interval_s: int = 300  # Notification-hook calls per session

    # Tool-call bounds (Claude Code's HTTP per-request timer is >= 60 s)
    max_tool_wait_s: int = 50
    default_listen_s: int = 30

    # Sessions
    default_ttl_min: int = 240
    max_ttl_min: int = 1440
    db_path: str = "/data/agent_phone.sqlite"

    # VAD / endpointing
    vad_model_path: str = "/app/models/silero_vad.onnx"
    vad_threshold: float = 0.5
    vad_preroll_ms: int = 300
    vad_endpoint_ms: int = 900
    vad_min_speech_ms: int = 250
    vad_max_utterance_s: int = 30
    inbound_listen_s: int = 20          # how long to wait for the caller's instruction
    inbound_claim_s: int = 30           # how long the session has to pick up a handed-over call
    bargein_min_speech_ms: int = 600       # a picked-up "hello?" (~0.4 s) must not cut the agent off
    answer_settle_ms: int = 800            # min pause after answer (phone switches audio route)
    answer_greeting_max_ms: int = 3000     # if the callee says "hello?", wait for it to finish - up to this

    # Optional debug capture (off by default)
    debug_audio_dir: str | None = None
    debug_retention_hours: int = 24

    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        port = _int("HTTP_PORT", 8765)
        missing = [k for k in ("ARI_URL", "RTP_ADVERTISE_HOST") if not os.environ.get(k)]
        if missing:
            raise SystemExit(f"missing required settings: {', '.join(missing)} (see .env.example)")
        return cls(
            mcp_bearer_token=_env("MCP_BEARER_TOKEN"),
            http_host=_env("HTTP_HOST", "0.0.0.0"),
            http_port=port,
            allowed_hosts=tuple(h.strip() for h in _env("MCP_ALLOWED_HOSTS", f"127.0.0.1:{port},localhost:{port}").split(",") if h.strip()),
            ari_url=_env("ARI_URL", cls.ari_url).rstrip("/"),
            ari_user=_env("ARI_USER", cls.ari_user),
            ari_password=_env("ARI_PASSWORD"),
            ari_app=_env("ARI_APP", cls.ari_app),
            rtp_advertise_host=_env("RTP_ADVERTISE_HOST", cls.rtp_advertise_host),
            rtp_bind_host=_env("RTP_BIND_HOST", cls.rtp_bind_host),
            rtp_port_min=_int("RTP_PORT_MIN", cls.rtp_port_min),
            rtp_port_max=_int("RTP_PORT_MAX", cls.rtp_port_max),
            media_codec=_env("MEDIA_CODEC", cls.media_codec),
            stt_url=_env("STT_URL", cls.stt_url),
            tts_url=_env("TTS_URL", cls.tts_url),
            tts_voice=_env("TTS_VOICE", cls.tts_voice),
            tts_speed=_float("TTS_SPEED", cls.tts_speed),
            allowed_destinations=tuple(d.strip() for d in _env("ALLOWED_DESTINATIONS", "1001,1002").split(",") if d.strip()),
            caller_id=_env("CALLER_ID", cls.caller_id),
            ring_timeout_s=_int("RING_TIMEOUT_S", cls.ring_timeout_s),
            idle_timeout_s=_int("IDLE_TIMEOUT_S", cls.idle_timeout_s),
            max_call_s=_int("MAX_CALL_S", cls.max_call_s),
            notify_min_interval_s=_int("NOTIFY_MIN_INTERVAL_S", cls.notify_min_interval_s),
            max_tool_wait_s=_int("MAX_TOOL_WAIT_S", cls.max_tool_wait_s),
            default_listen_s=_int("DEFAULT_LISTEN_S", cls.default_listen_s),
            default_ttl_min=_int("SESSION_TTL_MIN", cls.default_ttl_min),
            max_ttl_min=_int("SESSION_MAX_TTL_MIN", cls.max_ttl_min),
            db_path=_env("DB_PATH", cls.db_path),
            vad_model_path=_env("VAD_MODEL_PATH", cls.vad_model_path),
            vad_threshold=_float("VAD_THRESHOLD", cls.vad_threshold),
            vad_preroll_ms=_int("VAD_PREROLL_MS", cls.vad_preroll_ms),
            vad_endpoint_ms=_int("VAD_ENDPOINT_MS", cls.vad_endpoint_ms),
            vad_min_speech_ms=_int("VAD_MIN_SPEECH_MS", cls.vad_min_speech_ms),
            vad_max_utterance_s=_int("VAD_MAX_UTTERANCE_S", cls.vad_max_utterance_s),
            inbound_listen_s=_int("INBOUND_LISTEN_S", cls.inbound_listen_s),
            inbound_claim_s=_int("INBOUND_CLAIM_S", cls.inbound_claim_s),
            bargein_min_speech_ms=_int("BARGEIN_MIN_SPEECH_MS", cls.bargein_min_speech_ms),
            answer_settle_ms=_int("ANSWER_SETTLE_MS", cls.answer_settle_ms),
            answer_greeting_max_ms=_int("ANSWER_GREETING_MAX_MS", cls.answer_greeting_max_ms),
            debug_audio_dir=os.environ.get("DEBUG_AUDIO_DIR") or None,
            debug_retention_hours=_int("DEBUG_RETENTION_HOURS", cls.debug_retention_hours),
        )
