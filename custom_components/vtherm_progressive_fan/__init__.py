# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""VTherm Progressive Fan.

External integration for Versatile Thermostat using vtherm_api.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.climate import DATA_COMPONENT as CLIMATE_DATA_COMPONENT
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from vtherm_api import VThermAPI

from .const import (
    CONF_CLIMATE_ENTITY_ID,
    CONF_CURRENT_TEMPERATURE_SENSOR,
    CONF_FAN_MODE_ORDER,
    CONF_VTHERM_ENTITY_ID,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
)
from .fan_controller import VThermProgressiveFanPlugin

PLATFORMS: list[Platform] = [Platform.SWITCH]

_LOGGER = logging.getLogger(__name__)


def entry_value(entry: ConfigEntry, key: str, default: Any = None) -> Any:
    """Read a config value, preferring `options` over `data`.

    Both flows can write every key: the options flow writes `options`, and
    reconfigure writes `data` *and* mirrors into `options`. Reading through a
    single helper keeps the precedence identical everywhere — an earlier
    revision read the entity IDs from `data` only, so options-flow edits to
    them silently did nothing and the plugin kept driving the old entity.
    """
    return entry.options.get(key, entry.data.get(key, default))


@dataclass(slots=True)
class VThermProgressiveFanRuntimeData:
    """Runtime data stored for a config entry."""

    plugin: VThermProgressiveFanPlugin


# ConfigEntry is generic over its runtime_data, so annotating with this alias
# makes `entry.runtime_data.plugin` statically known and removes the need for
# defensive getattr() at every read site.
type VThermConfigEntry = ConfigEntry[VThermProgressiveFanRuntimeData]

# Config-entry only: there is no YAML schema for this integration.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup_entry(hass: HomeAssistant, entry: VThermConfigEntry) -> bool:
    """Set up VTherm Progressive Fan from a config entry."""

    # Raise (don't return False) so HA schedules a retry with backoff.
    # Returning False lands the entry in SETUP_ERROR, which cancels retry
    # outright — and both of these failures are ordinary startup races:
    # `dependencies` guarantees the versatile_thermostat *component* is set
    # up, not that its *config entries* have finished adding their climate
    # entities. Losing that race used to kill the integration until the user
    # noticed and manually reloaded.
    api = VThermAPI.get_vtherm_api(hass)
    if api is None:
        raise ConfigEntryNotReady(
            f"VThermAPI is not available yet for {DOMAIN}; will retry"
        )

    vtherm_entity_id = str(entry_value(entry, CONF_VTHERM_ENTITY_ID, ""))
    climate_entity_id = str(entry_value(entry, CONF_CLIMATE_ENTITY_ID, ""))
    fan_mode_order = entry_value(entry, CONF_FAN_MODE_ORDER, [])
    current_temperature_sensor_entity_id = entry_value(
        entry, CONF_CURRENT_TEMPERATURE_SENSOR, ""
    )
    if not vtherm_entity_id or not climate_entity_id:
        _LOGGER.error(
            "Entry %s is missing the VTherm or climate entity id; reconfigure it",
            entry.entry_id,
        )
        return False

    _LOGGER.info(
        "Setting up %s for VTherm=%s climate=%s current_source=%s modes=%s",
        DOMAIN,
        vtherm_entity_id,
        climate_entity_id,
        current_temperature_sensor_entity_id or "(from climate entity)",
        fan_mode_order,
    )

    plugin = VThermProgressiveFanPlugin(
        hass,
        climate_entity_id,
        fan_mode_order,
        current_temperature_sensor_entity_id=current_temperature_sensor_entity_id,
    )
    vtherm = hass.data[CLIMATE_DATA_COMPONENT].get_entity(vtherm_entity_id)
    if vtherm is None:
        raise ConfigEntryNotReady(
            f"VTherm climate entity {vtherm_entity_id} is not available yet; will retry"
        )

    plugin.link_to_vtherm(vtherm)

    entry.runtime_data = VThermProgressiveFanRuntimeData(plugin=plugin)

    # Reload the entry when its options change so the running plugin picks
    # up the new current_temperature_sensor / fan_mode_order without an HA
    # restart. Without this, options-flow submissions silently persist to
    # storage but leave the live plugin bound to its stale config.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    # HA only runs on-unload callbacks when async_unload_entry returned True,
    # so registering the teardown here removes the need to hand-check it.
    entry.async_on_unload(plugin.remove_listeners)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries forward.

    Without this handler HA logs "Migration handler not found" and fails
    setup permanently for any entry created before the current
    ``ConfigFlow.VERSION`` — leaving the user no path but delete-and-recreate.

    v1 → v2: v1 predates the optional smoothed current-temperature sensor, so
    the only change is to record the key with its "use the climate entity's
    own reading" default. Everything else carries over unchanged.
    """
    if entry.version > CONFIG_ENTRY_VERSION:
        # Downgrade — we can't know what a future schema means.
        _LOGGER.error(
            "Entry %s is version %s but this build only understands up to %s; "
            "downgrade the integration or recreate the entry",
            entry.entry_id,
            entry.version,
            CONFIG_ENTRY_VERSION,
        )
        return False

    if entry.version == 1:
        _LOGGER.info("Migrating %s entry %s from v1 to v2", DOMAIN, entry.entry_id)
        data = {**entry.data}
        data.setdefault(CONF_CURRENT_TEMPERATURE_SENSOR, "")
        hass.config_entries.async_update_entry(
            entry, data=data, version=CONFIG_ENTRY_VERSION
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: VThermConfigEntry) -> bool:
    """Unload the config entry. Listener teardown is an on-unload callback."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
