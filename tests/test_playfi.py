"""Tests for the Play-Fi protocol layer.

``PAUSE_WIRE`` is a genuine pause command sent by the official app, captured
off the wire, so the cipher and framing are pinned to real device behaviour.
``ANNOUNCE`` mirrors the structure of a real speaker broadcast with made-up
values.
"""

from unittest.mock import patch

from custom_components.loewe_klang import playfi
from custom_components.loewe_klang.const import PLAYFI_BLOWFISH_KEY as KEY
from custom_components.loewe_klang.playfi_client import PlayFiListener

# A speaker announce broadcast with the structure of a real klang mr3 frame
# (AirPlay session, paused). Every identifying value (MAC, addresses, serial,
# track) is made up; the frame is built with the same cipher the speaker uses.
ANNOUNCE_XML = (
    "<api><phoruslink><event><type>speaker-to-phone-announce-response-pulse</type>"
    "<payload><protocolversion>1.1</protocolversion><network>Caprica Docking Station v.1.0</network><devicecount>1</devicecount><deviceid>0a1b2c3d4e5f</deviceid><ipaddress>192.0.2.10</ipaddress><name>Loewe klang mr3</name><extin>hide src:Airplay</extin><redist>1234567890123</redist><inupdate>0</inupdate><spbalance>0</spbalance>"
    "<metadata><artist>Test Artist</artist><album>Test Album</album><trackname>Test Track</trackname><imageurl></imageurl><msource>airplay_media</msource><iconurl></iconurl><station></station><sqt>0</sqt>"
    "<transportcontrols><type>BUTTON</type><name>PLAY_PAUSE</name><enabled>1</enabled><selected>0</selected><type>BUTTON</type><name>NEXT</name><enabled>1</enabled><selected>0</selected><type>BUTTON</type><name>PREVIOUS</name><enabled>1</enabled><selected>0</selected></transportcontrols></metadata><paired>192.0.2.99</paired><pairedname>Home Assistant</pairedname><linked></linked><pstate>2</pstate><pmode>0$3</pmode><volume>17</volume><vi>17</vi><pv>17</pv><rssi>5</rssi><autopower>none</autopower><capricaversion>508.7.1.009</capricaversion><btversion>0.000</btversion><mcuversion>2.036</mcuversion><bluetoothmac>000000</bluetoothmac><bluetoothconnectedmac>00:00:00:00:00:00</bluetoothconnectedmac><btconnection>disconnected</btconnection><moduleversion>10</moduleversion><productid>MR3_GC</productid><partnerid>LOEWE</partnerid><productname>MR3_GC</productname><remoteport></remoteport><netif>wireless-5g$L5H</netif><auxin>speakerin1offout1off</auxin><wfregion>38</wfregion><additionaldspdelay>0</additionaldspdelay></payload></event></phoruslink></api>"
)
ANNOUNCE = playfi.encode(ANNOUNCE_XML, KEY)[:-1]

# Real pause command (phone -> speaker) exactly as the app sends it.
PAUSE_WIRE = bytes.fromhex(
    "9c5877449cb831a13d4abb15977d1801035e64c689e8c08da7d315122af572cd"
    "6dcfdb491c5d52a282c40a556c99fc32dd0e0eb2dc4c5d600a"
)


def test_decode_announce_to_xml() -> None:
    xml = playfi.decode(ANNOUNCE, KEY)
    assert xml is not None
    assert xml.startswith("<api>") and xml.endswith("</api>")
    assert "phoruslink" in xml


def test_parse_announce_metadata() -> None:
    state = playfi.parse_announce(playfi.decode(ANNOUNCE, KEY))
    assert state is not None
    assert state.name == "Loewe klang mr3"
    assert state.ip_address == "192.0.2.10"
    assert state.source == "airplay_media"
    assert state.artist == "Test Artist"
    assert state.track == "Test Track"
    assert state.volume == 17
    assert state.firmware == "508.7.1.009"
    # This captured frame is a paused AirPlay session (pstate 2).
    assert state.pstate == playfi.PSTATE_PAUSED
    assert state.playing is False
    assert state.has_metadata is True
    assert state.controls == {"PLAY_PAUSE": True, "NEXT": True, "PREVIOUS": True}


def test_pstate_playing_mapping() -> None:
    xml = (
        "<api><phoruslink><event><type>x</type><payload>"
        "<deviceid>abc</deviceid><pstate>1</pstate>"
        "<metadata><msource>airplay_media</msource></metadata>"
        "</payload></event></phoruslink></api>"
    )
    assert playfi.parse_announce(xml).playing is True
    assert playfi.parse_announce(xml.replace("<pstate>1", "<pstate>2")).playing is False


def test_transport_command_is_byte_exact() -> None:
    frame = playfi.encode(
        playfi.transport_command(playfi.TRANSPORT_PAUSE), KEY
    )
    assert frame == PAUSE_WIRE


def test_transport_codes() -> None:
    assert "<transport>5</transport>" in playfi.transport_command(playfi.TRANSPORT_NEXT)
    assert "<transport>6</transport>" in playfi.transport_command(playfi.TRANSPORT_PLAY)
    assert (
        "<transport>4</transport>"
        in playfi.transport_command(playfi.TRANSPORT_PREVIOUS)
    )


def test_volume_command_shape() -> None:
    doc = playfi.volume_command("0a1b2c3d4e5f", playfi.VOLUME_MUTE, 17)
    assert "<query>56</query>" in doc
    assert "<deviceids>0a1b2c3d4e5f:</deviceids>" in doc
    assert "<volumes>255:</volumes>" in doc
    assert "<internalvolumes>17:</internalvolumes>" in doc


def test_encode_round_trips() -> None:
    doc = playfi.transport_command(playfi.TRANSPORT_NEXT)
    assert playfi.decode(playfi.encode(doc, KEY), KEY) == doc


def test_decode_tolerates_command_ack() -> None:
    # Speaker acks are not announces.
    ack = playfi.encode("<api><transport>granted</transport></api>", KEY)
    assert playfi.parse_announce(playfi.decode(ack, KEY)) is None


def test_decode_rejects_garbage() -> None:
    assert playfi.decode(b"not a frame", KEY) is None
    assert playfi.parse_announce("<nonsense/>") is None


def test_resolve_image_url() -> None:
    host = "192.0.2.10"
    assert playfi.resolve_image_url(None, host, 8081) is None
    assert playfi.resolve_image_url("", host, 8081) is None
    assert (
        playfi.resolve_image_url("/images/airplay_media/ab12.jpg", host, 8081)
        == "http://192.0.2.10:8081/images/airplay_media/ab12.jpg"
    )
    assert (
        playfi.resolve_image_url("images/x.jpg", host, 8081)
        == "http://192.0.2.10:8081/images/x.jpg"
    )
    assert (
        playfi.resolve_image_url("https://cdn.example/a.jpg", host, 8081)
        == "https://cdn.example/a.jpg"
    )


SPEAKER_IP = "192.0.2.10"


def _listener() -> tuple[PlayFiListener, list[playfi.PlayFiState]]:
    states: list[playfi.PlayFiState] = []
    return PlayFiListener({SPEAKER_IP}, states.append), states


def test_listener_accepts_announce_from_speaker() -> None:
    listener, states = _listener()
    listener.datagram_received(ANNOUNCE, (SPEAKER_IP, 10102))
    assert [s.track for s in states] == ["Test Track"]


def test_listener_ignores_other_source_claiming_speaker_ip() -> None:
    # The frame's <ipaddress> says 192.0.2.10, but that is attacker-controlled.
    listener, states = _listener()
    listener.datagram_received(ANNOUNCE, ("192.0.2.66", 10102))
    assert states == []


def test_listener_does_not_decrypt_foreign_or_oversized_datagrams() -> None:
    listener, states = _listener()
    with patch.object(playfi, "decode") as decode:
        listener.datagram_received(b"A" * 8, ("192.0.2.66", 10102))
        listener.datagram_received(b"A" * 8200, (SPEAKER_IP, 10102))
    decode.assert_not_called()
    assert states == []


def test_cipher_is_reused() -> None:
    assert playfi._cipher(KEY) is playfi._cipher(KEY)


def test_source_name() -> None:
    assert playfi.source_name("airplay_media") == "AirPlay"
    assert playfi.source_name("spotify_media") == "Spotify Connect"
    assert playfi.source_name("tidal_media") == "Tidal"
    assert playfi.source_name("iheartradio_media") == "iHeartRadio"
    assert playfi.source_name("some_new_service_media") == "Some New Service"
    assert playfi.source_name("none") is None
    assert playfi.source_name("") is None
    assert playfi.source_name(None) is None
