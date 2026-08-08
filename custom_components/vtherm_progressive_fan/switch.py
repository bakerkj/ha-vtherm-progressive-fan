# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Master enable/disable switch for a VTherm Progressive Fan config entry.

Each config entry gets one switch entity named "Progressive Fan", attached to
the linked VTherm's device so HA names it "<VTherm name> Progressive Fan".
When on, the plugin controls the underlying climate's fan_mode. When off,
the plugin ignores every event and never writes fan_mode. State is
restored via RestoreEntity, so the switch survives reloads/restarts.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import VThermConfigEntry, entry_value
from .const import CONF_VTHERM_ENTITY_ID

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VThermConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the enable switch for this config entry."""
    # Resolve the VTherm's own device so we can attach our switch to it. Read
    # through the shared helper so an options-flow change to the VTherm entity
    # lands here too.
    vtherm_entity_id = entry_value(entry, CONF_VTHERM_ENTITY_ID, "")
    vtherm_device = (
        async_entity_id_to_device(hass, vtherm_entity_id) if vtherm_entity_id else None
    )

    async_add_entities(
        [ProgressiveFanEnabledSwitch(entry, entry.runtime_data.plugin, vtherm_device)]
    )


class ProgressiveFanEnabledSwitch(SwitchEntity, RestoreEntity):
    """Master on/off for the plugin's control of the underlying fan_mode."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Progressive Fan"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:fan-auto"

    def __init__(
        self, entry: ConfigEntry, plugin, vtherm_device: DeviceEntry | None
    ) -> None:
        self._entry = entry
        self._plugin = plugin
        self._attr_unique_id = f"{entry.entry_id}_enabled"
        # Default ON. Overridden by any restored state in async_added_to_hass.
        self._attr_is_on = True
        # Attach the switch to the VTherm's device so it appears alongside the
        # thermostat's own entities in the UI. Point at the device (device_entry)
        # rather than describe it (device_info): from HA 2026.8 a device_info
        # lookup only matches devices owned by our own config entry, so it would
        # miss VTherm's device and silently create a duplicate.
        if vtherm_device is not None:
            self.device_entry = vtherm_device

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in (STATE_ON, STATE_OFF):
            self._attr_is_on = last.state == STATE_ON
        # Push the restored value into the plugin. First-run default is ON.
        #
        # Deliberately no re-evaluation here. This runs milliseconds after
        # async_setup_entry constructed the plugin, so it is always inside the
        # startup grace and async_apply_now would return without doing
        # anything. The fan settles on the first VTherm event after the grace
        # expires; a genuine post-grace kick would have to be scheduled by the
        # controller, not fired from here.
        self._plugin.set_enabled(self._attr_is_on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self._plugin.set_enabled(True)
        self.async_write_ha_state()
        # Re-evaluate immediately so the fan settles without waiting for
        # the next VTherm event.
        await self._plugin.async_apply_now(reason="switch_turned_on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self._plugin.set_enabled(False)
        self.async_write_ha_state()
