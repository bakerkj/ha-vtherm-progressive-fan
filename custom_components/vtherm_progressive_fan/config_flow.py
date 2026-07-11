"""Config flow for VTherm Progressive Fan."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
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


def _read_climate_fan_modes(hass, climate_entity_id: str) -> list[str]:
    """Read live fan_modes attribute from the underlying climate."""
    state = hass.states.get(climate_entity_id)
    if state is None:
        return []
    return list(state.attributes.get("fan_modes") or [])


@config_entries.HANDLERS.register(DOMAIN)
class VThermProgressiveFanConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for this integration."""

    VERSION = 2

    def __init__(self) -> None:
        self._vtherm_entity_id: str | None = None
        self._climate_entity_id: str | None = None
        self._current_temperature_sensor: str = ""

    async def async_step_user(
        self, user_input: Mapping[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: pick VTherm and underlying climate."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._vtherm_entity_id = str(user_input[CONF_VTHERM_ENTITY_ID]).strip()
            self._climate_entity_id = str(user_input[CONF_CLIMATE_ENTITY_ID]).strip()
            self._current_temperature_sensor = str(
                user_input.get(CONF_CURRENT_TEMPERATURE_SENSOR, "")
            ).strip()
            if not self._vtherm_entity_id or not self._climate_entity_id:
                errors["base"] = "missing_entity"
            elif self._vtherm_entity_id == self._climate_entity_id:
                errors["base"] = "same_entity"
            else:
                return await self.async_step_fan_modes()

        return self.async_show_form(
            step_id="user",
            data_schema=_entities_schema(),
            errors=errors,
        )

    async def async_step_fan_modes(
        self, user_input: Mapping[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: multi-select fan_mode_order from the underlying's fan_modes."""
        errors: dict[str, str] = {}
        climate_fan_modes = _read_climate_fan_modes(
            self.hass, self._climate_entity_id or ""
        )
        default_order = _default_order_for(climate_fan_modes) or list(
            DEFAULT_FAN_MODE_ORDER
        )

        if user_input is not None:
            fan_mode_order = [
                str(m).strip().lower()
                for m in user_input.get(CONF_FAN_MODE_ORDER, [])
                if str(m).strip()
            ]
            available_lower = {m.strip().lower() for m in climate_fan_modes}
            unknown = [m for m in fan_mode_order if m not in available_lower]
            if not fan_mode_order:
                errors["base"] = "no_modes_selected"
            elif unknown:
                errors["base"] = "unknown_modes"
            else:
                return self.async_create_entry(
                    title=f"VTherm Progressive Fan - {self._vtherm_entity_id}",
                    data={
                        CONF_VTHERM_ENTITY_ID: self._vtherm_entity_id,
                        CONF_CLIMATE_ENTITY_ID: self._climate_entity_id,
                        CONF_FAN_MODE_ORDER: fan_mode_order,
                        CONF_CURRENT_TEMPERATURE_SENSOR: self._current_temperature_sensor,
                    },
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

    async def async_step_reconfigure(
        self, user_input: Mapping[str, Any] | None = None
    ) -> FlowResult:
        """Reconfigure: reuse step 1, remembering current values as defaults."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            self._vtherm_entity_id = str(user_input[CONF_VTHERM_ENTITY_ID]).strip()
            self._climate_entity_id = str(user_input[CONF_CLIMATE_ENTITY_ID]).strip()
            self._current_temperature_sensor = str(
                user_input.get(CONF_CURRENT_TEMPERATURE_SENSOR, "")
            ).strip()
            if not self._vtherm_entity_id or not self._climate_entity_id:
                errors["base"] = "missing_entity"
            elif self._vtherm_entity_id == self._climate_entity_id:
                errors["base"] = "same_entity"
            else:
                climate_fan_modes = _read_climate_fan_modes(
                    self.hass, self._climate_entity_id
                )
                # Preserve existing order for modes still available; append any new ones.
                current_order = list(
                    entry.options.get(
                        CONF_FAN_MODE_ORDER,
                        entry.data.get(CONF_FAN_MODE_ORDER, []),
                    )
                )
                available = {m.strip().lower() for m in climate_fan_modes}
                preserved = [m for m in current_order if m in available]
                for m in _default_order_for(climate_fan_modes):
                    if m not in preserved:
                        preserved.append(m)
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_VTHERM_ENTITY_ID: self._vtherm_entity_id,
                        CONF_CLIMATE_ENTITY_ID: self._climate_entity_id,
                        CONF_FAN_MODE_ORDER: preserved,
                        CONF_CURRENT_TEMPERATURE_SENSOR: self._current_temperature_sensor,
                    },
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_entities_schema(
                vtherm_default=str(entry.data.get(CONF_VTHERM_ENTITY_ID, "")),
                climate_default=str(entry.data.get(CONF_CLIMATE_ENTITY_ID, "")),
                current_sensor_default=str(
                    entry.data.get(CONF_CURRENT_TEMPERATURE_SENSOR, "")
                ),
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "VThermProgressiveFanOptionsFlowHandler":
        return VThermProgressiveFanOptionsFlowHandler(config_entry)


class VThermProgressiveFanOptionsFlowHandler(config_entries.OptionsFlowWithConfigEntry):
    """Options flow: re-pick entities + reorder fan modes."""

    async def async_step_init(
        self, user_input: Mapping[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self.config_entry
        climate_default = str(entry.data.get(CONF_CLIMATE_ENTITY_ID, "")).strip()
        vtherm_default = str(entry.data.get(CONF_VTHERM_ENTITY_ID, "")).strip()
        current_sensor_default = str(
            entry.options.get(
                CONF_CURRENT_TEMPERATURE_SENSOR,
                entry.data.get(CONF_CURRENT_TEMPERATURE_SENSOR, ""),
            )
        ).strip()
        current_order = list(
            entry.options.get(
                CONF_FAN_MODE_ORDER,
                entry.data.get(CONF_FAN_MODE_ORDER, DEFAULT_FAN_MODE_ORDER),
            )
        )

        climate_fan_modes = _read_climate_fan_modes(self.hass, climate_default)
        if not climate_fan_modes:
            # Fall back to a synthetic list from current_order so the picker
            # still renders when the underlying is offline.
            climate_fan_modes = list(current_order) or list(DEFAULT_FAN_MODE_ORDER)

        combined_schema = vol.Schema(
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
                vol.Required(
                    CONF_FAN_MODE_ORDER,
                    default=current_order
                    or _default_order_for(climate_fan_modes),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            m.strip().lower()
                            for m in climate_fan_modes
                            if m.strip()
                            and m.strip().lower() not in _EXCLUDED_MODES
                        ],
                        multiple=True,
                        sort=False,
                        mode=SelectSelectorMode.DROPDOWN,
                        custom_value=True,
                    )
                ),
            }
        )

        if user_input is not None:
            vtherm_entity_id = str(user_input[CONF_VTHERM_ENTITY_ID]).strip()
            climate_entity_id = str(user_input[CONF_CLIMATE_ENTITY_ID]).strip()
            current_temperature_sensor = str(
                user_input.get(CONF_CURRENT_TEMPERATURE_SENSOR, "")
            ).strip()
            fan_mode_order = [
                str(m).strip().lower()
                for m in user_input.get(CONF_FAN_MODE_ORDER, [])
                if str(m).strip()
            ]
            available_lower = {m.strip().lower() for m in climate_fan_modes}
            unknown = [m for m in fan_mode_order if m not in available_lower]
            if not vtherm_entity_id or not climate_entity_id:
                errors["base"] = "missing_entity"
            elif vtherm_entity_id == climate_entity_id:
                errors["base"] = "same_entity"
            elif not fan_mode_order:
                errors["base"] = "no_modes_selected"
            elif unknown:
                errors["base"] = "unknown_modes"
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
