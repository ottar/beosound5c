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

# MA's per-call page size for library listings. _library() pages through
# the whole library (the arc list needs the complete alphabet — one page
# used to truncate a large artist list around "D"), with a total cap so a
# pathological library can't stall the service or flood the UI.
BROWSE_LIMIT = 500
BROWSE_MAX_ITEMS = 10000

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
        # On-demand drill by full MA URI (context-menu "Go to artist/album").
        # MA URIs contain "/" (provider://type/id), so this must run BEFORE
        # the "/"-split below. Works for library:// URIs too.
        if path.startswith("uri/"):
            return await self._browse_uri(path[len("uri/"):])

        parts = path.split("/") if path else []
        if not parts:
            return {"path": "", "parent": None, "name": "Music",
                    "items": ROOT_CATEGORIES}

        category, item_id = parts[0], (parts[1] if len(parts) > 1 else None)

        if category == "discover":
            return await self._discover()

        if category == "artists":
            if item_id is None:
                # Album artists only — the BeoSound artist list should mirror
                # the album shelf, not every track-level guest artist.
                items = await self._library("artists",
                                            album_artists_only=True)
                return self._listing("Artists", "artists", "", [
                    self._container_item(i, f"artists/{i['item_id']}")
                    for i in items])
            albums = await self._client.call(
                "music/artists/artist_albums", item_id=item_id,
                provider_instance_id_or_domain="library")
            return self._listing("Albums", path, "artists", [
                self._container_item(a, f"albums/{a['item_id']}", cover=True)
                for a in self._sorted(albums)])

        if category == "albums":
            if item_id is None:
                items = await self._library("albums")
                return self._listing("Albums", "albums", "", [
                    self._container_item(i, f"albums/{i['item_id']}", cover=True)
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
                items = self._dedupe_by_name(await self._library("playlists"))
                return self._listing("Playlists", "playlists", "", [
                    self._container_item(i, f"playlists/{i['item_id']}", cover=True)
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
                self._track_item(r, radio=True, cover=True) for r in items])

        return {"path": path, "parent": "", "name": "Music", "items": []}

    @staticmethod
    def _parse_uri(uri: str):
        """Split an MA URI into (provider, media_type, item_id).
        e.g. "apple_music://album/xyz" → ("apple_music", "album", "xyz")."""
        provider, _, rest = uri.partition("://")
        media_type, _, item_id = rest.partition("/")
        return provider, media_type, item_id

    async def _browse_uri(self, uri: str) -> dict:
        """Drill into any album/artist/playlist by its real MA URI, using the
        item's own provider (not hard-coded "library") so non-library items
        (e.g. Apple Music from Discover) resolve. One MA call per level; album
        children carry parent_uri so "Play from here" works, artist children
        keep uri/… paths so drilling continues."""
        provider, media_type, item_id = self._parse_uri(uri)
        this = f"uri/{uri}"
        if media_type == "album":
            tracks = await self._client.call(
                "music/albums/album_tracks", item_id=item_id,
                provider_instance_id_or_domain=provider)
            return self._listing("Album", this, "", [
                self._track_item(t, parent_uri=uri) for t in (tracks or [])])
        if media_type == "playlist":
            tracks = await self._client.call(
                "music/playlists/playlist_tracks", item_id=item_id,
                provider_instance_id_or_domain=provider)
            return self._listing("Playlist", this, "", [
                self._track_item(t, parent_uri=uri) for t in (tracks or [])])
        if media_type == "artist":
            albums = await self._client.call(
                "music/artists/artist_albums", item_id=item_id,
                provider_instance_id_or_domain=provider)
            return self._listing("Albums", this, "", [
                self._container_item(a, f"uri/{a.get('uri', '')}", cover=True)
                for a in self._sorted(albums)])
        return self._listing("", this, "", [])

    async def _discover(self) -> dict:
        """Music Assistant's home/Explore page: recommendation folders
        (Recently Played, Recently added, Random artists, …) flattened into
        one list where each folder title becomes a header row and its items
        follow. Everything is playable on GO — MA's play_media resolves any
        track/album/playlist/artist/radio URI."""
        folders = await self._client.call("music/recommendations")
        # `music_assistant.discover_rows` (config UI: MUSIC card) selects
        # which recommendation rows show; absent/empty = all of them.
        selected = cfg("music_assistant", "discover_rows", default=None)
        enabled = set(selected) if isinstance(selected, list) and selected else None
        items: list[dict] = []
        for folder in folders or []:
            if enabled is not None and (folder.get("item_id") or "") not in enabled:
                continue
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
            **self._context_fields(item, media_type),
        }

    # ── Context-menu metadata ──

    def _context_fields(self, item: dict, media_type: str = "") -> dict:
        """Fields the hold-GO context menu needs to build actions and drill
        to artist/album — safe defaults so Discover items missing them still
        serialize. `provider` is MA's provider instance/domain ("library" for
        library items); `in_library` gates the library-only track expansion."""
        provider = item.get("provider") or "library"
        return {
            "media_type": media_type or item.get("media_type") or "",
            "provider": provider,
            "favorite": bool(item.get("favorite")),
            "in_library": provider == "library",
            "artist_uri": self._first_artist_uri(item),
            "album_uri": self._album_uri(item),
        }

    @staticmethod
    def _first_artist_uri(item: dict) -> str:
        for a in item.get("artists") or []:
            if a.get("uri"):
                return a["uri"]
        return ""

    @staticmethod
    def _album_uri(item: dict) -> str:
        album = item.get("album")
        return album.get("uri", "") if isinstance(album, dict) else ""

    async def _library(self, media_type: str, **extra) -> list:
        """Full library listing for one media type, paged past MA's
        per-call cap and bounded by BROWSE_MAX_ITEMS."""
        items: list = []
        offset = 0
        while True:
            page = await self._client.call(f"music/{media_type}/library_items",
                                           limit=BROWSE_LIMIT, offset=offset,
                                           order_by="sort_name", **extra) or []
            items.extend(page)
            if len(page) < BROWSE_LIMIT or len(items) >= BROWSE_MAX_ITEMS:
                break
            offset += BROWSE_LIMIT
        return items[:BROWSE_MAX_ITEMS]

    @staticmethod
    def _sorted(items) -> list:
        return sorted(items or [], key=lambda i: (i.get("sort_name")
                                                  or i.get("name", "")).lower())

    @staticmethod
    def _dedupe_by_name(items: list) -> list:
        """Collapse library rows that share a case-folded name.

        Observed live on the MA server (10.0.0.10): Apple Music periodically
        reissues catalog ids for its curated "<Genre/Artist> essentials"
        playlists (old "pl.<hex>" ids replaced by new "p.<token>" ids). MA's
        library sync adds a *second* library://playlist/<id> row for the
        reissued id instead of updating the existing one, so the same title
        shows twice in PLAYLISTS (e.g. item_id 132 and 213, both "'70s
        singer-songwriter essentials" — 79 of 219 playlists were duplicated
        this way). Both rows are genuinely provider="library", so provider
        can't disambiguate; item_id can, since it's assigned in add order —
        keep the lowest (the original entry, still fully playable) and drop
        the reissue's duplicate row. Order of first appearance is preserved.
        """
        seen: dict[str, dict] = {}
        for it in items:
            key = (it.get("name") or "").strip().lower()
            prev = seen.get(key)
            if prev is None:
                seen[key] = it
                continue
            try:
                keep_new = int(it.get("item_id")) < int(prev.get("item_id"))
            except (TypeError, ValueError):
                keep_new = False
            if keep_new:
                seen[key] = it
        return list(seen.values())

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

    def _container_item(self, item: dict, path: str,
                        cover: bool = False) -> dict:
        subtitle = self._artist_names(item)
        return {
            "type": "category",
            "name": item.get("name", "Unknown"),
            "id": str(item.get("item_id", "")),
            "path": path,
            "uri": item.get("uri", ""),
            "subtitle": subtitle,
            "image": self._image_of(item),
            # Albums/playlists show their cover left of the name (like
            # Discover rows); artists stay text-only.
            "cover": cover,
            **self._context_fields(item),
        }

    def _track_item(self, item: dict, parent_uri: str = "",
                    radio: bool = False, cover: bool = False) -> dict:
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
            # Radio stations show their logo left of the name (like albums);
            # album/playlist tracks stay text-only.
            "cover": cover,
            **self._context_fields(item, "radio" if radio else "track"),
        }

    # ── Commands (router + browse iframe) ──

    async def handle_command(self, cmd, data) -> dict:
        if cmd == "play_item":
            return await self._play_item(data)

        if cmd == "favorite":
            return await self._library_toggle("music/favorites", data)

        if cmd == "library":
            return await self._library_toggle("music/library", data)

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

    async def _library_toggle(self, base: str, data: dict) -> dict:
        """Add/remove an item to MA favorites (base="music/favorites") or the
        library (base="music/library"). add uses the item URI; remove needs the
        media_type + numeric library item_id.

        NB: the exact MA argument names (item vs uri, library_item_id) must be
        confirmed against the running MA server before relying on this — see
        the plan's step-1d probe. Errors are swallowed into a status so the UI
        can keep going.
        """
        uri = data.get("uri", "")
        add = bool(data.get("add"))
        try:
            if add:
                if not uri:
                    return {"status": "error", "message": "Missing 'uri'"}
                await self._client.call(f"{base}/add_item", item=uri)
            else:
                await self._client.call(
                    f"{base}/remove_item",
                    media_type=data.get("media_type", ""),
                    library_item_id=data.get("item_id", ""))
            return {"status": "ok", "add": add}
        except MAClientError as e:
            log.warning("%s toggle failed: %s", base, e)
            return {"status": "error", "message": str(e)}

    async def _play_item(self, data: dict) -> dict:
        uri = data.get("uri", "")
        if not uri:
            return {"status": "error", "message": "Missing 'uri'"}
        parent_uri = data.get("parent_uri", "")
        title = data.get("name", "")
        artist = data.get("artist") or data.get("subtitle", "")
        radio = bool(data.get("radio"))
        # Context-menu enqueue variant. Distinct from the station `radio`
        # display flag above: `radio_mode` asks MA to *generate* a dynamic
        # queue seeded by the item (Start radio action).
        option = data.get("option", "replace")
        if option not in ("play", "next", "add", "replace"):
            option = "replace"
        radio_mode = bool(data.get("radio_mode"))
        # Context-menu "Shuffle play" action: turn on queue shuffle once the
        # new queue is playing. Only meaningful when a fresh queue actually
        # starts (play/replace below) — enqueue variants (next/add) and the
        # dynamic-radio path return earlier and never consult this.
        shuffle = bool(data.get("shuffle"))

        self._current = {"uri": uri, "parent_uri": parent_uri, "name": title,
                         "subtitle": artist, "radio": radio,
                         "image": data.get("image", "")}
        meta = {"title": title, "artist": artist,
                "artwork_url": data.get("image", "")}

        # Play next / Add to queue: the item joins the queue without
        # interrupting the current track — nothing new starts playing, so skip
        # the PLAYING pre-broadcast and the artist-track expansion. MA enqueues
        # the bare URI directly.
        if option in ("next", "add"):
            ok = await self.player_play(uri=uri, radio=False, option=option,
                                        meta=meta)
            return ({"status": "ok"} if ok else
                    {"status": "error", "message": "player rejected enqueue"})

        # Start radio: MA builds the dynamic queue itself, so don't expand the
        # artist into tracks — pass the bare artist/track URI with radio_mode.
        if radio_mode:
            await self.register("playing", auto_power=True)
            await self.post_media_update(title=title, artist=artist,
                                         artwork=data.get("image", ""),
                                         state="playing", track_uri=uri)
            ok = await self.player_play(uri=uri, radio=True, meta=meta)
            if ok:
                self._playing_state = "playing"
                return {"status": "ok"}
            self._playing_state = "stopped"
            await self.register("available")
            return {"status": "error", "message": "player rejected radio"}

        # Artists play their whole catalogue. MA's play_media chokes on a bare
        # artist URI — it resolves the entire discography server-side and blows
        # past our 30s call timeout — so expand to the artist's tracks here
        # (artist_tracks returns in ~0.1s) and enqueue those URIs directly.
        # Only the "Play from here" (replace) path plays a bare container.
        track_uris = None
        if option == "replace" and uri.startswith("library://artist/"):
            artist_id = uri.rsplit("/", 1)[-1]
            tracks = await self._client.call(
                "music/artists/artist_tracks", item_id=artist_id,
                provider_instance_id_or_domain="library")
            track_uris = [t.get("uri") for t in (tracks or []) if t.get("uri")]
            if not track_uris:
                log.error("Artist %s has no playable tracks", uri)
                self._playing_state = "stopped"
                await self.register("available")
                return {"status": "error", "message": "artist has no tracks"}

        # Pre-broadcast so PLAYING shows the selection immediately; the
        # player refines it (artwork, position) from MA events.
        await self.register("playing", auto_power=True)
        await self.post_media_update(title=title, artist=artist,
                                     artwork=data.get("image", ""),
                                     state="playing", track_uri=uri)

        # Play now (option="play") plays just this item as a fresh queue; Play
        # from here (option="replace", the classic GO) plays the parent context
        # starting at the selected track.
        if option == "play":
            play_uri, start_item = uri, None
        else:  # replace
            play_uri = None if track_uris else (parent_uri or uri)
            start_item = uri if (parent_uri and not track_uris) else None

        ok = await self.player_play(
            uri=play_uri,
            track_uris=track_uris,
            track_uri=start_item,
            # NB: never radio_mode=True here (station streams play directly);
            # the dynamic-radio path is handled above.
            radio=False,
            option=option,
            meta=meta,
        )
        if ok:
            self._playing_state = "playing"
            if shuffle:
                # Best-effort: the queue is already playing at this point, so
                # a failed/unsupported shuffle call shouldn't fail the whole
                # play_item — the item is playing either way, just not shuffled.
                if not await self.player_set_shuffle(True):
                    log.warning("Shuffle-on-play requested but player "
                                "rejected /player/shuffle for %s", uri)
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
