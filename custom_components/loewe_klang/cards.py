"""Serve the integration's Lovelace cards and load them in the frontend."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

FRONTEND_DIR = Path(__file__).parent / "frontend"
CARDS_FILE = FRONTEND_DIR / "loewe-klang-cards.js"
LOADER_FILE = FRONTEND_DIR / "loewe-klang-loader.js"
FRONTEND_URL = f"/{DOMAIN}"


async def async_register_cards(hass: HomeAssistant) -> None:
    """Make the cards available on every dashboard, with no manual resource."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL, str(FRONTEND_DIR), cache_headers=True)]
    )
    # The files are cached long-term, so the version in the URL refreshes them.
    # The loader passes it on to the cards.
    integration = await async_get_integration(hass, DOMAIN)
    add_extra_js_url(hass, f"{FRONTEND_URL}/{LOADER_FILE.name}?v={integration.version}")
