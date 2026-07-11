"""VTherm Progressive Fan.

External integration for Versatile Thermostat using vtherm_api.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from vtherm_api import VThermAPI

from .const import (
    CONF_CLIMATE_ENTITY_ID,
    CONF_CURRENT_TEMPERATURE_SENSOR,
    CONF_FAN_MODE_ORDER,
    CONF_VTHERM_ENTITY_ID,
    DOMAIN,
)
from .fan_controller import VThermProgressiveFanPlugin

PLATFORMS: list[str] = ["switch"]

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class VThermProgressiveFanRuntimeData:
    """Runtime data stored for a config entry."""

    plugin: VThermProgressiveFanPlugin



async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration from YAML if needed."""

    hass.data.setdefault(DOMAIN, {})
    _LOGGER.debug("async_setup called for %s", DOMAIN)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up VTherm Progressive Fan from a config entry."""

    hass.data.setdefault(DOMAIN, {})

    api = VThermAPI.get_vtherm_api(hass)
    if api is None:
        _LOGGER.error("VThermAPI is not available; cannot initialize %s", DOMAIN)
        return False

    vtherm_entity_id = str(entry.data[CONF_VTHERM_ENTITY_ID])
    climate_entity_id = str(entry.data[CONF_CLIMATE_ENTITY_ID])
    fan_mode_order = entry.options.get(
        CONF_FAN_MODE_ORDER,
        entry.data.get(CONF_FAN_MODE_ORDER, []),
    )
    current_temperature_sensor_entity_id = entry.options.get(
        CONF_CURRENT_TEMPERATURE_SENSOR,
        entry.data.get(CONF_CURRENT_TEMPERATURE_SENSOR, ""),
    )

    _LOGGER.info(
        "Setting up %s for VTherm=%s climate=%s current_source=%s",
        DOMAIN,
        vtherm_entity_id,
        climate_entity_id,
        current_temperature_sensor_entity_id or "(from climate entity)",
    )
    _LOGGER.debug("Config values for %s: fan_mode_order=%s", entry.entry_id, fan_mode_order)

    plugin = VThermProgressiveFanPlugin(
        hass,
        climate_entity_id,
        fan_mode_order,
        current_temperature_sensor_entity_id=current_temperature_sensor_entity_id,
    )
    vtherm = _get_climate_entity(hass, vtherm_entity_id)
    if vtherm is None:
        _LOGGER.error("Unable to find VTherm climate entity: %s", vtherm_entity_id)
        return False

    plugin.link_to_vtherm(vtherm)

    runtime = VThermProgressiveFanRuntimeData(plugin=plugin)
    entry.runtime_data = runtime
    hass.data[DOMAIN][entry.entry_id] = runtime

    # Reload the entry when its options change so the running plugin picks
    # up the new current_temperature_sensor / fan_mode_order without an HA
    # restart. Without this, options-flow submissions silently persist to
    # storage but leave the live plugin bound to its stale config.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _cleanup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove runtime objects registered for an entry."""

    stored = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    runtime = stored or getattr(entry, "runtime_data", None)
    if runtime is None:
        return

    _LOGGER.info("Cleaning up %s for entry=%s", DOMAIN, entry.entry_id)
    runtime.plugin.remove_listeners()
    entry.runtime_data = None


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry and remove listeners."""

    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    _cleanup_entry(hass, entry)
    if not hass.data.get(DOMAIN):
        hass.data.pop(DOMAIN, None)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle entry removal from Home Assistant."""

    _LOGGER.info("Removing %s for entry=%s", DOMAIN, entry.entry_id)
    _cleanup_entry(hass, entry)
    if not hass.data.get(DOMAIN):
        hass.data.pop(DOMAIN, None)

async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry,
) -> bool:
    """Opt-in support for deleting the device from the UI."""
    return True


def _get_climate_entity(hass: HomeAssistant, entity_id: str) -> Any | None:
    component = hass.data.get("climate")
    if component is None:
        return None
    return component.get_entity(entity_id)
