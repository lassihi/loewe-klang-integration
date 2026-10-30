"""Config flow for Loewe klang speakers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from .const import CONF_CAST_UUID, CONF_SPOTIFY_ENTITY, CONF_UDN, DOMAIN
from .upnp import DeviceInfo, LoeweError, LoeweSpeaker


class LoeweKlangConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._device: DeviceInfo | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return LoeweKlangOptionsFlow()

    async def _async_probe(self, host: str) -> DeviceInfo:
        return await LoeweSpeaker(
            host, async_get_clientsession(self.hass)
        ).async_get_device_info()

    def _entry_data(self, device: DeviceInfo) -> dict[str, Any]:
        return {
            CONF_HOST: device.host,
            CONF_UDN: device.udn,
            CONF_CAST_UUID: device.cast_uuid,
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                device = await self._async_probe(host)
            except LoeweError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(device.udn)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                return self.async_create_entry(
                    title=device.name, data=self._entry_data(device)
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            errors=errors,
        )

    async def async_step_ssdp(
        self, discovery_info: SsdpServiceInfo
    ) -> ConfigFlowResult:
        host = urlparse(discovery_info.ssdp_location or "").hostname
        if not host:
            return self.async_abort(reason="cannot_connect")
        udn = (
            discovery_info.upnp.get("UDN") or discovery_info.ssdp_udn or ""
        ).removeprefix("uuid:")
        if udn:
            await self.async_set_unique_id(udn)
            self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        try:
            self._device = await self._async_probe(host)
        except LoeweError:
            return self.async_abort(reason="not_loewe")
        await self.async_set_unique_id(self._device.udn)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        self.context["title_placeholders"] = {"name": self._device.name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._device is not None
        if user_input is not None:
            return self.async_create_entry(
                title=self._device.name, data=self._entry_data(self._device)
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": self._device.name,
                "host": self._device.host,
            },
        )


class LoeweKlangOptionsFlow(OptionsFlow):
    """Options: pick the Spotify entity that supplies now-playing for Spotify Connect."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        schema = vol.Schema(
            {
                vol.Optional(CONF_SPOTIFY_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain="media_player", integration="spotify")
                )
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, self.config_entry.options
            ),
        )
