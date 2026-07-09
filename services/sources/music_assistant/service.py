#!/usr/bin/env python3
"""
BeoSound 5c Music Assistant Source (beo-source-music-assistant)

Browses the Music Assistant library (artists, albums, playlists, tracks,
radio) over the shared MAClient websocket and plays selections through
the player service (player_play with MA library:// URIs). Requires
player.type=music_assistant — other players can't resolve MA URIs.

Port: 8780
"""

import asyncio
import logging
import os
import sys
import time

from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lib.config import cfg
from lib.ma_client import MAClient, MAClientError
from lib.source_base import SourceBase

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
log = logging.getLogger('beo-music-assistant')

# One browse page is one ArcList level — cap list sizes so a huge
# library doesn't stall the UI. MA returns items sorted by sort_name.
BROWSE_LIMIT = 500

ROOT_CATEGORIES = [
    {"type": "category", "name": "Discover", "id": "discover", "path": "discover",
     "icon": "compass", "color": "#00CEC9"},
    {"type": "category", "name": "Artists", "id": "artists", "path": "artists",
     "icon": "microphone-stage", "color": "#FD79A8"},
    {"type": "category", "name": "Albums", "id": "albums", "path": "albums",
     "icon": "vinyl-record", "color": "#74B9FF"},
    {"type": "category", "name": "Playlists", "id": "playlists", "path": "playlists",
     "icon": "playlist", "color": "#A29BFE"},
    {"type": "category", "name": "Tracks", "id": "tracks", "path": "tracks",
     "icon": "music-notes", "color": "#F9CA24"},
    {"type": "category", "name": "Radio", "id": "radios", "path": "radios",
     "icon": "radio", "color": "#FF6B6B"},
]


class MusicAssistantSource(SourceBase):
    """Music Assistant library browser."""

    id = "music_assistant"
    name = "Music Assistant"
    port = 8780
    manages_queue = True
    action_map = {
        "play": "toggle",
        "pause": "toggle",
        "go": "toggle",
        "next": "next",
        "prev": "prev",
        "left": "prev",
        "right": "next",
        "up": "next",
        "down": "prev",
        "stop": "stop",
    }

    def __init__(self):
        super().__init__()
        self._client: MAClient | None = None
        self._playing_state = "stopped"
        self._current: dict | None = None  # last item played via this source

    # ── Lifecycle ──

    async def on_start(self):
        if cfg("player", "type", default="") != "music_assistant":
            log.warning("player.type is not music_assistant — MA URIs "
                        "won't be playable on the active player")
        self._client = MAClient()
        await self._client.start()
        await self.register("available")
        log.info("Music Assistant source ready (%s)", self._client.ws_url)

    async def on_stop(self):
        if self._client:
            await self._client.close()

    # ── Browse API ──

    def add_routes(self, app):
        app.router.add_get("/browse", self._handle_browse)

    async def _handle_browse(self, request):
        path = request.query.get("path", "").strip("/")
        try:
            result = await self._browse(path)
            return web.json_response(result, headers=self._cors_headers())
        except MAClientError as e:
            log.warning("Browse failed for path=%s: %s", path, e)
            return web.json_response({"error": str(e)}, status=502,
                                     headers=self._cors_headers())
        except Exception as e:
            log.exception("Browse error for path=%s", path)
            return web.json_response({"error": str(e)}, status=500,
                                     headers=self._cors_headers())

    async def _browse(self, path: str) -> dict:
        parts = path.split("/") if path else []
        if not parts:
            return {"path": "", "parent": None, "name": "Music",
                    "items": ROOT_CATEGORIES}

        category, item_id = parts[0], (parts[1] if len(parts) > 1 else None)

        if category == "discover":
            return await self._discover()

        if category == "artists":
            if item_id is None:
                items = await self._library("artists")
                return self._listing("Artists", "artists", "", [
                    self._container_item(i, f"artists/{i['item_id']}")
                    for i in items])
            albums = await self._client.call(
                "music/artists/artist_albums", item_id=item_id,
                provider_instance_id_or_domain="library")
            return self._listing("Albums", path, "artists", [
                self._container_item(a, f"albums/{a['item_id']}")
                for a in self._sorted(albums)])

        if category == "albums":
            if item_id is None:
                items = await self._library("albums")
                return self._listing("Albums", "albums", "", [
                    self._container_item(i, f"albums/{i['item_id']}")
                    for i in items])
            tracks = await self._client.call(
                "music/albums/album_tracks", item_id=item_id,
                provider_instance_id_or_domain="library")
            parent_uri = f"library://album/{item_id}"
            return self._listing("Album", path, "albums", [
                self._track_item(t, parent_uri=parent_uri)
                for t in (tracks or [])])

        if category == "playlists":
            if item_id is None:
                items = await self._library("playlists")
                return self._listing("Playlists", "playlists", "", [
                    self._container_item(i, f"playlists/{i['item_id']}")
                    for i in items])
            tracks = await self._client.call(
                "music/playlists/playlist_tracks", item_id=item_id,
                provider_instance_id_or_domain="library")
            parent_uri = f"library://playlist/{item_id}"
            return self._listing("Playlist", path, "playlists", [
                self._track_item(t, parent_uri=parent_uri)
                for t in (tracks or [])])

        if category == "tracks":
            items = await self._library("tracks")
            return self._listing("Tracks", "tracks", "", [
                self._track_item(t) for t in items])

        if category == "radios":
            items = await self._library("radios")
            return self._listing("Radio", "radios", "", [
                self._track_item(r, radio=True) for r in items])

        return {"path": path, "parent": "", "name": "Music", "items": []}

    async def _discover(self) -> dict:
        """Music Assistant's home/Explore page: recommendation folders
        (Recently Played, Recently added, Random artists, …) flattened into
        one list where each folder title becomes a header row and its items
        follow. Everything is playable on GO — MA's play_media resolves any
        track/album/playlist/artist/radio URI."""
        folders = await self._client.call("music/recommendations")
        items: list[dict] = []
        for folder in folders or []:
            folder_items = folder.get("items") or []
            if not folder_items:
                continue
            title = folder.get("name") or ""
            items.append({
                "type": "header",
                "name": title,
                "id": f"hdr-{folder.get('item_id') or folder.get('uri') or title}",
            })
            items.extend(self._recommend_item(it) for it in folder_items)
        return self._listing("Discover", "discover", "", items)

    _MEDIA_TYPE_LABELS = {"playlist": "Playlist", "album": "Album",
                          "artist": "Artist", "radio": "Radio", "track": ""}

    def _recommend_item(self, item: dict) -> dict:
        """Map an MA recommendation item to a playable list row. Subtitle is
        the artist(s) when present, otherwise the media-type label so albums/
        playlists/artists read as what they are."""
        media_type = item.get("media_type") or "track"
        subtitle = self._artist_names(item) or self._MEDIA_TYPE_LABELS.get(
            media_type, media_type.title())
        return {
            "type": "track",  # playable leaf — GO plays the URI directly
            "name": item.get("name", "Unknown"),
            "id": str(item.get("uri") or item.get("item_id") or ""),
            "uri": item.get("uri", ""),
            "parent_uri": "",
            "subtitle": subtitle,
            "image": self._image_of(item),
            "radio": media_type == "radio",
            "cover": True,  # Discover rows show a small cover left of the text
        }

    async def _library(self, media_type: str) -> list:
        items = await self._client.call(f"music/{media_type}/library_items",
                                        limit=BROWSE_LIMIT, offset=0,
                                        order_by="sort_name")
        return items or []

    @staticmethod
    def _sorted(items) -> list:
        return sorted(items or [], key=lambda i: (i.get("sort_name")
                                                  or i.get("name", "")).lower())

    def _listing(self, name, path, parent, items) -> dict:
        return {"path": path, "parent": parent, "name": name, "items": items}

    def _image_of(self, item: dict) -> str:
        images = ((item.get("metadata") or {}).get("images")) or []
        for img in images:
            if img.get("type") in ("thumb", None):
                url = self._client.image_url_for(img)
                if url:
                    return url
        # Recommendation (Discover) items carry a single top-level image
        # dict instead of metadata.images — fall back to it.
        img = item.get("image")
        if isinstance(img, dict):
            return self._client.image_url_for(img)
        return ""

    @staticmethod
    def _artist_names(item: dict) -> str:
        artists = item.get("artists") or []
        return ", ".join(a.get("name", "") for a in artists if a.get("name"))

    def _container_item(self, item: dict, path: str) -> dict:
        subtitle = self._artist_names(item)
        return {
            "type": "category",
            "name": item.get("name", "Unknown"),
            "id": str(item.get("item_id", "")),
            "path": path,
            "uri": item.get("uri", ""),
            "subtitle": subtitle,
            "image": self._image_of(item),
        }

    def _track_item(self, item: dict, parent_uri: str = "",
                    radio: bool = False) -> dict:
        return {
            "type": "track",
            "name": item.get("name", "Unknown"),
            "id": str(item.get("item_id", "")),
            "uri": item.get("uri", ""),
            "parent_uri": parent_uri,
            "subtitle": self._artist_names(item),
            "image": self._image_of(item),
            "duration": item.get("duration") or 0,
            "radio": radio,
        }

    # ── Commands (router + browse iframe) ──

    async def handle_command(self, cmd, data) -> dict:
        if cmd == "play_item":
            return await self._play_item(data)

        if cmd == "toggle":
            state = await self.player_state()
            if state == "playing":
                await self.player_pause()
                self._playing_state = "paused"
                await self.register("paused")
            elif state == "paused":
                await self.player_resume()
                self._playing_state = "playing"
                await self.register("playing")
            elif self._current:
                return await self._play_item(self._current)
            return {"status": "ok"}

        if cmd == "next":
            await self.player_next()
            return {"status": "ok"}

        if cmd == "prev":
            await self.player_prev()
            return {"status": "ok"}

        if cmd == "stop":
            await self.player_stop()
            self._playing_state = "stopped"
            await self.register("available")
            return {"status": "ok"}

        return {"status": "error", "message": f"Unknown command: {cmd}"}

    async def _play_item(self, data: dict) -> dict:
        uri = data.get("uri", "")
        if not uri:
            return {"status": "error", "message": "Missing 'uri'"}
        parent_uri = data.get("parent_uri", "")
        title = data.get("name", "")
        artist = data.get("artist") or data.get("subtitle", "")
        radio = bool(data.get("radio"))

        self._current = {"uri": uri, "parent_uri": parent_uri, "name": title,
                         "subtitle": artist, "radio": radio,
                         "image": data.get("image", "")}

        # Pre-broadcast so PLAYING shows the selection immediately; the
        # player refines it (artwork, position) from MA events.
        await self.register("playing", auto_power=True)
        await self.post_media_update(title=title, artist=artist,
                                     artwork=data.get("image", ""),
                                     state="playing", track_uri=uri)

        ok = await self.player_play(
            uri=parent_uri or uri,
            track_uri=uri if parent_uri else None,
            radio=radio,
            meta={"title": title, "artist": artist,
                  "artwork_url": data.get("image", "")},
        )
        if ok:
            self._playing_state = "playing"
            return {"status": "ok"}
        log.error("Player failed to start %s", uri)
        self._playing_state = "stopped"
        await self.register("available")
        return {"status": "error", "message": "player rejected play"}

    async def handle_resync(self) -> dict:
        """Re-register after a router restart (router probes /resync) —
        without this the MUSIC menu entry vanishes until this service is
        itself restarted."""
        state = (self._playing_state
                 if self._playing_state in ("playing", "paused")
                 else "available")
        await self.register(state)
        return {"status": "ok", "resynced": True}

    # ── Activation ──

    async def activate_playback(self):
        """Source button pressed while idle — resume the MA queue."""
        state = await self.player_state()
        if state in ("playing", "paused"):
            return
        if self._current:
            await self._play_item(self._current)
        else:
            await self.player_resume()


async def main():
    source = MusicAssistantSource()
    await source.run()


if __name__ == "__main__":
    asyncio.run(main())
