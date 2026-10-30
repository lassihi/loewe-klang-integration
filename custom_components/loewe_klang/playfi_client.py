"""Async socket I/O for the DTS Play-Fi local protocol.

A :class:`PlayFiListener` binds the shared UDP broadcast port and reports the
decoded now-playing state of one speaker; :func:`async_send_transport` and
:func:`async_send_volume` push a single control command. The wire format lives
in :mod:`.playfi`.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable

from . import playfi
from .const import (
    PLAYFI_BLOWFISH_KEY,
    PLAYFI_BROADCAST_PORT,
    PLAYFI_CONTROL_PORT,
)

_LOGGER = logging.getLogger(__name__)

# The speaker answers a command quickly; don't hang a service call on it.
_COMMAND_TIMEOUT = 4.0


class PlayFiListener(asyncio.DatagramProtocol):
    """Receives a speaker's announce broadcasts and decodes now-playing.

    The speaker broadcasts to the LAN, so every listener on the host sees every
    speaker; each instance keeps only the announces whose device id (or IP)
    matches its own speaker.
    """

    def __init__(
        self,
        host: str,
        on_state: Callable[[playfi.PlayFiState], None],
        *,
        device_id: str | None = None,
    ) -> None:
        self._host = host
        self._on_state = on_state
        self._device_id = device_id.lower() if device_id else None
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        xml = playfi.decode(data, PLAYFI_BLOWFISH_KEY)
        if xml is None:
            return
        state = playfi.parse_announce(xml)
        if state is None or not self._matches(state, addr[0]):
            return
        try:
            self._on_state(state)
        except Exception:
            _LOGGER.exception("Play-Fi state callback failed")

    def _matches(self, state: playfi.PlayFiState, src_ip: str) -> bool:
        if self._device_id and state.device_id:
            return state.device_id.lower() == self._device_id
        return src_ip == self._host or state.ip_address == self._host

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


async def async_create_listener(
    host: str,
    on_state: Callable[[playfi.PlayFiState], None],
    *,
    device_id: str | None = None,
) -> PlayFiListener:
    """Bind the broadcast port and start listening for one speaker."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Allow one listener per speaker (and co-existence with the app) on the host.
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:  # not all platforms honour it
            _LOGGER.debug("SO_REUSEPORT unavailable", exc_info=True)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", PLAYFI_BROADCAST_PORT))
    listener = PlayFiListener(host, on_state, device_id=device_id)
    await loop.create_datagram_endpoint(lambda: listener, sock=sock)
    return listener


async def _async_send(host: str, document: str) -> str | None:
    """Open a control connection, send one command, return the ack document."""
    frame = playfi.encode(document, PLAYFI_BLOWFISH_KEY)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, PLAYFI_CONTROL_PORT), _COMMAND_TIMEOUT
        )
    except (TimeoutError, OSError) as err:
        raise PlayFiError(f"connect {host}:{PLAYFI_CONTROL_PORT}: {err}") from err
    try:
        writer.write(frame)
        await writer.drain()
        try:
            reply = await asyncio.wait_for(reader.read(512), _COMMAND_TIMEOUT)
        except TimeoutError:
            reply = b""
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    return playfi.decode(reply, PLAYFI_BLOWFISH_KEY) if reply else None


async def async_send_transport(host: str, code: int) -> None:
    """Send a transport-control command to the speaker."""
    await _async_send(host, playfi.transport_command(code))


async def async_send_volume(host: str, device_id: str, volume: int, internal: int) -> None:
    """Send a remote-volumes command to the speaker."""
    await _async_send(host, playfi.volume_command(device_id, volume, internal))


class PlayFiError(Exception):
    """A Play-Fi command could not be delivered."""
