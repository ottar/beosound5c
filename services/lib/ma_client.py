"""Shared Music Assistant websocket client.

Both the music_assistant player (services/players/music_assistant_player.py) and
source (services/sources/music_assistant/service.py) talk to the Music
Assistant server's native websocket API (default ws://<host>:8095/ws).
This module centralises the protocol so the two services can't drift:

  * URL/token resolution: MASS_WS_URL / MASS_TOKEN env (from secrets.env)
    with config fallbacks — see resolve_ws_url().
  * Handshake: the server sends a ServerInfo hello, then requires an
    ``auth`` command before anything else; events are broadcast
    automatically to every authenticated client (MA 2.9, schema 31 —
    there is NO players/subscribe command).
  * message_id correlation for request/response, including ``partial``
    result chunks (large library listings arrive as several messages
    with the same message_id).
  * Reconnect loop with backoff, owned by the client (start()/close()).

Command result messages carry {message_id, result[, partial]}; errors
carry {message_id, error_code, details}. Event messages carry
{event, object_id[, data]} and no message_id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.parse import quote

import aiohttp

from .background_tasks import BackgroundTaskSet
from .config import cfg

log = logging.getLogger("ma-client")

DEFAULT_PORT = 8095
CONNECT_TIMEOUT = 10
CALL_TIMEOUT = 30
RECONNECT_DELAY_INITIAL = 1
RECONNECT_DELAY_MAX = 30


def resolve_ws_url() -> str:
    """Resolve the MA websocket URL.

    Precedence: MASS_WS_URL env (secrets.env, kept name-compatible with
    the beosound5c_extension fork so existing installs need no
    migration) → music_assistant.url in config.json → player.ip with
    the default port.
    """
    configured = (os.getenv("MASS_WS_URL") or "").strip()
    if not configured:
        configured = (cfg("music_assistant", "url", default="") or "").strip()
    if configured:
        if configured.startswith(("ws://", "wss://")):
            return configured
        return f"ws://{configured}:{DEFAULT_PORT}/ws"
    host = (cfg("player", "ip", default="") or "").strip()
    if host:
        return f"ws://{host}:{DEFAULT_PORT}/ws"
    return f"ws://localhost:{DEFAULT_PORT}/ws"


def resolve_token() -> str:
    return (os.getenv("MASS_TOKEN") or "").strip()


class MAClientError(Exception):
    """A command failed — either transport-level or an MA error result."""

    def __init__(self, message: str, error_code: int | None = None):
        super().__init__(message)
        self.error_code = error_code


class MAClient:
    """Persistent websocket connection to a Music Assistant server.

    Usage::

        client = MAClient(on_event=my_async_callback,
                          on_connect=my_async_resync)
        await client.start()          # spawns the maintain/reconnect loop
        players = await client.call("players/all")
        await client.close()

    ``ws_factory`` exists for tests: an async callable returning a
    ws-like object (send_json + async message iteration + close).
    """

    def __init__(self, ws_url: str | None = None, token: str | None = None,
                 session: aiohttp.ClientSession | None = None,
                 on_event=None, on_connect=None, on_disconnect=None,
                 ws_factory=None):
        self.ws_url = ws_url or resolve_ws_url()
        self.token = token if token is not None else resolve_token()
        self.server_info: dict = {}
        self._session = session
        self._own_session = session is None
        self._on_event = on_event
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._ws_factory = ws_factory
        self._ws = None
        self._connected = asyncio.Event()
        self._closing = False
        self._maintain_task: asyncio.Task | None = None
        self._msg_id = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._partials: dict[str, list] = {}
        self._tasks = BackgroundTaskSet(log, label="ma-client")

    # ── Public API ──

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self):
        """Spawn the maintain loop (connect + dispatch + reconnect)."""
        if self._maintain_task is None or self._maintain_task.done():
            self._closing = False
            self._maintain_task = self._tasks.spawn(
                self._maintain(), name="ma_client_maintain")

    async def wait_connected(self, timeout: float = CONNECT_TIMEOUT) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def call(self, command: str, timeout: float = CALL_TIMEOUT, **args):
        """Send a command and await its (chunk-assembled) result."""
        if not self.connected or self._ws is None:
            raise MAClientError(f"not connected to MA ({self.ws_url})")
        self._msg_id += 1
        msg_id = str(self._msg_id)
        payload = {"message_id": msg_id, "command": command}
        if args:
            payload["args"] = args
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._ws.send_json(payload)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise MAClientError(f"{command} timed out after {timeout}s") from None
        finally:
            self._pending.pop(msg_id, None)
            self._partials.pop(msg_id, None)

    @property
    def http_base(self) -> str:
        """The MA server's HTTP base URL, derived from the ws URL."""
        base = self.ws_url
        for scheme, http in (("wss://", "https://"), ("ws://", "http://")):
            if base.startswith(scheme):
                base = http + base[len(scheme):]
                break
        if base.endswith("/ws"):
            base = base[:-3]
        return base

    def imageproxy_url(self, path: str, provider: str, size: int = 256) -> str:
        """HTTP artwork URL on the MA server for a MediaItemImage path."""
        return (f"{self.http_base}/imageproxy?path={quote(path, safe='')}"
                f"&provider={quote(provider, safe='')}&size={size}")

    def image_url_for(self, image: dict, size: int = 256) -> str:
        """Best artwork URL for a MediaItemImage dict.

        Remotely accessible images are returned as-is; local ones go
        through the MA imageproxy via their proxy_id (the same URL shape
        MA itself puts in current_media.image_url).
        """
        path = image.get("path") or ""
        if image.get("remotely_accessible") and path.startswith("http"):
            return path
        proxy_id = image.get("proxy_id")
        if proxy_id:
            return f"{self.http_base}/imageproxy/{proxy_id}?size={size}&fmt=jpg"
        if path:
            return self.imageproxy_url(path, image.get("provider", ""), size)
        return ""

    async def close(self):
        self._closing = True
        await self._tasks.cancel_all()
        self._maintain_task = None
        await self._teardown_ws()
        if self._own_session and self._session:
            await self._session.close()
            self._session = None

    # ── Connection lifecycle ──

    async def _maintain(self):
        delay = RECONNECT_DELAY_INITIAL
        while not self._closing:
            try:
                await self._connect_once()
                delay = RECONNECT_DELAY_INITIAL
                await self._dispatch_loop()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("MA connection lost (%s): %s", self.ws_url, e)
            await self._teardown_ws()
            if self._closing:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)

    async def _connect_once(self):
        if self._ws_factory is not None:
            self._ws = await self._ws_factory()
        else:
            if self._session is None:
                self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(
                self.ws_url, heartbeat=25,
                timeout=aiohttp.ClientWSTimeout(ws_close=CONNECT_TIMEOUT))
        # Server hello (ServerInfo) arrives first, before any command.
        self.server_info = await self._receive_json()
        log.info("Connected to MA %s (schema %s)",
                 self.server_info.get("server_version"),
                 self.server_info.get("schema_version"))
        if self.token:
            await self._ws.send_json({
                "message_id": "auth", "command": "auth",
                "args": {"token": self.token}})
            res = await self._receive_json()
            authenticated = (res.get("result") or {}).get("authenticated")
            if not authenticated:
                raise MAClientError(
                    f"MA auth rejected: {res.get('details') or res.get('error_code')}",
                    error_code=res.get("error_code"))
        else:
            log.warning("No MASS_TOKEN set — MA commands will be rejected")
        self._connected.set()
        if self._on_connect:
            try:
                await self._on_connect()
            except Exception as e:
                log.error("on_connect callback failed: %s", e)

    async def _receive_json(self) -> dict:
        msg = await asyncio.wait_for(self._ws.receive(), CONNECT_TIMEOUT)
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise MAClientError(f"unexpected ws message during handshake: {msg.type}")
        return json.loads(msg.data)

    async def _teardown_ws(self):
        was_connected = self._connected.is_set()
        self._connected.clear()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MAClientError("connection lost"))
        self._pending.clear()
        self._partials.clear()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if was_connected and self._on_disconnect:
            try:
                await self._on_disconnect()
            except Exception as e:
                log.error("on_disconnect callback failed: %s", e)

    # ── Message dispatch ──

    async def _dispatch_loop(self):
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    log.warning("Non-JSON ws message ignored")
                    continue
                self._handle_message(data)
            elif msg.type in (aiohttp.WSMsgType.ERROR,
                              aiohttp.WSMsgType.CLOSE,
                              aiohttp.WSMsgType.CLOSED):
                break

    def _handle_message(self, data: dict):
        msg_id = data.get("message_id")
        if msg_id is not None:
            msg_id = str(msg_id)
            fut = self._pending.get(msg_id)
            if fut is None or fut.done():
                return
            if "error_code" in data:
                fut.set_exception(MAClientError(
                    str(data.get("details") or data.get("error_code")),
                    error_code=data.get("error_code")))
                return
            result = data.get("result")
            if data.get("partial"):
                self._partials.setdefault(msg_id, []).extend(result or [])
                return
            chunks = self._partials.pop(msg_id, None)
            if chunks is not None:
                chunks.extend(result or [])
                result = chunks
            fut.set_result(result)
            return
        if "event" in data:
            if self._on_event:
                self._tasks.spawn(self._safe_event(data),
                                  name=f"ma_event_{data.get('event')}")

    async def _safe_event(self, event: dict):
        try:
            await self._on_event(event)
        except Exception as e:
            log.error("MA event handler failed for %s: %s",
                      event.get("event"), e)
