"""VTherm-linked progressive fan controller.

Architecture (see also progressive_fan.py for the pure ladder):

  raw target ─────► EMA (τ=90s) ────┐
                                    ├──► delta ──► Schmitt ladder ─► band_index
  smoothed room sensor ─────────────┘

  band_index changes ─► multi-step demote / free promote
                       ─► promote dwell (30s) / demote dwell (240s)
                       ─► fast-lane bypass (delta ≥ next enter + 1.0)
                       ─► 10s throttle
                       ─► set_fan_mode
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any, Iterable

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from vtherm_api import PluginClimate

from .const import (
    DEFAULT_DEMOTE_DWELL_SECONDS,
    DEFAULT_FAST_LANE_MARGIN,
    DEFAULT_MIN_APPLY_INTERVAL_SECONDS,
    DEFAULT_PROMOTE_DWELL_SECONDS,
    DEFAULT_SCHMITT_ZONES,
    DEFAULT_SENSOR_UNAVAIL_WARN_SECONDS,
    DEFAULT_STARTUP_DELAY_SECONDS,
    DEFAULT_TARGET_EMA_JUMP_THRESHOLD,
    DEFAULT_TARGET_EMA_TAU_SECONDS,
)
from .progressive_fan import (
    choose_fan_mode,
    delta_band,
    next_enter_threshold,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FanModeSnapshot:
    current_temperature: float | None
    target_temperature: float | None
    hvac_mode: str | None
    fan_mode: str | None
    fan_modes: list[str]
    raw_fan_modes: list[str]


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

        self._climate_listener_remove = None
        self._vtherm_listener_remove = None
        self._vtherm_entity_id: str | None = None

        self._enabled: bool = False

        self._last_selected_index: int | None = None
        # Only reset when the band index actually changes — same-band
        # re-commits do not restart the dwell window.
        self._last_band_entered_ts: float | None = None

        self._last_apply_ts: float | None = None
        self._throttle_handle = None
        self._dwell_handle = None
        self._min_apply_interval = DEFAULT_MIN_APPLY_INTERVAL_SECONDS

        self._promote_dwell = DEFAULT_PROMOTE_DWELL_SECONDS
        self._demote_dwell = DEFAULT_DEMOTE_DWELL_SECONDS
        self._fast_lane_margin = DEFAULT_FAST_LANE_MARGIN

        self._startup_ts: float = hass.loop.time()
        self._startup_delay = DEFAULT_STARTUP_DELAY_SECONDS

        self._target_ema: float | None = None
        self._target_ema_last_ts: float | None = None
        self._target_ema_tau = DEFAULT_TARGET_EMA_TAU_SECONDS
        self._target_ema_jump_threshold = DEFAULT_TARGET_EMA_JUMP_THRESHOLD

        self._sensor_missing_since: float | None = None
        self._sensor_unavail_warn = DEFAULT_SENSOR_UNAVAIL_WARN_SECONDS
        self._sensor_warned = False

        _LOGGER.debug(
            "VThermProgressiveFanPlugin created for climate=%s preferred_order=%s "
            "current_source=%s",
            self._climate_entity_id, self._fan_mode_order,
            self._current_temperature_sensor_entity_id or "(from climate entity)",
        )

    # ─── Master enable/disable ──────────────────────────────────────────
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        _LOGGER.debug("Enabled flag for %s set to %s", self._climate_entity_id, self._enabled)
        if not self._enabled:
            # Clean slate on re-enable — no stale bias from before disable.
            self._last_apply_ts = None
            self._last_selected_index = None
            self._last_band_entered_ts = None
            self._target_ema = None
            self._target_ema_last_ts = None
            self._sensor_missing_since = None
            self._sensor_warned = False
            if self._throttle_handle is not None:
                self._throttle_handle.cancel()
                self._throttle_handle = None
            if self._dwell_handle is not None:
                self._dwell_handle.cancel()
                self._dwell_handle = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def climate_entity_id(self) -> str:
        return self._climate_entity_id

    # ─── VTherm linking / listeners ─────────────────────────────────────
    def link_to_vtherm(self, vtherm: Any) -> None:
        _LOGGER.info("Linking progressive auto-fan to VTherm for climate=%s", self._climate_entity_id)
        super().link_to_vtherm(vtherm)
        self._vtherm_entity_id = getattr(vtherm, "entity_id", None)
        self._listen_to_target_climate_state()
        self._listen_to_vtherm_state()

    def _listen_to_vtherm_state(self) -> None:
        # Watches the VTherm's own state so a current-temp drift without a
        # coincident VTherm event still triggers a re-evaluation.
        if self._vtherm_entity_id is None:
            return
        if self._vtherm_listener_remove is not None:
            self._vtherm_listener_remove()
            self._vtherm_listener_remove = None
        self._vtherm_listener_remove = async_track_state_change_event(
            self._hass, [self._vtherm_entity_id], self._handle_vtherm_state_change,
        )

    async def _handle_vtherm_state_change(self, event: Event) -> None:
        await self.async_apply_now(reason="vtherm_state_change")

    def _listen_to_target_climate_state(self) -> None:
        if self._climate_listener_remove is not None:
            self._climate_listener_remove()
            self._climate_listener_remove = None
        self._climate_listener_remove = async_track_state_change_event(
            self._hass, [self._climate_entity_id], self._handle_target_climate_state_change,
        )

    async def _handle_target_climate_state_change(self, event: Event) -> None:
        await self.async_apply_now(reason="target_state_change")

    def remove_listeners(self) -> None:
        _LOGGER.debug("Removing listeners for climate=%s", self._climate_entity_id)
        if self._climate_listener_remove is not None:
            self._climate_listener_remove()
            self._climate_listener_remove = None
        if self._vtherm_listener_remove is not None:
            self._vtherm_listener_remove()
            self._vtherm_listener_remove = None
        if self._throttle_handle is not None:
            self._throttle_handle.cancel()
            self._throttle_handle = None
        if self._dwell_handle is not None:
            self._dwell_handle.cancel()
            self._dwell_handle = None
        super().remove_listeners()

    # ─── vtherm_api event hooks ─────────────────────────────────────────
    def handle_temperature_event(self, event: Event) -> None:
        self._maybe_schedule_apply(event)

    def handle_hvac_mode_event(self, event: Event) -> None:
        self._maybe_schedule_apply(event)

    def handle_preset_event(self, event: Event) -> None:
        self._maybe_schedule_apply(event)

    def _maybe_schedule_apply(self, event: Event) -> None:
        # vtherm_api dispatches from its own thread pool; marshal onto loop.
        future = asyncio.run_coroutine_threadsafe(
            self.async_apply_now(reason=event.event_type), self._hass.loop,
        )
        future.add_done_callback(self._log_future_exception)

    def _log_future_exception(self, future) -> None:
        try:
            exc = future.exception()
        except Exception:
            return
        if exc is not None:
            _LOGGER.exception(
                "Unhandled exception in scheduled apply for %s: %s",
                self._climate_entity_id, exc, exc_info=exc,
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
                target - self._target_ema, self._climate_entity_id,
                self._target_ema_jump_threshold,
            )
            self._target_ema = target
        else:
            dt = max(0.0, now - self._target_ema_last_ts)
            alpha = 1.0 - math.exp(-dt / self._target_ema_tau)
            self._target_ema = self._target_ema + alpha * (target - self._target_ema)
        self._target_ema_last_ts = now
        return self._target_ema

    # ─── Core decision + apply ──────────────────────────────────────────
    async def async_apply_now(self, reason: str | None = None) -> str | None:
        if not self._enabled:
            _LOGGER.debug("Skipping auto-fan for %s: disabled", self._climate_entity_id)
            return None

        now = self._hass.loop.time()

        # Startup grace.
        elapsed_since_start = now - self._startup_ts
        if elapsed_since_start < self._startup_delay:
            _LOGGER.debug(
                "Skipping auto-fan for %s: startup grace (%.1fs of %.1fs)",
                self._climate_entity_id, elapsed_since_start, self._startup_delay,
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
            _LOGGER.debug("Skipping auto-fan for %s: missing temperatures", self._climate_entity_id)
            return None
        if self._sensor_missing_since is not None:
            _LOGGER.debug("Auto-fan for %s: temperatures restored", self._climate_entity_id)
            self._sensor_missing_since = None
            self._sensor_warned = False
            # Pause-the-dwell-clock semantics: restart the band-entry window
            # from now so a demote decision on the first post-recovery cycle
            # isn't gated on an outage-length stale timestamp.
            if self._last_selected_index is not None:
                self._last_band_entered_ts = now

        if snapshot.hvac_mode in {"off", "fan_only", None}:
            _LOGGER.debug(
                "Skipping auto-fan for %s: HVAC mode is %s",
                self._climate_entity_id, snapshot.hvac_mode,
            )
            return None
        if not snapshot.fan_modes:
            _LOGGER.debug("Skipping auto-fan for %s: no fan_modes attr", self._climate_entity_id)
            return None

        if not self._fan_mode_order:
            _LOGGER.warning(
                "Auto-fan for %s: fan_mode_order is empty; skipping (reconfigure the "
                "integration to pick at least one mode)",
                self._climate_entity_id,
            )
            return None

        # Compute smoothed target and the ladder decision.
        smoothed_target = self._update_target_ema(snapshot.target_temperature, now)
        preferred_order = self._fan_mode_order
        preferred_set = set(preferred_order)
        ignored_modes = [m for m in snapshot.fan_modes if m not in preferred_set]
        if ignored_modes:
            _LOGGER.debug(
                "Ignoring unsupported/non-ordered fan modes for %s: %s",
                self._climate_entity_id, ignored_modes,
            )

        signed_delta = snapshot.current_temperature - smoothed_target
        hvac = snapshot.hvac_mode
        boost_needed = (
            (hvac in {"cool", "dry"} and signed_delta > 0)
            or (hvac == "heat" and signed_delta < 0)
            or (hvac in {"heat_cool", "auto"})
        )
        # When we don't need to boost, feed delta=0 through the ladder so
        # demote (via Schmitt exit thresholds) still applies uniformly.
        cur_for_ladder = snapshot.current_temperature if boost_needed else smoothed_target
        decision = choose_fan_mode(
            cur_for_ladder, smoothed_target, snapshot.fan_modes, preferred_order,
            prev_index=self._last_selected_index,
        )
        selected_mode = decision.selected_mode
        band = delta_band(decision.delta)
        _LOGGER.debug(
            "Decision for %s: boost=%s signed_delta=%.2f smoothed_tgt=%.2f "
            "delta=%.2f band=%s prev_idx=%s -> selected=%s idx=%s",
            self._climate_entity_id, boost_needed, signed_delta, smoothed_target,
            decision.delta, band, self._last_selected_index,
            selected_mode, decision.selected_index,
        )

        if selected_mode is None:
            return None

        current_mode = (snapshot.fan_mode or "").lower().strip()
        target_mode = self._original_case(selected_mode, snapshot)
        if target_mode is None:
            _LOGGER.warning(
                "Auto-fan for %s: selected mode %r not in underlying fan_modes %s",
                self._climate_entity_id, selected_mode, snapshot.raw_fan_modes,
            )
            return None

        if current_mode == selected_mode.lower():
            return target_mode

        is_promote = (
            self._last_selected_index is not None
            and decision.selected_index > self._last_selected_index
        )
        is_demote = (
            self._last_selected_index is not None
            and decision.selected_index < self._last_selected_index
        )

        # Fast-lane: skip promote dwell when the room is well past the next
        # band's enter threshold — not a boundary graze. Only meaningful
        # on a real promote (which already requires prev_index is not None).
        fast_lane_ok = False
        if is_promote:
            next_enter = next_enter_threshold(self._last_selected_index)
            if next_enter is not None and decision.delta >= next_enter + self._fast_lane_margin:
                fast_lane_ok = True

        if self._last_band_entered_ts is not None and not fast_lane_ok:
            elapsed = now - self._last_band_entered_ts
            if is_promote and elapsed < self._promote_dwell:
                remaining = self._promote_dwell - elapsed
                _LOGGER.debug(
                    "Promote-dwell blocking for %s: %s -> %s but %.1fs remain (reason=%s)",
                    self._climate_entity_id, snapshot.fan_mode, target_mode, remaining, reason,
                )
                self._arm_dwell_timer(remaining)
                return None
            if is_demote and elapsed < self._demote_dwell:
                remaining = self._demote_dwell - elapsed
                _LOGGER.debug(
                    "Demote-dwell blocking for %s: %s -> %s but %.0fs remain (reason=%s)",
                    self._climate_entity_id, snapshot.fan_mode, target_mode, remaining, reason,
                )
                self._arm_dwell_timer(remaining)
                return None

        if self._last_apply_ts is not None:
            elapsed = now - self._last_apply_ts
            if elapsed < self._min_apply_interval:
                remaining = self._min_apply_interval - elapsed
                _LOGGER.debug(
                    "Throttle blocking for %s: %s -> %s but %.1fs remain (reason=%s)",
                    self._climate_entity_id, snapshot.fan_mode, target_mode, remaining, reason,
                )
                if self._throttle_handle is None:
                    self._throttle_handle = self._hass.loop.call_later(
                        remaining + 0.05, self._on_throttle_expired
                    )
                return None

        # State updates precede the await — a concurrent apply_now must see
        # the new throttle/dwell timestamps and not double-fire.
        prev_index = self._last_selected_index
        self._last_apply_ts = now
        self._last_selected_index = decision.selected_index
        if prev_index != decision.selected_index:
            self._last_band_entered_ts = now
        if self._throttle_handle is not None:
            self._throttle_handle.cancel()
            self._throttle_handle = None
        if self._dwell_handle is not None:
            self._dwell_handle.cancel()
            self._dwell_handle = None

        _LOGGER.info(
            "Setting fan_mode on %s: %s -> %s (hvac=%s signed_delta=%.2f "
            "smoothed_tgt=%.2f fast_lane=%s)",
            self._climate_entity_id, snapshot.fan_mode, target_mode, hvac,
            signed_delta, smoothed_target, fast_lane_ok,
        )
        await self._hass.services.async_call(
            CLIMATE_DOMAIN, "set_fan_mode",
            {"entity_id": self._climate_entity_id, "fan_mode": target_mode},
            blocking=False,
        )
        return target_mode

    def _arm_dwell_timer(self, remaining: float) -> None:
        if self._dwell_handle is None:
            self._dwell_handle = self._hass.loop.call_later(
                remaining + 0.05, self._on_dwell_expired
            )

    @callback
    def _on_throttle_expired(self) -> None:
        self._throttle_handle = None
        self._hass.async_create_task(self.async_apply_now(reason="throttle_expired"))

    @callback
    def _on_dwell_expired(self) -> None:
        self._dwell_handle = None
        self._hass.async_create_task(self.async_apply_now(reason="dwell_expired"))

    @staticmethod
    def _original_case(lower_mode: str, snapshot: FanModeSnapshot) -> str | None:
        target = lower_mode.lower().strip()
        for raw in snapshot.raw_fan_modes:
            if str(raw).strip().lower() == target:
                return str(raw)
        return None

    def _build_snapshot(self) -> FanModeSnapshot:
        state = self._hass.states.get(self._climate_entity_id)
        if state is None:
            return FanModeSnapshot(None, None, None, None, [], [])
        attrs = state.attributes
        raw_fan_modes = [str(m) for m in (attrs.get("fan_modes") or []) if str(m).strip()]
        fan_modes = [m.strip().lower() for m in raw_fan_modes]

        current_temperature = _as_float(attrs.get("current_temperature"))
        if self._current_temperature_sensor_entity_id is not None:
            override_state = self._hass.states.get(self._current_temperature_sensor_entity_id)
            if override_state is not None:
                override_val = _as_float(override_state.state)
                if override_val is not None:
                    current_temperature = override_val

        # For climate entities, state.state IS the hvac mode ("cool", "off"...);
        # attrs.get("hvac_mode") is always None on real climate implementations.
        hvac_mode = state.state.lower() if state.state else None

        return FanModeSnapshot(
            current_temperature=current_temperature,
            target_temperature=_as_float(attrs.get(ATTR_TEMPERATURE)),
            hvac_mode=hvac_mode,
            fan_mode=str(attrs.get("fan_mode")).lower() if attrs.get("fan_mode") else None,
            fan_modes=fan_modes,
            raw_fan_modes=raw_fan_modes,
        )


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # NaN / ±inf → sensor-loss path; otherwise they poison delta math.
    if not math.isfinite(result):
        return None
    return result
