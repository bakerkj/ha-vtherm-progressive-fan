# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""VTherm-linked progressive fan controller.

Architecture (see also progressive_fan.py for the pure ladder):

  raw target ─────► EMA (τ=90s) ────┐
                                    ├──► delta ──► Schmitt ladder ─► band_index
  smoothed room sensor ─────────────┘

  band_index changes ─► promote: immediate
                       ─► demote:  multi-step, gated by a 240s dwell
                       ─► set_fan_mode

Demote is the only gated direction. Earlier revisions also carried a promote
dwell, a fast-lane to bypass it, and a write throttle; those layers were each
added for a flap whose real cause turned out to be the underlying reporting
current_temperature quantized to 0.5°F, which the optional smoothed-sensor
override fixes at the source. They mostly cancelled each other out and their
interactions were the source of several bugs, so they were removed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_conversion import TemperatureConverter
from vtherm_api import PluginClimate

from .const import (
    DEFAULT_DEMOTE_DWELL_SECONDS,
    DEFAULT_SENSOR_UNAVAIL_WARN_SECONDS,
    DEFAULT_STARTUP_DELAY_SECONDS,
    DEFAULT_TARGET_EMA_JUMP_THRESHOLD,
    DEFAULT_TARGET_EMA_TAU_SECONDS,
    FAHRENHEIT_TEMPERATURE_MAX,
    FAHRENHEIT_TEMPERATURE_MIN,
)
from .progressive_fan import (
    delta_band,
    normalize_supported_modes,
    select_progressive_index,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FanModeSnapshot:
    current_temperature: float | None
    target_temperature: float | None
    hvac_mode: str | None
    fan_mode: str | None
    # lowercase mode -> the entity's declared spelling. The ladder works in
    # lowercase but climate.set_fan_mode validates against the declared
    # casing ("Very High", not "very high"), so both are needed; one mapping
    # carries them without a second list to keep in step.
    modes: dict[str, str]


class VThermProgressiveFanPlugin(PluginClimate):
    """Subscribe to VTherm events and drive the underlying climate fan mode."""

    def __init__(
        self,
        hass: HomeAssistant,
        climate_entity_id: str,
        fan_mode_order: Iterable[str] | None = None,
        current_temperature_sensor_entity_id: str | None = None,
    ) -> None:
        super().__init__(hass)
        self._climate_entity_id = climate_entity_id
        self._fan_mode_order = list(fan_mode_order or [])
        override = (current_temperature_sensor_entity_id or "").strip()
        self._current_temperature_sensor_entity_id: str | None = override or None

        self._climate_listener_remove: Callable[[], None] | None = None
        self._vtherm_listener_remove: Callable[[], None] | None = None
        self._vtherm_entity_id: str | None = None

        self._enabled: bool = False

        self._last_selected_index: int | None = None
        # Only reset when the band index actually changes — same-band
        # re-commits do not restart the dwell window.
        self._last_band_entered_ts: float | None = None
        self._last_hvac_mode: str | None = None

        # Demote is the only gated direction. Promotion is unrestricted:
        # a room getting further from setpoint should be answered at once,
        # and the observed flapping was never promote-side.
        self._demote_dwell = DEFAULT_DEMOTE_DWELL_SECONDS
        self._dwell_handle = None

        self._startup_ts: float = hass.loop.time()
        self._startup_delay = DEFAULT_STARTUP_DELAY_SECONDS

        self._target_ema: float | None = None
        self._target_ema_last_ts: float | None = None
        self._target_ema_tau = DEFAULT_TARGET_EMA_TAU_SECONDS
        self._target_ema_jump_threshold = DEFAULT_TARGET_EMA_JUMP_THRESHOLD

        self._sensor_missing_since: float | None = None
        self._sensor_unavail_warn = DEFAULT_SENSOR_UNAVAIL_WARN_SECONDS
        self._sensor_warned = False

        # The mode we last commanded, which is NOT the same as the mode the
        # underlying reports: a polled integration lags our write by seconds.
        # Guards against re-issuing an identical command while it catches up.
        self._last_written_mode: str | None = None

        _LOGGER.debug(
            "VThermProgressiveFanPlugin created for climate=%s preferred_order=%s "
            "current_source=%s",
            self._climate_entity_id,
            self._fan_mode_order,
            self._current_temperature_sensor_entity_id or "(from climate entity)",
        )

    # ─── Master enable/disable ──────────────────────────────────────────
    def set_enabled(self, enabled: bool) -> None:
        was_enabled = self._enabled
        self._enabled = bool(enabled)
        _LOGGER.debug(
            "Enabled flag for %s set to %s", self._climate_entity_id, self._enabled
        )
        if self._enabled == was_enabled:
            return
        # Either direction is a clean slate. On re-enable we deliberately do
        # NOT seed a band or start the dwell clock: leaving both unset makes
        # the next decision a cold start, which is both correct (we have no
        # idea what happened while we were off) and immediate. Seeding the
        # clock used to block the first demote for the full dwell, so
        # toggling the switch to force a re-settle did the opposite.
        self._last_selected_index = None
        self._last_band_entered_ts = None
        self._last_hvac_mode = None
        self._last_written_mode = None
        self._target_ema = None
        self._target_ema_last_ts = None
        self._sensor_missing_since = None
        self._sensor_warned = False
        self._cancel_dwell_timer()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def climate_entity_id(self) -> str:
        return self._climate_entity_id

    # ─── VTherm linking / listeners ─────────────────────────────────────
    def link_to_vtherm(self, vtherm: Any) -> None:
        _LOGGER.info(
            "Linking progressive auto-fan to VTherm for climate=%s",
            self._climate_entity_id,
        )
        super().link_to_vtherm(vtherm)
        self._vtherm_entity_id = getattr(vtherm, "entity_id", None)
        self._listen_to_target_climate_state()
        self._listen_to_vtherm_state()

    def _listen_to_vtherm_state(self) -> None:
        # Watches the VTherm's own state so a current-temp drift without a
        # coincident VTherm event still triggers a re-evaluation.
        if self._vtherm_entity_id is None:
            return
        self._vtherm_listener_remove = async_track_state_change_event(
            self._hass,
            [self._vtherm_entity_id],
            self._handle_vtherm_state_change,
        )

    async def _handle_vtherm_state_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        await self.async_apply_now(reason="vtherm_state_change")

    def _listen_to_target_climate_state(self) -> None:
        self._climate_listener_remove = async_track_state_change_event(
            self._hass,
            [self._climate_entity_id],
            self._handle_target_climate_state_change,
        )

    async def _handle_target_climate_state_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        # Ignore updates whose ONLY difference is fan_mode. Two things produce
        # those, and we want neither to trigger a re-decision:
        #   - our own write echoing back
        #   - a polled integration (e.g. mUART) republishing the stale
        #     pre-write value before the hardware acknowledges
        # Everything else we do consume still wakes us: hvac_mode (which flips
        # how the delta's sign is read, and which VTherm's auto_start_stop
        # writes directly), the underlying's regulated target, and its
        # current_temperature when no override sensor is configured.
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]
        if (
            old_state is not None
            and new_state is not None
            and old_state.state == new_state.state
        ):
            old_attrs = dict(old_state.attributes)
            new_attrs = dict(new_state.attributes)
            old_fan = old_attrs.pop("fan_mode", None)
            new_fan = new_attrs.pop("fan_mode", None)
            if old_attrs == new_attrs:
                _LOGGER.debug(
                    "Ignoring fan_mode-only state change for %s (%s -> %s)",
                    self._climate_entity_id,
                    old_fan,
                    new_fan,
                )
                return
        await self.async_apply_now(reason="target_state_change")

    def remove_listeners(self) -> None:
        _LOGGER.debug("Removing listeners for climate=%s", self._climate_entity_id)
        if self._climate_listener_remove is not None:
            self._climate_listener_remove()
            self._climate_listener_remove = None
        if self._vtherm_listener_remove is not None:
            self._vtherm_listener_remove()
            self._vtherm_listener_remove = None
        self._cancel_dwell_timer()
        super().remove_listeners()

    # ─── vtherm_api event hooks ─────────────────────────────────────────
    def _maybe_schedule_apply(self, event: Event) -> None:
        # vtherm_api dispatches from its own thread pool; marshal onto loop.
        future = asyncio.run_coroutine_threadsafe(
            self.async_apply_now(reason=str(event.event_type)),
            self._hass.loop,
        )
        future.add_done_callback(self._log_future_exception)

    def _log_future_exception(self, future) -> None:
        try:
            exc = future.exception()
        except concurrent.futures.CancelledError:
            return
        if exc is not None:
            _LOGGER.exception(
                "Unhandled exception in scheduled apply for %s: %s",
                self._climate_entity_id,
                exc,
                exc_info=exc,
            )

    # ─── EMA helper ─────────────────────────────────────────────────────
    def _update_target_ema(self, target: float, now: float) -> float:
        # Variable-α = 1 - exp(-dt/τ) — irregular sample intervals still
        # decay at the configured time constant. Large steps snap through
        # instead of damping (user intent, not regulation noise).
        if self._target_ema is None or self._target_ema_last_ts is None:
            self._target_ema = target
        elif abs(target - self._target_ema) > self._target_ema_jump_threshold:
            _LOGGER.debug(
                "Target step %.2f°F for %s exceeds jump threshold %.2f°F; snapping EMA",
                target - self._target_ema,
                self._climate_entity_id,
                self._target_ema_jump_threshold,
            )
            self._target_ema = target
        else:
            dt = max(0.0, now - self._target_ema_last_ts)
            alpha = 1.0 - math.exp(-dt / self._target_ema_tau)
            self._target_ema = self._target_ema + alpha * (target - self._target_ema)
        self._target_ema_last_ts = now
        return self._target_ema

    def _skip(self, reason: str, *args: Any) -> None:
        """Log why a decision was abandoned. Every caller returns None after."""
        _LOGGER.debug(
            "Skipping auto-fan for %s: " + reason, self._climate_entity_id, *args
        )

    # ─── Core decision + apply ──────────────────────────────────────────
    async def async_apply_now(self, reason: str | None = None) -> str | None:
        if not self._enabled:
            self._skip("disabled")
            return None

        now = self._hass.loop.time()

        elapsed_since_start = now - self._startup_ts
        if elapsed_since_start < self._startup_delay:
            self._skip(
                "startup grace (%.1fs of %.1fs)",
                elapsed_since_start,
                self._startup_delay,
            )
            return None

        snapshot = self._build_snapshot()

        # Sensor-loss handling: hold the current fan when we can't compute delta.
        if snapshot.current_temperature is None or snapshot.target_temperature is None:
            if self._sensor_missing_since is None:
                self._sensor_missing_since = now
                self._sensor_warned = False
            elif (
                not self._sensor_warned
                and now - self._sensor_missing_since > self._sensor_unavail_warn
            ):
                _LOGGER.warning(
                    "Auto-fan for %s: current/target unavailable for %.0f min; holding",
                    self._climate_entity_id,
                    (now - self._sensor_missing_since) / 60,
                )
                self._sensor_warned = True
            self._skip("missing temperatures")
            return None
        if self._sensor_missing_since is not None:
            _LOGGER.debug(
                "Auto-fan for %s: temperatures restored", self._climate_entity_id
            )
            self._sensor_missing_since = None
            self._sensor_warned = False
            # We deliberately do NOT reset _last_band_entered_ts here: the
            # outage represents "band X has been applied on the hardware
            # this whole time," which is exactly what dwell measures.

        if snapshot.hvac_mode in {"off", "fan_only", None}:
            self._skip("HVAC mode is %s", snapshot.hvac_mode)
            self._last_hvac_mode = snapshot.hvac_mode
            return None
        if not snapshot.modes:
            self._skip("no fan_modes attr")
            return None

        # HVAC mode change is a discontinuity: the previous mode's dwell
        # arithmetic doesn't apply to the new mode's decisions. Reset the
        # ladder + dwell state so the very next decision fires without
        # inheriting a stale window.
        if (
            self._last_hvac_mode is not None
            and self._last_hvac_mode != snapshot.hvac_mode
        ):
            _LOGGER.debug(
                "HVAC mode transition for %s: %s -> %s; resetting ladder/dwell",
                self._climate_entity_id,
                self._last_hvac_mode,
                snapshot.hvac_mode,
            )
            self._last_selected_index = None
            self._last_band_entered_ts = None
        self._last_hvac_mode = snapshot.hvac_mode

        if not self._fan_mode_order:
            _LOGGER.warning(
                "Auto-fan for %s: fan_mode_order is empty; skipping (reconfigure the "
                "integration to pick at least one mode)",
                self._climate_entity_id,
            )
            return None

        # Compute the smoothed target and run the ladder.
        smoothed_target = self._update_target_ema(snapshot.target_temperature, now)
        preferred_order = self._fan_mode_order

        signed_delta = snapshot.current_temperature - smoothed_target
        hvac = snapshot.hvac_mode
        # TODO(heat-mode edge cases, tracked separately):
        #   1. Defrost cycles. In cold weather a heat pump periodically
        #      reverses to melt the outdoor coil, blowing cold air for
        #      1-10 min. We currently see hvac_mode="heat", delta widens
        #      (room cools), and we PROMOTE the fan — which is exactly
        #      wrong (cold air + high fan = colder room faster). Detect
        #      via hvac_action="defrosting" if the underlying exposes it,
        #      or a "heat mode + delta rising" heuristic.
        #   2. Warm-up delay. First 30-60s after a heat call, the coil
        #      is still warming. Blowing hard pushes cold air into the
        #      room. Add a warm-up grace on hvac transitions INTO heat
        #      similar to the startup grace we already have.
        boost_needed = (
            (hvac in {"cool", "dry"} and signed_delta > 0)
            or (hvac == "heat" and signed_delta < 0)
            or (hvac in {"heat_cool", "auto"})
        )
        # No boost wanted means delta 0, which still runs the ladder so the
        # Schmitt exit thresholds demote uniformly rather than short-circuiting.
        ladder_delta = abs(signed_delta) if boost_needed else 0.0
        modes = normalize_supported_modes(list(snapshot.modes), preferred_order)
        selected_index = select_progressive_index(
            ladder_delta, len(modes), prev_index=self._last_selected_index
        )
        selected_mode = modes[selected_index] if selected_index >= 0 else None
        _LOGGER.debug(
            "Decision for %s: boost=%s signed_delta=%.2f smoothed_tgt=%.2f "
            "delta=%.2f band=%s prev_idx=%s -> selected=%s idx=%s",
            self._climate_entity_id,
            boost_needed,
            signed_delta,
            smoothed_target,
            ladder_delta,
            delta_band(ladder_delta),
            self._last_selected_index,
            selected_mode,
            selected_index,
        )

        if selected_mode is None:
            return None

        current_mode = (snapshot.fan_mode or "").lower().strip()
        target_mode = snapshot.modes.get(selected_mode)
        if target_mode is None:
            _LOGGER.warning(
                "Auto-fan for %s: selected mode %r not in underlying fan_modes %s",
                self._climate_entity_id,
                selected_mode,
                list(snapshot.modes.values()),
            )
            return None

        # Demote gate. Promotion is never blocked. Record the band on EVERY
        # path — including the no-write path below — because the band we
        # believe we're in is what the next decision measures against; leaving
        # it stale used to make a real demote look like "no change" and skip
        # the dwell entirely.
        is_demote = (
            self._last_selected_index is not None
            and selected_index < self._last_selected_index
        )
        if (
            is_demote
            and self._last_band_entered_ts is not None
            and (now - self._last_band_entered_ts) < self._demote_dwell
        ):
            remaining = self._demote_dwell - (now - self._last_band_entered_ts)
            _LOGGER.debug(
                "Demote-dwell blocking for %s: %s -> %s but %.0fs remain (reason=%s)",
                self._climate_entity_id,
                snapshot.fan_mode,
                target_mode,
                remaining,
                reason,
            )
            self._arm_dwell_timer(remaining)
            return None

        prev_index = self._last_selected_index
        self._last_selected_index = selected_index
        if prev_index != selected_index:
            self._last_band_entered_ts = now
        self._cancel_dwell_timer()

        if current_mode == selected_mode.lower():
            # Hardware already there — nothing to write, but the band state
            # above is now recorded, which is the point.
            self._last_written_mode = target_mode
            return target_mode

        if target_mode == self._last_written_mode and prev_index == selected_index:
            # We already commanded this exact mode and the band has not moved
            # since. snapshot.fan_mode is simply the pre-write value: a polled
            # underlying takes seconds to acknowledge, and VTherm events keep
            # arriving in that window, so comparing against the *reported*
            # mode alone would re-issue an identical command several times per
            # real transition.
            _LOGGER.debug(
                "Not re-issuing %s for %s: already commanded, band unchanged",
                target_mode,
                self._climate_entity_id,
            )
            return target_mode

        _LOGGER.info(
            "Setting fan_mode on %s: %s -> %s (hvac=%s signed_delta=%.2f "
            "smoothed_tgt=%.2f)",
            self._climate_entity_id,
            snapshot.fan_mode,
            target_mode,
            hvac,
            signed_delta,
            smoothed_target,
        )
        # Record before the await: a concurrent apply scheduled during it must
        # see that this mode is already commanded.
        self._last_written_mode = target_mode
        await self._hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_fan_mode",
            {"entity_id": self._climate_entity_id, "fan_mode": target_mode},
            blocking=False,
        )
        return target_mode

    def _arm_dwell_timer(self, remaining: float) -> None:
        # Guarantees a re-evaluation when the dwell expires, so a blocked
        # demote still lands even if no further event ever arrives. Only one
        # dwell can be pending (demote is the only gated direction), so a
        # plain replace is correct.
        self._cancel_dwell_timer()
        self._dwell_handle = self._hass.loop.call_later(
            remaining + 0.05, self._on_dwell_expired
        )

    def _cancel_dwell_timer(self) -> None:
        if self._dwell_handle is not None:
            self._dwell_handle.cancel()
            self._dwell_handle = None

    @callback
    def _on_dwell_expired(self) -> None:
        self._dwell_handle = None
        self._hass.async_create_task(self.async_apply_now(reason="dwell_expired"))

    # The three vtherm_api hooks have identical bodies; bind them to the one
    # implementation rather than writing it out three times.
    handle_temperature_event = _maybe_schedule_apply
    handle_hvac_mode_event = _maybe_schedule_apply
    handle_preset_event = _maybe_schedule_apply

    def _build_snapshot(self) -> FanModeSnapshot:
        state = self._hass.states.get(self._climate_entity_id)
        if state is None:
            return FanModeSnapshot(None, None, None, None, {})
        attrs = state.attributes
        modes = {
            str(m).strip().lower(): str(m)
            for m in (attrs.get("fan_modes") or [])
            if str(m).strip()
        }

        # Prefer the climate entity's declared unit, fall back to the HA
        # global. Temperatures are normalized to °F internally so all
        # threshold constants can stay in one unit regardless of how HA is
        # configured.
        unit = attrs.get("temperature_unit") or self._hass.config.units.temperature_unit

        current_temperature = _temp_f(attrs.get("current_temperature"), unit)
        if self._current_temperature_sensor_entity_id is not None:
            override_state = self._hass.states.get(
                self._current_temperature_sensor_entity_id
            )
            if override_state is not None:
                override_val = _temp_f(
                    override_state.state,
                    override_state.attributes.get("unit_of_measurement"),
                    unit,
                )
                if override_val is not None:
                    current_temperature = override_val

        # For climate entities, state.state IS the hvac mode ("cool", "off"...);
        # attrs.get("hvac_mode") is always None on real climate implementations.
        hvac_mode = state.state.lower() if state.state else None

        return FanModeSnapshot(
            current_temperature=current_temperature,
            target_temperature=_temp_f(attrs.get(ATTR_TEMPERATURE), unit),
            hvac_mode=hvac_mode,
            fan_mode=str(attrs.get("fan_mode")).lower()
            if attrs.get("fan_mode")
            else None,
            modes=modes,
        )


def _temp_f(
    value: Any, unit: str | None, fallback_unit: str | None = None
) -> float | None:
    """Parse a temperature and normalize it to °F, or None if unusable.

    None is the sensor-loss signal, returned for anything we can't trust:
    non-numeric, non-finite, or outside the plausible indoor range. The range
    check subsumes the finite check — every comparison against NaN is False,
    and both infinities fall outside — so NaN cannot reach the delta math,
    where it would read as "room is exactly at setpoint" and walk the fan
    down in silence.

    An unrecognized unit falls back to `fallback_unit` (the climate entity's)
    rather than being rejected: template sensors frequently declare no
    unit_of_measurement, and treating those as sensor loss would freeze the
    fan for a sensor that is working fine.
    """
    try:
        parsed = float(value)
    except TypeError, ValueError:
        return None
    for candidate in (unit, fallback_unit):
        if candidate in TemperatureConverter.VALID_UNITS:
            parsed = TemperatureConverter.convert(
                parsed, candidate, UnitOfTemperature.FAHRENHEIT
            )
            break
    return (
        parsed
        if FAHRENHEIT_TEMPERATURE_MIN <= parsed <= FAHRENHEIT_TEMPERATURE_MAX
        else None
    )
