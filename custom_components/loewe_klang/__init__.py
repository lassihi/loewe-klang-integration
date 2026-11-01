"""Loewe klang (DTS Play-Fi) speakers."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_HOST, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .cast import CastBridge
from .const import CONF_CAST_UUID
from .coordinator import LoeweConfigEntry, LoeweCoordinator, LoeweRuntimeData
from .playfi_client import PlayFiListener, async_create_listener
from .upnp import LoeweError, LoeweSpeaker

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BINARY_SENSOR, Platform.MEDIA_PLAYER, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: LoeweConfigEntry) -> bool:
    """Set up a speaker from a config entry."""
    speaker = LoeweSpeaker(entry.data[CONF_HOST], async_get_clientsession(hass))
    try:
        device = await speaker.async_get_device_info()
    except LoeweError as err:
        raise ConfigEntryNotReady(str(err)) from err
    if entry.unique_id and device.udn != entry.unique_id:
        raise ConfigEntryNotReady(
            f"{speaker.host} answered as {device.udn}, expected {entry.unique_id}"
        )

    coordinator = LoeweCoordinator(hass, entry, speaker)
    await coordinator.async_config_entry_first_refresh()

    cast: CastBridge | None = None
    # The live value comes from an unauthenticated endpoint, so it must not
    # override what was stored when the speaker was set up.
    cast_uuid = entry.data.get(CONF_CAST_UUID) or device.cast_uuid
    if cast_uuid:
        cast = CastBridge(hass, speaker.host, cast_uuid, coordinator.async_cast_update)
        try:
            await cast.async_start()
        except Exception:
            _LOGGER.warning(
                "Could not connect to Cast on %s", speaker.host, exc_info=True
            )
            cast = None

    if cast is not None:
        bridge = cast

        async def _async_stop_cast(_event: Event) -> None:
            await bridge.async_stop()

        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop_cast)
        )

    # Passive Play-Fi now-playing: the speaker broadcasts its state on the LAN,
    # so this only listens and never interrupts playback. Optional.
    listener: PlayFiListener | None = None
    try:
        listener = await async_create_listener(
            speaker.host, coordinator.async_playfi_update
        )
    except Exception:
        _LOGGER.warning(
            "Could not start Play-Fi listener for %s", speaker.host, exc_info=True
        )
    if listener is not None:
        entry.async_on_unload(listener.close)

    entry.runtime_data = LoeweRuntimeData(
        speaker, device, coordinator, cast, listener
    )
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: LoeweConfigEntry) -> None:
    """Reload so option changes (such as the Spotify entity) take effect."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LoeweConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and entry.runtime_data.cast is not None:
        await entry.runtime_data.cast.async_stop()
    return unloaded
