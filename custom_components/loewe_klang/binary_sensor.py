"""Update-available binary sensor for a Loewe klang speaker."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LoeweConfigEntry, LoeweCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LoeweConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the update-available binary sensor."""
    async_add_entities([LoeweUpdateAvailable(entry)])


class LoeweUpdateAvailable(CoordinatorEntity[LoeweCoordinator], BinarySensorEntity):
    """Reports the Cast platform's firmware update-available flag.

    This reflects the Chromecast built-in firmware (``has_update`` from the
    device's eureka_info), which updates itself over the air. The Play-Fi
    firmware is still updated through the Play-Fi app or a USB stick.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "update_available"
    _attr_device_class = BinarySensorDeviceClass.UPDATE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: LoeweConfigEntry) -> None:
        super().__init__(entry.runtime_data.coordinator)
        dev = entry.runtime_data.device
        self._attr_unique_id = f"{dev.udn}_update_available"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dev.udn)}, name=dev.name
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether a Cast firmware update is available."""
        return self.coordinator.data.firmware.update_available

    @property
    def available(self) -> bool:
        """Only available once the update flag has been read."""
        return (
            super().available
            and self.coordinator.data.firmware.update_available is not None
        )
