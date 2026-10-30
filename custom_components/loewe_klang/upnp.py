"""Async client for the Phorus (DTS Play-Fi) UPnP renderer in Loewe klang speakers.

The firmware answers with non-standard response namespaces and sometimes
emits unescaped XML inside event payloads, so this client parses responses
leniently instead of relying on a strict UPnP library.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import plistlib
import re
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import aiohttp

_LOGGER = logging.getLogger(__name__)

UPNP_PORT = 49152
CAST_HTTP_PORT = 8008
AIRPLAY_PORT = 7000
# AirPlay statusFlags bit set while a sender holds a session (playing or paused).
AIRPLAY_SESSION_ACTIVE = 0x800
TIMEOUT = aiohttp.ClientTimeout(total=6)

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
    if not value or not re.match(r"^\d+:\d{1,2}:\d{1,2}", value):
        return None
    hours, minutes, seconds = value.split(":")[:3]
    return int(hours) * 3600 + int(minutes) * 60 + int(float(seconds))


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
                    text = await resp.text()
                break
            except aiohttp.ServerDisconnectedError as err:
                if attempt:
                    raise LoeweError(f"{action} failed: {err!r}") from err
            except (aiohttp.ClientError, TimeoutError) as err:
                raise LoeweError(f"{action} failed: {err!r}") from err
        try:
            root = ET.fromstring(text)
        except ET.ParseError as err:
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
                return await resp.read()
        except (aiohttp.ClientError, TimeoutError):
            return None

    async def async_get_device_info(self) -> DeviceInfo:
        """Read name/UDN from UPnP plus extra identity from Cast and AirPlay."""
        raw = await self._get(f"{self._base}/description.xml")
        if raw is None:
            raise LoeweError(f"No UPnP renderer at {self.host}:{UPNP_PORT}")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as err:
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
        if eureka:
            try:
                data: dict[str, Any] = json.loads(eureka)
                info.cast_uuid = data.get("ssdp_udn")
                info.mac = data.get("mac_address")
            except ValueError:
                pass
        if airplay:
            try:
                plist = plistlib.loads(airplay)
                info.model = plist.get("model")
                info.firmware = plist.get("firmwareRevision")
                info.manufacturer = str(plist.get("manufacturer", "Loewe")).title()
                info.mac = info.mac or plist.get("deviceID")
            except (plistlib.InvalidFileException, ValueError):
                pass
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
        if eureka:
            try:
                data: dict[str, Any] = json.loads(eureka)
                fw.cast = data.get("cast_build_revision")
                fw.cast_build = data.get("build_version")
                if "has_update" in data:
                    fw.update_available = bool(data["has_update"])
            except ValueError:
                pass
        if airplay:
            try:
                fw.playfi = plistlib.loads(airplay).get("firmwareRevision")
            except (plistlib.InvalidFileException, ValueError):
                pass
        return fw

    async def async_get_airplay_active(self) -> bool:
        """Whether an AirPlay sender currently has a session on the speaker.

        The speaker exposes no AirPlay track data or transport state, only this
        flag, which stays set while the sender is paused.
        """
        raw = await self._get(f"http://{self.host}:{AIRPLAY_PORT}/info")
        if not raw:
            return False
        try:
            flags = plistlib.loads(raw).get("statusFlags")
        except (plistlib.InvalidFileException, ValueError):
            return False
        return isinstance(flags, int) and bool(flags & AIRPLAY_SESSION_ACTIVE)
