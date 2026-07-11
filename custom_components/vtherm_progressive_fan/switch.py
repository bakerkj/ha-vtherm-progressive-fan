"""Master enable/disable switch for a VTherm Progressive Fan config entry.

Each config entry gets one `switch.<vtherm>_progressive_fan_enabled` entity.
When on, the plugin controls the underlying climate's fan_mode. When off,
the plugin ignores every event and never writes fan_mode. State is
restored via RestoreEntity, so the switch survives reloads/restarts.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_VTHERM_ENTITY_ID, DOMAIN

VTHERM_DOMAIN = "versatile_thermostat"

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the enable switch for this config entry."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id) or getattr(
        entry, "runtime_data", None
    )
    if runtime is None:
        _LOGGER.error(
            "No runtime data for entry %s; enable-switch not created",
            entry.entry_id,
        )
        return

    # Look up the VTherm's config_entry_id so we can attach our switch to
    # that device rather than creating a fresh one.
    vtherm_entity_id = entry.data.get(CONF_VTHERM_ENTITY_ID)
    vtherm_config_entry_id: str | None = None
    if vtherm_entity_id:
        registry = er.async_get(hass)
        vtherm_entry = registry.async_get(vtherm_entity_id)
        if vtherm_entry is not None:
            vtherm_config_entry_id = vtherm_entry.config_entry_id

    async_add_entities([
        ProgressiveFanEnabledSwitch(entry, runtime.plugin, vtherm_config_entry_id)
    ])


class ProgressiveFanEnabledSwitch(SwitchEntity, RestoreEntity):
    """Master on/off for the plugin's control of the underlying fan_mode."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Progressive Fan"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:fan-auto"

    def __init__(self, entry: ConfigEntry, plugin, vtherm_config_entry_id: str | None) -> None:
        self._entry = entry
        self._plugin = plugin
        self._attr_unique_id = f"{entry.entry_id}_enabled"
        # Default ON. Overridden by any restored state in async_added_to_hass.
        self._attr_is_on = True
        # Attach the switch to the VTherm's device so it appears alongside
        # the thermostat's own entities in the UI. VTherm identifies its
        # devices as ("versatile_thermostat", <its_config_entry_id>).
        if vtherm_config_entry_id is not None:
            self._attr_device_info = DeviceInfo(
                identifiers={(VTHERM_DOMAIN, vtherm_config_entry_id)}
            )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"
        # Push the restored value into the plugin. First-run default is ON.
        self._plugin.set_enabled(self._attr_is_on)
        # If we just turned on, kick a re-evaluation so the fan settles
        # without waiting for the next VTherm event.
        if self._attr_is_on:
            await self._plugin.async_apply_now(reason="switch_restored_on")

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
