"""Tests for the shared Music Assistant websocket client.

Exercised with a fake ws object injected via ws_factory — no network.
Pins the MA 2.9 protocol contract: hello → auth handshake, message_id
correlation (including out-of-order responses and partial result
chunks), error results, automatic event dispatch (no subscribe
command), URL/token resolution, and imageproxy URL derivation.
"""

from __future__ import annotations

import asyncio
import json
import types

import aiohttp
import pytest

from lib.ma_client import (
    MAClient,
    MAClientError,
    resolve_ws_url,
)

HELLO = {"server_id": "abc", "server_version": "2.9.2", "schema_version": 31}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _text_msg(data: dict):
    return types.SimpleNamespace(type=aiohttp.WSMsgType.TEXT,
                                 data=json.dumps(data))


_CLOSE = types.SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None)


class FakeWS:
    """Minimal stand-in for an aiohttp ClientWebSocketResponse."""

    def __init__(self, hello=HELLO, auth_ok=True, responder=None):
        self.sent = []
        self.queue = asyncio.Queue()
        self.auth_ok = auth_ok
        self.responder = responder  # fn(payload) -> iterable of reply dicts
        self.closed = False
        if hello is not None:
            self.feed(hello)

    def feed(self, data: dict):
        self.queue.put_nowait(_text_msg(data))

    async def send_json(self, payload):
        self.sent.append(payload)
        if payload.get("command") == "auth":
            self.feed({"message_id": payload["message_id"],
                       "result": {"authenticated": self.auth_ok}})
        elif self.responder is not None:
            for reply in self.responder(payload) or []:
                self.feed(reply)

    async def receive(self):
        return await self.queue.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self.queue.get()
        if msg.type == aiohttp.WSMsgType.CLOSED:
            raise StopAsyncIteration
        return msg

    async def close(self):
        self.closed = True
        self.queue.put_nowait(_CLOSE)


def _client(ws: FakeWS, **kwargs) -> MAClient:
    return MAClient(ws_url="ws://ma.test:8095/ws", token="tok",
                    ws_factory=lambda: _aret(ws), **kwargs)


async def _aret(value):
    return value


# ── Handshake ──

def test_handshake_sends_auth_and_connects():
    async def scenario():
        ws = FakeWS()
        client = _client(ws)
        await client.start()
        assert await client.wait_connected(2)
        assert client.server_info["server_version"] == "2.9.2"
        auth = ws.sent[0]
        assert auth["command"] == "auth"
        assert auth["args"] == {"token": "tok"}
        await client.close()
    _run(scenario())


def test_auth_rejected_raises():
    async def scenario():
        ws = FakeWS(auth_ok=False)
        client = _client(ws)
        client._ws = await client._ws_factory()
        with pytest.raises(MAClientError):
            await client._connect_once()
        assert not client.connected
    _run(scenario())


# ── Command correlation ──

def test_call_correlates_out_of_order_responses():
    async def scenario():
        ws = FakeWS()
        client = _client(ws)
        await client.start()
        assert await client.wait_connected(2)

        t1 = asyncio.create_task(client.call("players/all"))
        t2 = asyncio.create_task(client.call("players/get", player_id="x"))
        await asyncio.sleep(0.01)
        ids = [m["message_id"] for m in ws.sent if m["command"] != "auth"]
        # Respond in reverse order
        ws.feed({"message_id": ids[1], "result": {"player_id": "x"}})
        ws.feed({"message_id": ids[0], "result": [1, 2, 3]})
        assert await t1 == [1, 2, 3]
        assert (await t2)["player_id"] == "x"
        await client.close()
    _run(scenario())


def test_partial_chunks_are_concatenated():
    async def scenario():
        ws = FakeWS()
        client = _client(ws)
        await client.start()
        assert await client.wait_connected(2)

        task = asyncio.create_task(client.call("music/tracks/library_items"))
        await asyncio.sleep(0.01)
        msg_id = [m["message_id"] for m in ws.sent if m["command"] != "auth"][0]
        ws.feed({"message_id": msg_id, "result": ["a", "b"], "partial": True})
        ws.feed({"message_id": msg_id, "result": ["c"], "partial": True})
        ws.feed({"message_id": msg_id, "result": ["d"]})
        assert await task == ["a", "b", "c", "d"]
        await client.close()
    _run(scenario())


def test_error_result_raises():
    async def scenario():
        def responder(payload):
            return [{"message_id": payload["message_id"],
                     "error_code": 4, "details": "no such player"}]
        ws = FakeWS(responder=responder)
        client = _client(ws)
        await client.start()
        assert await client.wait_connected(2)
        with pytest.raises(MAClientError) as exc:
            await client.call("players/get", player_id="nope")
        assert exc.value.error_code == 4
        await client.close()
    _run(scenario())


def test_call_when_disconnected_raises():
    async def scenario():
        client = MAClient(ws_url="ws://ma.test:8095/ws", token="tok",
                          ws_factory=lambda: _aret(FakeWS()))
        with pytest.raises(MAClientError):
            await client.call("players/all")
    _run(scenario())


# ── Events ──

def test_events_dispatch_to_callback():
    async def scenario():
        events = []

        async def on_event(evt):
            events.append(evt)

        ws = FakeWS()
        client = _client(ws, on_event=on_event)
        await client.start()
        assert await client.wait_connected(2)
        ws.feed({"event": "player_updated", "object_id": "p1",
                 "data": {"state": "playing"}})
        for _ in range(10):
            if events:
                break
            await asyncio.sleep(0.01)
        assert events and events[0]["object_id"] == "p1"
        await client.close()
    _run(scenario())


def test_on_connect_and_disconnect_callbacks():
    async def scenario():
        calls = []
        connected = asyncio.Event()

        async def on_connect():
            calls.append("connect")
            connected.set()

        async def on_disconnect():
            calls.append("disconnect")

        ws = FakeWS()
        client = _client(ws, on_connect=on_connect,
                         on_disconnect=on_disconnect)
        await client.start()
        assert await client.wait_connected(2)
        # on_connect runs concurrently with the dispatch loop now, so wait
        # for it rather than assuming it finished before wait_connected.
        await asyncio.wait_for(connected.wait(), 2)
        assert calls == ["connect"]
        await client.close()
        assert calls == ["connect", "disconnect"]
    _run(scenario())


def test_on_connect_callback_can_issue_calls():
    """Regression: an on_connect that awaits a command must not deadlock.

    The callback's call() response is only read once the dispatch loop is
    running; awaiting the callback inline in _connect_once used to block
    every such call until it timed out (30s), so player restore never ran.
    """
    async def scenario():
        result = {}

        ws = FakeWS(responder=lambda p: (
            [{"message_id": p["message_id"], "result": [{"player_id": "p1"}]}]
            if p.get("command") == "players/all" else []))

        async def on_connect():
            result["players"] = await client.call("players/all", timeout=2)

        client = _client(ws, on_connect=on_connect)
        await client.start()
        assert await client.wait_connected(2)
        # If the deadlock regressed this would sit until the 2s call timeout.
        for _ in range(50):
            if "players" in result:
                break
            await asyncio.sleep(0.02)
        assert result.get("players") == [{"player_id": "p1"}]
        await client.close()
    _run(scenario())


# ── URL/token resolution ──

def test_resolve_ws_url_env_wins(monkeypatch, mock_config):
    mock_config({"music_assistant": {"url": "ws://cfg:8095/ws"},
                 "player": {"ip": "10.0.0.3"}})
    monkeypatch.setenv("MASS_WS_URL", "ws://env:8095/ws")
    assert resolve_ws_url() == "ws://env:8095/ws"


def test_resolve_ws_url_config_then_player_ip(monkeypatch, mock_config):
    monkeypatch.delenv("MASS_WS_URL", raising=False)
    mock_config({"music_assistant": {"url": "ma.local"},
                 "player": {"ip": "10.0.0.3"}})
    assert resolve_ws_url() == "ws://ma.local:8095/ws"
    mock_config({"player": {"ip": "10.0.0.10"}})
    assert resolve_ws_url() == "ws://10.0.0.10:8095/ws"


def test_imageproxy_url():
    client = MAClient(ws_url="ws://10.0.0.10:8095/ws", token="tok")
    url = client.imageproxy_url("some/path.jpg", "spotify", size=256)
    assert url.startswith("http://10.0.0.10:8095/imageproxy?")
    assert "path=some%2Fpath.jpg" in url
    assert "provider=spotify" in url
    assert "size=256" in url
