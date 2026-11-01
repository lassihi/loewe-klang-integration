"""Media player tests."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.media_player import (
    ATTR_INPUT_SOURCE,
    ATTR_MEDIA_ALBUM_NAME,
    ATTR_MEDIA_ARTIST,
    ATTR_MEDIA_DURATION,
    ATTR_MEDIA_POSITION,
    ATTR_MEDIA_TITLE,
    ATTR_MEDIA_VOLUME_LEVEL,
    ATTR_MEDIA_VOLUME_MUTED,
    MediaPlayerEntityFeature,
)
from homeassistant.components.media_player import (
    DOMAIN as MP_DOMAIN,
)
from homeassistant.const import (
    CONF_HOST,
    STATE_IDLE,
    STATE_ON,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.loewe_klang import playfi
from custom_components.loewe_klang.const import DOMAIN
from custom_components.loewe_klang.playfi import PlayFiState

from .conftest import CAST_UUID, ENTITY_ID, HOST, UDN, FakePlayFiListener


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


async def _call(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        MP_DOMAIN, service, {"entity_id": ENTITY_ID, **data}, blocking=True
    )


async def _poll(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_idle_state(hass: HomeAssistant, setup) -> None:
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_IDLE
    assert state.attributes[ATTR_MEDIA_VOLUME_LEVEL] == 0.3
    assert state.attributes[ATTR_MEDIA_VOLUME_MUTED] is False
    assert ATTR_MEDIA_TITLE not in state.attributes
    assert state.attributes["play_fi_medium"] == "THIRD-DLNA"


async def test_upnp_playing(hass: HomeAssistant, setup, speaker) -> None:
    speaker.set_playing()
    await _poll(hass)
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_PLAYING
    assert state.attributes[ATTR_MEDIA_TITLE] == "Paranoid Android"
    assert state.attributes[ATTR_MEDIA_ARTIST] == "Radiohead"
    assert state.attributes[ATTR_MEDIA_ALBUM_NAME] == "OK Computer"
    assert state.attributes[ATTR_MEDIA_DURATION] == 225
    assert state.attributes[ATTR_MEDIA_POSITION] == 70
    assert state.attributes[ATTR_INPUT_SOURCE] == "DLNA"
    assert state.attributes["supported_features"] & MediaPlayerEntityFeature.SEEK

    await _call(hass, "media_pause")
    await _call(hass, "media_next_track")
    await _call(hass, "media_seek", seek_position=30)
    await _call(hass, "shuffle_set", shuffle=True)
    assert speaker.actions() == ["Pause", "Next", "Seek", "SetPlayMode"]
    assert (
        "<NewPlayMode>SHUFFLE_NOREPEAT</NewPlayMode>"
        in dict(speaker.calls)["SetPlayMode"]
    )


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("http://192.0.2.5/media/a.mp3?authSig=secret", "http://192.0.2.5/media/a.mp3"),
        ("http://192.0.2.5/a.mp3#authSig=secret", "http://192.0.2.5/a.mp3"),
        ("http://[bad/a.mp3?authSig=secret", "http://[bad/a.mp3"),
        ("http://192.0.2.5/a.mp3", "http://192.0.2.5/a.mp3"),
    ],
)
async def test_media_content_id_hides_query(
    hass: HomeAssistant, speaker, setup, uri: str, expected: str
) -> None:
    speaker.set_playing()
    speaker.responses["GetPositionInfo"] = (
        "<s:Envelope xmlns:s='http://schemas.xmlsoap.org/soap/envelope/'><s:Body>"
        f"<r><TrackURI>{uri}</TrackURI></r></s:Body></s:Envelope>"
    )
    await _poll(hass)
    assert hass.states.get(ENTITY_ID).attributes["media_content_id"] == expected


async def test_cast_session_takes_over(
    hass: HomeAssistant, setup, speaker, cast
) -> None:
    cast.instance.push(
        app_id="CC32E753",
        app_name="Spotify",
        player_state="PAUSED",
        title="Song",
        artist="Artist",
        album="Album",
        image_url="https://i.scdn.co/image/x",
        duration=200.0,
        position=12.0,
        supports_pause=True,
        supports_next=True,
    )
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_PAUSED
    assert state.attributes[ATTR_MEDIA_TITLE] == "Song"
    assert state.attributes[ATTR_INPUT_SOURCE] == "Chromecast: Spotify"
    assert state.attributes["app_name"] == "Spotify"
    features = state.attributes["supported_features"]
    assert features & MediaPlayerEntityFeature.NEXT_TRACK
    assert not features & MediaPlayerEntityFeature.PREVIOUS_TRACK
    assert not features & MediaPlayerEntityFeature.SEEK

    await _call(hass, "media_play")
    await _call(hass, "media_next_track")
    cast.instance.async_play.assert_awaited_once()
    cast.instance.async_next.assert_awaited_once()
    assert speaker.actions() == []

    # Session ends: back to the UPnP view.
    cast.instance.push(app_id=None, player_state=None)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_IDLE


async def test_volume_uses_cast_master(
    hass: HomeAssistant, setup, speaker, cast
) -> None:
    await _call(hass, "volume_set", volume_level=0.45)
    await _call(hass, "volume_mute", is_volume_muted=True)
    cast.instance.async_set_volume.assert_awaited_once_with(0.45)
    cast.instance.async_set_mute.assert_awaited_once_with(True)
    assert speaker.actions() == []

    cast.instance.push(volume=0.45, muted=True)
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_ID)
    assert state.attributes[ATTR_MEDIA_VOLUME_LEVEL] == 0.45
    assert state.attributes[ATTR_MEDIA_VOLUME_MUTED] is True


async def test_volume_falls_back_to_upnp(
    hass: HomeAssistant, setup, speaker, cast
) -> None:
    cast.instance.async_set_volume.return_value = False
    cast.instance.push(connected=False)
    await hass.async_block_till_done()
    await _call(hass, "volume_set", volume_level=0.2)
    assert "<DesiredVolume>20</DesiredVolume>" in dict(speaker.calls)["SetVolume"]


async def test_play_media_prefers_cast(
    hass: HomeAssistant, setup, speaker, cast
) -> None:
    await _call(
        hass,
        "play_media",
        media_content_type="music",
        media_content_id="http://192.0.2.5/a.mp3",
    )
    cast.instance.async_play_url.assert_awaited_once_with(
        "http://192.0.2.5/a.mp3", "audio/mpeg", None
    )
    assert speaker.actions() == []

    cast.instance.async_play_url.return_value = False
    await _call(
        hass,
        "play_media",
        media_content_type="music",
        media_content_id="http://192.0.2.5/b.mp3",
    )
    assert speaker.actions() == ["SetAVTransportURI", "Play"]


async def test_unavailable_and_unload(
    hass: HomeAssistant, setup, speaker, aioclient_mock
) -> None:
    speaker.responses["GetTransportInfo"] = "not xml"
    await _poll(hass)
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    assert await hass.config_entries.async_unload(setup.entry_id)


SPOTIFY = "media_player.spotify_test"


def _spotify_attrs(source: str = "Loewe klang mr3") -> dict:
    return {
        "source": source,
        "media_title": "Kiitos ei",
        "media_artist": "Testi Esittäjä",
        "media_album_name": "Testilevy",
        "media_duration": 200,
        "media_position": 12,
    }


@pytest.fixture
async def spotify_setup(hass: HomeAssistant, speaker, cast):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=UDN,
        data={CONF_HOST: HOST, "udn": UDN, "cast_uuid": CAST_UUID},
        options={"spotify_entity": SPOTIFY},
    )
    entry.add_to_hass(hass)
    hass.states.async_set(SPOTIFY, "playing", _spotify_attrs())
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_spotify_connect_now_playing(hass: HomeAssistant, spotify_setup) -> None:
    """Spotify playing on this speaker shows up as now playing."""
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_PLAYING
    assert state.attributes[ATTR_MEDIA_TITLE] == "Kiitos ei"
    assert state.attributes[ATTR_MEDIA_ARTIST] == "Testi Esittäjä"
    assert state.attributes[ATTR_MEDIA_ALBUM_NAME] == "Testilevy"
    assert state.attributes[ATTR_MEDIA_DURATION] == 200
    assert state.attributes[ATTR_INPUT_SOURCE] == "Spotify Connect"
    assert state.attributes["spotify_entity"] == SPOTIFY


async def test_spotify_updates_on_change(hass: HomeAssistant, spotify_setup) -> None:
    hass.states.async_set(SPOTIFY, "paused", _spotify_attrs())
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_PAUSED


async def test_spotify_other_device_ignored(hass: HomeAssistant, spotify_setup) -> None:
    """Spotify playing somewhere else must not leak into this speaker."""
    hass.states.async_set(SPOTIFY, "playing", _spotify_attrs("Kitchen speaker"))
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_IDLE
    assert state.attributes["spotify_entity"] is None


async def test_spotify_commands_forwarded(hass: HomeAssistant, spotify_setup) -> None:
    """Transport commands go to the Spotify entity while Spotify is active."""
    with patch(
        "custom_components.loewe_klang.media_player.LoeweMediaPlayer._spotify_call",
        AsyncMock(),
    ) as forward:
        await _call(hass, "media_pause")
        await _call(hass, "media_next_track")
    assert [c.args for c in forward.call_args_list] == [
        ("media_pause",),
        ("media_next_track",),
    ]


async def test_upnp_session_beats_spotify(
    hass: HomeAssistant, speaker, spotify_setup
) -> None:
    """A UPnP session on the speaker wins over a stale Spotify state."""
    speaker.set_playing()
    await _poll(hass)
    assert hass.states.get(ENTITY_ID).attributes["spotify_entity"] is None


@pytest.fixture
async def auto_setup(hass: HomeAssistant, speaker, cast):
    """Spotify entity registered by the spotify integration, no option set."""
    er.async_get(hass).async_get_or_create(
        "media_player", "spotify", "user-1", suggested_object_id="spotify_test"
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=UDN,
        data={CONF_HOST: HOST, "udn": UDN, "cast_uuid": CAST_UUID},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_spotify_detected_automatically(hass: HomeAssistant, auto_setup) -> None:
    """Without configuration, a Spotify integration entity is picked up."""
    hass.states.async_set(SPOTIFY, "playing", _spotify_attrs())
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_PLAYING
    assert state.attributes[ATTR_MEDIA_TITLE] == "Kiitos ei"
    assert state.attributes["spotify_entity"] == SPOTIFY


async def test_non_spotify_player_ignored(hass: HomeAssistant, auto_setup) -> None:
    """Another integration's player with a matching source is not mirrored."""
    hass.states.async_set("media_player.other", "playing", _spotify_attrs())
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_IDLE


async def test_airplay_session_shown(hass: HomeAssistant, speaker, setup) -> None:
    """An AirPlay session shows as on with source AirPlay and volume only."""
    speaker.airplay_flags = 0xC04
    await _poll(hass)
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_INPUT_SOURCE] == "AirPlay"
    assert state.attributes["airplay_session"] is True
    features = state.attributes["supported_features"]
    assert features & MediaPlayerEntityFeature.VOLUME_SET
    assert not features & MediaPlayerEntityFeature.PAUSE
    assert not features & MediaPlayerEntityFeature.NEXT_TRACK

    speaker.airplay_flags = 0x404
    await _poll(hass)
    assert hass.states.get(ENTITY_ID).state == STATE_IDLE


async def test_airplay_now_playing_via_playfi(
    hass: HomeAssistant, speaker, setup
) -> None:
    """A decoded Play-Fi announce enriches the AirPlay session with metadata."""
    speaker.airplay_flags = 0xC04
    await _poll(hass)
    FakePlayFiListener.instance.push(
        PlayFiState(
            name="Loewe klang mr3",
            ip_address=HOST,
            source="airplay_media",
            pstate=playfi.PSTATE_PLAYING,
            artist="Test Artist",
            track="Test Track",
            image_url="/images/airplay_media/ab12.jpg",
            controls={"PLAY_PAUSE": True, "NEXT": True, "PREVIOUS": True},
            metadata_reported=True,
        )
    )
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_PLAYING
    assert state.attributes[ATTR_INPUT_SOURCE] == "AirPlay"
    assert state.attributes[ATTR_MEDIA_TITLE] == "Test Track"
    assert state.attributes[ATTR_MEDIA_ARTIST] == "Test Artist"
    # Cover art is proxied through Home Assistant, so the picture is set.
    assert state.attributes["entity_picture"]
    component = hass.data["media_player"]
    entity = component.get_entity(ENTITY_ID)
    assert (
        entity.media_image_url
        == f"http://{HOST}:8081/images/airplay_media/ab12.jpg"
    )
    features = state.attributes["supported_features"]
    assert features & MediaPlayerEntityFeature.PAUSE
    assert features & MediaPlayerEntityFeature.NEXT_TRACK
    assert features & MediaPlayerEntityFeature.PREVIOUS_TRACK

    with patch(
        "custom_components.loewe_klang.media_player.async_send_transport",
        new=AsyncMock(),
    ) as send:
        await _call(hass, "media_pause")
        await _call(hass, "media_next_track")
    assert [c.args for c in send.await_args_list] == [
        (HOST, playfi.TRANSPORT_PAUSE),
        (HOST, playfi.TRANSPORT_NEXT),
    ]
    # Paused announce flips the state without ending the session.
    FakePlayFiListener.instance.push(
        PlayFiState(
            source="airplay_media", ip_address=HOST, pstate=0, metadata_reported=True
        )
    )
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_PAUSED


async def test_upnp_session_beats_airplay(hass: HomeAssistant, speaker, setup) -> None:
    speaker.airplay_flags = 0xC04
    speaker.set_playing()
    await _poll(hass)
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_PLAYING
    assert state.attributes[ATTR_INPUT_SOURCE] != "AirPlay"


def _playfi_spotify(pstate: int = playfi.PSTATE_PLAYING) -> PlayFiState:
    return PlayFiState(
        ip_address=HOST,
        source=playfi.PLAYFI_SPOTIFY,
        pstate=pstate,
        artist="Test Artist",
        album="Test Album",
        track="Test Episode",
        image_url="https://i.scdn.co/image/abc",
        controls={"PLAY_PAUSE": True, "NEXT": True, "PREVIOUS": True},
        metadata_reported=True,
    )


async def test_announces_do_not_restart_the_poll_timer(
    hass: HomeAssistant, setup
) -> None:
    """Announces arrive faster than the poll interval and must not starve it."""
    coordinator = setup.runtime_data.coordinator
    timer = coordinator._unsub_refresh
    assert timer is not None
    FakePlayFiListener.instance.push(_playfi_spotify())
    await hass.async_block_till_done()
    assert coordinator._unsub_refresh is timer
    assert hass.states.get(ENTITY_ID).attributes[ATTR_MEDIA_TITLE] == "Test Episode"


async def test_announce_does_not_mark_speaker_reachable(
    hass: HomeAssistant, setup
) -> None:
    coordinator = setup.runtime_data.coordinator
    coordinator.last_update_success = False
    FakePlayFiListener.instance.push(_playfi_spotify())
    await hass.async_block_till_done()
    assert coordinator.last_update_success is False


@pytest.mark.parametrize(
    ("image_url", "expected"),
    [
        ("/images/a.jpg", (b"jpeg", "image/jpeg")),
        ("http://169.254.169.254/latest/meta-data/", (None, None)),
        ("http://127.0.0.1:8123/api/config", (None, None)),
        (None, (None, None)),
    ],
)
async def test_cover_art_proxy_is_guarded(
    hass: HomeAssistant,
    setup,
    aioclient_mock,
    image_url: str | None,
    expected: tuple[bytes | None, str | None],
) -> None:
    """A URL from an announce can't make the server fetch internal addresses."""
    aioclient_mock.get(
        f"http://{HOST}:8081/images/a.jpg",
        content=b"jpeg",
        headers={"Content-Type": "image/jpeg"},
    )
    state = _playfi_spotify()
    state.image_url = image_url
    FakePlayFiListener.instance.push(state)
    await hass.async_block_till_done()

    entity = hass.data["media_player"].get_entity(ENTITY_ID)
    assert await entity.async_get_media_image() == expected


async def test_spotify_connect_via_playfi(hass: HomeAssistant, setup) -> None:
    """Without the Spotify integration, Spotify Connect comes from Play-Fi."""
    FakePlayFiListener.instance.push(_playfi_spotify())
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_PLAYING
    assert state.attributes[ATTR_INPUT_SOURCE] == "Spotify Connect"
    assert state.attributes["app_name"] == "Spotify"
    assert state.attributes[ATTR_MEDIA_TITLE] == "Test Episode"
    assert state.attributes[ATTR_MEDIA_ARTIST] == "Test Artist"
    assert state.attributes[ATTR_MEDIA_ALBUM_NAME] == "Test Album"
    assert ATTR_MEDIA_DURATION not in state.attributes
    entity = hass.data["media_player"].get_entity(ENTITY_ID)
    assert entity.media_image_url == "https://i.scdn.co/image/abc"
    features = state.attributes["supported_features"]
    assert features & MediaPlayerEntityFeature.PAUSE
    assert features & MediaPlayerEntityFeature.NEXT_TRACK
    assert not features & MediaPlayerEntityFeature.SEEK

    with patch(
        "custom_components.loewe_klang.media_player.async_send_transport",
        new=AsyncMock(),
    ) as send:
        await _call(hass, "media_pause")
        await _call(hass, "media_previous_track")
    assert [c.args for c in send.await_args_list] == [
        (HOST, playfi.TRANSPORT_PAUSE),
        (HOST, playfi.TRANSPORT_PREVIOUS),
    ]

    FakePlayFiListener.instance.push(_playfi_spotify(playfi.PSTATE_PAUSED))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_PAUSED


async def test_spotify_integration_beats_playfi(
    hass: HomeAssistant, spotify_setup
) -> None:
    """The Spotify integration (with progress and seek) takes priority."""
    FakePlayFiListener.instance.push(_playfi_spotify())
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_ID)
    assert state.attributes[ATTR_MEDIA_TITLE] == "Kiitos ei"
    assert state.attributes[ATTR_MEDIA_DURATION] == 200
    assert state.attributes["spotify_entity"] == SPOTIFY


async def test_playfi_frame_without_metadata_is_ignored_briefly(
    hass: HomeAssistant, setup
) -> None:
    """A stray status frame without metadata does not blank the card."""
    clock = [1000.0]
    with patch(
        "custom_components.loewe_klang.coordinator.time.monotonic",
        side_effect=lambda: clock[0],
    ):
        FakePlayFiListener.instance.push(_playfi_spotify())
        await hass.async_block_till_done()
        clock[0] += 3
        FakePlayFiListener.instance.push(PlayFiState(ip_address=HOST, pstate=2))
        await hass.async_block_till_done()
        assert hass.states.get(ENTITY_ID).attributes[ATTR_MEDIA_TITLE] == "Test Episode"

        # If the speaker keeps sending them, the session really ended.
        clock[0] += 10
        FakePlayFiListener.instance.push(PlayFiState(ip_address=HOST, pstate=2))
        await hass.async_block_till_done()
        assert hass.states.get(ENTITY_ID).state == STATE_IDLE


async def test_other_playfi_service_shown(hass: HomeAssistant, setup) -> None:
    """A service we have no special handling for still shows its metadata."""
    FakePlayFiListener.instance.push(
        PlayFiState(
            ip_address=HOST,
            source="tidal_media",
            pstate=playfi.PSTATE_PLAYING,
            artist="Test Artist",
            track="Test Track",
            image_url="https://example.com/cover.jpg",
            controls={"PLAY_PAUSE": True, "NEXT": False},
            metadata_reported=True,
        )
    )
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_PLAYING
    assert state.attributes[ATTR_INPUT_SOURCE] == "Tidal"
    assert state.attributes["app_name"] == "Tidal"
    assert state.attributes[ATTR_MEDIA_TITLE] == "Test Track"
    features = state.attributes["supported_features"]
    assert features & MediaPlayerEntityFeature.PAUSE
    # Unverified source: only controls the speaker marks enabled.
    assert not features & MediaPlayerEntityFeature.NEXT_TRACK
    assert not features & MediaPlayerEntityFeature.PREVIOUS_TRACK


async def test_radio_station_as_title(hass: HomeAssistant, setup) -> None:
    FakePlayFiListener.instance.push(
        PlayFiState(
            ip_address=HOST,
            source="tunein_media",
            pstate=playfi.PSTATE_PLAYING,
            station="Test Radio",
            metadata_reported=True,
        )
    )
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_ID)
    assert state.attributes[ATTR_MEDIA_TITLE] == "Test Radio"
    assert state.attributes[ATTR_INPUT_SOURCE] == "TuneIn"


async def test_lingering_playfi_source_ignored(hass: HomeAssistant, setup) -> None:
    """A service name left over after playback, with no track, is not shown."""
    FakePlayFiListener.instance.push(
        PlayFiState(
            ip_address=HOST,
            source="tidal_media",
            pstate=playfi.PSTATE_PAUSED,
            metadata_reported=True,
        )
    )
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_IDLE
