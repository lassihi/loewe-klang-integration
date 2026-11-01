"""Diagnostics for Loewe klang speakers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_SPOTIFY_ENTITY
from .coordinator import LoeweConfigEntry

# Diagnostics get attached to public issues. Keep what helps map Play-Fi sources
# (source, state, firmware) and drop addresses, identifiers and listening history.
# Track URIs may also carry a signed Home Assistant URL.
TO_REDACT = {
    "host",
    "mac",
    "udn",
    "cast_uuid",
    "device_id",
    "ip_address",
    "name",
    "uri",
    "image_url",
    "title",
    "artist",
    "album",
    "album_artist",
    "track",
    "station",
    CONF_SPOTIFY_ENTITY,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LoeweConfigEntry
) -> dict[str, Any]:
    """Return the redacted device state; useful for mapping new Play-Fi sources."""
    rt = entry.runtime_data
    data = rt.coordinator.data
    return async_redact_data(
        {
            "device": asdict(rt.device),
            "upnp": asdict(data.upnp) if data else None,
            "cast": asdict(data.cast) if data else None,
            "firmware": asdict(data.firmware) if data else None,
            "airplay_active": data.airplay_active if data else None,
            "playfi": asdict(data.playfi) if data and data.playfi else None,
            "options": dict(entry.options),
        },
        TO_REDACT,
    )
