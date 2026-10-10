"""Phone-mode sessions (persisted) and idempotency cache (in memory).

A phone session is created by phone_enable and identified by a random bearer
token returned only to the caller. Only a SHA-256 hash of the token is stored.
Every other tool requires the token, so concurrent CLI sessions cannot see or
drive each other's calls. The CLI's own session id (Claude Code's
CLAUDE_SESSION_ID) is stored alongside so lifecycle hooks can find the session.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable


class PhoneError(Exception):
    """Error returned to the agent as a structured result."""

    def __init__(self, code: str, message: str, retryable: bool = False, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details

    def to_dict(self) -> dict:
        return {"ok": False, "error": {"code": self.code, "message": self.message,
                                       "retryable": self.retryable, **self.details}}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class PhoneSession:
    session_id: str
    destination: str
    project_label: str
    cli: str
    cli_session_id: str | None
    created_at: float
    expires_at: float
    disabled_at: float | None
    last_contact_at: float | None
    last_stop_check_at: float | None

    @property
    def active(self) -> bool:
        return self.disabled_at is None and time.time() < self.expires_at

    def public(self) -> dict:
        d = asdict(self)
        d["active"] = self.active
        d["expires_in_s"] = max(0, int(self.expires_at - time.time()))
        return d


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  session_id TEXT UNIQUE NOT NULL,
  destination TEXT NOT NULL,
  project_label TEXT NOT NULL,
  cli TEXT NOT NULL,
  cli_session_id TEXT,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  disabled_at REAL,
  last_contact_at REAL,
  last_stop_check_at REAL
);
CREATE INDEX IF NOT EXISTS sessions_cli ON sessions(cli, cli_session_id);
"""
_COLS = "session_id, destination, project_label, cli, cli_session_id, created_at, expires_at, disabled_at, last_contact_at, last_stop_check_at"


class SessionStore:
    def __init__(self, db_path: str):
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def _row(self, row) -> PhoneSession | None:
        return PhoneSession(*row) if row else None

    def enable(self, destination: str, project_label: str, cli: str, cli_session_id: str | None,
               ttl_s: int) -> tuple[str, PhoneSession]:
        now = time.time()
        if cli_session_id:
            # Re-enabling from the same CLI session replaces the previous phone session.
            self._db.execute("UPDATE sessions SET disabled_at=? WHERE cli=? AND cli_session_id=? AND disabled_at IS NULL",
                             (now, cli, cli_session_id))
        token = "pt_" + secrets.token_urlsafe(24)
        session_id = "ps_" + secrets.token_hex(6)
        self._db.execute(
            f"INSERT INTO sessions (token_hash, {_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_hash(token), session_id, destination, project_label, cli, cli_session_id, now, now + ttl_s,
             None, None, now))
        self._db.commit()
        return token, self.get_by_id(session_id)

    def get_by_id(self, session_id: str) -> PhoneSession | None:
        cur = self._db.execute(f"SELECT {_COLS} FROM sessions WHERE session_id=?", (session_id,))
        return self._row(cur.fetchone())

    def authenticate(self, token: str | None) -> PhoneSession:
        """Resolve a token to an active session or raise."""
        if not token or not isinstance(token, str):
            raise PhoneError("invalid_session_token", "session_token is required; call phone_enable first")
        h = _hash(token)
        cur = self._db.execute(f"SELECT token_hash, {_COLS} FROM sessions WHERE token_hash=?", (h,))
        row = cur.fetchone()
        if not row or not hmac.compare_digest(row[0], h):
            raise PhoneError("invalid_session_token", "unknown session_token")
        s = PhoneSession(*row[1:])
        if s.disabled_at is not None:
            raise PhoneError("session_disabled", "phone mode was disabled for this session; call phone_enable again")
        if time.time() >= s.expires_at:
            raise PhoneError("session_expired", "phone mode expired for this session; call phone_enable again")
        return s

    def disable(self, session_id: str) -> None:
        self._db.execute("UPDATE sessions SET disabled_at=? WHERE session_id=? AND disabled_at IS NULL",
                         (time.time(), session_id))
        self._db.commit()

    def touch_contact(self, session_id: str) -> None:
        self._db.execute("UPDATE sessions SET last_contact_at=? WHERE session_id=?", (time.time(), session_id))
        self._db.commit()

    def find_active_for_cli(self, cli: str, cli_session_id: str) -> PhoneSession | None:
        cur = self._db.execute(
            f"SELECT {_COLS} FROM sessions WHERE cli=? AND cli_session_id=? AND disabled_at IS NULL "
            "AND expires_at>? ORDER BY created_at DESC LIMIT 1", (cli, cli_session_id, time.time()))
        return self._row(cur.fetchone())

    def active_for_destination(self, destination: str) -> list[PhoneSession]:
        """Active phone sessions that call this extension, newest first (inbound-call routing)."""
        cur = self._db.execute(
            f"SELECT {_COLS} FROM sessions WHERE destination=? AND disabled_at IS NULL AND expires_at>? "
            "ORDER BY created_at DESC", (destination, time.time()))
        return [PhoneSession(*r) for r in cur.fetchall()]

    def mark_stop_check(self, session_id: str) -> None:
        self._db.execute("UPDATE sessions SET last_stop_check_at=? WHERE session_id=?", (time.time(), session_id))
        self._db.commit()

    def expired_unswept(self) -> list[PhoneSession]:
        """Sessions past expiry that were never disabled (so their calls can be hung up)."""
        cur = self._db.execute(f"SELECT {_COLS} FROM sessions WHERE disabled_at IS NULL AND expires_at<=?",
                               (time.time(),))
        return [PhoneSession(*r) for r in cur.fetchall()]


class IdempotencyCache:
    """Maps (session, tool, key) -> in-flight or finished result.

    A retry with the same key awaits the original operation instead of starting
    a second one, so a client-side timeout followed by a retry never places a
    second call or replays speech. Reusing a key with different arguments is an
    error. Entries expire after `ttl_s`.
    """

    def __init__(self, ttl_s: float = 3600, max_entries: int = 5000):
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._entries: dict[tuple[str, str, str], tuple[float, str, asyncio.Task]] = {}

    def _sweep(self) -> None:
        now = time.monotonic()
        for k in [k for k, (t, _, task) in self._entries.items() if now - t > self.ttl_s and task.done()]:
            del self._entries[k]
        while len(self._entries) > self.max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            del self._entries[oldest]

    async def run(self, session_id: str, tool: str, key: str, args: dict,
                  fn: Callable[[], Awaitable[dict]], wait_s: float | None = None) -> dict:
        if not key or len(key) > 200:
            raise PhoneError("invalid_idempotency_key", "idempotency_key must be 1-200 characters")
        self._sweep()
        fingerprint = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
        k = (session_id, tool, key)
        replay = k in self._entries
        if replay:
            _, fp, task = self._entries[k]
            if fp != fingerprint:
                raise PhoneError("idempotency_key_reused", "idempotency_key was already used with different arguments")
        else:
            task = asyncio.create_task(fn())
            self._entries[k] = (time.monotonic(), fingerprint, task)
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=wait_s)
        except asyncio.TimeoutError:
            return {"ok": True, "in_progress": True, "idempotent_replay": replay,
                    "hint": "operation still running; retry with the same idempotency_key to get its result"}
        except PhoneError as e:
            out = e.to_dict()
            out["idempotent_replay"] = replay
            return out
        result = dict(result)
        result["idempotent_replay"] = replay
        return result
