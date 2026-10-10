"""Asterisk REST Interface client: REST calls plus the event websocket.

Only the handful of operations the bridge needs. Events are normalised to
small dicts and handed to a single callback on the event loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from urllib.parse import urlencode, urlparse

import httpx
import websockets

log = logging.getLogger(__name__)


class AriError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"ARI {status}: {message}")
        self.status = status


class AriTelephony:
    def __init__(self, base_url: str, user: str, password: str, app: str):
        self.base = base_url.rstrip("/") + "/ari"
        self.app = app
        self.host = urlparse(base_url).hostname
        self._auth = (user, password)
        self._ws_url = (base_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
                        + "/ari/events?" + urlencode({"app": app, "subscribeAll": "false"}))
        self._http = httpx.AsyncClient(auth=self._auth, timeout=10)
        self._task: asyncio.Task | None = None
        self._handler: Callable[[dict], None] | None = None
        self.connected = asyncio.Event()

    # ---- lifecycle --------------------------------------------------------
    async def start(self, handler: Callable[[dict], None]) -> None:
        self._handler = handler
        self._task = asyncio.create_task(self._ws_loop(), name="ari-events")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self._http.aclose()

    async def _ws_loop(self) -> None:
        backoff = 1.0
        import base64
        auth = base64.b64encode(f"{self._auth[0]}:{self._auth[1]}".encode()).decode()
        while True:
            try:
                async with websockets.connect(self._ws_url, additional_headers={"Authorization": f"Basic {auth}"},
                                              ping_interval=20, ping_timeout=20, open_timeout=10) as ws:
                    log.info("ARI websocket connected (app=%s)", self.app)
                    self.connected.set()
                    backoff = 1.0
                    await self._cleanup_orphans()
                    async for raw in ws:
                        try:
                            ev = json.loads(raw)
                        except ValueError:
                            continue
                        if self._handler:
                            try:
                                self._handler(ev)
                            except Exception:  # noqa: BLE001
                                log.exception("event handler failed for %s", ev.get("type"))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("ARI websocket error: %s; reconnecting in %.0fs", type(e).__name__, backoff)
            self.connected.clear()
            if self._handler:
                self._handler({"type": "AriDisconnected"})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _cleanup_orphans(self) -> None:
        """Hang up channels/bridges left by a previous process (ids are prefixed 'ap-'; inbound calls are
        recognised by their Stasis dialplan app)."""
        try:
            for ch in await self._req("GET", "/channels"):
                dp = ch.get("dialplan") or {}
                inbound = dp.get("app_name") == "Stasis" and dp.get("app_data", "").startswith(f"{self.app},inbound")
                if ch.get("id", "").startswith("ap-") or inbound:
                    await self.hangup(ch["id"])
            for br in await self._req("GET", "/bridges"):
                if br.get("id", "").startswith("ap-"):
                    await self._req("DELETE", f"/bridges/{br['id']}", ok_missing=True)
        except Exception as e:  # noqa: BLE001
            log.warning("orphan cleanup failed: %s", e)

    # ---- REST ---------------------------------------------------------------
    async def _req(self, method: str, path: str, params: dict | None = None, json_body: dict | None = None,
                   ok_missing: bool = False):
        r = await self._http.request(method, self.base + path, params=params, json=json_body)
        if ok_missing and r.status_code == 404:
            return None
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise AriError(r.status_code, msg)
        return r.json() if r.content else None

    async def ping(self) -> dict:
        return await self._req("GET", "/asterisk/info", params={"only": "system"})

    async def endpoint_state(self, tech: str, resource: str) -> str:
        """'online' | 'offline' | 'unknown' (Asterisk endpoint state)."""
        d = await self._req("GET", f"/endpoints/{tech}/{resource}", ok_missing=True)
        return (d or {}).get("state", "unknown") if d is not None else "missing"

    async def originate(self, channel_id: str, endpoint: str, caller_id: str, timeout_s: int, app_args: str) -> None:
        await self._req("POST", "/channels", params={
            "endpoint": endpoint, "app": self.app, "appArgs": app_args, "callerId": caller_id,
            "timeout": str(timeout_s), "channelId": channel_id})

    async def answer(self, channel_id: str) -> None:
        await self._req("POST", f"/channels/{channel_id}/answer")

    async def hangup(self, channel_id: str, reason: str = "normal") -> None:
        await self._req("DELETE", f"/channels/{channel_id}", params={"reason": reason}, ok_missing=True)

    async def setup_media(self, call_channel_id: str, media_channel_id: str, bridge_id: str,
                          external_host: str, codec: str = "g722") -> tuple[str, int]:
        """Create ExternalMedia (RTP) + mixing bridge. Returns Asterisk's RTP (host, port)."""
        ch = await self._req("POST", "/channels/externalMedia", params={
            "app": self.app, "external_host": external_host, "format": codec,
            "encapsulation": "rtp", "transport": "udp", "direction": "both", "channelId": media_channel_id})
        port = (ch.get("channelvars") or {}).get("UNICASTRTP_LOCAL_PORT")
        if not port:
            v = await self._req("GET", f"/channels/{media_channel_id}/variable",
                                params={"variable": "UNICASTRTP_LOCAL_PORT"})
            port = v["value"]
        await self._req("POST", "/bridges", params={"type": "mixing", "bridgeId": bridge_id, "name": bridge_id})
        await self._req("POST", f"/bridges/{bridge_id}/addChannel",
                        params={"channel": f"{call_channel_id},{media_channel_id}"})
        # Asterisk binds UnicastRTP on all interfaces; reach it via the ARI host (Tailscale IP).
        return self.host, int(port)

    async def teardown_media(self, media_channel_id: str, bridge_id: str) -> None:
        for coro in (self.hangup(media_channel_id),
                     self._req("DELETE", f"/bridges/{bridge_id}", ok_missing=True)):
            try:
                await coro
            except Exception as e:  # noqa: BLE001
                log.debug("teardown: %s", e)
