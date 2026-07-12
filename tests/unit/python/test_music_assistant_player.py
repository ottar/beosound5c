"""Tests for the Music Assistant player backend.

Exercised with a fake MAClient injected directly — no network. Pins the
contract the plan established against the fork's failure modes: play()
is implemented (player_queues/play_media), target selection is
persisted and never auto-ranked, grouping maps to players/cmd/group /
ungroup, and PlayerState events map to media broadcasts.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from lib.ma_client import MAClientError
from lib.token_store import TokenStore
from players.music_assistant_player import MusicAssistantPlayer, _fmt_time


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _player_state(pid, name, available=True, playback_state="idle",
                  group_childs=None, can_group_with=None, current_media=None,
                  volume_level=30):
    return {
        "player_id": pid,
        "display_name": name,
        "available": available,
        "playback_state": playback_state,
        "group_childs": group_childs or [],
        "can_group_with": can_group_with or [],
        "current_media": current_media,
        "volume_level": volume_level,
    }


PLAYERS = [
    _player_state("stage", "Beosound Stage", can_group_with=["shelf", "coffee"]),
    _player_state("shelf", "Bokhylle", can_group_with=["stage", "coffee"]),
    _player_state("coffee", "Kaffi", can_group_with=["stage", "shelf"]),
    _player_state("mac", "MacBook", available=False),
]


class FakeMAClient:
    """Minimal stand-in for lib.ma_client.MAClient."""

    def __init__(self, players=None, responses=None, fail_commands=()):
        self.calls = []
        self.players = players if players is not None else [dict(p) for p in PLAYERS]
        self.responses = responses or {}
        self.fail_commands = set(fail_commands)
        self.connected = True
        self.ws_url = "ws://10.0.0.10:8095/ws"
        self.server_info = {"server_version": "2.9.2"}

    async def call(self, command, **args):
        self.calls.append((command, args))
        if command in self.fail_commands:
            raise MAClientError(f"{command} failed")
        if command in self.responses:
            return self.responses[command]
        if command == "players/all":
            return self.players
        if command == "players/get":
            pid = args["player_id"]
            return next((p for p in self.players if p["player_id"] == pid), None)
        return None

    def commands(self, name=None):
        return [(c, a) for c, a in self.calls if name is None or c == name]

    async def close(self):
        pass


class FakeRequest:
    def __init__(self, body=None):
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.fixture
def player(tmp_path):
    p = MusicAssistantPlayer()
    p._client = FakeMAClient()
    p._state_store = TokenStore("music_assistant_state.json",
                                dev_dir=str(tmp_path), prod_dir=str(tmp_path))
    p._players = {pl["player_id"]: dict(pl) for pl in PLAYERS}
    p._target_id = "shelf"
    return p


# ── play() contract ──

def test_play_uri_maps_to_play_media(player):
    ok = _run(player.play(uri="library://track/15664",
                          meta={"title": "T", "artist": "A"}))
    assert ok is True
    cmd, args = player._client.commands("player_queues/play_media")[0]
    assert args == {"queue_id": "shelf", "media": "library://track/15664",
                    "option": "replace"}
    # meta seeds the cached media until MA events refine it
    assert player._cached_media_data["title"] == "T"


def test_play_radio_url_sets_radio_mode(player):
    ok = _run(player.play(url="http://stream.example/radio.mp3", radio=True))
    assert ok is True
    _, args = player._client.commands("player_queues/play_media")[0]
    assert args["media"] == "http://stream.example/radio.mp3"
    assert args["radio_mode"] is True


def test_play_track_uris_and_start_item(player):
    ok = _run(player.play(uri="library://album/1",
                          track_uri="library://track/3"))
    assert ok is True
    _, args = player._client.commands("player_queues/play_media")[0]
    assert args["media"] == "library://album/1"
    assert args["start_item"] == "library://track/3"

    ok = _run(player.play(track_uris=["library://track/1", "library://track/2"]))
    _, args = player._client.commands("player_queues/play_media")[1]
    assert args["media"] == ["library://track/1", "library://track/2"]


def test_play_without_target_fails(player):
    player._target_id = None
    assert _run(player.play(uri="library://track/1")) is False
    assert player._client.commands("player_queues/play_media") == []


def test_play_media_error_returns_false(player):
    player._client.fail_commands = {"player_queues/play_media"}
    assert _run(player.play(uri="library://track/1")) is False


def test_play_without_media_resumes(player):
    ok = _run(player.play())
    assert ok is True
    assert player._client.commands("players/cmd/play") == [
        ("players/cmd/play", {"player_id": "shelf"})]


def test_play_option_forwarded_to_play_media(player):
    _run(player.play(uri="library://track/1", option="next"))
    _, args = player._client.commands("player_queues/play_media")[0]
    assert args["option"] == "next"


def test_play_invalid_option_falls_back_to_replace(player):
    _run(player.play(uri="library://track/1", option="bogus"))
    _, args = player._client.commands("player_queues/play_media")[0]
    assert args["option"] == "replace"


def test_handle_play_omits_option_when_absent(player):
    """_handle_play must not pass `option` to play() when the request body
    lacks it — the other players' play() signatures have no such kwarg."""
    seen = {}

    async def fake_play(**kwargs):
        seen.update(kwargs)
        return True

    player.play = fake_play
    _run(player._handle_play(FakeRequest({"uri": "library://track/1"})))
    assert "option" not in seen


def test_handle_play_forwards_option_when_present(player):
    seen = {}

    async def fake_play(**kwargs):
        seen.update(kwargs)
        return True

    player.play = fake_play
    _run(player._handle_play(
        FakeRequest({"uri": "library://track/1", "option": "add"})))
    assert seen["option"] == "add"


# ── Transport ──

def test_transport_commands_map_to_players_cmd(player):
    assert _run(player.pause()) is True
    assert _run(player.resume()) is True
    assert _run(player.next_track()) is True
    assert _run(player.prev_track()) is True
    assert _run(player.stop()) is True
    names = [c for c, _ in player._client.calls]
    assert names == ["players/cmd/pause", "players/cmd/play",
                     "players/cmd/next", "players/cmd/previous",
                     "players/cmd/stop"]
    assert all(a == {"player_id": "shelf"} for _, a in player._client.calls)


def test_set_shuffle(player):
    assert _run(player.set_shuffle(True)) is True
    assert player._client.commands("player_queues/shuffle")[0][1] == {
        "queue_id": "shelf", "shuffle_enabled": True}


def test_capabilities_include_url_stream(player):
    caps = _run(player.get_capabilities())
    assert "url_stream" in caps
    assert "music_assistant" in caps


# ── Target selection / persistence ──

def test_restore_target_prefers_persisted(player):
    player._state_store.save({"player_id": "coffee"})
    player._target_id = None
    player._restore_target()
    assert player._target_id == "coffee"


def test_restore_target_ignores_unavailable_persisted(player):
    player._state_store.save({"player_id": "mac"})  # unavailable
    player._target_id = None
    player._restore_target()
    # three players available → no auto-pick, wait for the UI selection
    assert player._target_id is None


def test_restore_target_picks_sole_available(player):
    player._players = {"shelf": _player_state("shelf", "Bokhylle"),
                       "mac": _player_state("mac", "MacBook", available=False)}
    player._target_id = None
    player._restore_target()
    assert player._target_id == "shelf"


def test_restore_target_prefers_playing_over_persisted(player):
    # Persisted target is idle; a different speaker is playing → adopt the
    # playing one so a restart re-attaches to live playback.
    player._players["coffee"]["playback_state"] = "playing"
    player._state_store.save({"player_id": "shelf"})
    player._target_id = None
    player._restore_target()
    assert player._target_id == "coffee"
    # New live target is persisted so it survives the next restart too.
    assert player._state_store.load()["player_id"] == "coffee"


def test_restore_target_keeps_persisted_when_it_is_playing(player):
    # Both the persisted target and another speaker are playing → keep the
    # persisted one (no needless hop between independent playbacks).
    player._players["shelf"]["playback_state"] = "playing"
    player._players["coffee"]["playback_state"] = "playing"
    player._state_store.save({"player_id": "shelf"})
    player._target_id = None
    player._restore_target()
    assert player._target_id == "shelf"


def test_set_target_persists(player):
    player._set_target("coffee")
    assert player._state_store.load()["player_id"] == "coffee"


def test_play_track_radio_uses_radio_mode(player):
    ok = _run(player.play_track_radio("library://track/42"))
    assert ok
    cmd, args = player._client.commands("player_queues/play_media")[0]
    assert args == {"queue_id": "shelf", "media": "library://track/42",
                    "radio_mode": True}


def test_play_track_radio_without_target_or_uri(player):
    player._target_id = None
    assert not _run(player.play_track_radio("library://track/42"))
    player._target_id = "shelf"
    assert not _run(player.play_track_radio(""))


def test_get_shuffle_reads_queue_state(player):
    player._client.responses["player_queues/get"] = {"shuffle_enabled": True}
    assert _run(player.get_shuffle()) is True
    player._client.responses["player_queues/get"] = {"shuffle_enabled": False}
    assert _run(player.get_shuffle()) is False


def test_get_shuffle_unknown_when_unavailable(player):
    player._target_id = None
    assert _run(player.get_shuffle()) is None
    player._target_id = "shelf"
    player._client.fail_commands.add("player_queues/get")
    assert _run(player.get_shuffle()) is None


def test_select_target_transfers_queue_when_playing(player):
    player._current_playback_state = "playing"
    player._refresh_target_state = AsyncMock()
    resp = _run(player._handle_select_target(FakeRequest({"id": "coffee"})))
    assert resp.status == 200
    assert player._target_id == "coffee"
    _, args = player._client.commands("player_queues/transfer")[0]
    assert args == {"source_queue_id": "shelf", "target_queue_id": "coffee",
                    "auto_play": True}


def test_select_target_idle_no_transfer(player):
    player._refresh_target_state = AsyncMock()
    resp = _run(player._handle_select_target(FakeRequest({"id": "coffee"})))
    assert resp.status == 200
    assert player._target_id == "coffee"
    assert player._client.commands("player_queues/transfer") == []


def test_select_target_rejects_unavailable(player):
    resp = _run(player._handle_select_target(FakeRequest({"id": "mac"})))
    assert resp.status == 404
    assert player._target_id == "shelf"


# ── Grouping ──

def test_join_maps_to_players_cmd_group(player):
    resp = _run(player._handle_join(FakeRequest({"id": "coffee"})))
    assert resp.status == 200
    assert player._client.commands("players/cmd/group")[0][1] == {
        "player_id": "coffee", "target_player": "shelf"}


def test_join_matches_new_member_volume_to_target(player):
    """A joined speaker is raised to the target's level so it is audible and
    the proportional group volume moves it (a member at 0 would stay silent)."""
    player._players["shelf"]["volume_level"] = 42
    _run(player._handle_join(FakeRequest({"id": "coffee"})))
    vol_sets = player._client.commands("players/cmd/volume_set")
    assert vol_sets[0][1] == {"player_id": "coffee", "volume_level": 42}
    # Local cache updated so the arc list reflects it immediately
    assert player._players["coffee"]["volume_level"] == 42


def test_join_without_target_conflicts(player):
    player._target_id = None
    resp = _run(player._handle_join(FakeRequest({"id": "coffee"})))
    assert resp.status == 409


def test_join_updates_group_cache_immediately(player):
    """MA's player_updated event lags the group command — the local cache
    must reflect the new member at once so an immediate /player/network
    re-fetch (speaker overlay after GO) doesn't snap the UI back."""
    _run(player._handle_join(FakeRequest({"id": "coffee"})))
    assert "coffee" in player._players["shelf"]["group_childs"]
    assert "coffee" in player._group_member_ids()


def test_unjoin_updates_group_cache_immediately(player):
    player._players["shelf"]["group_childs"] = ["shelf", "coffee"]
    _run(player._handle_unjoin(FakeRequest({"id": "coffee"})))
    assert "coffee" not in player._players["shelf"]["group_childs"]


def test_unjoin_all_clears_group_cache(player):
    player._players["shelf"]["group_childs"] = ["shelf", "coffee", "stage"]
    _run(player._handle_unjoin(FakeRequest({})))
    assert player._group_member_ids() == []


def test_ungroup_stop_auto_resumes(player):
    """The B&O provider can collapse the stream seconds after a member is
    ungrouped — a stop landing inside the window of our own unjoin must
    resume the queue instead of being treated as an external stop."""
    player._current_playback_state = "playing"
    player.notify_router_playback_override = AsyncMock()
    spawned = []
    player._spawn = lambda coro, name=None: spawned.append((name, coro))
    _run(player._handle_unjoin(FakeRequest({"id": "coffee"})))
    assert player._ungroup_resume_pending is True

    # Target reports stopped shortly after → auto-resume, one-shot
    _run(player._process_target_state(
        _player_state("shelf", "Bokhylle", playback_state="idle")))
    names = [n for n, _ in spawned]
    assert "ungroup_resume" in names
    assert player._ungroup_resume_pending is False
    player.notify_router_playback_override.assert_not_awaited()
    for _, coro in spawned:
        coro.close()  # not awaited in this test


def test_unjoin_member(player):
    resp = _run(player._handle_unjoin(FakeRequest({"id": "coffee"})))
    assert resp.status == 200
    assert player._client.commands("players/cmd/ungroup")[0][1] == {
        "player_id": "coffee"}


def test_unjoin_all_dissolves_group(player):
    player._players["shelf"]["group_childs"] = ["shelf", "coffee", "stage"]
    resp = _run(player._handle_unjoin(FakeRequest({})))
    assert resp.status == 200
    _, args = player._client.commands("players/cmd/ungroup_many")[0]
    assert sorted(args["player_ids"]) == ["coffee", "stage"]


def test_group_members_exclude_target_and_unavailable(player):
    player._players["shelf"]["group_childs"] = ["shelf", "coffee", "mac"]
    assert player._group_member_ids() == ["coffee"]


# ── /player/network ──

def test_network_lists_available_players_target_first(player):
    player._players["shelf"]["group_childs"] = ["shelf", "coffee"]
    resp = _run(player._handle_network(FakeRequest()))
    items = json.loads(resp.text)
    # target first, then alphabetical by display name
    assert [i["id"] for i in items] == ["shelf", "stage", "coffee"]
    assert items[0]["is_target"] is True
    by_id = {i["id"]: i for i in items}
    assert by_id["coffee"]["in_group"] is True
    assert by_id["stage"]["in_group"] is False
    assert by_id["stage"]["can_join"] is True
    assert "mac" not in by_id  # unavailable filtered out


# ── Volume (group-aware) ──

def test_volume_solo_target_maps_to_volume_set(player):
    resp = _run(player._handle_volume(FakeRequest({"volume": 35})))
    assert resp.status == 200
    assert player._client.commands("players/cmd/volume_set")[0][1] == {
        "player_id": "shelf", "volume_level": 35}
    assert player._client.commands("players/cmd/group_volume") == []


def test_volume_grouped_target_maps_to_group_volume(player):
    player._players["shelf"]["group_childs"] = ["shelf", "coffee"]
    resp = _run(player._handle_volume(FakeRequest({"volume": 35})))
    assert resp.status == 200
    assert player._client.commands("players/cmd/group_volume")[0][1] == {
        "player_id": "shelf", "volume_level": 35}
    assert player._client.commands("players/cmd/volume_set") == []


def test_volume_without_target_conflicts(player):
    player._target_id = None
    resp = _run(player._handle_volume(FakeRequest({"volume": 35})))
    assert resp.status == 409


def test_volume_rejects_bad_body(player):
    assert _run(player._handle_volume(FakeRequest({}))).status == 400
    assert _run(player._handle_volume(
        FakeRequest({"volume": "loud"}))).status == 400


def test_volume_clamps_to_0_100(player):
    _run(player._handle_volume(FakeRequest({"volume": 250})))
    _, args = player._client.commands("players/cmd/volume_set")[0]
    assert args["volume_level"] == 100


def test_member_volume_sets_single_player(player):
    resp = _run(player._handle_member_volume(
        FakeRequest({"id": "coffee", "volume": 22})))
    assert resp.status == 200
    assert player._client.commands("players/cmd/volume_set")[0][1] == {
        "player_id": "coffee", "volume_level": 22}
    # optimistic cache update so /player/network reflects the trim at once
    assert player._players["coffee"]["volume_level"] == 22


def test_member_volume_rejects_unavailable(player):
    resp = _run(player._handle_member_volume(
        FakeRequest({"id": "mac", "volume": 22})))
    assert resp.status == 404
    assert player._client.commands("players/cmd/volume_set") == []


def test_master_volume_prefers_ma_group_volume_field(player):
    player._players["shelf"]["group_childs"] = ["shelf", "coffee"]
    player._players["shelf"]["group_volume"] = 44
    assert player._master_volume() == 44


def test_master_volume_averages_members_without_field(player):
    player._players["shelf"]["group_childs"] = ["shelf", "coffee"]
    player._players["shelf"]["volume_level"] = 20
    player._players["coffee"]["volume_level"] = 40
    assert player._master_volume() == 30


def test_master_volume_solo_uses_target_level(player):
    player._players["shelf"]["volume_level"] = 27
    assert player._master_volume() == 27


def test_output_label_solo_grouped_and_unset(player):
    assert player._output_label() == "Bokhylle"
    player._players["shelf"]["group_childs"] = ["shelf", "coffee"]
    assert player._output_label() == "Bokhylle +1"
    player._target_id = None
    assert player._output_label() is None


def test_process_target_state_reports_volume_with_label(player):
    """When the MA adapter owns the wheel, target-state events push the
    master volume AND the target/group label to the router, so the
    overlay follows a PLAY ON switch."""
    player._report_ma_volume = True
    player.report_volume_to_router = AsyncMock()
    spawned = []
    player._spawn = lambda coro, name=None: spawned.append(coro)
    player._players["shelf"]["group_childs"] = ["shelf", "coffee"]
    player._players["shelf"]["group_volume"] = 44

    async def run():
        await player._process_target_state(dict(player._players["shelf"]))
        for coro in spawned:
            await coro

    _run(run())
    player.report_volume_to_router.assert_awaited_once_with(44, "Bokhylle +1")


def test_network_includes_per_player_volume(player):
    resp = _run(player._handle_network(FakeRequest()))
    items = json.loads(resp.text)
    assert all(i["volume"] == 30 for i in items)  # PLAYERS default level


# ── State/event → media mapping ──

def test_process_target_state_broadcasts_track(player):
    player.broadcast_media_update = AsyncMock()
    player.fetch_artwork = AsyncMock(return_value=None)
    state = _player_state(
        "shelf", "Bokhylle", playback_state="playing", volume_level=42,
        current_media={"uri": "library://track/1", "title": "Song",
                       "artist": "Artist", "album": "Album",
                       "image_url": "http://100.87.246.101:8095/imageproxy/abc?size=512",
                       "duration": 199, "elapsed_time": 7})
    _run(player._process_target_state(state))
    media = player.broadcast_media_update.await_args.args[0]
    assert media["title"] == "Song"
    assert media["artist"] == "Artist"
    assert media["duration"] == "3:19"
    assert media["position"] == "0:07"
    assert media["state"] == "playing"
    # artwork host rewritten to the ws host before fetching
    art_url = player.fetch_artwork.await_args.args[0]
    assert art_url.startswith("http://10.0.0.10:8095/imageproxy/abc")


def test_process_target_state_same_track_updates_state_only(player):
    player.broadcast_media_update = AsyncMock()
    player.fetch_artwork = AsyncMock(return_value=None)
    cm = {"uri": "library://track/1", "title": "Song", "artist": "Artist"}
    _run(player._process_target_state(
        _player_state("shelf", "Bokhylle", playback_state="playing",
                      current_media=cm)))
    player._cached_media_data = dict(
        player.broadcast_media_update.await_args.args[0])
    player.broadcast_media_update.reset_mock()
    _run(player._process_target_state(
        _player_state("shelf", "Bokhylle", playback_state="paused",
                      current_media=cm)))
    media = player.broadcast_media_update.await_args.args[0]
    assert media["state"] == "paused"
    assert media["title"] == "Song"


def test_event_updates_player_cache_and_target(player):
    player._process_target_state = AsyncMock()
    updated = _player_state("shelf", "Bokhylle", playback_state="playing")
    _run(player._on_ma_event({"event": "player_updated",
                              "object_id": "shelf", "data": updated}))
    assert player._players["shelf"]["playback_state"] == "playing"
    player._process_target_state.assert_awaited_once()

    other = _player_state("stage", "Beosound Stage", playback_state="playing")
    player._process_target_state.reset_mock()
    _run(player._on_ma_event({"event": "player_updated",
                              "object_id": "stage", "data": other}))
    player._process_target_state.assert_not_awaited()


def test_queue_time_event_updates_position(player):
    player._cached_media_data = {"position": "0:00"}
    _run(player._on_ma_event({"event": "queue_time_updated",
                              "object_id": "shelf", "data": 75}))
    assert player._cached_media_data["position"] == "1:15"


# ── Helpers ──

def test_fmt_time():
    assert _fmt_time(None) == "0:00"
    assert _fmt_time(7) == "0:07"
    assert _fmt_time(199.184) == "3:19"


def test_status_reports_target_and_group(player):
    player._players["shelf"]["group_childs"] = ["shelf", "coffee"]
    status = _run(player.get_status())
    assert status["target_name"] == "Bokhylle"
    assert status["is_grouped"] is True
    assert status["group"] == ["Kaffi"]
    assert status["server_version"] == "2.9.2"
