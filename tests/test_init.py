"""Tests for config entry setup."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.loewe_klang.const import DOMAIN

from .conftest import CAST_UUID, HOST, UDN, FakeCast

STORED_UUID = "00000000-0000-4000-8000-000000000002"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"cast_uuid": STORED_UUID}, STORED_UUID),
        ({"cast_uuid": None}, CAST_UUID),
        ({}, CAST_UUID),
    ],
    ids=["stored_wins_over_live", "stored_none_uses_live", "missing_uses_live"],
)
async def test_cast_uuid_source(
    hass: HomeAssistant, speaker, data: dict[str, str | None], expected: str
) -> None:
    """The stored Cast UUID wins; the live one is an unpersisted fallback."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=UDN,
        data={CONF_HOST: HOST, "udn": UDN, **data},
    )
    entry.add_to_hass(hass)
    stored = dict(entry.data)

    with patch("custom_components.loewe_klang.CastBridge", wraps=FakeCast) as bridge:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert bridge.call_args.args[2] == expected
    assert dict(entry.data) == stored
