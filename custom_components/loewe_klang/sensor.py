"""Firmware version sensors for a Loewe klang speaker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LoeweConfigEntry, LoeweCoordinator
from .upnp import FirmwareInfo


@dataclass(frozen=True, kw_only=True)
class LoeweSensorDescription(SensorEntityDescription):
    """Describes a firmware sensor and how to read it from the state."""

    value_fn: Callable[[FirmwareInfo], str | None]


SENSORS: tuple[LoeweSensorDescription, ...] = (
    LoeweSensorDescription(
        key="playfi_firmware",
        translation_key="playfi_firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda fw: fw.playfi,
    ),
    LoeweSensorDescription(
        key="cast_firmware",
        translation_key="cast_firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda fw: fw.cast,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LoeweConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the firmware sensors."""
    async_add_entities(LoeweFirmwareSensor(entry, desc) for desc in SENSORS)


class LoeweFirmwareSensor(CoordinatorEntity[LoeweCoordinator], SensorEntity):
    """A diagnostic sensor exposing a firmware version string."""

    _attr_has_entity_name = True
    entity_description: LoeweSensorDescription

    def __init__(
        self, entry: LoeweConfigEntry, description: LoeweSensorDescription
    ) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self.entity_description = description
        dev = entry.runtime_data.device
        self._attr_unique_id = f"{dev.udn}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dev.udn)}, name=dev.name
        )

    @property
    def native_value(self) -> str | None:
        """Return the firmware version."""
        return self.entity_description.value_fn(self.coordinator.data.firmware)
