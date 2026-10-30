"""Fixtures for Loewe klang tests."""

from __future__ import annotations

import plistlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

# Import our package before the HA test harness puts its own testing_config
# custom_components directory first on sys.path.
import custom_components  # noqa: F401
from custom_components.loewe_klang.cast import CastSnapshot
from custom_components.loewe_klang.playfi import PlayFiState

pytest_plugins = ["pytest_homeassistant_custom_component"]

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "192.0.2.10"
UDN = "3d62cbc4-32f8-4fbc-8618-0a1b2c3d4e5f"
CAST_UUID = "00000000-0000-4000-8000-000000000001"
ENTITY_ID = "media_player.loewe_klang_mr3"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom_components in every test."""
    yield


class FakeSpeaker:
    """Serves recorded responses from a real klang mr3 and records SOAP calls."""

    def __init__(self, mock: AiohttpClientMocker) -> None:
        self.calls: list[tuple[str, str]] = []
        self.responses: dict[str, str] = {
            name: fixture(f"{name}.xml")
            for name in (
                "GetTransportInfo",
                "GetPositionInfo",
                "GetMediaInfo",
                "GetCurrentTransportActions",
                "GetTransportSettings",
                "GetVolume",
                "GetMute",
            )
        }
        base = f"http://{HOST}:49152"
        mock.get(f"{base}/description.xml", text=fixture("description.xml"))
        mock.get(
            f"http://{HOST}:8008/setup/eureka_info",
            json={
                "ssdp_udn": CAST_UUID,
                "mac_address": "60:B6:06:00:00:01",
                "name": "Loewe klang mr3",
                "cast_build_revision": "1.56.310669",
                "build_version": "310669",
                "has_update": False,
            },
        )
        self.airplay_flags = 0x404
        mock.get(f"http://{HOST}:7000/info", side_effect=self._airplay_info)
        for path in ("AVTransport1", "RenderingControl1"):
            mock.post(f"{base}/upnp/control/{path}", side_effect=self._handle)

    async def _handle(
        self, method: str, url: Any, data: Any
    ) -> AiohttpClientMockResponse:
        body = data.decode() if isinstance(data, bytes) else str(data)
        action = re.search(r"<u:(\w+) ", body).group(1)
        self.calls.append((action, body))
        text = self.responses.get(action)
        if text is None:
            text = (
                f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                f'<u:{action}Response xmlns:u="urn:upnp-org:serviceId:X"></u:{action}Response>'
                f"</s:Body></s:Envelope>"
            )
        return AiohttpClientMockResponse(method, url, text=text)

    async def _airplay_info(
        self, method: str, url: Any, data: Any
    ) -> AiohttpClientMockResponse:
        body = plistlib.dumps(
            {
                "model": "MR3_GC",
                "firmwareRevision": "50871009",
                "manufacturer": "LOEWE",
                "statusFlags": self.airplay_flags,
            },
            fmt=plistlib.FMT_BINARY,
        )
        return AiohttpClientMockResponse(method, url, response=body)

    def actions(self) -> list[str]:
        return [a for a, _ in self.calls if not a.startswith("Get")]

    def set_playing(self) -> None:
        self.responses["GetTransportInfo"] = self.responses["GetTransportInfo"].replace(
            "STOPPED", "PLAYING"
        )
        self.responses["GetPositionInfo"] = fixture("GetPositionInfo_playing.xml")


@pytest.fixture
def speaker(aioclient_mock: AiohttpClientMocker) -> FakeSpeaker:
    return FakeSpeaker(aioclient_mock)


class FakeCast:
    """Stand-in for CastBridge."""

    instance: FakeCast | None = None

    def __init__(
        self, hass, host: str, uuid: str, on_update: Callable[[CastSnapshot], None]
    ) -> None:
        self.on_update = on_update
        self.snapshot = CastSnapshot(connected=True, volume=0.3, muted=False)
        for name in (
            "async_play",
            "async_pause",
            "async_stop_media",
            "async_next",
            "async_previous",
            "async_seek",
            "async_stop",
        ):
            setattr(self, name, AsyncMock())
        self.async_set_volume = AsyncMock(return_value=True)
        self.async_set_mute = AsyncMock(return_value=True)
        self.async_play_url = AsyncMock(return_value=True)
        FakeCast.instance = self

    async def async_start(self) -> None:
        self.on_update(self.snapshot)

    def push(self, **changes: Any) -> None:
        self.snapshot = CastSnapshot(**{**self.snapshot.__dict__, **changes})
        self.on_update(self.snapshot)


@pytest.fixture
def cast():
    with patch("custom_components.loewe_klang.CastBridge", FakeCast):
        yield FakeCast


class FakePlayFiListener:
    """Stand-in for the UDP listener; lets tests push announce states."""

    instance: FakePlayFiListener | None = None

    def __init__(self, host: str, on_state: Callable[[PlayFiState], None]) -> None:
        self.on_state = on_state
        self.closed = False
        FakePlayFiListener.instance = self

    def close(self) -> None:
        self.closed = True

    def push(self, state: PlayFiState) -> None:
        self.on_state(state)


@pytest.fixture(autouse=True)
def playfi_listener():
    """Patch the Play-Fi listener so setup never touches a real socket."""
    FakePlayFiListener.instance = None

    async def _create(host, on_state, *, device_id=None):
        return FakePlayFiListener(host, on_state)

    with patch(
        "custom_components.loewe_klang.async_create_listener", side_effect=_create
    ):
        yield FakePlayFiListener
