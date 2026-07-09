# BeoSound 5c
# Copyright (C) 2024-2026 Markus Kirsten
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Attribution required — see LICENSE, Section 7(b).

"""
Music Assistant volume adapter — forwards to the MA player service.

The player service (beo-player-music-assistant, port 8766) owns the MA
websocket, the target player and its group state, so this adapter stays
dumb: it POSTs the wheel volume to /player/volume and the player decides
whether that means MA's proportional group volume (grouped target,
individual trims survive) or a plain volume_set (solo target).
"""

import logging

import aiohttp

from .base import VolumeAdapter

logger = logging.getLogger("beo-router.volume.music_assistant")

# Matches lib.player_base port for the music_assistant player
# (see service-registry.sh: "Music Assistant Player (Port 8766)").
PLAYER_PORT = 8766


class MusicAssistantVolume(VolumeAdapter):
    """Master volume via the local Music Assistant player service."""

    def __init__(self, max_volume: int, session: aiohttp.ClientSession,
                 port: int = PLAYER_PORT):
        super().__init__(max_volume, debounce_ms=50)
        self._session = session
        self._base = f"http://127.0.0.1:{port}"

    async def _apply_volume(self, volume: float) -> None:
        try:
            async with self._session.post(
                f"{self._base}/player/volume",
                json={"volume": int(volume)},
                timeout=aiohttp.ClientTimeout(total=3.0),
            ) as resp:
                if resp.status == 200:
                    logger.info("-> MA volume: %.0f%%", volume)
                else:
                    body = await resp.text()
                    logger.warning("MA volume set failed: HTTP %d %s",
                                   resp.status, body[:120])
        except Exception as e:
            logger.warning("MA player service unreachable: %s", e)

    async def get_volume(self) -> float | None:
        try:
            async with self._session.get(
                f"{self._base}/player/status",
                timeout=aiohttp.ClientTimeout(total=3.0),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                vol = data.get("volume")
                if vol is None:
                    return None
                logger.info("MA volume read: %d%%", int(vol))
                return float(vol)
        except Exception as e:
            logger.warning("Could not read MA volume: %s", e)
            return None

    async def is_on(self) -> bool:
        return True  # MA players power-manage themselves
