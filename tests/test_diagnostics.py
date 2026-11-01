"""Diagnostics must be safe to attach to a public issue."""

from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.loewe_klang.const import CONF_SPOTIFY_ENTITY, DOMAIN
from custom_components.loewe_klang.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.loewe_klang.playfi import PlayFiState

from .conftest import CAST_UUID, ENTITY_ID, HOST, UDN, FakePlayFiListener

SIGNATURE = "secret-signature"
SIGNED_URL = f"http://192.0.2.5:8123/media/a.mp3?authSig={SIGNATURE}"


async def test_diagnostics_redacted(hass: HomeAssistant, speaker, cast) -> None:
    speaker.set_playing()
    speaker.responses["GetPositionInfo"] = (
        "<s:Envelope xmlns:s='http://schemas.xmlsoap.org/soap/envelope/'><s:Body>"
        f"<r><TrackURI>{SIGNED_URL}</TrackURI><TrackMetaData>"
        "&lt;DIDL-Lite&gt;&lt;item&gt;&lt;title&gt;Song&lt;/title&gt;"
        "&lt;/item&gt;&lt;/DIDL-Lite&gt;</TrackMetaData></r></s:Body></s:Envelope>"
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=UDN,
        data={CONF_HOST: HOST, "udn": UDN, "cast_uuid": CAST_UUID},
        options={CONF_SPOTIFY_ENTITY: "media_player.my_spotify"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    cast.instance.push(title="Cast Song", artist="Cast Artist", image_url=SIGNED_URL)
    FakePlayFiListener.instance.push(
        PlayFiState(
            device_id="60b606000001",
            name="Living room",
            ip_address=HOST,
            source="tidal_media",
            pstate=1,
            artist="Artist",
            album="Album",
            track="Track",
            image_url=SIGNED_URL,
            station="Station",
            firmware="508.7.1.009",
            metadata_reported=True,
        )
    )
    await hass.async_block_till_done()

    # The fixture reaches the entity, so redaction below is tested on real data.
    assert entry.runtime_data.coordinator.data.upnp.track.uri == SIGNED_URL
    assert hass.states.get(ENTITY_ID).attributes["media_content_id"] == (
        "http://192.0.2.5:8123/media/a.mp3"
    )

    diag = await async_get_config_entry_diagnostics(hass, entry)

    text = repr(diag)
    for value in (
        SIGNATURE,
        HOST,
        UDN,
        CAST_UUID,
        "60:B6:06:00:00:01",
        "60b606000001",
        "Living room",
        "Song",
        "Artist",
        "Album",
        "Station",
        "media_player.my_spotify",
    ):
        assert value not in text
    for key in ("host", "udn", "cast_uuid", "mac", "name"):
        assert diag["device"][key] == REDACTED
    assert diag["upnp"]["track"] == REDACTED
    for key in ("title", "artist", "image_url"):
        assert diag["cast"][key] == REDACTED
    for key in (
        "device_id",
        "ip_address",
        "name",
        "artist",
        "album",
        "track",
        "image_url",
        "station",
    ):
        assert diag["playfi"][key] == REDACTED
    assert diag["options"][CONF_SPOTIFY_ENTITY] == REDACTED

    # What the export is for stays readable.
    assert diag["playfi"]["source"] == "tidal_media"
    assert diag["playfi"]["pstate"] == 1
    assert diag["playfi"]["firmware"] == "508.7.1.009"
    assert diag["upnp"]["transport_state"] == "PLAYING"
    assert diag["device"]["firmware"] == "50871009"
