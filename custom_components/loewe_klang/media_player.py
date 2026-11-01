"""Media player entity for Loewe klang speakers."""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Concatenate

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
    async_process_play_media_url,
)
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import playfi
from .const import CONF_SPOTIFY_ENTITY, DOMAIN, PLAYFI_IMAGE_PORT
from .coordinator import LoeweConfigEntry, LoeweCoordinator
from .playfi import PLAYFI_AIRPLAY, PLAYFI_SOURCE_NAMES, PLAYFI_SPOTIFY, PlayFiState
from .playfi_client import PlayFiError, async_send_transport
from .upnp import PLAY_MEDIUM_NAMES, LoeweError, TrackInfo

_LOGGER = logging.getLogger(__name__)

UPNP_STATES = {
    "PLAYING": MediaPlayerState.PLAYING,
    "PAUSED_PLAYBACK": MediaPlayerState.PAUSED,
    "PAUSED": MediaPlayerState.PAUSED,
    "TRANSITIONING": MediaPlayerState.BUFFERING,
}
# Spotify Connect plays on the speaker itself, so neither UPnP nor Cast sees it.
# The Home Assistant Spotify entity reports it with this speaker as `source`.
SPOTIFY_STATES = {
    "playing": MediaPlayerState.PLAYING,
    "paused": MediaPlayerState.PAUSED,
}
CAST_STATES = {
    "PLAYING": MediaPlayerState.PLAYING,
    "PAUSED": MediaPlayerState.PAUSED,
    "BUFFERING": MediaPlayerState.BUFFERING,
}
# UPnP PlayMode <-> (shuffle, repeat)
PLAY_MODES: dict[str, tuple[bool, RepeatMode]] = {
    "NORMAL": (False, RepeatMode.OFF),
    "REPEAT_ALL": (False, RepeatMode.ALL),
    "REPEAT_ONE": (False, RepeatMode.ONE),
    "SHUFFLE_NOREPEAT": (True, RepeatMode.OFF),
    "SHUFFLE": (True, RepeatMode.ALL),
}
# One level of the speaker's 30-level Cast scale. A smaller step can land halfway
# between levels and snap back: 0.17 - 0.02 = 0.15 returns to 0.17.
VOLUME_STEP = 1 / 30
# Seconds the speaker may take to swap its AirPlay picture after a new track.
AIRPLAY_ART_SETTLE = 2.0
# Sessions the speaker runs itself (AirPlay, Spotify Connect) take no UPnP
# transport commands; volume always works, and play/pause/skip are added when
# the speaker's Play-Fi broadcast advertises them.
SESSION_FEATURES = (
    MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.BROWSE_MEDIA
)

BASE_FEATURES = (
    MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
)


def _command[**P](
    func: Callable[Concatenate[LoeweMediaPlayer, P], Awaitable[None]],
) -> Callable[Concatenate[LoeweMediaPlayer, P], Awaitable[None]]:
    """Turn speaker errors into HomeAssistantError and refresh afterwards."""

    @functools.wraps(func)
    async def wrapper(
        self: LoeweMediaPlayer, *args: P.args, **kwargs: P.kwargs
    ) -> None:
        try:
            await func(self, *args, **kwargs)
        except (LoeweError, PlayFiError) as err:
            raise HomeAssistantError(f"{self.name}: {err}") from err
        await self.coordinator.async_request_refresh()

    return wrapper


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LoeweConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the media player."""
    async_add_entities([LoeweMediaPlayer(entry)])


class LoeweMediaPlayer(CoordinatorEntity[LoeweCoordinator], MediaPlayerEntity):
    """A Loewe klang speaker."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_volume_step = VOLUME_STEP

    def __init__(self, entry: LoeweConfigEntry) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self._entry = entry
        self._rt = entry.runtime_data
        dev = self._rt.device
        self._attr_unique_id = dev.udn
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dev.udn)},
            connections={(CONNECTION_NETWORK_MAC, dev.mac)} if dev.mac else set(),
            manufacturer=dev.manufacturer,
            model=dev.model or "klang",
            name=dev.name,
            sw_version=dev.firmware,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _is_spotify_event(event_data: EventStateChangedData) -> bool:
            return self._is_spotify_entity(event_data["entity_id"])

        @callback
        def _spotify_changed(_event: Event[EventStateChangedData]) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, _spotify_changed, event_filter=_is_spotify_event
            )
        )

    # ---- helpers -------------------------------------------------------------

    @callback
    def _is_spotify_entity(self, entity_id: str) -> bool:
        """Whether an entity may report Spotify Connect playback on this speaker.

        Uses the configured entity if one is set, otherwise every media player
        from Home Assistant's Spotify integration (one per Spotify account).
        """
        if configured := self._entry.options.get(CONF_SPOTIFY_ENTITY):
            return entity_id == configured
        if not entity_id.startswith("media_player."):
            return False
        entry = er.async_get(self.hass).async_get(entity_id)
        return entry is not None and entry.platform == "spotify"

    @property
    def _spotify(self) -> State | None:
        """Spotify state while this speaker is the active Spotify Connect device.

        Cast and UPnP sessions take priority; Spotify Connect leaves both idle.
        """
        if (
            self._cast_active
            or self.coordinator.data.upnp.transport_state in UPNP_STATES
        ):
            return None
        name = self._rt.device.name.casefold()
        for state in self.hass.states.async_all("media_player"):
            if (
                state.state in SPOTIFY_STATES
                and (state.attributes.get("source") or "").casefold() == name
                and self._is_spotify_entity(state.entity_id)
            ):
                return state
        return None

    async def _spotify_call(self, service: str, **data: Any) -> None:
        if (spotify := self._spotify) is None:
            return
        await self.hass.services.async_call(
            "media_player",
            service,
            {"entity_id": spotify.entity_id, **data},
            blocking=True,
        )

    @property
    def _airplay_active(self) -> bool:
        """An AirPlay sender holds a session and nothing else is playing.

        The speaker only reports that a session exists, not whether it is
        paused, and exposes no track data.
        """
        data = self.coordinator.data
        return (
            data.airplay_active
            and not self._cast_active
            and data.upnp.transport_state not in UPNP_STATES
            and self._spotify is None
        )

    @property
    def _playfi(self) -> PlayFiState | None:
        """Play-Fi now-playing for a session the speaker runs itself.

        The speaker broadcasts the metadata and transport state of AirPlay and
        Spotify Connect sessions, and of other services it streams, over
        Play-Fi. This returns that state while such a session is the active
        one: AirPlay while a sender holds a session, others when no Cast, UPnP,
        AirPlay or Home Assistant Spotify session is active. None otherwise.
        """
        state = self.coordinator.data.playfi
        if state is None:
            return None
        if state.source == PLAYFI_AIRPLAY:
            return state if self._airplay_active else None
        if (
            self._spotify is not None
            or self._airplay_active
            or self._cast_active
            or self.coordinator.data.upnp.transport_state in UPNP_STATES
        ):
            return None
        # Spotify Connect is a backup for when Home Assistant's Spotify
        # integration is not set up: that one also has progress and seek, so
        # it takes priority (checked above).
        if state.source == PLAYFI_SPOTIFY:
            return state
        # Any other service the speaker plays (Tidal, Qobuz, radio, ...). Its
        # name may linger after playback ends, so require it to report a track
        # or to be playing.
        if playfi.source_name(state.source) and (state.has_metadata or state.playing):
            return state
        return None

    @property
    def _cast_active(self) -> bool:
        return self.coordinator.data.cast.active and self._rt.cast is not None

    @property
    def _track(self) -> TrackInfo:
        """UPnP track metadata, blanked when the renderer is stopped."""
        upnp = self.coordinator.data.upnp
        if upnp.transport_state not in UPNP_STATES:
            return TrackInfo()
        return upnp.track

    # ---- state ---------------------------------------------------------------

    @property
    def state(self) -> MediaPlayerState:
        data = self.coordinator.data
        if (spotify := self._spotify) is not None:
            return SPOTIFY_STATES[spotify.state]
        if self._cast_active:
            return CAST_STATES.get(data.cast.player_state or "", MediaPlayerState.IDLE)
        if (pf := self._playfi) is not None:
            return MediaPlayerState.PLAYING if pf.playing else MediaPlayerState.PAUSED
        if self._airplay_active:
            return MediaPlayerState.ON
        return UPNP_STATES.get(data.upnp.transport_state, MediaPlayerState.IDLE)

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        if (pf := self._playfi) is not None:
            # The speaker accepts play/pause/skip for its own sessions over
            # Play-Fi; expose what its transport controls advertise.
            # Verified sources keep the controls unless the speaker disables
            # them; other services get only what the speaker marks enabled.
            verified = pf.source in PLAYFI_SOURCE_NAMES
            features = SESSION_FEATURES
            if pf.controls.get("PLAY_PAUSE", verified):
                features |= (
                    MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE
                )
            if pf.controls.get("NEXT", verified):
                features |= MediaPlayerEntityFeature.NEXT_TRACK
            if pf.controls.get("PREVIOUS", verified):
                features |= MediaPlayerEntityFeature.PREVIOUS_TRACK
            return features
        if self._airplay_active:
            return SESSION_FEATURES
        features = BASE_FEATURES
        data = self.coordinator.data
        if self._cast_active:
            cast = data.cast
            if cast.supports_seek:
                features |= MediaPlayerEntityFeature.SEEK
            if cast.supports_next:
                features |= MediaPlayerEntityFeature.NEXT_TRACK
            if cast.supports_previous:
                features |= MediaPlayerEntityFeature.PREVIOUS_TRACK
            return features
        if (spotify := self._spotify) is not None:
            features |= (
                MediaPlayerEntityFeature.NEXT_TRACK
                | MediaPlayerEntityFeature.PREVIOUS_TRACK
            )
            if spotify.attributes.get("media_duration"):
                features |= MediaPlayerEntityFeature.SEEK
            return features
        upnp = data.upnp
        features |= (
            MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
        )
        if "Seek" in upnp.actions and upnp.duration:
            features |= MediaPlayerEntityFeature.SEEK
        if upnp.play_mode in PLAY_MODES:
            features |= (
                MediaPlayerEntityFeature.SHUFFLE_SET
                | MediaPlayerEntityFeature.REPEAT_SET
            )
        return features

    # The speaker's master volume is the Cast receiver volume. The UPnP
    # RenderingControl volume is only a per-session DLNA value that the
    # firmware may ignore, so it is used only when Cast is unreachable.
    @property
    def volume_level(self) -> float | None:
        data = self.coordinator.data
        if data.cast.connected and data.cast.volume is not None:
            return round(data.cast.volume, 2)
        vol = data.upnp.volume
        return None if vol is None else vol / 100

    @property
    def is_volume_muted(self) -> bool | None:
        data = self.coordinator.data
        if data.cast.connected and data.cast.muted is not None:
            return data.cast.muted
        return data.upnp.muted

    @property
    def source(self) -> str | None:
        data = self.coordinator.data
        if self._cast_active:
            return (
                f"Chromecast: {data.cast.app_name}"
                if data.cast.app_name
                else "Chromecast"
            )
        if self._spotify is not None:
            return "Spotify Connect"
        if (pf := self._playfi) is not None:
            return playfi.source_name(pf.source)
        if self._airplay_active:
            return "AirPlay"
        if self.state == MediaPlayerState.IDLE:
            return None
        medium = data.upnp.play_medium
        return PLAY_MEDIUM_NAMES.get(medium or "", medium)

    @property
    def app_name(self) -> str | None:
        if (pf := self._playfi) is not None and pf.source != PLAYFI_AIRPLAY:
            return "Spotify" if pf.source == PLAYFI_SPOTIFY else self.source
        if self._airplay_active:
            return "AirPlay"
        return self.coordinator.data.cast.app_name if self._cast_active else None

    @property
    def media_content_type(self) -> MediaType | None:
        return MediaType.MUSIC if self.media_title else None

    @property
    def media_content_id(self) -> str | None:
        if self._cast_active or not (uri := self._track.uri):
            return None
        # Home Assistant media URLs carry a signature (authSig) in the query, and
        # entity state is visible to every user and kept in history.
        return re.split(r"[?#]", uri, maxsplit=1)[0]

    @property
    def media_title(self) -> str | None:
        data = self.coordinator.data
        if (spotify := self._spotify) is not None:
            return spotify.attributes.get("media_title")
        if self._cast_active:
            return data.cast.title
        if (pf := self._playfi) is not None:
            # Radio streams may report only the station.
            return pf.track or pf.station
        return self._track.title

    @property
    def media_artist(self) -> str | None:
        data = self.coordinator.data
        if (spotify := self._spotify) is not None:
            return spotify.attributes.get("media_artist")
        if self._cast_active:
            return data.cast.artist
        if (pf := self._playfi) is not None:
            return pf.artist
        return self._track.artist

    @property
    def media_album_name(self) -> str | None:
        data = self.coordinator.data
        if (spotify := self._spotify) is not None:
            return spotify.attributes.get("media_album_name")
        if self._cast_active:
            return data.cast.album
        if (pf := self._playfi) is not None:
            return pf.album
        return self._track.album

    @property
    def media_album_artist(self) -> str | None:
        data = self.coordinator.data
        return data.cast.album_artist if self._cast_active else self._track.album_artist

    @property
    def media_image_url(self) -> str | None:
        data = self.coordinator.data
        if (spotify := self._spotify) is not None:
            # The Spotify entity only exposes a proxied picture path in its
            # state, so read the real cover URL from the entity itself.
            component = self.hass.data.get("media_player")
            entity = component.get_entity(spotify.entity_id) if component else None
            return getattr(entity, "media_image_url", None)
        if self._cast_active:
            return data.cast.image_url
        if (pf := self._playfi) is not None:
            host = self._rt.speaker.host
            if pf.source == PLAYFI_AIRPLAY and not pf.image_url:
                return playfi.airplay_image_url(pf, host, PLAYFI_IMAGE_PORT)
            return playfi.resolve_image_url(pf.image_url, host, PLAYFI_IMAGE_PORT)
        return self._track.image_url

    @property
    def media_image_remotely_accessible(self) -> bool:
        # Cover art often lives on the speaker's LAN address; let HA proxy it.
        return False

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        # Only the Spotify entity's URL is trusted; every other source's URL
        # can be set by anything on the LAN, so it goes through the guarded fetch.
        if self._spotify is not None:
            return await super().async_get_media_image()
        if (url := self.media_image_url) is None:
            return None, None
        if (pf := self._playfi) is not None and pf.source == PLAYFI_AIRPLAY:
            # Home Assistant caches by URL, so fetching before the speaker has
            # swapped its picture would keep the last track's for this one.
            elapsed = time.monotonic() - self.coordinator.playfi_track_changed
            if elapsed < AIRPLAY_ART_SETTLE:
                await asyncio.sleep(AIRPLAY_ART_SETTLE - elapsed)
        return await self._rt.speaker.async_get_image(url) or (None, None)

    @property
    def media_duration(self) -> int | None:
        data = self.coordinator.data
        if (spotify := self._spotify) is not None:
            return spotify.attributes.get("media_duration")
        if self._cast_active:
            return int(data.cast.duration) if data.cast.duration else None
        if self._playfi is not None:
            return None
        return data.upnp.duration

    @property
    def media_position(self) -> int | None:
        data = self.coordinator.data
        if (spotify := self._spotify) is not None:
            return spotify.attributes.get("media_position")
        if self._cast_active:
            return int(data.cast.position) if data.cast.position is not None else None
        if self._playfi is not None:
            return None
        return data.upnp.position

    @property
    def media_position_updated_at(self) -> datetime | None:
        data = self.coordinator.data
        if (spotify := self._spotify) is not None:
            updated = spotify.attributes.get("media_position_updated_at")
            return (
                dt_util.parse_datetime(updated) if isinstance(updated, str) else updated
            )
        ts = data.cast.position_updated if self._cast_active else data.upnp_updated
        if ts is None or self.media_position is None:
            return None
        return dt_util.utc_from_timestamp(ts)

    @property
    def shuffle(self) -> bool | None:
        mode = PLAY_MODES.get(self.coordinator.data.upnp.play_mode or "")
        if mode is None or self._cast_active or self._spotify or self._playfi:
            return None
        return mode[0]

    @property
    def repeat(self) -> RepeatMode | None:
        mode = PLAY_MODES.get(self.coordinator.data.upnp.play_mode or "")
        if mode is None or self._cast_active or self._spotify or self._playfi:
            return None
        return mode[1]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {
            "play_fi_medium": data.upnp.play_medium,
            "upnp_transport_state": data.upnp.transport_state,
            "cast_app_id": data.cast.app_id,
            "cast_connected": data.cast.connected,
            "airplay_session": data.airplay_active,
            "spotify_entity": spotify.entity_id if (spotify := self._spotify) else None,
            "playfi_source": data.playfi.source if data.playfi else None,
        }

    # ---- commands ------------------------------------------------------------

    @_command
    async def async_media_play(self) -> None:
        if self._playfi is not None:
            await async_send_transport(self._rt.speaker.host, playfi.TRANSPORT_PLAY)
            return
        if self._spotify is not None:
            await self._spotify_call("media_play")
            return
        if self._cast_active:
            await self._rt.cast.async_play()  # type: ignore[union-attr]
        else:
            await self._rt.speaker.async_play()

    @_command
    async def async_media_pause(self) -> None:
        if self._playfi is not None:
            await async_send_transport(self._rt.speaker.host, playfi.TRANSPORT_PAUSE)
            return
        if self._spotify is not None:
            await self._spotify_call("media_pause")
            return
        if self._cast_active:
            await self._rt.cast.async_pause()  # type: ignore[union-attr]
        else:
            await self._rt.speaker.async_pause()

    @_command
    async def async_media_stop(self) -> None:
        if self._spotify is not None:
            await self._spotify_call("media_pause")
            return
        if self._cast_active:
            await self._rt.cast.async_stop_media()  # type: ignore[union-attr]
        else:
            await self._rt.speaker.async_stop()

    @_command
    async def async_media_next_track(self) -> None:
        if self._playfi is not None:
            await async_send_transport(self._rt.speaker.host, playfi.TRANSPORT_NEXT)
            return
        if self._spotify is not None:
            await self._spotify_call("media_next_track")
            return
        if self._cast_active:
            await self._rt.cast.async_next()  # type: ignore[union-attr]
        else:
            await self._rt.speaker.async_next()

    @_command
    async def async_media_previous_track(self) -> None:
        if self._playfi is not None:
            await async_send_transport(
                self._rt.speaker.host, playfi.TRANSPORT_PREVIOUS
            )
            return
        if self._spotify is not None:
            await self._spotify_call("media_previous_track")
            return
        if self._cast_active:
            await self._rt.cast.async_previous()  # type: ignore[union-attr]
        else:
            await self._rt.speaker.async_previous()

    @_command
    async def async_media_seek(self, position: float) -> None:
        if self._spotify is not None:
            await self._spotify_call("media_seek", seek_position=position)
            return
        if self._cast_active:
            await self._rt.cast.async_seek(position)  # type: ignore[union-attr]
        else:
            await self._rt.speaker.async_seek(position)

    @_command
    async def async_set_volume_level(self, volume: float) -> None:
        if self._rt.cast is not None and await self._rt.cast.async_set_volume(volume):
            return
        await self._rt.speaker.async_set_volume(round(volume * 100))

    @_command
    async def async_mute_volume(self, mute: bool) -> None:
        if self._rt.cast is not None and await self._rt.cast.async_set_mute(mute):
            return
        await self._rt.speaker.async_set_mute(mute)

    @_command
    async def async_set_shuffle(self, shuffle: bool) -> None:
        await self._set_play_mode(shuffle, self.repeat or RepeatMode.OFF)

    @_command
    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        await self._set_play_mode(bool(self.shuffle), repeat)

    async def _set_play_mode(self, shuffle: bool, repeat: RepeatMode) -> None:
        if shuffle:
            # UPnP has no "shuffle + repeat one"; fall back to shuffle + repeat all.
            mode = "SHUFFLE_NOREPEAT" if repeat == RepeatMode.OFF else "SHUFFLE"
        else:
            mode = {
                RepeatMode.OFF: "NORMAL",
                RepeatMode.ALL: "REPEAT_ALL",
                RepeatMode.ONE: "REPEAT_ONE",
            }[repeat]
        await self._rt.speaker.async_set_play_mode(mode)

    @_command
    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        title: str | None = (kwargs.get("extra") or {}).get("title")
        mime = "audio/mpeg"
        if media_source.is_media_source_id(media_id):
            item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id, mime = item.url, item.mime_type
        media_id = async_process_play_media_url(self.hass, media_id)
        if self._rt.cast is not None and await self._rt.cast.async_play_url(
            media_id, mime, title
        ):
            return
        await self._rt.speaker.async_play_url(media_id, title or "Home Assistant", mime)

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )
