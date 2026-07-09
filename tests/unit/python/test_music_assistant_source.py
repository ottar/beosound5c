"""Tests for the Music Assistant source (library browse + playback).

Exercised with a fake MAClient and mocked player proxies — no network.
Pins the browse tree shape the iframe browser consumes and the
source→player play payload contract (parent_uri + track start_item).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from sources.music_assistant.service import MusicAssistantSource


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _media_item(item_id, name, media_type, artists=None, image_path=None):
    item = {
        "item_id": str(item_id),
        "provider": "library",
        "name": name,
        "sort_name": name.lower(),
        "media_type": media_type,
        "uri": f"library://{media_type}/{item_id}",
        "metadata": {},
    }
    if artists:
        item["artists"] = [{"name": a} for a in artists]
    if image_path:
        item["metadata"]["images"] = [
            {"type": "thumb", "path": image_path, "provider": "library",
             "remotely_accessible": False}]
    return item


class FakeMAClient:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}
        self.connected = True
        self.ws_url = "ws://10.0.0.10:8095/ws"

    async def call(self, command, **args):
        self.calls.append((command, args))
        return self.responses.get(command, [])

    def image_url_for(self, image, size=256):
        if image.get("remotely_accessible") and image.get("path", "").startswith("http"):
            return image["path"]
        if image.get("proxy_id"):
            return f"http://10.0.0.10:8095/imageproxy/{image['proxy_id']}?size={size}&fmt=jpg"
        return (f"http://10.0.0.10:8095/imageproxy?path={image.get('path')}"
                f"&provider={image.get('provider')}&size={size}")

    async def close(self):
        pass


@pytest.fixture
def source():
    s = MusicAssistantSource()
    s._client = FakeMAClient()
    s.register = AsyncMock()
    s.post_media_update = AsyncMock()
    s.player_play = AsyncMock(return_value=True)
    s.player_pause = AsyncMock(return_value=True)
    s.player_resume = AsyncMock(return_value=True)
    s.player_next = AsyncMock(return_value=True)
    s.player_prev = AsyncMock(return_value=True)
    s.player_stop = AsyncMock(return_value=True)
    s.player_state = AsyncMock(return_value="stopped")
    return s


# ── Browse tree ──

def test_browse_root_categories(source):
    result = _run(source._browse(""))
    ids = [i["id"] for i in result["items"]]
    assert ids == ["discover", "artists", "albums", "playlists", "tracks", "radios"]
    assert all(i["type"] == "category" for i in result["items"])


def test_browse_discover_flattens_folders_with_headers(source):
    """Discover interleaves a header row per non-empty recommendation folder
    followed by its (playable) items; empty folders are dropped."""
    source._client.responses["music/recommendations"] = [
        {"item_id": "rp", "name": "Recently Played", "items": [
            _media_item(1, "Song A", "track", artists=["Artist A"]),
            _media_item(2, "An Album", "album"),
        ]},
        {"item_id": "empty", "name": "Empty Folder", "items": []},
        {"item_id": "ra", "name": "Random Artists", "items": [
            _media_item(3, "Some Artist", "artist"),
        ]},
    ]
    result = _run(source._browse("discover"))
    rows = [(i["type"], i["name"], i.get("subtitle")) for i in result["items"]]
    assert rows == [
        ("header", "Recently Played", None),
        ("track", "Song A", "Artist A"),
        ("track", "An Album", "Album"),      # media-type label when no artist
        ("header", "Random Artists", None),
        ("track", "Some Artist", "Artist"),
    ]
    # Content rows carry a playable URI + cover flag; headers do not
    items = result["items"]
    assert items[1]["uri"] == "library://track/1"
    assert items[1]["cover"] is True
    assert "uri" not in items[0] or not items[0].get("uri")


def test_discover_uses_top_level_image(source):
    """Recommendation items store artwork in a top-level `image` dict (not
    metadata.images); _image_of must fall back to it so covers show."""
    item = _media_item(1, "Song A", "track", artists=["Artist A"])
    item["metadata"] = {}  # no metadata.images
    item["image"] = {"type": "thumb", "path": "some/cover.jpg",
                     "provider": "filesystem_local"}
    source._client.responses["music/recommendations"] = [
        {"item_id": "rp", "name": "Recently Played", "items": [item]},
    ]
    result = _run(source._browse("discover"))
    track = result["items"][1]
    assert track["image"].startswith("http://10.0.0.10:8095/imageproxy?")


def test_browse_artists_lists_containers(source):
    source._client.responses["music/artists/library_items"] = [
        _media_item(1, "ABBA", "artist", image_path="img/abba.jpg"),
        _media_item(2, "Beatles", "artist"),
    ]
    result = _run(source._browse("artists"))
    assert [i["name"] for i in result["items"]] == ["ABBA", "Beatles"]
    first = result["items"][0]
    assert first["type"] == "category"
    assert first["path"] == "artists/1"
    assert first["image"].startswith("http://10.0.0.10:8095/imageproxy?")
    _, args = source._client.calls[0]
    assert args["limit"] == 500


def test_browse_artist_albums(source):
    source._client.responses["music/artists/artist_albums"] = [
        _media_item(10, "Waterloo", "album", artists=["ABBA"]),
    ]
    result = _run(source._browse("artists/1"))
    assert result["items"][0]["path"] == "albums/10"
    assert result["items"][0]["subtitle"] == "ABBA"
    cmd, args = source._client.calls[0]
    assert cmd == "music/artists/artist_albums"
    assert args == {"item_id": "1",
                    "provider_instance_id_or_domain": "library"}


def test_browse_album_tracks_have_parent_uri(source):
    source._client.responses["music/albums/album_tracks"] = [
        _media_item(100, "Waterloo", "track", artists=["ABBA"]),
    ]
    result = _run(source._browse("albums/10"))
    track = result["items"][0]
    assert track["type"] == "track"
    assert track["uri"] == "library://track/100"
    assert track["parent_uri"] == "library://album/10"


def test_browse_radios_flagged_radio(source):
    source._client.responses["music/radios/library_items"] = [
        _media_item(5, "NRK P1", "radio"),
    ]
    result = _run(source._browse("radios"))
    assert result["items"][0]["radio"] is True


# ── Playback ──

def test_play_item_track_in_album_uses_start_item(source):
    res = _run(source.handle_command("play_item", {
        "uri": "library://track/100", "parent_uri": "library://album/10",
        "name": "Waterloo", "artist": "ABBA"}))
    assert res["status"] == "ok"
    kwargs = source.player_play.await_args.kwargs
    assert kwargs["uri"] == "library://album/10"
    assert kwargs["track_uri"] == "library://track/100"
    assert kwargs["radio"] is False
    source.register.assert_any_await("playing", auto_power=True)


def test_play_item_standalone_track(source):
    _run(source.handle_command("play_item", {"uri": "library://track/1",
                                             "name": "Song"}))
    kwargs = source.player_play.await_args.kwargs
    assert kwargs["uri"] == "library://track/1"
    assert kwargs["track_uri"] is None


def test_play_item_radio(source):
    _run(source.handle_command("play_item", {"uri": "library://radio/5",
                                             "name": "NRK P1", "radio": True}))
    assert source.player_play.await_args.kwargs["radio"] is True


def test_play_item_failure_rolls_back(source):
    source.player_play = AsyncMock(return_value=False)
    res = _run(source.handle_command("play_item", {"uri": "library://track/1"}))
    assert res["status"] == "error"
    source.register.assert_any_await("available")


def test_toggle_pauses_when_playing(source):
    source.player_state = AsyncMock(return_value="playing")
    _run(source.handle_command("toggle", {}))
    source.player_pause.assert_awaited_once()


def test_toggle_resumes_when_paused(source):
    source.player_state = AsyncMock(return_value="paused")
    _run(source.handle_command("toggle", {}))
    source.player_resume.assert_awaited_once()


def test_toggle_replays_last_item_when_stopped(source):
    source._current = {"uri": "library://track/1", "name": "Song"}
    _run(source.handle_command("toggle", {}))
    source.player_play.assert_awaited_once()


def test_transport_commands(source):
    _run(source.handle_command("next", {}))
    _run(source.handle_command("prev", {}))
    _run(source.handle_command("stop", {}))
    source.player_next.assert_awaited_once()
    source.player_prev.assert_awaited_once()
    source.player_stop.assert_awaited_once()
    source.register.assert_any_await("available")
