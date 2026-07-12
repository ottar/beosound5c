#!/usr/bin/env python3
"""
BeoSound 5c Music Assistant Player (beo-player-music-assistant)

Controls playback on a Music Assistant server (ws://<host>:8095/ws) via
the shared MAClient (services/lib/ma_client.py). The MA *target player*
(which speaker/queue this BS5c drives) is chosen at runtime from the UI
— never auto-ranked; the last selection persists across restarts in
/etc/beosound5c/music_assistant_state.json. On startup a speaker that is
actively playing wins (so a restart re-attaches to live playback); else
the persisted target is used if still available; else, with exactly one
available MA player, that one is picked; otherwise no target is set until
the user selects one in the JOIN/speakers view.

Speaker grouping uses MA's native group commands (players/cmd/group /
ungroup) and is exposed through the same /player/network + /player/join
endpoints the Sonos JOIN feature uses, plus /player/select_target for
switching the playback target (transferring the queue when playing).

Sources start playback through the normal PlayerBase play() contract
(player_queues/play_media) — unlike the beosound5c_extension fork,
which refused direct play() calls and needed fragile source-side
kick/verify machinery.
"""

import asyncio
import logging
import os
import sys
import time

# Ensure services/ is on the path for sibling imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aiohttp
from aiohttp import web

from lib.config import cfg
from lib.ma_client import MAClient, MAClientError
from lib.player_base import PlayerBase
from lib.timings import USER_ACTION_HORIZON
from lib.token_store import TokenStore

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# The B&O (NetworkLink) provider can stop the whole stream up to ~10s after
# a group member is removed (observed live 2026-07-07: stop 9s after
# ungroup). A stop within this window of our own ungroup is auto-resumed.
UNGROUP_RESUME_WINDOW_S = 15.0

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('beo-player-music-assistant')


def _fmt_time(seconds) -> str:
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        seconds = 0
    return f"{seconds // 60}:{seconds % 60:02d}"


class MusicAssistantPlayer(PlayerBase):
    """Music Assistant player service using the native MA websocket API."""

    id = "music_assistant"
    name = "Music Assistant"
    port = 8766

    def __init__(self):
        super().__init__()
        self._client: MAClient | None = None
        self._target_id: str | None = None
        self._players: dict[str, dict] = {}
        self._state_store = TokenStore("music_assistant_state.json",
                                       dev_dir=SCRIPT_DIR)
        self._current_track_id: str | None = None
        self._current_track_uri: str | None = None
        # Timestamp of our last ungroup command — used to auto-resume if the
        # B&O provider collapses the stream right after a member leaves.
        self._last_ungroup_ts = 0.0
        self._ungroup_resume_pending = False
        # Only report MA volume to the router's arc when the volume
        # adapter is MA too — with e.g. volume.type=beoplay the wheel
        # drives the speaker's hardware volume and the two scales differ.
        # Mirrors infer_volume_type(): an unset volume.type falls back to
        # player.type, so a pure-MA config needs no explicit volume block.
        vol_type = (cfg("volume", "type", default="")
                    or cfg("player", "type", default=""))
        self._report_ma_volume = str(vol_type).lower() == self.id

    # ── Target selection ──

    @property
    def target(self) -> dict | None:
        return self._players.get(self._target_id) if self._target_id else None

    def _available_players(self) -> list[dict]:
        return [p for p in self._players.values() if p.get("available")]

    def _set_target(self, player_id: str, persist: bool = True):
        self._target_id = player_id
        if persist:
            try:
                self._state_store.save({"player_id": player_id})
            except Exception as e:
                logger.warning("Could not persist target player: %s", e)
        logger.info("MA target player: %s (%s)",
                    self._player_name(player_id), player_id)

    def _player_name(self, player_id: str) -> str:
        p = self._players.get(player_id) or {}
        return p.get("display_name") or p.get("name") or player_id

    def _restore_target(self):
        """Pick the target: currently-playing player → persisted id →
        sole available player → none.

        A live playback wins over the stored id so a service/UI restart
        re-attaches to whatever speaker is actually playing instead of a
        stale target — otherwise, with several MA players available, the
        wheel/buttons stay pointed at the last selection and the user has
        to reselect in the speakers view to regain control. If the stored
        target is itself the playing one it's kept (no needless hop when
        multiple speakers play independently)."""
        stored = self._state_store.load() or {}
        stored_id = stored.get("player_id")

        playing = [p for p in self._available_players()
                   if p.get("playback_state") == "playing"]
        if playing:
            if stored_id and any(p["player_id"] == stored_id for p in playing):
                self._set_target(stored_id, persist=False)
            else:
                self._set_target(playing[0]["player_id"])
            return

        if stored_id and (self._players.get(stored_id) or {}).get("available"):
            self._set_target(stored_id, persist=False)
            return
        available = self._available_players()
        if len(available) == 1:
            self._set_target(available[0]["player_id"])
            return
        self._target_id = None
        logger.info("No MA target player yet (%d available) — "
                    "select one in the speakers view", len(available))

    # ── PlayerBase abstract methods ──

    async def play(self, uri=None, url=None, track_uri=None, meta=None,
                   radio=False, track_uris=None, option=None) -> bool:
        if not self._target_id:
            logger.error("Play rejected: no MA target player selected")
            return False
        media = track_uris or uri or url
        if not media:
            return await self.resume()
        # option chooses how the queue is affected: play/next/add enqueue
        # variants vs replace (default). Anything unexpected → replace.
        opt = option if option in ("play", "next", "add", "replace") else "replace"
        args = {"queue_id": self._target_id, "media": media, "option": opt}
        if radio:
            args["radio_mode"] = True
        if track_uri:
            args["start_item"] = track_uri
        try:
            await self._client.call("player_queues/play_media", **args)
        except MAClientError as e:
            logger.error("play_media failed: %s", e)
            return False
        self._current_track_uri = uri or url or ""
        if meta:
            self._cache_media_from_meta(meta)
        logger.info("Playing %s on %s", media, self._player_name(self._target_id))
        return True

    async def _transport(self, command: str) -> bool:
        if not self._target_id:
            logger.warning("%s ignored: no MA target player", command)
            return False
        try:
            await self._client.call(f"players/cmd/{command}",
                                    player_id=self._target_id)
            return True
        except MAClientError as e:
            logger.error("%s failed: %s", command, e)
            return False

    async def pause(self) -> bool:
        return await self._transport("pause")

    async def resume(self) -> bool:
        return await self._transport("play")

    async def next_track(self) -> bool:
        return await self._transport("next")

    async def prev_track(self) -> bool:
        return await self._transport("previous")

    async def stop(self) -> bool:
        return await self._transport("stop")

    async def set_shuffle(self, enabled: bool) -> bool:
        if not self._target_id:
            return False
        try:
            await self._client.call("player_queues/shuffle",
                                    queue_id=self._target_id,
                                    shuffle_enabled=bool(enabled))
            return True
        except MAClientError as e:
            logger.error("shuffle failed: %s", e)
            return False

    async def get_capabilities(self) -> list:
        # url_stream/radio: MA plays arbitrary stream URLs, so the radio
        # source can offer internet radio when this player is active.
        return ["music_assistant", "url_stream", "radio"]

    async def get_track_uri(self) -> str:
        cm = (self.target or {}).get("current_media") or {}
        return cm.get("uri") or self._current_track_uri or ""

    async def fade_volume(self, target: float, duration: float = 0.5):
        """Set MA volume (0-100) — used for TTS announce ducking."""
        if not self._target_id:
            return
        try:
            await self._set_master_volume(target)
        except MAClientError as e:
            logger.warning("fade_volume failed: %s", e)

    # ── Volume (group-aware) ──

    def _master_volume(self) -> int | None:
        """The volume the wheel arc should show: group volume when the
        target is grouped (MA keeps it as the members' average), the
        target's own level otherwise."""
        target = self.target or {}
        if self._group_member_ids():
            gv = target.get("group_volume")
            if gv is not None:
                return int(gv)
            levels = [v for v in
                      ((self._players.get(pid) or {}).get("volume_level")
                       for pid in [self._target_id, *self._group_member_ids()])
                      if v is not None]
            if levels:
                return round(sum(levels) / len(levels))
        vol = target.get("volume_level")
        return int(vol) if vol is not None else None

    async def _set_master_volume(self, level: float):
        """Set the wheel volume on the target: MA's group_volume scales
        every member proportionally (individual trims survive), a solo
        target gets a plain volume_set."""
        cmd = ("players/cmd/group_volume" if self._group_member_ids()
               else "players/cmd/volume_set")
        await self._client.call(cmd, player_id=self._target_id,
                                volume_level=int(max(0, min(level, 100))))

    # ── Queue support ──

    async def get_queue(self, start=0, max_items=50) -> dict:
        if not self._target_id or not self._client.connected:
            return {"tracks": [], "current_index": -1, "total": 0}
        try:
            queue = await self._client.call("player_queues/get",
                                            queue_id=self._target_id)
            items = await self._client.call("player_queues/items",
                                            queue_id=self._target_id,
                                            limit=max_items, offset=start)
        except MAClientError as e:
            logger.warning("get_queue failed: %s", e)
            return {"tracks": [], "current_index": -1, "total": 0}
        tracks = []
        for item in items or []:
            mi = item.get("media_item") or {}
            artists = mi.get("artists") or []
            tracks.append({
                "title": mi.get("name") or item.get("name", ""),
                "artist": artists[0].get("name", "") if artists else "",
                "album": (mi.get("album") or {}).get("name", ""),
                "duration": _fmt_time(item.get("duration")),
                "uri": mi.get("uri") or "",
            })
        queue = queue or {}
        return {"tracks": tracks,
                "current_index": queue.get("current_index", -1),
                "total": queue.get("items", len(tracks))}

    async def play_from_queue(self, position: int) -> bool:
        if not self._target_id:
            return False
        try:
            await self._client.call("player_queues/play_index",
                                    queue_id=self._target_id,
                                    index=int(position))
            return True
        except MAClientError as e:
            logger.error("play_from_queue failed: %s", e)
            return False

    async def play_track_radio(self, track_uri) -> bool:
        """Start MA's dynamic radio mode seeded by *track_uri* — replaces
        the queue with an endless similar-tracks stream."""
        if not self._target_id or not track_uri:
            return False
        try:
            await self._client.call("player_queues/play_media",
                                    queue_id=self._target_id,
                                    media=track_uri, radio_mode=True)
            return True
        except MAClientError as e:
            logger.error("play_track_radio failed: %s", e)
            return False

    async def get_shuffle(self) -> bool | None:
        """Current queue shuffle state, or None when unknown (no target /
        disconnected / call failed) — the UI hides the toggle then."""
        if not self._target_id or not self._client.connected:
            return None
        try:
            queue = await self._client.call("player_queues/get",
                                            queue_id=self._target_id)
        except MAClientError as e:
            logger.warning("shuffle state fetch failed: %s", e)
            return None
        if not queue:
            return None
        return bool(queue.get("shuffle_enabled"))

    # ── Status ──

    async def get_status(self) -> dict:
        base = await super().get_status()
        cached = self._cached_media_data or {}
        target = self.target or {}
        group_names = [self._player_name(pid)
                       for pid in self._group_member_ids()]
        base.update({
            "connected": bool(self._client and self._client.connected),
            "server": self._client.ws_url if self._client else "",
            "server_version": (self._client.server_info.get("server_version")
                               if self._client else None),
            "state": self._current_playback_state or "stopped",
            "shuffle": await self.get_shuffle(),
            "volume": self._master_volume(),
            "target_id": self._target_id,
            "target_name": (self._player_name(self._target_id)
                            if self._target_id else None),
            "is_grouped": bool(group_names),
            "group": group_names,
            "current_track": {
                "title": cached.get("title", "—"),
                "artist": cached.get("artist", "—"),
                "album": cached.get("album", "—"),
            } if cached else None,
        })
        return base

    def _output_label(self) -> str | None:
        """Volume overlay label: the target's name, plus member count when
        grouped ("Stage +2") — mirrors the JOIN view's group icon."""
        if not self._target_id:
            return None
        name = self._player_name(self._target_id)
        members = len(self._group_member_ids())
        return f"{name} +{members}" if members else name

    def _group_member_ids(self) -> list[str]:
        """Available group members of the target, excluding the target."""
        target = self.target or {}
        members = []
        for pid in target.get("group_childs") or []:
            if pid == self._target_id:
                continue
            if (self._players.get(pid) or {}).get("available"):
                members.append(pid)
        return members

    # ── Extra routes (speakers view: grouping + target selection) ──

    def add_routes(self, app):
        app.router.add_get("/player/network", self._handle_network)
        app.router.add_post("/player/join", self._handle_join)
        app.router.add_post("/player/unjoin", self._handle_unjoin)
        app.router.add_post("/player/select_target", self._handle_select_target)
        app.router.add_get("/player/resync", self._handle_resync)
        app.router.add_post("/player/volume", self._handle_volume)
        app.router.add_post("/player/member_volume", self._handle_member_volume)

    async def _handle_network(self, request) -> web.Response:
        """GET /player/network — available MA players for the speakers view."""
        members = set(self._group_member_ids())
        result = []
        for p in self._available_players():
            pid = p["player_id"]
            cm = p.get("current_media") or {}
            result.append({
                "id": pid,
                "name": self._player_name(pid),
                "state": "playing" if p.get("playback_state") == "playing"
                         else "stopped",
                "is_target": pid == self._target_id,
                "in_group": pid in members,
                "volume": (int(p["volume_level"])
                           if p.get("volume_level") is not None else None),
                "can_join": (self._target_id or "") in (p.get("can_group_with") or []),
                "title": cm.get("title", ""),
                "artist": cm.get("artist", ""),
                "album": cm.get("album", ""),
                "artwork_url": self._rewrite_art_url(cm.get("image_url") or ""),
                "group": [self._player_name(c)
                          for c in p.get("group_childs") or [] if c != pid],
            })
        # Target first, then alphabetical — stable order for the arc list
        result.sort(key=lambda d: (not d["is_target"], d["name"].lower()))
        return web.json_response(result, headers=self._cors_headers())

    async def _handle_join(self, request) -> web.Response:
        """POST /player/join {"id": ...} — add a player to the target's group."""
        self._stamp_command()
        data = await self._json_body(request)
        pid = data.get("id") or data.get("player_id")
        if not pid and data.get("name"):
            # BLUE→JOIN sends {"name": join.default_player}
            pid = next((p["player_id"] for p in self._players.values()
                        if self._player_name(p["player_id"]) == data["name"]),
                       None)
        if not self._target_id:
            return web.json_response({"error": "no target player"}, status=409,
                                     headers=self._cors_headers())
        if not pid or pid not in self._players:
            return web.json_response({"error": f"unknown player: {pid}"},
                                     status=404, headers=self._cors_headers())
        try:
            await self._client.call("players/cmd/group", player_id=pid,
                                    target_player=self._target_id)
            logger.info("Grouped %s with %s", self._player_name(pid),
                        self._player_name(self._target_id))
            # Optimistic cache update: MA's player_updated event with the new
            # group_childs arrives asynchronously, so an immediate
            # /player/network re-fetch (speaker overlay after GO) would still
            # show the old membership and snap the UI back.
            target = self._players.get(self._target_id)
            if target is not None:
                childs = target.setdefault("group_childs", [])
                if pid not in childs:
                    childs.append(pid)
            # Bring the joined speaker up to the group's level so it is
            # audible and the wheel's *proportional* group_volume moves it
            # too — a member left at 0 would otherwise stay silent no matter
            # how far the wheel turns.
            target_vol = (self._players.get(self._target_id) or {}).get("volume_level")
            if target_vol is not None:
                try:
                    await self._client.call("players/cmd/volume_set",
                                            player_id=pid,
                                            volume_level=int(target_vol))
                    joined = self._players.get(pid)
                    if joined is not None:
                        joined["volume_level"] = int(target_vol)
                except MAClientError as e:
                    logger.warning("Could not match joined player volume: %s", e)
            return web.json_response(
                {"status": "ok", "joined": self._player_name(pid)},
                headers=self._cors_headers())
        except MAClientError as e:
            logger.error("Join failed: %s", e)
            return web.json_response({"error": str(e)}, status=500,
                                     headers=self._cors_headers())

    async def _handle_unjoin(self, request) -> web.Response:
        """POST /player/unjoin {"id": ...} — remove a player from the group.

        Without an id the whole target group is dissolved (all members
        ungrouped) — that is what the join view's UNJOIN entry does.
        """
        self._stamp_command()
        data = await self._json_body(request)
        pid = data.get("id") or data.get("player_id")
        was_playing = self._current_playback_state == "playing"
        try:
            target = self._players.get(self._target_id) if self._target_id else None
            if pid:
                await self._client.call("players/cmd/ungroup", player_id=pid)
                logger.info("Ungrouped %s", self._player_name(pid))
                # Optimistic cache update (see _handle_join): immediate
                # re-fetches must see the member gone.
                if target is not None and pid in (target.get("group_childs") or []):
                    target["group_childs"].remove(pid)
                if was_playing:
                    # The B&O (NetworkLink) provider can collapse the whole
                    # stream seconds after a member leaves the group — arm a
                    # one-shot resume so the target picks the queue back up.
                    self._last_ungroup_ts = time.time()
                    self._ungroup_resume_pending = True
            else:
                members = self._group_member_ids()
                if members:
                    await self._client.call("players/cmd/ungroup_many",
                                            player_ids=members)
                    if target is not None:
                        target["group_childs"] = [
                            c for c in (target.get("group_childs") or [])
                            if c not in members]
                logger.info("Dissolved group (%d members)", len(members))
            return web.json_response({"status": "ok"},
                                     headers=self._cors_headers())
        except MAClientError as e:
            logger.error("Unjoin failed: %s", e)
            return web.json_response({"error": str(e)}, status=500,
                                     headers=self._cors_headers())

    async def _handle_select_target(self, request) -> web.Response:
        """POST /player/select_target {"id": ...} — switch the playback target.

        Transfers the active queue when something is playing so the music
        follows the selection.
        """
        self._stamp_command()
        data = await self._json_body(request)
        pid = data.get("id") or data.get("player_id")
        player = self._players.get(pid)
        if not player or not player.get("available"):
            return web.json_response({"error": f"player not available: {pid}"},
                                     status=404, headers=self._cors_headers())
        old_target = self._target_id
        if pid == old_target:
            return web.json_response({"status": "ok", "target": pid},
                                     headers=self._cors_headers())
        try:
            if old_target and self._current_playback_state == "playing":
                await self._client.call("player_queues/transfer",
                                        source_queue_id=old_target,
                                        target_queue_id=pid, auto_play=True)
                logger.info("Transferred queue %s → %s",
                            self._player_name(old_target), self._player_name(pid))
        except MAClientError as e:
            logger.warning("Queue transfer failed (continuing): %s", e)
        self._set_target(pid)
        self._current_track_id = None  # force a fresh media broadcast
        await self._refresh_target_state()
        return web.json_response(
            {"status": "ok", "target": pid, "name": self._player_name(pid)},
            headers=self._cors_headers())

    async def _handle_volume(self, request) -> web.Response:
        """POST /player/volume {"volume": 0-100} — master volume from the
        wheel (via the music_assistant volume adapter). Group-aware, see
        _set_master_volume()."""
        self._stamp_command()
        data = await self._json_body(request)
        vol = data.get("volume")
        if vol is None or not isinstance(vol, (int, float)):
            return web.json_response({"error": "missing or invalid 'volume'"},
                                     status=400, headers=self._cors_headers())
        if not self._target_id:
            return web.json_response({"error": "no target player"}, status=409,
                                     headers=self._cors_headers())
        try:
            await self._set_master_volume(float(vol))
            return web.json_response({"status": "ok", "volume": int(vol)},
                                     headers=self._cors_headers())
        except MAClientError as e:
            logger.error("Volume set failed: %s", e)
            return web.json_response({"error": str(e)}, status=500,
                                     headers=self._cors_headers())

    async def _handle_member_volume(self, request) -> web.Response:
        """POST /player/member_volume {"id": ..., "volume": 0-100} — trim a
        single speaker (JOIN view wheel on a highlighted row). Plain
        volume_set on that player; MA preserves the trim when the group
        volume changes later."""
        self._stamp_command()
        data = await self._json_body(request)
        pid = data.get("id") or data.get("player_id")
        vol = data.get("volume")
        if vol is None or not isinstance(vol, (int, float)):
            return web.json_response({"error": "missing or invalid 'volume'"},
                                     status=400, headers=self._cors_headers())
        player = self._players.get(pid)
        if not player or not player.get("available"):
            return web.json_response({"error": f"player not available: {pid}"},
                                     status=404, headers=self._cors_headers())
        level = int(max(0, min(vol, 100)))
        try:
            await self._client.call("players/cmd/volume_set", player_id=pid,
                                    volume_level=level)
            # Optimistic local update so /player/network reflects the trim
            # before MA's player_updated event lands.
            player["volume_level"] = level
            return web.json_response(
                {"status": "ok", "id": pid, "volume": level},
                headers=self._cors_headers())
        except MAClientError as e:
            logger.error("Member volume set failed: %s", e)
            return web.json_response({"error": str(e)}, status=500,
                                     headers=self._cors_headers())

    async def _handle_resync(self, request) -> web.Response:
        """GET /player/resync — refresh the MA player list."""
        try:
            await self._sync_players()
            return web.json_response({"resynced": True},
                                     headers=self._cors_headers())
        except Exception as e:
            return web.json_response({"resynced": False, "error": str(e)},
                                     headers=self._cors_headers())

    async def _json_body(self, request) -> dict:
        try:
            return await request.json()
        except Exception:
            return {}

    # ── PlayerBase hooks ──

    async def on_start(self):
        self._client = MAClient(on_event=self._on_ma_event,
                                on_connect=self._on_ma_connect)
        logger.info("Starting Music Assistant player for %s", self._client.ws_url)
        await self._client.start()

    async def on_stop(self):
        if self._client:
            await self._client.close()

    # ── MA connection callbacks ──

    async def _on_ma_connect(self):
        await self._sync_players()
        self._restore_target()
        if self._target_id:
            await self._refresh_target_state()
        # NOTE: the JOIN menu source is deliberately NOT registered for MA —
        # speaker grouping/target selection lives in the double-GO speaker
        # overlay (web/js/speaker-overlay.js) instead of a menu entry.

    async def _sync_players(self):
        players = await self._client.call("players/all")
        self._players = {p["player_id"]: p for p in players or []}
        logger.info("MA players: %s",
                    ", ".join(self._player_name(pid) + ("*" if avail else "")
                              for pid, avail in
                              ((p["player_id"], p.get("available"))
                               for p in self._players.values())) or "none")

    async def _refresh_target_state(self):
        try:
            player = await self._client.call("players/get",
                                             player_id=self._target_id)
        except MAClientError as e:
            logger.warning("players/get failed: %s", e)
            return
        if player:
            self._players[self._target_id] = player
            await self._process_target_state(player)

    # ── Event processing ──

    async def _on_ma_event(self, event: dict):
        etype = event.get("event")
        oid = event.get("object_id")
        data = event.get("data")
        if etype in ("player_added", "player_updated"):
            if isinstance(data, dict) and data.get("player_id"):
                self._players[data["player_id"]] = data
            if oid == self._target_id and isinstance(data, dict):
                await self._process_target_state(data)
        elif etype == "player_removed":
            self._players.pop(oid, None)
        elif etype == "queue_updated" and oid == self._target_id:
            if isinstance(data, dict):
                self._apply_queue_position(data)
        elif etype == "queue_time_updated" and oid == self._target_id:
            if self._cached_media_data and isinstance(data, (int, float)):
                self._cached_media_data["position"] = _fmt_time(data)

    def _apply_queue_position(self, queue: dict):
        if not self._cached_media_data:
            return
        self._cached_media_data["position"] = _fmt_time(queue.get("elapsed_time"))
        item = queue.get("current_item") or {}
        if item.get("duration"):
            self._cached_media_data["duration"] = _fmt_time(item["duration"])

    async def _process_target_state(self, player: dict):
        """Map a PlayerState of the target to UI/router updates."""
        try:
            raw = player.get("playback_state")
            if raw == "playing":
                state = "playing"
            elif raw == "paused":
                state = "paused"
            else:
                state = "stopped"

            if state == "playing" and self._current_playback_state in (
                "paused", "stopped", None
            ):
                logger.info("Playback started (was: %s), triggering wake",
                            self._current_playback_state)
                self._spawn(self.trigger_wake(), name="trigger_wake")
                if self.seconds_since_command() > USER_ACTION_HORIZON:
                    logger.info("External playback detected, clearing active source")
                    self._spawn(self.notify_router_playback_override(force=True),
                                name="playback_override")
            elif state == "stopped" and self._current_playback_state == "playing":
                # One-shot recovery: the B&O provider sometimes collapses the
                # stream a few seconds after we ungroup a member. If the stop
                # lands within the window of our own ungroup, resume the
                # queue instead of treating it as an external stop.
                if (self._ungroup_resume_pending
                        and time.time() - self._last_ungroup_ts
                        <= UNGROUP_RESUME_WINDOW_S):
                    self._ungroup_resume_pending = False
                    logger.info("Stop within %.0fs of ungroup — auto-resuming",
                                UNGROUP_RESUME_WINDOW_S)
                    self._stamp_command()
                    self._spawn(self.resume(), name="ungroup_resume")
                elif self.seconds_since_command() > USER_ACTION_HORIZON:
                    logger.info("External stop detected")
                    self._spawn(self.notify_router_playback_override(force=True),
                                name="playback_override")

            self._current_playback_state = state

            if self._report_ma_volume:
                master = self._master_volume()
                if master is not None:
                    self._spawn(self.report_volume_to_router(
                                    master, self._output_label()),
                                name="report_volume")

            cm = player.get("current_media") or {}
            title = cm.get("title") or ""
            artist = cm.get("artist") or ""
            if not (title or artist):
                if self._cached_media_data:
                    self._cached_media_data["state"] = state
                return

            track_id = f"{cm.get('uri')}|{title}|{artist}"
            if track_id != self._current_track_id:
                self._current_track_id = track_id

                artwork_base64 = None
                artwork_size = None
                art_url = self._rewrite_art_url(cm.get("image_url") or "")
                if art_url:
                    result = await self.fetch_artwork(art_url,
                                                      session=self._http_session)
                    if result:
                        artwork_base64 = result["base64"]
                        artwork_size = result["size"]

                media_data = {
                    "title": title or "—",
                    "artist": artist or "—",
                    "album": cm.get("album") or "—",
                    "artwork": (f"data:image/jpeg;base64,{artwork_base64}"
                                if artwork_base64 else None),
                    "artwork_size": artwork_size,
                    "position": _fmt_time(cm.get("elapsed_time")),
                    "duration": _fmt_time(cm.get("duration")),
                    "state": state,
                    "volume": player.get("volume_level") or 0,
                    "speaker_ip": self._player_name(player.get("player_id", "")),
                    "service": "Music Assistant",
                    "uri": cm.get("uri") or "",
                    "timestamp": int(time.time()),
                }
                await self.broadcast_media_update(media_data, "track_change")
                logger.info("Track changed: %s — %s", artist, title)

                if self.seconds_since_command() > USER_ACTION_HORIZON:
                    self._spawn(self.notify_router_playback_override(force=True),
                                name="playback_override")
            elif self._cached_media_data:
                if self._cached_media_data.get("state") != state:
                    self._cached_media_data["state"] = state
                    await self.broadcast_media_update(self._cached_media_data,
                                                      "state_change")
                if player.get("volume_level") is not None:
                    self._cached_media_data["volume"] = int(player["volume_level"])
        except Exception as e:
            logger.error("Error processing MA player state: %s", e)

    # ── Helpers ──

    def _rewrite_art_url(self, url: str) -> str:
        """Point artwork URLs at the host we reach MA on.

        MA builds image_url from its configured base_url, which on this
        install is a Tailscale address the BS5c can't necessarily reach —
        swap in the host from our own ws URL.
        """
        if not url or not self._client:
            return url
        from urllib.parse import urlsplit, urlunsplit
        try:
            art = urlsplit(url)
            ws = urlsplit(self._client.ws_url)
            if not art.netloc or not ws.netloc:
                return url
            scheme = "https" if ws.scheme == "wss" else "http"
            return urlunsplit((scheme, ws.netloc, art.path,
                               art.query, art.fragment))
        except ValueError:
            return url

    def _cache_media_from_meta(self, meta: dict):
        """Seed cached media from the source's meta until MA events refine it."""
        self._cached_media_data = {
            "title": meta.get("title", "—"),
            "artist": meta.get("artist", "—"),
            "album": meta.get("album", "—"),
            "artwork": None,
            "artwork_size": None,
            "position": "0:00",
            "duration": "0:00",
            "state": "playing",
            "volume": (self.target or {}).get("volume_level") or 0,
            "speaker_ip": (self._player_name(self._target_id)
                           if self._target_id else ""),
            "service": "Music Assistant",
            "uri": self._current_track_uri or "",
            "timestamp": int(time.time()),
        }


async def main():
    player = MusicAssistantPlayer()
    await player.run()


if __name__ == "__main__":
    asyncio.run(main())
