#!/usr/bin/env python3
"""
BeoSound 5c BeoPlay Player (beo-player-beoplay)

Controls a B&O BeoPlay/NetworkLink speaker (Beosound Stage, CA17, ...) via
the pybeoplay library (vendored git submodule at external/pybeoplay).
Monitors the speaker's notification stream for state/track/volume changes
and broadcasts updates to the UI (port 8766), reporting volume to the
router so the volume arc stays in sync.

Milestone 1 scope: the speaker's built-in "B&O Radio" (netRadio) source.
The radio source sends synthetic URLs of the form beoplay://netradio/<id>;
generic stream URLs and Spotify URIs are rejected with a log warning.

BeoPlay HTTP API (port 8080, JSON):
  GET  /BeoDevice                    — device info
  GET  /BeoNotify/Notifications      — long-poll notification stream
  POST /BeoZone/Zone/PlayQueue/...   — station/queue playback
"""

import asyncio
import logging
import os
import sys
import time

# Ensure services/ is on the path for sibling imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.config import cfg
from lib.player_base import PlayerBase
from lib.timings import USER_ACTION_HORIZON
from lib.vendor import add_vendor_path

add_vendor_path("pybeoplay")
try:
    from pybeoplay import BeoPlay
except ImportError:
    BeoPlay = None

# Configuration
BEOPLAY_IP = cfg("player", "ip", default="")
NETRADIO_URL_PREFIX = "beoplay://netradio/"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('beo-player-beoplay')


class BeoplayPlayer(PlayerBase):
    """BeoPlay player service using the B&O NetworkLink HTTP API."""

    id = "beoplay"
    name = "BeoPlay"
    port = 8766

    def __init__(self):
        super().__init__()
        self.ip = BEOPLAY_IP
        self._device = None
        self._current_track_uri = None
        self._current_track_id = None

    # ── PlayerBase abstract methods ──

    async def play(self, uri=None, url=None, track_uri=None, meta=None,
                   radio=False, track_uris=None) -> bool:
        try:
            if uri:
                logger.warning("BeoPlay does not support Spotify URIs — ignoring uri=%s", uri)
            if url and url.startswith(NETRADIO_URL_PREFIX):
                station_id = url[len(NETRADIO_URL_PREFIX):]
                if not station_id:
                    logger.error("Empty station id in %s", url)
                    return False
                if not self._device.on:
                    await self._device.async_turn_on()
                await self._device.async_play_radio_station(station_id)
                self._current_track_uri = url
                if meta:
                    self._cache_media_from_meta(meta)
                logger.info("Playing B&O Radio station %s", station_id)
                return True
            if url:
                logger.warning(
                    "BeoPlay backend cannot stream URL %s (only B&O Radio is supported)", url)
                return False
            # Resume playback
            return await self.resume()
        except Exception as e:
            logger.error("Play failed: %s", e)
            return False

    async def pause(self) -> bool:
        try:
            await self._device.async_pause()
            logger.info("Paused")
            return True
        except Exception as e:
            logger.error("Pause failed: %s", e)
            return False

    async def resume(self) -> bool:
        try:
            await self._device.async_play()
            logger.info("Resumed")
            return True
        except Exception as e:
            logger.error("Resume failed: %s", e)
            return False

    async def next_track(self) -> bool:
        try:
            await self._device.async_forward()
            logger.info("Next track")
            return True
        except Exception as e:
            logger.error("Next track failed: %s", e)
            return False

    async def prev_track(self) -> bool:
        try:
            await self._device.async_backward()
            logger.info("Previous track")
            return True
        except Exception as e:
            logger.error("Previous track failed: %s", e)
            return False

    async def stop(self) -> bool:
        try:
            await self._device.async_stop()
            logger.info("Stopped")
            return True
        except Exception as e:
            logger.error("Stop failed: %s", e)
            return False

    async def get_capabilities(self) -> list:
        return ["bo_radio"]

    async def get_track_uri(self) -> str:
        return self._current_track_uri or ""

    async def get_status(self) -> dict:
        base = await super().get_status()
        cached = self._cached_media_data or {}
        base.update({
            "speaker_ip": self.ip,
            "state": self._current_playback_state or "stopped",
            "volume": cached.get("volume"),
            "current_track": {
                "title": cached.get("title", "—"),
                "artist": cached.get("artist", "—"),
                "album": cached.get("album", "—"),
            } if cached else None,
        })
        return base

    async def fade_volume(self, target: float, duration: float = 0.5):
        """Set speaker volume (0-100) — used for TTS announce ducking."""
        try:
            await self._device.async_set_volume(max(0.0, min(target, 100.0)) / 100.0)
        except Exception as e:
            logger.warning("fade_volume failed: %s", e)

    # ── Extra routes (consumed by the radio source) ──

    def add_routes(self, app):
        app.router.add_get("/beoplay/radio_favorites", self._handle_radio_favorites)

    async def _handle_radio_favorites(self, request):
        """GET /beoplay/radio_favorites — the speaker's B&O Radio favourites."""
        from aiohttp import web
        try:
            favorites = await self._device.async_get_radio_favorites()
            return web.json_response({"favorites": favorites or []},
                                     headers=self._cors_headers())
        except Exception as e:
            logger.warning("radio_favorites failed: %s", e)
            return web.json_response({"favorites": []},
                                     headers=self._cors_headers())

    # ── PlayerBase hooks ──

    async def on_start(self):
        if BeoPlay is None:
            logger.error("pybeoplay not available — run: git submodule update --init")
            sys.exit(1)
        if not BEOPLAY_IP:
            logger.error("No BeoPlay IP configured (set player.ip in config)")
            sys.exit(1)
        logger.info("Starting BeoPlay player for %s", self.ip)
        self._device = BeoPlay(self.ip, session=self._http_session)
        self._monitor_task = self._spawn(self._monitor_notifications(),
                                         name="beoplay_monitor")

    # ── Monitoring (notification stream) ──

    async def _monitor_notifications(self):
        """Consume the speaker's notification long-poll stream.

        pybeoplay updates the BeoPlay object's attributes per notification
        and invokes the (sync) callback; the stream disconnects after ~5
        minutes of inactivity, so reconnect in a loop.
        """
        logger.info("Starting BeoPlay notification monitoring for %s", self.ip)
        while self.running:
            try:
                await self._device.async_notificationsTask(
                    callback=self._on_notification)
                # Stream ended normally (idle disconnect) — reconnect
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Notification stream lost: %s", e)
                await asyncio.sleep(2)

    def _on_notification(self, notification: dict):
        """Sync callback from pybeoplay — hand off to async processing."""
        self._spawn(self._process_device_state(), name="beoplay_process")

    async def _process_device_state(self):
        """Map the BeoPlay object's auto-updated attributes to UI/router updates."""
        try:
            raw_state = self._device.state
            if raw_state == "play":
                state = "playing"
            elif raw_state == "pause":
                state = "paused"
            else:
                state = "stopped"

            # Wake trigger on transition to playing
            if state == "playing" and self._current_playback_state in (
                "paused", "stopped", None
            ):
                logger.info("Playback started (was: %s), triggering wake",
                            self._current_playback_state)
                self._spawn(self.trigger_wake(), name="trigger_wake")
                if self.seconds_since_command() > USER_ACTION_HORIZON:
                    logger.info("External playback detected, clearing active source")
                    self._spawn(
                        self.notify_router_playback_override(force=True),
                        name="playback_override")
            elif state == "stopped" and self._current_playback_state == "playing":
                if self.seconds_since_command() > USER_ACTION_HORIZON:
                    logger.info("External stop detected")
                    self._spawn(
                        self.notify_router_playback_override(force=True),
                        name="playback_override")

            self._current_playback_state = state

            # Volume reporting (0-1 → 0-100; base method deduplicates)
            if self._device.volume is not None:
                self._spawn(
                    self.report_volume_to_router(round(self._device.volume * 100)),
                    name="report_volume")

            # Track change detection
            title = self._device.media_track or ""
            artist = self._device.media_artist or ""
            album = self._device.media_album or ""
            if not (title or artist):
                return

            track_id = f"{title}|{artist}|{album}"
            if track_id != self._current_track_id:
                self._current_track_id = track_id

                artwork_base64 = None
                artwork_size = None
                if self._device.media_url:
                    result = await self.fetch_artwork(
                        self._device.media_url, session=self._http_session)
                    if result:
                        artwork_base64 = result["base64"]
                        artwork_size = result["size"]

                media_data = {
                    "title": title or "—",
                    "artist": artist or "—",
                    "album": album or "—",
                    "artwork": f"data:image/jpeg;base64,{artwork_base64}" if artwork_base64 else None,
                    "artwork_size": artwork_size,
                    "position": "0:00",
                    "duration": "0:00",
                    "state": state,
                    "volume": round(self._device.volume * 100) if self._device.volume is not None else 0,
                    "speaker_ip": self.ip,
                    "service": self._device.source or "",
                    "uri": self._current_track_uri or "",
                    "timestamp": int(time.time()),
                }
                self._cached_media_data = media_data
                await self.broadcast_media_update(media_data, "track_change")
                logger.info("Track changed: %s — %s", artist, title)

                # External track change? Clear active source
                if self.seconds_since_command() > USER_ACTION_HORIZON:
                    self._spawn(
                        self.notify_router_playback_override(force=True),
                        name="playback_override")
            elif self._cached_media_data:
                self._cached_media_data["state"] = state
                if self._device.volume is not None:
                    self._cached_media_data["volume"] = round(self._device.volume * 100)
        except Exception as e:
            logger.error("Error processing BeoPlay state: %s", e)

    # ── Helpers ──

    def _cache_media_from_meta(self, meta: dict):
        """Seed cached media from the source's meta until notifications refine it."""
        self._cached_media_data = {
            "title": meta.get("title", "—"),
            "artist": meta.get("artist", "—"),
            "album": "—",
            "artwork": None,
            "artwork_size": None,
            "position": "0:00",
            "duration": "0:00",
            "state": "playing",
            "volume": round(self._device.volume * 100) if self._device.volume is not None else 0,
            "speaker_ip": self.ip,
            "service": "netRadio",
            "uri": self._current_track_uri or "",
            "timestamp": int(time.time()),
        }


async def main():
    player = BeoplayPlayer()
    await player.run()


if __name__ == "__main__":
    asyncio.run(main())
