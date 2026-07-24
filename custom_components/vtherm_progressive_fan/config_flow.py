# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Config flow for VTherm Progressive Fan."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_CLIMATE_ENTITY_ID,
    CONF_CURRENT_TEMPERATURE_SENSOR,
    CONF_FAN_MODE_ORDER,
    CONF_VTHERM_ENTITY_ID,
    CONFIG_ENTRY_VERSION,
    DEFAULT_FAN_MODE_ORDER,
    DOMAIN,
)

# Modes we never want in the boost sequence — "auto" is reserved as the
# hand-back signal, and vendor-specific labels like "on" don't map to a speed.
_EXCLUDED_MODES = {"auto", "on", "off"}


def _entities_schema(
    vtherm_default: str = "",
    climate_default: str = "",
    current_sensor_default: str = "",
) -> vol.Schema:
    """Step 1 schema: pick the VTherm and the underlying climate entity.

    `current_temperature_sensor` is optional — leave empty to use the
    underlying climate's own current_temperature attribute (default);
    set to point at a smoothed sensor (e.g. the same one VTherm regulates
    from) to avoid the 0.5°F quantization noise the mini-split reports.
    """
    return vol.Schema(
        {
            vol.Required(
                CONF_VTHERM_ENTITY_ID,
                default=vtherm_default or vol.UNDEFINED,
            ): EntitySelector(EntitySelectorConfig(domain=CLIMATE_DOMAIN)),
            vol.Required(
                CONF_CLIMATE_ENTITY_ID,
                default=climate_default or vol.UNDEFINED,
            ): EntitySelector(EntitySelectorConfig(domain=CLIMATE_DOMAIN)),
            vol.Optional(
                CONF_CURRENT_TEMPERATURE_SENSOR,
                default=current_sensor_default or vol.UNDEFINED,
            ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
        }
    )


def _default_order_for(climate_fan_modes: list[str]) -> list[str]:
    """Return a sensible default order: quietest → strongest, excluding auto/on.

    Uses DEFAULT_FAN_MODE_ORDER as the canonical ordering and keeps only
    modes the underlying actually exposes.
    """
    lower_modes = {mode.strip().lower() for mode in climate_fan_modes}
    ordered = [
        mode
        for mode in DEFAULT_FAN_MODE_ORDER
        if mode in lower_modes and mode not in _EXCLUDED_MODES
    ]
    # Append any modes the underlying has that DEFAULT_FAN_MODE_ORDER didn't
    # know about (e.g. "very high"), so the user sees them by default.
    for mode in climate_fan_modes:
        norm = mode.strip().lower()
        if norm not in ordered and norm not in _EXCLUDED_MODES:
            ordered.append(norm)
    return ordered


def _fan_modes_schema(
    climate_fan_modes: list[str],
    default_order: list[str],
) -> vol.Schema:
    """Step 2 schema: multi-select the fan modes to use, in selection order."""
    # Present modes in lowercase, matching how fan_controller normalizes.
    options = [
        mode.strip().lower()
        for mode in climate_fan_modes
        if mode.strip() and mode.strip().lower() not in _EXCLUDED_MODES
    ]
    # Keep the default order as-is; SelectSelector preserves selection order.
    return vol.Schema(
        {
            vol.Required(
                CONF_FAN_MODE_ORDER,
                default=default_order,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    sort=False,
                    mode=SelectSelectorMode.DROPDOWN,
                    custom_value=True,
                )
            ),
        }
    )


def _clean_modes(raw: Any) -> list[str]:
    """Normalize a submitted fan-mode selection to lowercase, no blanks."""
    return [str(m).strip().lower() for m in (raw or []) if str(m).strip()]


def _validate_entities(vtherm_entity_id: str, climate_entity_id: str) -> str | None:
    """Return an error key for the entity pair, or None if it's valid."""
    if not vtherm_entity_id or not climate_entity_id:
        return "missing_entity"
    if vtherm_entity_id == climate_entity_id:
        return "same_entity"
    return None


def _validate_fan_modes(fan_mode_order: list[str], available: list[str]) -> str | None:
    """Return an error key for the mode selection, or None if it's valid.

    `custom_value=True` on the selector lets users type free text, so a typo
    would otherwise persist and silently never match the underlying.
    """
    if not fan_mode_order:
        return "no_modes_selected"
    available_lower = {m.strip().lower() for m in available}
    if any(m not in available_lower for m in fan_mode_order):
        return "unknown_modes"
    return None


def _entry_value(
    entry: config_entries.ConfigEntry | None, key: str, default: Any = None
) -> Any:
    """Read a config value preferring `options` over `data`.

    Mirrors the precedence `async_setup_entry` uses, so the forms always show
    the value that is actually in effect. `entry` is None while creating a
    new entry, where every field simply starts empty.
    """
    if entry is None:
        return default
    return entry.options.get(key, entry.data.get(key, default))


def _read_climate_fan_modes(hass, climate_entity_id: str) -> list[str]:
    """Read live fan_modes attribute from the underlying climate."""
    state = hass.states.get(climate_entity_id)
    if state is None:
        return []
    return list(state.attributes.get("fan_modes") or [])


@config_entries.HANDLERS.register(DOMAIN)
class VThermProgressiveFanConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for this integration."""

    VERSION = CONFIG_ENTRY_VERSION

    def __init__(self) -> None:
        self._vtherm_entity_id: str | None = None
        self._climate_entity_id: str | None = None
        self._current_temperature_sensor: str = ""
        # Set only on the reconfigure path; decides how step 2 terminates.
        self._reconfigure_entry: config_entries.ConfigEntry | None = None

    # Create and reconfigure ask for exactly the same four values, so they
    # share both steps and differ only in how the result is persisted. Step 2
    # has to be a separate step because its options come from the climate
    # entity chosen in step 1.
    async def async_step_user(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1 (create): pick VTherm, underlying climate, optional sensor."""
        return await self._async_entities_step(user_input, step_id="user")

    async def async_step_reconfigure(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1 (reconfigure): same form, prefilled from the existing entry."""
        self._reconfigure_entry = self._get_reconfigure_entry()
        return await self._async_entities_step(user_input, step_id="reconfigure")

    async def _async_entities_step(
        self, user_input: Mapping[str, Any] | None, step_id: str
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._vtherm_entity_id = str(user_input[CONF_VTHERM_ENTITY_ID]).strip()
            self._climate_entity_id = str(user_input[CONF_CLIMATE_ENTITY_ID]).strip()
            self._current_temperature_sensor = str(
                user_input.get(CONF_CURRENT_TEMPERATURE_SENSOR, "")
            ).strip()
            error = _validate_entities(self._vtherm_entity_id, self._climate_entity_id)
            if error:
                errors["base"] = error
            else:
                return await self.async_step_fan_modes()

        entry = self._reconfigure_entry
        return self.async_show_form(
            step_id=step_id,
            data_schema=_entities_schema(
                vtherm_default=_entry_value(entry, CONF_VTHERM_ENTITY_ID, ""),
                climate_default=_entry_value(entry, CONF_CLIMATE_ENTITY_ID, ""),
                current_sensor_default=_entry_value(
                    entry, CONF_CURRENT_TEMPERATURE_SENSOR, ""
                ),
            ),
            errors=errors,
        )

    async def async_step_fan_modes(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: multi-select fan_mode_order from the underlying's fan_modes."""
        errors: dict[str, str] = {}
        climate_fan_modes = _read_climate_fan_modes(
            self.hass, self._climate_entity_id or ""
        )
        entry = self._reconfigure_entry
        current_order = _clean_modes(_entry_value(entry, CONF_FAN_MODE_ORDER, []))
        available = {m.strip().lower() for m in climate_fan_modes}
        # Keep the order already in effect for modes the underlying still
        # exposes, then append anything new it has gained.
        default_order = [m for m in current_order if m in available] or []
        for mode in _default_order_for(climate_fan_modes) or DEFAULT_FAN_MODE_ORDER:
            if mode not in default_order:
                default_order.append(mode)

        if user_input is not None:
            fan_mode_order = _clean_modes(user_input.get(CONF_FAN_MODE_ORDER))
            error = _validate_fan_modes(fan_mode_order, climate_fan_modes)
            if error:
                errors["base"] = error
            else:
                payload = {
                    CONF_VTHERM_ENTITY_ID: self._vtherm_entity_id,
                    CONF_CLIMATE_ENTITY_ID: self._climate_entity_id,
                    CONF_FAN_MODE_ORDER: fan_mode_order,
                    CONF_CURRENT_TEMPERATURE_SENSOR: self._current_temperature_sensor,
                }
                if entry is not None:
                    # Write both stores. Setup reads options-then-data, so
                    # writing `data` alone would leave an earlier options-flow
                    # submission shadowing these values — the reconfigure would
                    # appear to save and then silently do nothing.
                    return self.async_update_reload_and_abort(
                        entry, data_updates=payload, options=payload
                    )
                return self.async_create_entry(
                    title=f"VTherm Progressive Fan - {self._vtherm_entity_id}",
                    data=payload,
                )

        return self.async_show_form(
            step_id="fan_modes",
            data_schema=_fan_modes_schema(climate_fan_modes, default_order),
            errors=errors,
            description_placeholders={
                "climate_entity": self._climate_entity_id or "",
                "detected_modes": ", ".join(climate_fan_modes) or "(none)",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> VThermProgressiveFanOptionsFlowHandler:
        return VThermProgressiveFanOptionsFlowHandler()


class VThermProgressiveFanOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow: re-pick entities + reorder fan modes.

    Plain OptionsFlow — `self.config_entry` is provided by HA. The
    OptionsFlowWithConfigEntry base is documented as "being phased out, and
    should not be referenced in new code".
    """

    async def async_step_init(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self.config_entry
        climate_default = str(_entry_value(entry, CONF_CLIMATE_ENTITY_ID, "")).strip()
        vtherm_default = str(_entry_value(entry, CONF_VTHERM_ENTITY_ID, "")).strip()
        current_sensor_default = str(
            _entry_value(entry, CONF_CURRENT_TEMPERATURE_SENSOR, "")
        ).strip()
        current_order = list(
            _entry_value(entry, CONF_FAN_MODE_ORDER, DEFAULT_FAN_MODE_ORDER)
        )

        climate_fan_modes = _read_climate_fan_modes(self.hass, climate_default)
        if not climate_fan_modes:
            # Fall back to a synthetic list from current_order so the picker
            # still renders when the underlying is offline.
            climate_fan_modes = list(current_order) or list(DEFAULT_FAN_MODE_ORDER)

        # Same two builders the config flow uses, merged into one form.
        combined_schema = vol.Schema(
            {
                **_entities_schema(
                    vtherm_default=vtherm_default,
                    climate_default=climate_default,
                    current_sensor_default=current_sensor_default,
                ).schema,
                **_fan_modes_schema(
                    climate_fan_modes,
                    current_order or _default_order_for(climate_fan_modes),
                ).schema,
            }
        )

        if user_input is not None:
            vtherm_entity_id = str(user_input[CONF_VTHERM_ENTITY_ID]).strip()
            climate_entity_id = str(user_input[CONF_CLIMATE_ENTITY_ID]).strip()
            current_temperature_sensor = str(
                user_input.get(CONF_CURRENT_TEMPERATURE_SENSOR, "")
            ).strip()
            fan_mode_order = _clean_modes(user_input.get(CONF_FAN_MODE_ORDER))
            error = _validate_entities(vtherm_entity_id, climate_entity_id) or (
                _validate_fan_modes(fan_mode_order, climate_fan_modes)
            )
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_VTHERM_ENTITY_ID: vtherm_entity_id,
                        CONF_CLIMATE_ENTITY_ID: climate_entity_id,
                        CONF_FAN_MODE_ORDER: fan_mode_order,
                        CONF_CURRENT_TEMPERATURE_SENSOR: current_temperature_sensor,
                    },
                )

        return self.async_show_form(
            step_id="init",
            data_schema=combined_schema,
            errors=errors,
        )
