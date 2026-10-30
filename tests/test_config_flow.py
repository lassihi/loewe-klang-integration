"""Config flow tests."""

from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.loewe_klang.const import DOMAIN

from .conftest import CAST_UUID, HOST, UDN

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


async def test_ssdp_updates_host_of_existing_entry(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=UDN, data={CONF_HOST: "192.0.2.99", "udn": UDN}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_SSDP}, data=SSDP
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == HOST
