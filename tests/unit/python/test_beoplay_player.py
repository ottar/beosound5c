"""Tests for the BeoPlay player backend and the radio source's B&O Radio
browse hook.

The player is exercised with a fake pybeoplay BeoPlay object injected
directly — no network. Pins the milestone-1 contract: netRadio station
playback via the synthetic beoplay://netradio/<id> URL, rejection of
generic stream URLs, capability gating, and the favourites → station-dict
mapping the radio source relies on.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from players.beoplay import BeoplayPlayer, NETRADIO_URL_PREFIX
from sources.radio.service import RadioService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeBeoPlay:
    """Minimal stand-in for pybeoplay.BeoPlay."""

    def __init__(self, on=True):
        self.on = on
        self.volume = 0.42
        self.state = None
        self.media_track = None
        self.media_artist = None
        self.media_album = None
        self.media_url = None
        self.source = None
        self.async_turn_on = AsyncMock()
        self.sourcesID = ["beoradio:1234.5678@products.bang-olufsen.com"]
        self.async_get_sources = AsyncMock()
        self.async_play_beoradio_station = AsyncMock(return_value=True)
        self.async_play = AsyncMock()
        self.async_pause = AsyncMock()
        self.async_stop = AsyncMock()
        self.async_forward = AsyncMock()
        self.async_backward = AsyncMock()
        self.async_set_volume = AsyncMock()


@pytest.fixture
def player():
    p = BeoplayPlayer()
    p._device = FakeBeoPlay()
    return p


def test_play_netradio_station(player):
    ok = _run(player.play(url=f"{NETRADIO_URL_PREFIX}s12345", radio=True))
    assert ok is True
    player._device.async_play_beoradio_station.assert_awaited_once_with(
        "beoradio:1234.5678@products.bang-olufsen.com", "s12345")
    # Speaker was already on — no turn_on
    player._device.async_turn_on.assert_not_awaited()
    assert _run(player.get_track_uri()) == f"{NETRADIO_URL_PREFIX}s12345"


def test_play_netradio_wakes_speaker_from_standby(player):
    player._device.on = False
    ok = _run(player.play(url=f"{NETRADIO_URL_PREFIX}s1", radio=True))
    assert ok is True
    player._device.async_turn_on.assert_awaited_once()


def test_play_rejects_plain_stream_url(player):
    ok = _run(player.play(url="http://example.com/stream.mp3", radio=True))
    assert ok is False
    player._device.async_play_beoradio_station.assert_not_awaited()


def test_play_rejects_empty_station_id(player):
    ok = _run(player.play(url=NETRADIO_URL_PREFIX, radio=True))
    assert ok is False


def test_play_meta_seeds_cached_media(player):
    _run(player.play(url=f"{NETRADIO_URL_PREFIX}s2", radio=True,
                     meta={"title": "P1", "artist": "B&O Radio"}))
    assert player._cached_media_data["title"] == "P1"
    assert player._cached_media_data["state"] == "playing"


def test_capabilities_is_bo_radio_only(player):
    assert _run(player.get_capabilities()) == ["bo_radio"]


def test_transport_maps_to_pybeoplay(player):
    assert _run(player.pause()) is True
    player._device.async_pause.assert_awaited_once()
    assert _run(player.resume()) is True
    player._device.async_play.assert_awaited_once()
    assert _run(player.next_track()) is True
    player._device.async_forward.assert_awaited_once()
    assert _run(player.prev_track()) is True
    player._device.async_backward.assert_awaited_once()
    assert _run(player.stop()) is True
    player._device.async_stop.assert_awaited_once()


# ── Radio source B&O Radio hook ──────────────────────────────────────────


@pytest.fixture
def radio_svc(mock_config, monkeypatch):
    mock_config({"player": {"type": "beoplay", "ip": "192.0.2.10"}})
    monkeypatch.setattr(RadioService, "_load_favourites", lambda self: None)
    monkeypatch.setattr(RadioService, "_load_last_station", lambda self: None)
    return RadioService()


def test_root_categories_beoplay_mode(radio_svc):
    items = radio_svc._root_categories()["items"]
    paths = [i["path"] for i in items]
    assert paths == ["bo_radio", "favourites"]


def test_root_categories_normal_mode(mock_config, monkeypatch):
    mock_config({"player": {"type": "sonos", "ip": "192.0.2.11"}})
    monkeypatch.setattr(RadioService, "_load_favourites", lambda self: None)
    monkeypatch.setattr(RadioService, "_load_last_station", lambda self: None)
    svc = RadioService()
    paths = [i["path"] for i in svc._root_categories()["items"]]
    assert "bo_radio" not in paths
    assert "popular" in paths


def test_browse_bo_radio_maps_favourites(radio_svc, monkeypatch):
    favs = [
        {"name": "NRK P1", "station": "s101"},
        {"name": "NRK P2", "station": "s102"},
    ]

    async def fake_fetch(self):
        return [
            {
                "stationuuid": f"beoplay-{f['station']}",
                "name": f["name"],
                "url_resolved": f"beoplay://netradio/{f['station']}",
                "favicon": "", "country": "", "tags": "B&O Radio",
                "codec": "", "bitrate": 0,
            }
            for f in favs
        ]

    monkeypatch.setattr(RadioService, "_fetch_beoplay_favorites", fake_fetch)
    result = _run(radio_svc._browse("bo_radio"))
    assert result["name"] == "B&O Radio"
    assert [i["name"] for i in result["items"]] == ["NRK P1", "NRK P2"]
    assert result["items"][0]["url_resolved"] == "beoplay://netradio/s101"
    # Browse context snapshotted for next/prev cycling
    assert len(radio_svc._browse_stations) == 2


def test_fetch_beoplay_favorites_maps_payload(radio_svc):
    """The HTTP payload {"favorites": [{"name","station"}]} maps to the
    station-dict shape with a synthetic beoplay:// URL."""
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={"favorites": [
        {"name": "P3", "station": "s303"},
        {"name": "broken", "station": ""},   # skipped — no station id
    ]})
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    radio_svc._http_session = MagicMock()
    radio_svc._http_session.get = MagicMock(return_value=ctx)

    stations = _run(radio_svc._fetch_beoplay_favorites())
    assert stations == [{
        "stationuuid": "beoplay-s303",
        "name": "P3",
        "url_resolved": "beoplay://netradio/s303",
        "favicon": "", "country": "", "tags": "B&O Radio",
        "codec": "", "bitrate": 0,
    }]


def test_fetch_beoplay_favorites_degrades_on_error(radio_svc):
    radio_svc._http_session = MagicMock()
    radio_svc._http_session.get = MagicMock(side_effect=RuntimeError("down"))
    assert _run(radio_svc._fetch_beoplay_favorites()) == []
