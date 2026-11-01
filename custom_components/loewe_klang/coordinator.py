"""Data coordinator combining UPnP polling with pushed Cast updates."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .cast import CastBridge, CastSnapshot
from .const import DOMAIN, SCAN_INTERVAL
from .playfi import PlayFiState
from .upnp import DeviceInfo, FirmwareInfo, LoeweError, LoeweSpeaker, UpnpStatus

_LOGGER = logging.getLogger(__name__)

# Firmware rarely changes; don't hammer the Cast/AirPlay endpoints every poll.
FIRMWARE_INTERVAL = 900.0
# The speaker now and then broadcasts a status frame without its <metadata>
# block, mostly around source changes. Keep the last full state through such a
# frame unless they persist this long (it broadcasts every ~2.5 s).
PLAYFI_METADATA_GRACE = 8.0


@dataclass
class LoeweState:
    """Merged view of the speaker."""

    upnp: UpnpStatus
    upnp_updated: float
    cast: CastSnapshot = field(default_factory=CastSnapshot)
    firmware: FirmwareInfo = field(default_factory=FirmwareInfo)
    airplay_active: bool = False
    playfi: PlayFiState | None = None


@dataclass
class LoeweRuntimeData:
    """Objects stored on the config entry."""

    speaker: LoeweSpeaker
    device: DeviceInfo
    coordinator: LoeweCoordinator
    cast: CastBridge | None
    playfi_listener: object | None = None


type LoeweConfigEntry = ConfigEntry[LoeweRuntimeData]


class LoeweCoordinator(DataUpdateCoordinator[LoeweState]):
    """Polls the UPnP renderer and folds in Cast pushes."""

    config_entry: LoeweConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: LoeweConfigEntry, speaker: LoeweSpeaker
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
            always_update=False,
        )
        self.speaker = speaker
        self._cast = CastSnapshot()
        self._firmware = FirmwareInfo()
        self._firmware_updated = 0.0
        self._playfi: PlayFiState | None = None
        self._playfi_metadata_seen = 0.0

    async def _async_update_data(self) -> LoeweState:
        try:
            status, airplay_active = await asyncio.gather(
                self.speaker.async_get_status(),
                self.speaker.async_get_airplay_active(),
            )
        except LoeweError as err:
            raise UpdateFailed(str(err)) from err
        now = time.time()
        if now - self._firmware_updated >= FIRMWARE_INTERVAL:
            try:
                self._firmware = await self.speaker.async_get_firmware()
            except LoeweError:  # firmware is optional, keep the last value
                _LOGGER.debug("Firmware refresh failed", exc_info=True)
            self._firmware_updated = now
        return LoeweState(
            upnp=status,
            upnp_updated=now,
            cast=self._cast,
            firmware=self._firmware,
            airplay_active=airplay_active,
            playfi=self._playfi,
        )

    @callback
    def async_cast_update(self, snapshot: CastSnapshot) -> None:
        """Handle a Cast status push from the pychromecast thread."""
        self._cast = snapshot
        if self.data is not None:
            self.async_set_updated_data(
                LoeweState(
                    upnp=self.data.upnp,
                    upnp_updated=self.data.upnp_updated,
                    cast=snapshot,
                    firmware=self.data.firmware,
                    airplay_active=self.data.airplay_active,
                    playfi=self.data.playfi,
                )
            )
        # Volume and transport may have changed as well; refresh soon.
        self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(), f"{DOMAIN}_cast_refresh"
        )

    @callback
    def async_playfi_update(self, state: PlayFiState) -> None:
        """Handle a decoded Play-Fi announce broadcast from the listener."""
        now = time.monotonic()
        if state.metadata_reported:
            self._playfi_metadata_seen = now
        elif (
            self._playfi is not None
            and self._playfi.metadata_reported
            and now - self._playfi_metadata_seen < PLAYFI_METADATA_GRACE
        ):
            return
        self._playfi = state
        # The speaker repeats itself every few seconds; only a change needs a
        # state write, so a flood of identical frames can't grow the recorder.
        if self.data is not None and state != self.data.playfi:
            # Not async_set_updated_data: it restarts the poll timer, so announces
            # arriving faster than SCAN_INTERVAL would stop polling altogether. It
            # would also mark the speaker reachable on a frame anyone can forge.
            self.data = LoeweState(
                upnp=self.data.upnp,
                upnp_updated=self.data.upnp_updated,
                cast=self.data.cast,
                firmware=self.data.firmware,
                airplay_active=self.data.airplay_active,
                playfi=state,
            )
            self.async_update_listeners()
