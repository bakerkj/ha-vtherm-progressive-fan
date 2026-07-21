# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Controller behaviour tests.

Covers the two gates that survived the anti-flap simplification — the
fan_mode-only echo filter and the demote dwell — plus enable/disable state
handling.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.vtherm_progressive_fan.const import (
    DEFAULT_DEMOTE_DWELL_SECONDS,
)
from custom_components.vtherm_progressive_fan.fan_controller import (
    VThermProgressiveFanPlugin,
)

CLIMATE_ENTITY = "climate.test_mini_split"
MODES = ["quiet", "low", "medium", "high"]


@pytest.fixture
def set_fan_mode_calls(hass):
    """Register climate.set_fan_mode and capture what the controller writes."""
    return async_mock_service(hass, "climate", "set_fan_mode")


@pytest.fixture
def plugins(hass):
    """Factory yielding plugins, with timers cancelled on teardown.

    A dwell-blocked decision arms a `call_later`; leaving it pending trips the
    HA test harness's lingering-timer check.
    """
    made: list[VThermProgressiveFanPlugin] = []

    def _make() -> VThermProgressiveFanPlugin:
        plugin = VThermProgressiveFanPlugin(
            hass=hass,
            climate_entity_id=CLIMATE_ENTITY,
            fan_mode_order=list(MODES),
        )
        # Tests exercise decisions, not the post-restart quiet period.
        plugin._startup_delay = 0.0
        plugin.set_enabled(True)
        made.append(plugin)
        return plugin

    yield _make
    for plugin in made:
        plugin._cancel_dwell_timer()


def _set_state(
    hass,
    *,
    fan_mode: str = "medium",
    state: str = "cool",
    current: float = 74.0,
    target: float = 72.0,
) -> None:
    hass.states.async_set(
        CLIMATE_ENTITY,
        state,
        {
            "temperature_unit": "°F",
            "fan_mode": fan_mode,
            "fan_modes": list(MODES),
            "current_temperature": current,
            "temperature": target,
        },
    )


def _state(fan_mode: str | None, *, hvac: str = "cool", current: float = 74.0) -> Any:
    attrs: dict[str, Any] = {
        "temperature_unit": "°F",
        "fan_modes": list(MODES),
        "current_temperature": current,
        "temperature": 72.0,
    }
    if fan_mode is not None:
        attrs["fan_mode"] = fan_mode
    return SimpleNamespace(state=hvac, attributes=attrs)


def _event(old: Any, new: Any) -> Any:
    return SimpleNamespace(data={"old_state": old, "new_state": new})


# ─── fan_mode-only echo filter ──────────────────────────────────────────


async def test_fan_mode_only_change_does_not_redecide(hass, plugins) -> None:
    """Our own write echoing back must not trigger a re-decision.

    This is what replaced the settle window: a polled integration republishes
    the pre-write fan_mode a few seconds after we command it, and re-deciding
    off that stale value is what produced the observed flapping.
    """
    plugin = plugins()
    plugin.async_apply_now = AsyncMock()
    await plugin._handle_target_climate_state_change(
        _event(_state("medium"), _state("high"))
    )
    plugin.async_apply_now.assert_not_awaited()


async def test_hvac_mode_change_does_redecide(hass, plugins) -> None:
    """hvac_mode flips how the delta's sign is read, so it must wake us."""
    plugin = plugins()
    plugin.async_apply_now = AsyncMock()
    await plugin._handle_target_climate_state_change(
        _event(_state("medium", hvac="cool"), _state("medium", hvac="heat"))
    )
    plugin.async_apply_now.assert_awaited_once()


async def test_temperature_change_does_redecide(hass, plugins) -> None:
    """A real temperature move must wake us even if fan_mode also changed."""
    plugin = plugins()
    plugin.async_apply_now = AsyncMock()
    await plugin._handle_target_climate_state_change(
        _event(_state("medium", current=74.0), _state("high", current=77.0))
    )
    plugin.async_apply_now.assert_awaited_once()


@pytest.mark.parametrize(
    ("old", "new"),
    [(None, _state("medium")), (_state("medium"), None), (None, None)],
)
async def test_missing_states_fall_through(hass, plugins, old, new) -> None:
    """Entity added/removed — no old/new pair to compare, so don't filter."""
    plugin = plugins()
    plugin.async_apply_now = AsyncMock()
    await plugin._handle_target_climate_state_change(_event(old, new))
    plugin.async_apply_now.assert_awaited_once()


# ─── demote dwell ───────────────────────────────────────────────────────


async def test_promote_is_never_dwell_blocked(
    hass, plugins, set_fan_mode_calls
) -> None:
    """A room drifting further off setpoint is answered immediately."""
    _set_state(hass, fan_mode="quiet", current=77.0, target=72.0)
    plugin = plugins()
    plugin._last_selected_index = 0
    plugin._last_band_entered_ts = hass.loop.time()  # band entered just now
    result = await plugin.async_apply_now(reason="test")
    assert result is not None
    assert result.lower() != "quiet"


async def test_demote_is_blocked_inside_the_dwell(hass, plugins) -> None:
    _set_state(hass, fan_mode="high", current=72.0, target=72.0)
    plugin = plugins()
    plugin._last_selected_index = 3
    plugin._last_band_entered_ts = hass.loop.time()
    assert await plugin.async_apply_now(reason="test") is None


async def test_demote_proceeds_once_the_dwell_expires(
    hass, plugins, set_fan_mode_calls
) -> None:
    _set_state(hass, fan_mode="high", current=72.0, target=72.0)
    plugin = plugins()
    plugin._last_selected_index = 3
    plugin._last_band_entered_ts = hass.loop.time() - DEFAULT_DEMOTE_DWELL_SECONDS - 1.0
    result = await plugin.async_apply_now(reason="test")
    assert result is not None
    assert result.lower() != "high"


async def test_band_state_is_recorded_even_when_no_write_is_needed(
    hass, plugins, set_fan_mode_calls
) -> None:
    """The no-write path must still record the band.

    Regression guard: leaving the index stale made the *next* decision compare
    against the wrong band, so a genuine demote read as "no change" and skipped
    the dwell entirely.
    """
    _set_state(hass, fan_mode="high", current=75.5, target=72.0)
    plugin = plugins()
    plugin._last_selected_index = 1
    plugin._last_band_entered_ts = hass.loop.time()
    await plugin.async_apply_now(reason="test")
    assert plugin._last_selected_index == 3  # "high", matching the hardware


# ─── duplicate writes ───────────────────────────────────────────────────


async def test_identical_command_is_not_reissued_while_hardware_catches_up(
    hass, plugins, set_fan_mode_calls
) -> None:
    """A polled underlying lags our write; that must not cause a second write.

    mUART takes seconds to acknowledge a set_fan_mode, and VTherm events keep
    arriving in that window. Comparing only against the mode the underlying
    *reports* would re-issue the same command several times per transition —
    churn on exactly the queue whose behaviour started the flap investigation.
    """
    # delta 1.0 -> band 1 ("low")
    _set_state(hass, fan_mode="quiet", current=73.0, target=72.0)
    plugin = plugins()

    assert await plugin.async_apply_now(reason="first") is not None
    assert len(set_fan_mode_calls) == 1
    assert set_fan_mode_calls[0].data["fan_mode"] == "low"

    # The underlying has NOT acknowledged yet — it still reports "quiet" —
    # and two more VTherm-driven applies land at the same temperatures.
    await plugin.async_apply_now(reason="vtherm_state_change")
    await plugin.async_apply_now(reason="temperature_event")
    assert len(set_fan_mode_calls) == 1, "re-issued a command already in flight"

    # A real band change still writes, even though the underlying is still
    # reporting the stale pre-write value. delta 2.0 -> band 2 ("medium").
    _set_state(hass, fan_mode="quiet", current=74.0, target=72.0)
    await plugin.async_apply_now(reason="hotter")
    assert len(set_fan_mode_calls) == 2
    assert set_fan_mode_calls[1].data["fan_mode"] == "medium"


# ─── Celsius installs ───────────────────────────────────────────────────


async def test_celsius_entity_normalizes_to_fahrenheit(hass, plugins) -> None:
    """A °C climate entity must produce the same delta a °F one would.

    Every ladder threshold is in °F, so an install running HA in Celsius
    depends entirely on the snapshot converting on read.
    """
    hass.states.async_set(
        CLIMATE_ENTITY,
        "cool",
        {
            "temperature_unit": "°C",
            "fan_mode": "quiet",
            "fan_modes": list(MODES),
            "current_temperature": 24.0,  # 75.2 °F
            "temperature": 22.0,  # 71.6 °F
        },
    )
    snap = plugins()._build_snapshot()
    assert snap.current_temperature == pytest.approx(75.2)
    assert snap.target_temperature == pytest.approx(71.6)
    # 2 °C apart is 3.6 °F — band 3, not the band 2 an unconverted read gives.
    assert snap.current_temperature - snap.target_temperature == pytest.approx(3.6)


async def test_celsius_override_sensor_is_converted(hass, plugins) -> None:
    """The override sensor carries its own unit, independent of the climate."""
    hass.states.async_set(
        CLIMATE_ENTITY,
        "cool",
        {
            "temperature_unit": "°C",
            "fan_mode": "quiet",
            "fan_modes": list(MODES),
            "current_temperature": 24.0,
            "temperature": 22.0,
        },
    )
    hass.states.async_set("sensor.room_avg", "23.5", {"unit_of_measurement": "°C"})
    plugin = plugins()
    plugin._current_temperature_sensor_entity_id = "sensor.room_avg"
    assert plugin._build_snapshot().current_temperature == pytest.approx(74.3)


async def test_override_sensor_without_a_unit_uses_the_climate_unit(
    hass, plugins
) -> None:
    """Template sensors often declare no unit; that must not read as loss."""
    hass.states.async_set(
        CLIMATE_ENTITY,
        "cool",
        {
            "temperature_unit": "°C",
            "fan_mode": "quiet",
            "fan_modes": list(MODES),
            "current_temperature": 24.0,
            "temperature": 22.0,
        },
    )
    hass.states.async_set("sensor.room_avg", "23.5", {})
    plugin = plugins()
    plugin._current_temperature_sensor_entity_id = "sensor.room_avg"
    assert plugin._build_snapshot().current_temperature == pytest.approx(74.3)


# ─── enable / disable ───────────────────────────────────────────────────


async def test_disable_stops_applying(hass, plugins) -> None:
    _set_state(hass)
    plugin = plugins()
    plugin.set_enabled(False)
    assert await plugin.async_apply_now(reason="test") is None


async def test_re_enable_starts_from_a_cold_ladder(hass, plugins) -> None:
    """Re-enabling must not seed a band or start the dwell clock.

    Seeding used to block the first demote for the full dwell, so toggling the
    switch to force a re-settle did the opposite of what the user intended.
    """
    _set_state(hass, fan_mode="high")
    plugin = plugins()
    plugin._last_selected_index = 3
    plugin._last_band_entered_ts = hass.loop.time()
    plugin.set_enabled(False)
    plugin.set_enabled(True)
    assert plugin._last_selected_index is None
    assert plugin._last_band_entered_ts is None
