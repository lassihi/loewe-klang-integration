"""Diagnostics for Loewe klang speakers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import LoeweConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LoeweConfigEntry
) -> dict[str, Any]:
    """Return the raw device state; useful for mapping new Play-Fi sources."""
    rt = entry.runtime_data
    data = rt.coordinator.data
    return {
        "device": asdict(rt.device),
        "upnp": asdict(data.upnp) if data else None,
        "cast": asdict(data.cast) if data else None,
        "firmware": asdict(data.firmware) if data else None,
        "airplay_active": data.airplay_active if data else None,
        "playfi": asdict(data.playfi) if data and data.playfi else None,
        "options": dict(entry.options),
    }
