"""Google Cast side channel for Loewe klang speakers.

The speaker is Cast-certified. When something is cast to it (Spotify,
YouTube Music, a phone app ...) the UPnP renderer reports nothing, but the
Cast receiver exposes full metadata and transport controls.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pychromecast
from homeassistant.components import zeroconf
from homeassistant.core import HomeAssistant
from pychromecast.controllers.media import MediaStatus
from pychromecast.models import CastInfo, HostServiceInfo
from pychromecast.socket_client import CONNECTION_STATUS_CONNECTED

_LOGGER = logging.getLogger(__name__)

# Backdrop / idle screen apps are not real playback sessions.
IDLE_APP_IDS = {None, "", "E8C28D3C"}


@dataclass
class CastSnapshot:
    """What the Cast receiver is doing right now."""

    connected: bool = False
    volume: float | None = None
    muted: bool | None = None
    app_id: str | None = None
    app_name: str | None = None
    player_state: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    image_url: str | None = None
    duration: float | None = None
    position: float | None = None
    position_updated: float | None = None
    supports_pause: bool = False
    supports_seek: bool = False
    supports_next: bool = False
    supports_previous: bool = False

    @property
    def active(self) -> bool:
        """True when a real Cast app owns the speaker."""
        return (
            self.connected
            and self.app_id not in IDLE_APP_IDS
            and self.player_state in ("PLAYING", "PAUSED", "BUFFERING")
        )


class CastBridge:
    """Owns a pychromecast connection and forwards updates to the event loop."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        uuid: str,
        on_update: Callable[[CastSnapshot], None],
    ) -> None:
        self._hass = hass
        self._host = host
        self._uuid = uuid
        self._on_update = on_update
        self._cast: pychromecast.Chromecast | None = None
        self.snapshot = CastSnapshot()

    async def async_start(self) -> None:
        info = CastInfo(
            services={HostServiceInfo(self._host, 8009)},
            uuid=UUID(self._uuid),
            model_name=None,
            friendly_name=None,
            host=self._host,
            port=8009,
            cast_type=None,
            manufacturer=None,
        )
        zc = await zeroconf.async_get_instance(self._hass)
        cast = await self._hass.async_add_executor_job(
            pychromecast.get_chromecast_from_cast_info, info, zc
        )
        cast.register_status_listener(self)
        cast.media_controller.register_status_listener(self)
        cast.register_connection_listener(self)
        self._cast = cast
        cast.start()

    async def async_stop(self) -> None:
        if self._cast is not None:
            cast, self._cast = self._cast, None
            await self._hass.async_add_executor_job(cast.disconnect, 5)

    # ---- pychromecast listener callbacks (run in the socket thread) --------

    def new_cast_status(self, status: Any) -> None:
        snap = self.snapshot
        snap.app_id = status.app_id
        snap.app_name = status.display_name
        snap.volume = status.volume_level
        snap.muted = status.volume_muted
        if status.app_id in IDLE_APP_IDS:
            snap.player_state = None
        self._push()

    def new_media_status(self, status: MediaStatus) -> None:
        snap = self.snapshot
        snap.player_state = status.player_state
        snap.title = status.title
        snap.artist = status.artist
        snap.album = status.album_name
        snap.album_artist = status.album_artist
        snap.image_url = status.images[0].url if status.images else None
        snap.duration = status.duration
        snap.position = status.adjusted_current_time
        snap.position_updated = (
            status.last_updated.timestamp() if status.last_updated else None
        )
        snap.supports_pause = status.supports_pause
        snap.supports_seek = status.supports_seek
        snap.supports_next = status.supports_queue_next
        snap.supports_previous = status.supports_queue_prev
        self._push()

    def load_media_failed(self, queue_item_id: int, error_code: int) -> None:
        _LOGGER.warning("Cast failed to load media (error %s)", error_code)

    def new_connection_status(self, status: Any) -> None:
        connected = status.status == CONNECTION_STATUS_CONNECTED
        if connected != self.snapshot.connected:
            _LOGGER.debug("Cast connection to %s: %s", self._host, status.status)
        self.snapshot.connected = connected
        if not connected:
            self.snapshot.player_state = None
        self._push()

    def _push(self) -> None:
        snap = CastSnapshot(**self.snapshot.__dict__)
        self._hass.loop.call_soon_threadsafe(self._on_update, snap)

    # ---- control (blocking pychromecast calls, run in executor) -----------

    async def _run(self, func: Callable[..., Any], *args: Any) -> None:
        if self._cast is None:
            return
        await self._hass.async_add_executor_job(func, *args)

    async def async_play(self) -> None:
        if self._cast:
            await self._run(self._cast.media_controller.play)

    async def async_pause(self) -> None:
        if self._cast:
            await self._run(self._cast.media_controller.pause)

    async def async_stop_media(self) -> None:
        if self._cast:
            await self._run(self._cast.quit_app)

    async def async_next(self) -> None:
        if self._cast:
            await self._run(self._cast.media_controller.queue_next)

    async def async_previous(self) -> None:
        if self._cast:
            await self._run(self._cast.media_controller.queue_prev)

    async def async_seek(self, position: float) -> None:
        if self._cast:
            await self._run(self._cast.media_controller.seek, position)

    async def async_set_volume(self, volume: float) -> bool:
        """Set master volume (0..1). False if Cast is not connected."""
        if self._cast is None or not self.snapshot.connected:
            return False
        await self._run(self._cast.set_volume, volume)
        return True

    async def async_set_mute(self, mute: bool) -> bool:
        if self._cast is None or not self.snapshot.connected:
            return False
        await self._run(self._cast.set_volume_muted, mute)
        return True

    async def async_play_url(self, url: str, mime: str, title: str | None) -> bool:
        """Play a URL through the default media receiver. False if not connected."""
        if self._cast is None or not self.snapshot.connected:
            return False

        def _play() -> None:
            assert self._cast is not None
            self._cast.media_controller.play_media(url, mime, title=title)

        await self._hass.async_add_executor_job(_play)
        return True
