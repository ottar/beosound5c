"""Tests for the Music Assistant source (library browse + playback).

Exercised with a fake MAClient and mocked player proxies — no network.
Pins the browse tree shape the iframe browser consumes and the
source→player play payload contract (parent_uri + track start_item).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from lib.ma_client import MAClientError
from sources.music_assistant.service import MusicAssistantSource


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _media_item(item_id, name, media_type, artists=None, image_path=None,
                provider="library"):
    item = {
        "item_id": str(item_id),
        "provider": provider,
        "name": name,
        "sort_name": name.lower(),
        "media_type": media_type,
        "uri": f"{provider}://{media_type}/{item_id}",
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


def test_discover_rows_filtered_by_config(source, mock_config):
    mock_config({"music_assistant": {"discover_rows": ["random_artists"]}})
    source._client.responses["music/recommendations"] = [
        {"item_id": "recently_played", "name": "Recently Played", "items": [
            _media_item(1, "Song A", "track"),
        ]},
        {"item_id": "random_artists", "name": "Random Artists", "items": [
            _media_item(3, "Some Artist", "artist"),
        ]},
    ]
    result = _run(source._browse("discover"))
    assert [i["name"] for i in result["items"]] == ["Random Artists", "Some Artist"]


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
    # Album artists only — guest/track artists stay out of the arc list
    assert args["album_artists_only"] is True


def test_library_pages_through_full_library(source):
    # 1137 artists = pages of 500 + 500 + 137; a single page used to
    # truncate the list around "D" on large libraries.
    all_items = [_media_item(i, f"Artist {i:04d}", "artist")
                 for i in range(1137)]

    async def paged_call(command, **args):
        source._client.calls.append((command, args))
        if command == "music/artists/library_items":
            off = args.get("offset", 0)
            return all_items[off:off + args.get("limit", 500)]
        return []

    source._client.call = paged_call
    result = _run(source._browse("artists"))
    assert len(result["items"]) == 1137
    offsets = [a["offset"] for c, a in source._client.calls
               if c == "music/artists/library_items"]
    assert offsets == [0, 500, 1000]


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
    # A radio station plays its stream directly — never radio_mode=True, which
    # MA refuses for a Radio MediaItem ("Dynamic tracks not supported").
    _run(source.handle_command("play_item", {"uri": "library://radio/5",
                                             "name": "NRK P1", "radio": True}))
    kwargs = source.player_play.await_args.kwargs
    assert kwargs["radio"] is False
    assert kwargs["uri"] == "library://radio/5"


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


# ── Context-menu metadata on serialized rows ──

def test_container_item_carries_context_fields(source):
    source._client.responses["music/artists/library_items"] = [
        _media_item(1, "ABBA", "artist"),
    ]
    it = _run(source._browse("artists"))["items"][0]
    assert it["media_type"] == "artist"
    assert it["provider"] == "library"
    assert it["in_library"] is True
    assert it["favorite"] is False


def test_discover_apple_music_item_not_in_library(source):
    """A Discover item from a non-library provider serializes with
    in_library:false so the context menu offers 'Add to library'."""
    item = _media_item(1, "Song", "track", artists=["A"], provider="apple_music")
    source._client.responses["music/recommendations"] = [
        {"item_id": "rp", "name": "RP", "items": [item]}]
    track = _run(source._browse("discover"))["items"][1]
    assert track["provider"] == "apple_music"
    assert track["in_library"] is False
    assert track["media_type"] == "track"


def test_track_item_carries_artist_and_album_uri(source):
    album_track = {
        "item_id": "100", "provider": "library", "name": "Waterloo",
        "sort_name": "waterloo", "media_type": "track",
        "uri": "library://track/100", "metadata": {},
        "artists": [{"name": "ABBA", "uri": "library://artist/1"}],
        "album": {"name": "Waterloo", "uri": "library://album/10"},
    }
    source._client.responses["music/albums/album_tracks"] = [album_track]
    it = _run(source._browse("albums/10"))["items"][0]
    assert it["artist_uri"] == "library://artist/1"
    assert it["album_uri"] == "library://album/10"


# ── URI browse (Go to artist/album, non-library providers) ──

def test_browse_uri_album_uses_item_provider(source):
    source._client.responses["music/albums/album_tracks"] = [
        _media_item(100, "Track", "track", artists=["A"], provider="apple_music"),
    ]
    result = _run(source._browse("uri/apple_music://album/xyz"))
    cmd, args = source._client.calls[0]
    assert cmd == "music/albums/album_tracks"
    assert args == {"item_id": "xyz",
                    "provider_instance_id_or_domain": "apple_music"}
    # tracks carry the album URI as parent so "Play from here" works
    assert result["items"][0]["parent_uri"] == "apple_music://album/xyz"


def test_browse_uri_artist_lists_albums_with_uri_paths(source):
    source._client.responses["music/artists/artist_albums"] = [
        _media_item(5, "Alb", "album", provider="apple_music"),
    ]
    result = _run(source._browse("uri/apple_music://artist/9"))
    cmd, args = source._client.calls[0]
    assert cmd == "music/artists/artist_albums"
    assert args == {"item_id": "9",
                    "provider_instance_id_or_domain": "apple_music"}
    # children keep uri/ paths so further drilling continues
    assert result["items"][0]["path"] == "uri/apple_music://album/5"


# ── play_item queue options ──

def test_play_item_next_enqueues_bare_uri_no_broadcast(source):
    res = _run(source.handle_command("play_item", {
        "uri": "library://track/100", "parent_uri": "library://album/10",
        "name": "T", "option": "next"}))
    assert res["status"] == "ok"
    kwargs = source.player_play.await_args.kwargs
    assert kwargs["uri"] == "library://track/100"  # bare, ignores parent
    assert kwargs["option"] == "next"
    assert kwargs.get("track_uri") is None
    # nothing starts playing → no PLAYING pre-broadcast
    source.register.assert_not_awaited()
    source.post_media_update.assert_not_awaited()


def test_play_item_add_enqueues(source):
    _run(source.handle_command("play_item", {
        "uri": "library://album/10", "name": "Alb", "option": "add"}))
    assert source.player_play.await_args.kwargs["option"] == "add"
    source.register.assert_not_awaited()


def test_play_item_play_now_broadcasts_and_bare_uri(source):
    _run(source.handle_command("play_item", {
        "uri": "library://track/100", "parent_uri": "library://album/10",
        "name": "T", "option": "play"}))
    kwargs = source.player_play.await_args.kwargs
    assert kwargs["uri"] == "library://track/100"  # bare, ignores parent
    assert kwargs["track_uri"] is None
    assert kwargs["option"] == "play"
    source.register.assert_any_await("playing", auto_power=True)


def test_play_item_radio_mode_sets_radio_no_expansion(source):
    res = _run(source.handle_command("play_item", {
        "uri": "library://artist/9", "name": "Artist", "radio_mode": True}))
    assert res["status"] == "ok"
    kwargs = source.player_play.await_args.kwargs
    assert kwargs["radio"] is True
    assert kwargs["uri"] == "library://artist/9"
    # MA builds the dynamic queue itself → no artist_tracks expansion
    assert source._client.calls == []
    source.register.assert_any_await("playing", auto_power=True)


def test_play_item_replace_still_expands_artist(source):
    """Default (replace) path keeps the artist-track expansion — the classic
    'Play from here' behaviour is unchanged."""
    source._client.responses["music/artists/artist_tracks"] = [
        _media_item(1, "t1", "track"), _media_item(2, "t2", "track")]
    _run(source.handle_command("play_item", {
        "uri": "library://artist/9", "name": "Artist"}))
    kwargs = source.player_play.await_args.kwargs
    assert kwargs["track_uris"] == ["library://track/1", "library://track/2"]


# ── favorites / library commands ──

def test_favorite_add_uses_uri(source):
    res = _run(source.handle_command("favorite", {
        "uri": "library://track/1", "media_type": "track",
        "item_id": "1", "add": True}))
    assert res["status"] == "ok"
    cmd, args = source._client.calls[0]
    assert cmd == "music/favorites/add_item"
    assert args == {"item": "library://track/1"}


def test_library_remove_uses_item_id(source):
    _run(source.handle_command("library", {
        "uri": "library://album/2", "media_type": "album",
        "item_id": "2", "add": False}))
    cmd, args = source._client.calls[0]
    assert cmd == "music/library/remove_item"
    assert args == {"media_type": "album", "library_item_id": "2"}


def test_favorite_error_returns_status(source):
    async def boom(command, **a):
        raise MAClientError("nope")
    source._client.call = boom
    res = _run(source.handle_command("favorite", {
        "uri": "library://track/1", "add": True}))
    assert res["status"] == "error"
