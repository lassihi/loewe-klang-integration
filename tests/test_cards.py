"""Tests for the bundled Lovelace cards."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.loewe_klang import cards
from custom_components.loewe_klang.cards import (
    CARDS_FILE,
    FRONTEND_DIR,
    FRONTEND_URL,
    LOADER_FILE,
)


async def test_cards_are_served_and_loaded(hass: HomeAssistant) -> None:
    """The file is served and added to the frontend with a versioned URL."""
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    integration = MagicMock(version="1.2.3")
    with (
        patch.object(cards, "async_get_integration", AsyncMock(return_value=integration)),
        patch.object(cards, "add_extra_js_url") as add_url,
    ):
        await cards.async_register_cards(hass)

    config = hass.http.async_register_static_paths.await_args.args[0][0]
    assert config.url_path == FRONTEND_URL
    assert config.path == str(FRONTEND_DIR)
    assert config.cache_headers is True
    # The loader, not the cards: see test_cards_load_after_the_app.
    add_url.assert_called_once_with(hass, f"{FRONTEND_URL}/{LOADER_FILE.name}?v=1.2.3")


def test_cards_load_after_the_app() -> None:
    """The frontend's registry polyfill must be in place before the cards run.

    Cards evaluated earlier fail with "Custom element doesn't exist".
    """
    loader = LOADER_FILE.read_text(encoding="utf-8")
    assert 'whenDefined("home-assistant")' in loader
    assert CARDS_FILE.name in loader
    assert re.search(r"^\s*import\s+[\"'{]", loader, re.MULTILINE) is None


def test_cards_never_build_markup_from_dynamic_values() -> None:
    """Now-playing text is LAN-controlled, so it must never reach innerHTML."""
    source = CARDS_FILE.read_text(encoding="utf-8")
    assignments = re.findall(r"\.innerHTML\s*=\s*`(.*?)`;", source, re.DOTALL)
    assert len(assignments) == source.count("innerHTML =") == 1
    # Only the card's own static style and template constants are interpolated.
    assert set(re.findall(r"\$\{(.*?)\}", assignments[0])) == {
        "BASE_STYLE",
        "style",
        "body",
    }
    assert "insertAdjacentHTML" not in source
    assert "outerHTML" not in source


def test_cards_are_all_registered() -> None:
    """Every card class is listed for the card picker."""
    source = CARDS_FILE.read_text(encoding="utf-8")
    for card in ("player", "mini", "status"):
        assert f'"loewe-klang-{card}-card"' in source
