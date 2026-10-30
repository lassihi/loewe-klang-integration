"""Firmware sensor and update-available binary sensor tests."""

import pytest
from homeassistant.const import CONF_HOST, STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.loewe_klang.const import DOMAIN

from .conftest import CAST_UUID, HOST, UDN


@pytest.fixture
async def setup(hass: HomeAssistant, speaker, cast):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=UDN,
        data={CONF_HOST: HOST, "udn": UDN, "cast_uuid": CAST_UUID},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_playfi_firmware_sensor(hass: HomeAssistant, setup) -> None:
    """The Play-Fi firmware revision is exposed from the AirPlay info."""
    state = hass.states.get("sensor.loewe_klang_mr3_play_fi_firmware")
    assert state is not None
    assert state.state == "50871009"


async def test_cast_firmware_sensor_disabled_by_default(
    hass: HomeAssistant, setup
) -> None:
    """The Chromecast firmware sensor exists but is disabled by default."""
    registry = er.async_get(hass)
    entry = registry.async_get("sensor.loewe_klang_mr3_chromecast_firmware")
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_update_available_off(hass: HomeAssistant, setup) -> None:
    """has_update false -> the update binary sensor is off."""
    state = hass.states.get("binary_sensor.loewe_klang_mr3_firmware_update_available")
    assert state is not None
    assert state.state == STATE_OFF


async def test_get_firmware_parses_all_fields(hass: HomeAssistant, speaker) -> None:
    """async_get_firmware reads Play-Fi, Cast and the update flag."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.loewe_klang.upnp import LoeweSpeaker

    fw = await LoeweSpeaker(HOST, async_get_clientsession(hass)).async_get_firmware()
    assert fw.playfi == "50871009"
    assert fw.cast == "1.56.310669"
    assert fw.cast_build == "310669"
    assert fw.update_available is False


async def test_get_firmware_update_true(hass: HomeAssistant, aioclient_mock) -> None:
    """has_update true is surfaced as update_available True."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.loewe_klang.upnp import LoeweSpeaker

    aioclient_mock.get(
        f"http://{HOST}:8008/setup/eureka_info",
        json={"has_update": True, "cast_build_revision": "1.56.310669"},
    )
    aioclient_mock.get(f"http://{HOST}:7000/info", content=b"")

    fw = await LoeweSpeaker(HOST, async_get_clientsession(hass)).async_get_firmware()
    assert fw.update_available is True
    assert fw.cast == "1.56.310669"
    assert fw.playfi is None


async def test_diagnostics_include_firmware(hass: HomeAssistant, setup) -> None:
    from custom_components.loewe_klang.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diag = await async_get_config_entry_diagnostics(hass, setup)
    assert diag["firmware"]["playfi"] == "50871009"
    assert diag["firmware"]["update_available"] is False
    assert diag["options"] == {}
