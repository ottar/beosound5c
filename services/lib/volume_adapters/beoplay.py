"""
BeoPlay volume adapter — controls volume via the B&O NetworkLink HTTP API
(pybeoplay, vendored git submodule at external/pybeoplay).
"""

import logging

import aiohttp

from .base import VolumeAdapter
from ..vendor import add_vendor_path

add_vendor_path("pybeoplay")
try:
    from pybeoplay import BeoPlay
except ImportError:
    BeoPlay = None

logger = logging.getLogger("beo-router.volume.beoplay")


class BeoplayVolume(VolumeAdapter):
    """Volume control via B&O BeoPlay/NetworkLink API (port 8080)."""

    def __init__(self, ip: str, max_volume: int, session: aiohttp.ClientSession):
        super().__init__(max_volume, debounce_ms=50)
        self._ip = ip
        if BeoPlay is None:
            logger.error("pybeoplay not available — run: git submodule update --init")
            self._device = None
        else:
            self._device = BeoPlay(ip, session)

    async def _apply_volume(self, volume: float) -> None:
        if self._device is None:
            return
        try:
            # ``volume`` is already in the device's hardware scale (the router
            # maps UI 0-100 → 0-volume.max before calling us). pybeoplay takes
            # a 0-1 value and sends int(value*100) as the device level, so a
            # hardware value of e.g. 35 (of max 50) → level 35.
            await self._device.async_set_volume(volume / 100.0)
            logger.info("-> BeoPlay volume: hw level %.0f", volume)
        except Exception as e:
            logger.warning("BeoPlay unreachable: %s", e)

    async def get_volume(self) -> float | None:
        if self._device is None:
            return None
        try:
            await self._device.async_get_volume()
            if self._device.volume is None:
                return None
            # Return the raw device level (hardware scale); the router applies
            # _hw_to_ui with volume.max to get the 0-100 UI value.
            vol = round(self._device.volume * 100)
            logger.info("BeoPlay volume read: hw level %d", vol)
            return float(vol)
        except Exception as e:
            logger.warning("Could not read BeoPlay volume: %s", e)
            return None

    async def is_on(self) -> bool:
        if self._device is None:
            return False
        try:
            await self._device.async_get_standby()
            return bool(self._device.on)
        except Exception as e:
            logger.warning("Could not read BeoPlay standby state: %s", e)
            return False

    async def power_on(self) -> None:
        if self._device is None:
            return
        try:
            await self._device.async_turn_on()
        except Exception as e:
            logger.warning("BeoPlay power on failed: %s", e)

    async def power_off(self) -> None:
        if self._device is None:
            return
        try:
            await self._device.async_standby()
        except Exception as e:
            logger.warning("BeoPlay standby failed: %s", e)
