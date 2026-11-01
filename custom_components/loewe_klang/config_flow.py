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
from homeassistant.data_entry_flow import AbortFlow
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
        self._move_entry_id: str | None = None
        self._move_from: str | None = None
        self._move_to: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return LoeweKlangOptionsFlow()

    async def _async_probe(self, host: str) -> DeviceInfo:
        return await LoeweSpeaker(
            host, async_get_clientsession(self.hass)
        ).async_get_device_info()

    async def _async_answers_as(self, host: str, udn: str) -> bool:
        try:
            return (await self._async_probe(host)).udn == udn
        except LoeweError:
            return False

    async def _async_moved_entry(self, udn: str, host: str) -> ConfigEntry | None:
        """Return the entry for a known speaker that moved to host, else abort.

        Returns None if the speaker is not configured. SSDP is unauthenticated
        and a speaker's UDN is public, so even a speaker that passes these checks
        moves only once the user confirms: the configured host must have stopped
        answering as this speaker, and the new one must answer as it.
        """
        entry = await self.async_set_unique_id(udn)
        if entry is None:
            return None
        current = entry.data.get(CONF_HOST)
        if (
            not current
            or current == host
            or await self._async_answers_as(current, udn)
            or not await self._async_answers_as(host, udn)
        ):
            raise AbortFlow("already_configured")
        return entry

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
        if udn and (entry := await self._async_moved_entry(udn, host)):
            return await self._async_confirm_move(entry, host)
        try:
            self._device = await self._async_probe(host)
        except LoeweError:
            return self.async_abort(reason="not_loewe")
        if entry := await self._async_moved_entry(self._device.udn, host):
            return await self._async_confirm_move(entry, host)
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

    async def _async_confirm_move(
        self, entry: ConfigEntry, host: str
    ) -> ConfigFlowResult:
        self._move_entry_id = entry.entry_id
        self._move_from = entry.data[CONF_HOST]
        self._move_to = host
        self.context["title_placeholders"] = {"name": entry.title}
        return await self.async_step_confirm_move()

    async def async_step_confirm_move(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask before pointing an entry at the host a discovery announced."""
        assert self._move_entry_id and self._move_from and self._move_to
        entry = self.hass.config_entries.async_get_entry(self._move_entry_id)
        if entry is None or entry.data.get(CONF_HOST) != self._move_from:
            return self.async_abort(reason="already_configured")
        if user_input is None:
            self._set_confirm_only()
            return self.async_show_form(
                step_id="confirm_move",
                description_placeholders={
                    "name": entry.title,
                    "old_host": self._move_from,
                    "new_host": self._move_to,
                },
            )
        # The form may have been open for a while, so check both hosts again.
        udn = entry.unique_id or ""
        if await self._async_answers_as(self._move_from, udn):
            return self.async_abort(reason="already_configured")
        if not await self._async_answers_as(self._move_to, udn):
            return self.async_abort(reason="cannot_connect")
        # A loaded entry's update listener reloads it. One whose setup failed,
        # the usual state for a speaker that moved, has no listener registered.
        reload_needed = not entry.update_listeners
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HOST: self._move_to}
        )
        if reload_needed:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason="speaker_moved")


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
