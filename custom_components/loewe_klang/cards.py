"""Serve the integration's Lovelace cards and load them in the frontend."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

CARDS_FILE = Path(__file__).parent / "frontend" / "loewe-klang-cards.js"
CARDS_URL = f"/{DOMAIN}/{CARDS_FILE.name}"


async def async_register_cards(hass: HomeAssistant) -> None:
    """Make the cards available on every dashboard, with no manual resource."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARDS_URL, str(CARDS_FILE), cache_headers=True)]
    )
    # The file is cached long-term, so the version in the URL refreshes it.
    integration = await async_get_integration(hass, DOMAIN)
    add_extra_js_url(hass, f"{CARDS_URL}?v={integration.version}")
