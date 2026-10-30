"""Tests for the UPnP client against recorded speaker responses."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.loewe_klang.upnp import (
    LoeweError,
    LoeweSpeaker,
    build_didl,
    parse_didl,
    parse_time,
)

from .conftest import CAST_UUID, HOST, UDN, fixture


def test_parse_time() -> None:
    assert parse_time("0:03:45.000") == 225
    assert parse_time("01:00:00") == 3600
    assert parse_time("NOT_IMPLEMENTED") is None
    assert parse_time("") is None


def test_parse_didl_roundtrip() -> None:
    info = parse_didl(build_didl("http://x/a.mp3", "Tom & Jerry", "audio/mpeg"))
    assert info.title == "Tom & Jerry"
    assert info.uri == "http://x/a.mp3"
    assert parse_didl("").title is None
    assert parse_didl("<broken").title is None


async def test_status_idle(hass: HomeAssistant, speaker) -> None:
    client = LoeweSpeaker(HOST, async_get_clientsession(hass))
    status = await client.async_get_status()
    assert status.transport_state == "STOPPED"
    assert status.play_medium == "THIRD-DLNA"
    assert status.volume == 30
    assert status.muted is False
    assert status.play_mode == "NORMAL"
    assert "Seek" in status.actions
    # The renderer keeps the last item's metadata after it stops.
    assert status.track.title == "Previous track"


async def test_status_playing(hass: HomeAssistant, speaker) -> None:
    speaker.set_playing()
    status = await LoeweSpeaker(HOST, async_get_clientsession(hass)).async_get_status()
    assert status.transport_state == "PLAYING"
    assert status.track.title == "Paranoid Android"
    assert status.track.artist == "Radiohead"
    assert status.track.album_artist == "Radiohead"
    assert status.track.album == "OK Computer"
    assert status.track.image_url == "http://192.0.2.20/cover.jpg"
    assert status.duration == 225
    assert status.position == 70


async def test_device_info(hass: HomeAssistant, speaker) -> None:
    info = await LoeweSpeaker(
        HOST, async_get_clientsession(hass)
    ).async_get_device_info()
    assert info.name == "Loewe klang mr3"
    assert info.udn == UDN
    assert info.cast_uuid == CAST_UUID
    assert info.model == "MR3_GC"
    assert info.firmware == "50871009"
    assert info.manufacturer == "Loewe"


async def test_fault_raises(hass: HomeAssistant, speaker) -> None:
    speaker.responses["SetVolume"] = fixture("fault_501.xml")
    with pytest.raises(LoeweError, match="501"):
        await LoeweSpeaker(HOST, async_get_clientsession(hass)).async_set_volume(10)


async def test_commands_send_expected_soap(hass: HomeAssistant, speaker) -> None:
    client = LoeweSpeaker(HOST, async_get_clientsession(hass))
    await client.async_seek(125)
    await client.async_set_volume(150)
    await client.async_play_url("http://h/a.mp3?x=1&y=2", "Clip", "audio/mpeg")
    bodies = dict(speaker.calls)
    assert "<Target>0:02:05</Target>" in bodies["Seek"]
    assert "<DesiredVolume>100</DesiredVolume>" in bodies["SetVolume"]
    assert (
        "<CurrentURI>http://h/a.mp3?x=1&amp;y=2</CurrentURI>"
        in bodies["SetAVTransportURI"]
    )
    assert speaker.actions() == ["Seek", "SetVolume", "SetAVTransportURI", "Play"]
