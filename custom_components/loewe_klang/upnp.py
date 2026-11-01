"""Async client for the Phorus (DTS Play-Fi) UPnP renderer in Loewe klang speakers.

The firmware answers with non-standard response namespaces and sometimes
emits unescaped XML inside event payloads, so this client parses responses
leniently instead of relying on a strict UPnP library.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import plistlib
import re
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET
from xml.parsers.expat import ExpatError
from xml.sax.saxutils import escape

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

_LOGGER = logging.getLogger(__name__)

UPNP_PORT = 49152
CAST_HTTP_PORT = 8008
AIRPLAY_PORT = 7000
# AirPlay statusFlags bit set while a sender holds a session (playing or paused).
AIRPLAY_SESSION_ACTIVE = 0x800
TIMEOUT = aiohttp.ClientTimeout(total=6)
IMAGE_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
# Replies come from unauthenticated LAN peers or hosts named by them, and the
# timeout alone would let a fast server fill memory.
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_READ_CHUNK = 64 * 1024
# What plistlib and json can raise on hostile input, beyond their documented errors.
_PARSE_ERRORS = (
    plistlib.InvalidFileException,
    ExpatError,
    ValueError,
    LookupError,
    AttributeError,
    TypeError,
    OverflowError,
    RecursionError,
)

AVT = "AVTransport"
RC = "RenderingControl"
_CONTROL = {AVT: "/upnp/control/AVTransport1", RC: "/upnp/control/RenderingControl1"}

# PlayMedium values reported by the renderer, mapped to readable source names.
PLAY_MEDIUM_NAMES: dict[str, str] = {
    "NONE": "Idle",
    "STATION-NETWORK": "Internet radio",
    "SONGLIST-NETWORK": "Play-Fi",
    "SONGLIST-LOCAL": "Local media",
    "SONGLIST-LOCAL_TF": "Local media",
    "THIRD-DLNA": "DLNA",
    "AIRPLAY": "AirPlay",
    "QPLAY": "QPlay",
    "SPOTIFY": "Spotify Connect",
    "UNKNOWN": "Unknown",
}


class LoeweError(Exception):
    """Raised when the speaker cannot be reached or rejects a request."""


@dataclass
class TrackInfo:
    """Metadata of the current track."""

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    image_url: str | None = None
    duration: int | None = None
    uri: str | None = None


@dataclass
class UpnpStatus:
    """Snapshot of the renderer state."""

    transport_state: str = "NO_MEDIA_PRESENT"
    play_medium: str | None = None
    volume: int | None = None
    muted: bool | None = None
    position: int | None = None
    duration: int | None = None
    track: TrackInfo = field(default_factory=TrackInfo)
    actions: set[str] = field(default_factory=set)
    play_mode: str | None = None


@dataclass
class DeviceInfo:
    """Static identity of the speaker."""

    host: str
    name: str
    udn: str
    manufacturer: str = "Loewe"
    model: str | None = None
    firmware: str | None = None
    mac: str | None = None
    cast_uuid: str | None = None


@dataclass
class FirmwareInfo:
    """Firmware versions and update availability, refreshed at runtime."""

    playfi: str | None = None
    cast: str | None = None
    cast_build: str | None = None
    update_available: bool | None = None


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(root: ET.Element, name: str) -> str | None:
    """Return text of the first descendant whose local name matches."""
    for elem in root.iter():
        if _strip_ns(elem.tag) == name:
            return elem.text or ""
    return None


def parse_time(value: str | None) -> int | None:
    """Parse H:MM:SS(.ms) into seconds."""
    if not value or not re.fullmatch(r"\d+:\d{1,2}:\d{1,2}(\.\d+)?", value):
        return None
    hours, minutes, seconds = value.split(":")
    try:
        return int(hours) * 3600 + int(minutes) * 60 + int(float(seconds))
    except (ValueError, OverflowError):  # e.g. beyond Python's int digit limit
        return None


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def parse_didl(didl: str | None) -> TrackInfo:
    """Parse a DIDL-Lite metadata blob."""
    info = TrackInfo()
    if not didl or didl.strip() in ("", "NOT_IMPLEMENTED"):
        return info
    try:
        root = ET.fromstring(didl)
    except ET.ParseError:
        # Some firmwares double-escape metadata; try once more unescaped.
        try:
            root = ET.fromstring(
                didl.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            )
        except ET.ParseError:
            _LOGGER.debug("Unparseable DIDL metadata: %s", didl[:200])
            return info
    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        text = (elem.text or "").strip() or None
        if tag == "title" and info.title is None:
            info.title = text
        elif tag == "artist":
            role = elem.attrib.get("role", "").lower()
            if role == "albumartist":
                info.album_artist = info.album_artist or text
            else:
                info.artist = info.artist or text
        elif tag == "creator" and info.artist is None:
            info.artist = text
        elif tag == "album" and info.album is None:
            info.album = text
        elif tag == "albumArtURI" and info.image_url is None:
            info.image_url = text
        elif tag == "res":
            info.uri = info.uri or text
            info.duration = info.duration or parse_time(elem.attrib.get("duration"))
    if info.title in ("", "Unknown"):
        info.title = None
    return info


def _load_plist(raw: bytes | None) -> dict[str, Any] | None:
    """Parse a plist from the speaker; None unless it is a well-formed dict."""
    if not raw:
        return None
    try:
        data = plistlib.loads(raw)
    except _PARSE_ERRORS:
        return None
    return data if isinstance(data, dict) else None


def _load_json(raw: bytes | None) -> dict[str, Any] | None:
    """Parse JSON from the speaker; None unless it is a well-formed dict."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except _PARSE_ERRORS:
        return None
    return data if isinstance(data, dict) else None


def _str_value(data: dict[str, Any], key: str) -> str | None:
    """A string field from speaker-supplied data; other types are dropped."""
    value = data.get(key)
    return value if isinstance(value, str) else None


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def _read_capped(resp: aiohttp.ClientResponse, limit: int) -> bytes | None:
    """Read a response body, or return None if it is larger than ``limit``."""
    length = resp.headers.get("Content-Length", "")
    if length.isdigit() and int(length) > limit:
        return None
    stream = resp.content
    body = bytearray()
    while chunk := await stream.read(_READ_CHUNK):
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


def _decode_text(raw: bytes, charset: str | None) -> str:
    """Decode a SOAP reply; a bad charset or bytes will just fail XML parsing."""
    try:
        return raw.decode(charset or "utf-8", "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


class _PinnedResolver(AbstractResolver):
    """Answers for one hostname with addresses that were already validated."""

    def __init__(self, host: str, addresses: list[str]) -> None:
        self._host = host
        self._addresses = addresses

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_UNSPEC
    ) -> list[ResolveResult]:
        if host != self._host:
            raise OSError(f"Unexpected host {host}")
        results: list[ResolveResult] = []
        for address in self._addresses:
            addr_family = (
                socket.AF_INET6
                if ipaddress.ip_address(address).version == 6
                else socket.AF_INET
            )
            if family in (socket.AF_UNSPEC, addr_family):
                results.append(
                    {
                        "hostname": host,
                        "host": address,
                        "port": port,
                        "family": addr_family,
                        "proto": 0,
                        "flags": socket.AI_NUMERICHOST,
                    }
                )
        if not results:
            raise OSError(f"No usable address for {host}")
        return results

    async def close(self) -> None:
        """Nothing to release."""


def _is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return ip.is_global and not ip.is_multicast


def build_didl(url: str, title: str, mime: str) -> str:
    """Build minimal DIDL-Lite metadata for SetAVTransportURI."""
    upnp_class = (
        "object.item.audioItem.musicTrack"
        if mime.startswith("audio")
        else "object.item"
    )
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{escape(title)}</dc:title>"
        f"<upnp:class>{upnp_class}</upnp:class>"
        f'<res protocolInfo="http-get:*:{escape(mime)}:*">{escape(url)}</res>'
        "</item></DIDL-Lite>"
    )


class LoeweSpeaker:
    """Talks to one speaker over UPnP plus its small HTTP info endpoints."""

    def __init__(self, host: str, session: aiohttp.ClientSession) -> None:
        self.host = host
        self._session = session
        self._base = f"http://{host}:{UPNP_PORT}"

    async def _soap(self, service: str, action: str, args: str = "") -> ET.Element:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
            f"<InstanceID>0</InstanceID>{args}</u:{action}></s:Body></s:Envelope>"
        )
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:{service}:1#{action}"',
            "Connection": "close",
        }
        # The speaker's libupnp server closes sockets after each reply, so
        # never reuse pooled connections and retry once if one was dropped.
        for attempt in range(2):
            try:
                async with self._session.post(
                    self._base + _CONTROL[service],
                    data=body,
                    headers=headers,
                    timeout=TIMEOUT,
                ) as resp:
                    raw = await _read_capped(resp, MAX_RESPONSE_BYTES)
                    if raw is None:
                        raise LoeweError(f"{action} failed: response too large")
                    text = _decode_text(raw, resp.charset)
                break
            except aiohttp.ServerDisconnectedError as err:
                if attempt:
                    raise LoeweError(f"{action} failed: {err!r}") from err
            except (aiohttp.ClientError, TimeoutError) as err:
                raise LoeweError(f"{action} failed: {err!r}") from err
        try:
            root = ET.fromstring(text)
        except (ET.ParseError, LookupError) as err:
            raise LoeweError(f"{action}: invalid response") from err
        if _find_text(root, "Fault") is not None or resp.status >= 400:
            code = _find_text(root, "errorCode")
            desc = _find_text(root, "errorDescription")
            raise LoeweError(f"{action} rejected: {code} {desc}")
        return root

    # ---- state -------------------------------------------------------------

    async def async_get_status(self) -> UpnpStatus:
        """Read the full renderer state."""
        (
            transport,
            position,
            media,
            volume,
            mute,
            actions,
            settings,
        ) = await asyncio.gather(
            self._soap(AVT, "GetTransportInfo"),
            self._soap(AVT, "GetPositionInfo"),
            self._soap(AVT, "GetMediaInfo"),
            self._soap(RC, "GetVolume", "<Channel>Master</Channel>"),
            self._soap(RC, "GetMute", "<Channel>Master</Channel>"),
            self._soap(AVT, "GetCurrentTransportActions"),
            self._soap(AVT, "GetTransportSettings"),
            return_exceptions=True,
        )
        # Transport info is mandatory; everything else is best effort.
        if isinstance(transport, BaseException):
            raise (
                transport
                if isinstance(transport, LoeweError)
                else LoeweError(str(transport))
            )
        status = UpnpStatus(
            transport_state=(
                _find_text(transport, "CurrentTransportState") or ""
            ).upper()
            or "NO_MEDIA_PRESENT"
        )
        if isinstance(media, ET.Element):
            status.play_medium = _find_text(media, "PlayMedium") or None
            media_track = parse_didl(_find_text(media, "CurrentURIMetaData"))
        else:
            media_track = TrackInfo()
        if isinstance(position, ET.Element):
            track = parse_didl(_find_text(position, "TrackMetaData"))
            # Fall back to the media-level metadata for fields the track lacks.
            for attr in ("title", "artist", "album", "album_artist", "image_url"):
                if getattr(track, attr) is None:
                    setattr(track, attr, getattr(media_track, attr))
            track.uri = track.uri or _find_text(position, "TrackURI") or None
            status.track = track
            status.duration = (
                parse_time(_find_text(position, "TrackDuration")) or track.duration
            )
            status.position = parse_time(_find_text(position, "RelTime"))
            if not status.duration:
                status.position = None
        else:
            status.track = media_track
        if isinstance(volume, ET.Element):
            with contextlib.suppress(ValueError):
                status.volume = int(_find_text(volume, "CurrentVolume") or "")
        if isinstance(mute, ET.Element):
            status.muted = (_find_text(mute, "CurrentMute") or "0") in ("1", "true")
        if isinstance(actions, ET.Element):
            raw = _find_text(actions, "Actions") or ""
            status.actions = {a.strip() for a in raw.split(",") if a.strip()}
        if isinstance(settings, ET.Element):
            status.play_mode = _find_text(settings, "PlayMode")
        return status

    # ---- control -----------------------------------------------------------

    async def async_play(self) -> None:
        await self._soap(AVT, "Play", "<Speed>1</Speed>")

    async def async_pause(self) -> None:
        await self._soap(AVT, "Pause")

    async def async_stop(self) -> None:
        await self._soap(AVT, "Stop")

    async def async_next(self) -> None:
        await self._soap(AVT, "Next")

    async def async_previous(self) -> None:
        await self._soap(AVT, "Previous")

    async def async_seek(self, seconds: float) -> None:
        await self._soap(
            AVT, "Seek", f"<Unit>REL_TIME</Unit><Target>{format_time(seconds)}</Target>"
        )

    async def async_set_volume(self, volume: int) -> None:
        volume = max(0, min(100, int(volume)))
        await self._soap(
            RC,
            "SetVolume",
            f"<Channel>Master</Channel><DesiredVolume>{volume}</DesiredVolume>",
        )

    async def async_set_mute(self, mute: bool) -> None:
        await self._soap(
            RC,
            "SetMute",
            f"<Channel>Master</Channel><DesiredMute>{int(mute)}</DesiredMute>",
        )

    async def async_set_play_mode(self, mode: str) -> None:
        await self._soap(
            AVT, "SetPlayMode", f"<NewPlayMode>{escape(mode)}</NewPlayMode>"
        )

    async def async_play_url(
        self, url: str, title: str = "Home Assistant", mime: str = "audio/mpeg"
    ) -> None:
        """Play a URL on the renderer (DLNA push)."""
        meta = escape(build_didl(url, title, mime))
        await self._soap(
            AVT,
            "SetAVTransportURI",
            f"<CurrentURI>{escape(url)}</CurrentURI><CurrentURIMetaData>{meta}</CurrentURIMetaData>",
        )
        await self.async_play()

    # ---- identity ----------------------------------------------------------

    async def _get(self, url: str) -> bytes | None:
        try:
            async with self._session.get(
                url, timeout=TIMEOUT, headers={"Connection": "close"}
            ) as resp:
                if resp.status != 200:
                    return None
                return await _read_capped(resp, MAX_RESPONSE_BYTES)
        except (aiohttp.ClientError, TimeoutError):
            return None

    async def _image_addresses(self, url: URL) -> list[str] | None:
        """Vet a cover art URL: only the speaker and public addresses may serve it.

        Returns None if refused, otherwise the addresses to connect to. The list
        is empty when there is no later lookup to pin (the configured speaker or
        an IP literal). A hostname's addresses must be pinned, as resolving it
        again at connect time could give a different, internal, answer.
        """
        if url.scheme not in ("http", "https") or not url.host:
            return None
        if url.host == self.host:
            return []
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                url.host, url.port, type=socket.SOCK_STREAM
            )
            if not infos or not all(_is_public_ip(info[4][0]) for info in infos):
                return None
        except (OSError, ValueError):
            return None
        if _is_ip_literal(url.host):
            return []
        return list(dict.fromkeys(info[4][0] for info in infos))

    @contextlib.asynccontextmanager
    async def _image_session(
        self, host: str, addresses: list[str]
    ) -> AsyncIterator[aiohttp.ClientSession]:
        """A session that can only reach the vetted addresses of ``host``.

        TLS still verifies against ``host``, as only name resolution is replaced.
        """
        if not addresses:
            yield self._session
            return
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                resolver=_PinnedResolver(host, addresses),
                use_dns_cache=False,
                force_close=True,
            ),
            cookie_jar=aiohttp.DummyCookieJar(),
        ) as session:
            yield session

    async def async_get_image(self, url: str) -> tuple[bytes, str] | None:
        """Fetch cover art whose URL came from unauthenticated LAN traffic.

        Announces, DLNA metadata and Cast status can all name any URL, and
        Home Assistant's own image fetch would request it from the server with
        no checks. Redirects are followed by hand so each hop is checked too.
        """
        try:
            target = URL(url)
            for _ in range(IMAGE_MAX_REDIRECTS + 1):
                addresses = await self._image_addresses(target)
                if addresses is None:
                    _LOGGER.debug("Refusing cover art from %s", target.host)
                    return None
                async with (
                    self._image_session(target.host or "", addresses) as session,
                    session.get(target, timeout=TIMEOUT, allow_redirects=False) as resp,
                ):
                    if resp.status in _REDIRECT_STATUSES:
                        target = target.join(URL(resp.headers.get("Location", "")))
                        continue
                    mime = resp.headers.get("Content-Type", "").split(";")[0].strip()
                    if resp.status != 200 or not mime.lower().startswith("image/"):
                        return None
                    body = await _read_capped(resp, MAX_IMAGE_BYTES)
                    return None if body is None else (body, mime)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None
        return None

    async def async_get_device_info(self) -> DeviceInfo:
        """Read name/UDN from UPnP plus extra identity from Cast and AirPlay."""
        raw = await self._get(f"{self._base}/description.xml")
        if raw is None:
            raise LoeweError(f"No UPnP renderer at {self.host}:{UPNP_PORT}")
        try:
            root = ET.fromstring(raw)
        except (ET.ParseError, LookupError) as err:
            raise LoeweError("Invalid device description") from err
        manufacturer = _find_text(root, "manufacturer") or ""
        model_name = _find_text(root, "modelName") or ""
        if "phorus" not in (manufacturer + model_name).lower():
            raise LoeweError(f"{self.host} is not a Play-Fi renderer ({manufacturer})")
        udn = (_find_text(root, "UDN") or "").removeprefix("uuid:")
        info = DeviceInfo(
            host=self.host,
            name=_find_text(root, "friendlyName") or "Loewe klang",
            udn=udn,
        )
        eureka, airplay = await asyncio.gather(
            self._get(f"http://{self.host}:{CAST_HTTP_PORT}/setup/eureka_info"),
            self._get(f"http://{self.host}:{AIRPLAY_PORT}/info"),
        )
        if (data := _load_json(eureka)) is not None:
            info.cast_uuid = _str_value(data, "ssdp_udn")
            info.mac = _str_value(data, "mac_address")
        if (plist := _load_plist(airplay)) is not None:
            info.model = _str_value(plist, "model")
            info.firmware = _str_value(plist, "firmwareRevision")
            info.manufacturer = (_str_value(plist, "manufacturer") or "Loewe").title()
            info.mac = info.mac or _str_value(plist, "deviceID")
        return info

    async def async_get_firmware(self) -> FirmwareInfo:
        """Read firmware versions and the Cast update-available flag.

        The Play-Fi firmware comes from the AirPlay ``/info`` plist; the Cast
        build and ``has_update`` flag come from ``/setup/eureka_info``. Both are
        best-effort: a missing source leaves its fields ``None``.
        """
        fw = FirmwareInfo()
        eureka, airplay = await asyncio.gather(
            self._get(f"http://{self.host}:{CAST_HTTP_PORT}/setup/eureka_info"),
            self._get(f"http://{self.host}:{AIRPLAY_PORT}/info"),
        )
        if (data := _load_json(eureka)) is not None:
            fw.cast = _str_value(data, "cast_build_revision")
            fw.cast_build = _str_value(data, "build_version")
            if "has_update" in data:
                fw.update_available = bool(data["has_update"])
        if (plist := _load_plist(airplay)) is not None:
            fw.playfi = _str_value(plist, "firmwareRevision")
        return fw

    async def async_get_airplay_active(self) -> bool:
        """Whether an AirPlay sender currently has a session on the speaker.

        The speaker exposes no AirPlay track data or transport state, only this
        flag, which stays set while the sender is paused.
        """
        plist = _load_plist(await self._get(f"http://{self.host}:{AIRPLAY_PORT}/info"))
        flags = plist.get("statusFlags") if plist else None
        return isinstance(flags, int) and bool(flags & AIRPLAY_SESSION_ACTIVE)
