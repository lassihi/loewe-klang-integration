"""Tests for the UPnP client against recorded speaker responses."""

import socket
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.loewe_klang.upnp import (
    IMAGE_MAX_REDIRECTS,
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


PUBLIC_IMAGE = "http://8.8.8.8/cover.jpg"


async def test_image_from_speaker(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    url = f"http://{HOST}:8081/images/a.jpg"
    aioclient_mock.get(
        url, content=b"jpeg", headers={"Content-Type": "image/jpeg; charset=x"}
    )
    client = LoeweSpeaker(HOST, async_get_clientsession(hass))
    assert await client.async_get_image(url) == (b"jpeg", "image/jpeg")


async def test_image_from_public_host(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        PUBLIC_IMAGE, content=b"jpeg", headers={"Content-Type": "image/jpeg"}
    )
    client = LoeweSpeaker(HOST, async_get_clientsession(hass))
    assert await client.async_get_image(PUBLIC_IMAGE) == (b"jpeg", "image/jpeg")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8123/api/config",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/",
        "http://10.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://224.0.0.1/",
        "file:///etc/passwd",
        "/relative.jpg",
    ],
)
async def test_image_refuses_internal_targets(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, url: str
) -> None:
    client = LoeweSpeaker(HOST, async_get_clientsession(hass))
    assert await client.async_get_image(url) is None
    assert aioclient_mock.call_count == 0


async def test_image_refuses_name_resolving_inside(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    infos = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 80)),
    ]
    client = LoeweSpeaker(HOST, async_get_clientsession(hass))
    with patch.object(hass.loop, "getaddrinfo", AsyncMock(return_value=infos)):
        assert await client.async_get_image("http://nas.example/a.jpg") is None
    assert aioclient_mock.call_count == 0


async def test_image_refuses_redirect_to_internal(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        PUBLIC_IMAGE, status=302, headers={"Location": "http://127.0.0.1/secret"}
    )
    client = LoeweSpeaker(HOST, async_get_clientsession(hass))
    assert await client.async_get_image(PUBLIC_IMAGE) is None
    assert aioclient_mock.call_count == 1


async def test_image_follows_public_redirect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(PUBLIC_IMAGE, status=302, headers={"Location": "/moved.jpg"})
    aioclient_mock.get(
        "http://8.8.8.8/moved.jpg", content=b"jpeg", headers={"Content-Type": "image/png"}
    )
    client = LoeweSpeaker(HOST, async_get_clientsession(hass))
    assert await client.async_get_image(PUBLIC_IMAGE) == (b"jpeg", "image/png")


async def test_image_stops_redirect_loop(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(PUBLIC_IMAGE, status=302, headers={"Location": PUBLIC_IMAGE})
    client = LoeweSpeaker(HOST, async_get_clientsession(hass))
    assert await client.async_get_image(PUBLIC_IMAGE) is None
    assert aioclient_mock.call_count == IMAGE_MAX_REDIRECTS + 1


async def test_image_refuses_non_image_response(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        PUBLIC_IMAGE, text="<html>", headers={"Content-Type": "text/html"}
    )
    client = LoeweSpeaker(HOST, async_get_clientsession(hass))
    assert await client.async_get_image(PUBLIC_IMAGE) is None


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
