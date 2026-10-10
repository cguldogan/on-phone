"""Push channel to running Claude Code sessions.

Each Claude Code session started with the agent-phone channel runs a small stdio bridge
(integrations/claude/bin/agent-phone-channel). The bridge holds an SSE stream open to
GET /channel/events?cli_session_id=<its session id> and turns every event into a
`notifications/claude/channel` message, which wakes the session even when it is idle.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class ChannelHub:
    def __init__(self, queue_size: int = 16):
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._queue_size = queue_size

    def subscribe(self, cli_session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subs.setdefault(cli_session_id, set()).add(q)
        log.info("channel listener connected for cli session %s", cli_session_id)
        return q

    def unsubscribe(self, cli_session_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(cli_session_id)
        if subs is not None:
            subs.discard(q)
            if not subs:
                del self._subs[cli_session_id]
        log.info("channel listener disconnected for cli session %s", cli_session_id)

    def listening(self, cli_session_id: str | None) -> bool:
        return bool(cli_session_id and self._subs.get(cli_session_id))

    def publish(self, cli_session_id: str, content: str, meta: dict[str, str]) -> int:
        """Queue an event for every listener of that session; returns how many got it."""
        delivered = 0
        for q in list(self._subs.get(cli_session_id, ())):
            try:
                q.put_nowait({"content": content, "meta": meta})
                delivered += 1
            except asyncio.QueueFull:
                log.warning("channel listener for %s is not draining; event dropped", cli_session_id)
        return delivered

    def snapshot(self) -> dict[str, int]:
        return {k: len(v) for k, v in self._subs.items()}
