"""Config flow tests."""

import dataclasses

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.loewe_klang.const import DOMAIN

from .conftest import CAST_UUID, HOST, UDN, fixture

SSDP = SsdpServiceInfo(
    ssdp_usn=f"uuid:{UDN}::urn:schemas-upnp-org:device:MediaRenderer:1",
    ssdp_st="urn:schemas-upnp-org:device:MediaRenderer:1",
    ssdp_location=f"http://{HOST}:49152/description.xml",
    upnp={
        "UDN": f"uuid:{UDN}",
        "manufacturer": "Phorus",
        "friendlyName": "Loewe klang mr3",
    },
)


async def test_user_flow(hass: HomeAssistant, speaker, cast) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Loewe klang mr3"
    assert result["data"] == {CONF_HOST: HOST, "udn": UDN, "cast_uuid": CAST_UUID}
    assert result["result"].unique_id == UDN


async def test_user_flow_cannot_connect(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"http://{HOST}:49152/description.xml", status=404)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_ssdp_flow(hass: HomeAssistant, speaker, cast) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_SSDP}, data=SSDP
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == HOST


OLD_HOST = "192.0.2.99"


def _existing_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=UDN, data={CONF_HOST: OLD_HOST, "udn": UDN}
    )
    entry.add_to_hass(hass)
    return entry


async def _discover(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_SSDP}, data=SSDP
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def _serve_description(aioclient_mock, host: str, udn: str = UDN) -> None:
    aioclient_mock.get(
        f"http://{host}:49152/description.xml",
        text=fixture("description.xml").replace(UDN, udn),
    )
    aioclient_mock.get(f"http://{host}:8008/setup/eureka_info", status=404)
    aioclient_mock.get(f"http://{host}:7000/info", status=404)


async def test_ssdp_follows_a_speaker_that_moved(
    hass: HomeAssistant, speaker, aioclient_mock
) -> None:
    entry = _existing_entry(hass)
    aioclient_mock.get(f"http://{OLD_HOST}:49152/description.xml", status=404)
    await _discover(hass)
    assert entry.data[CONF_HOST] == HOST


async def test_ssdp_ignores_spoof_while_speaker_still_answers(
    hass: HomeAssistant, speaker, aioclient_mock
) -> None:
    entry = _existing_entry(hass)
    _serve_description(aioclient_mock, OLD_HOST)
    await _discover(hass)
    assert entry.data[CONF_HOST] == OLD_HOST


async def test_ssdp_ignores_new_host_with_a_different_udn(
    hass: HomeAssistant, aioclient_mock
) -> None:
    entry = _existing_entry(hass)
    aioclient_mock.get(f"http://{OLD_HOST}:49152/description.xml", status=404)
    _serve_description(aioclient_mock, HOST, "00000000-0000-4000-8000-00000000dead")
    await _discover(hass)
    assert entry.data[CONF_HOST] == OLD_HOST


async def test_ssdp_probed_udn_cannot_repoint_either(
    hass: HomeAssistant, speaker, aioclient_mock
) -> None:
    """Announcing an unknown UDN doesn't get around the check at probe time."""
    entry = _existing_entry(hass)
    _serve_description(aioclient_mock, OLD_HOST)
    data = dataclasses.replace(SSDP, upnp={**SSDP.upnp, "UDN": "uuid:unknown"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_SSDP}, data=data
    )
    assert result["type"] is FlowResultType.ABORT
    assert entry.data[CONF_HOST] == OLD_HOST


async def test_setup_rejects_a_different_speaker(
    hass: HomeAssistant, speaker, cast
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="00000000-0000-4000-8000-00000000dead",
        data={CONF_HOST: HOST},
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
