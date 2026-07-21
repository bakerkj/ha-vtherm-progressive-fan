# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Pure-function tests for the Schmitt-trigger ladder and snapshot helpers.

These cover `progressive_fan.py` end to end plus the unit/plausibility
helpers in `fan_controller.py`. Everything here is deterministic and needs
no Home Assistant harness.

Zone reference (DEFAULT_SCHMITT_ZONES), (enter, exit) on |delta| in °F:

    band 0  quiet      (0.00, 0.00)   floor — no entry from below
    band 1  low        (0.75, 0.40)
    band 2  medium     (1.75, 1.30)
    band 3  high       (3.00, 2.50)
    band 4  very_high  (4.50, 4.00)
    band 5  turbo      (6.00, 5.50)
"""

from __future__ import annotations

import pytest

from custom_components.vtherm_progressive_fan.const import (
    DEFAULT_SCHMITT_ZONES,
    FAHRENHEIT_TEMPERATURE_MAX,
    FAHRENHEIT_TEMPERATURE_MIN,
)
from custom_components.vtherm_progressive_fan.fan_controller import _temp_f
from custom_components.vtherm_progressive_fan.progressive_fan import (
    delta_band,
    normalize_supported_modes,
    select_progressive_index,
)

# A conventional 5-speed mini-split, quietest → strongest.
FIVE_MODES = ["quiet", "low", "medium", "high", "very high"]


# ─── select_progressive_index: cold start ───────────────────────────────


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (0.0, 0),  # at setpoint
        (0.74, 0),  # just under band 1 enter
        (0.75, 1),  # exactly at band 1 enter
        (1.74, 1),
        (1.75, 2),  # exactly at band 2 enter
        (3.00, 3),
        (4.50, 4),
        (12.0, 4),  # way past the top of a 5-mode ladder
    ],
)
def test_cold_start_uses_enter_thresholds(delta: float, expected: int) -> None:
    """prev_index=None picks the band directly off the enter thresholds."""
    assert select_progressive_index(delta, mode_count=5, prev_index=None) == expected


def test_cold_start_is_symmetric_in_delta_sign() -> None:
    """The ladder consumes |delta| — heat and cool are treated alike."""
    assert select_progressive_index(-3.0, mode_count=5, prev_index=None) == (
        select_progressive_index(3.0, mode_count=5, prev_index=None)
    )


# ─── select_progressive_index: Schmitt hold zone ────────────────────────


@pytest.mark.parametrize("delta", [1.30, 1.5, 1.74])
def test_holds_band_inside_hysteresis_gap(delta: float) -> None:
    """Between exit[2]=1.30 and enter[2]=1.75 we neither promote nor demote.

    This is the whole point of the Schmitt ladder: a VTherm 0.5 °F target
    nudge that drags delta across a single threshold must not flap the band.
    """
    assert select_progressive_index(delta, mode_count=5, prev_index=2) == 2


def test_promote_requires_crossing_enter_not_exit() -> None:
    """Sitting at band 1 with delta in band 2's hold gap does not promote."""
    assert select_progressive_index(1.60, mode_count=5, prev_index=1) == 1
    # ...but crossing enter[2] does.
    assert select_progressive_index(1.75, mode_count=5, prev_index=1) == 2


# ─── select_progressive_index: demote ───────────────────────────────────


def test_demote_walks_one_band_when_delta_sits_in_the_next_band() -> None:
    """delta=0.6 is below exit[2] but above exit[1] → stop at band 1."""
    assert select_progressive_index(0.6, mode_count=5, prev_index=2) == 1


def test_demote_unwinds_fully_on_a_fast_cooldown() -> None:
    """A room that reaches setpoint from band 4 walks all the way to quiet.

    Regression guard: an earlier revision always stepped exactly one band per
    dwell window, which stranded the fan at "very high" for ~16 min after the
    room was already at target.
    """
    assert select_progressive_index(0.1, mode_count=5, prev_index=4) == 0


def test_demote_stops_at_the_matching_band() -> None:
    """From band 4, delta=2.0 walks down to band 2 and stops.

    The walk clears exit[4]=4.00 and exit[3]=2.50 but not exit[2]=1.30, so it
    halts in medium rather than unwinding to quiet.
    """
    assert select_progressive_index(2.0, mode_count=5, prev_index=4) == 2


def test_no_demote_below_band_zero() -> None:
    assert select_progressive_index(0.0, mode_count=5, prev_index=0) == 0


# ─── select_progressive_index: mode_count clamping ──────────────────────


@pytest.mark.parametrize("mode_count", [2, 3, 4, 5, 6])
def test_top_band_is_reachable_for_every_supported_mode_count(mode_count: int) -> None:
    """A huge delta must reach the user's strongest configured mode.

    Regression guard: zones were once truncated to the mode count without
    clamping the promote target, so a user who selected 6 modes could never
    reach the 6th — their loudest setting was silently dead.
    """
    idx = select_progressive_index(99.0, mode_count=mode_count, prev_index=None)
    assert idx == mode_count - 1


@pytest.mark.parametrize("mode_count", [7, 8, 12])
def test_surplus_modes_never_create_an_absorbing_band(mode_count: int) -> None:
    """More modes than zones must not strand the fan at maximum forever.

    Regression guard for a real defect: with mode_count=7, `prev_index=6` had
    no exit threshold (zones stop at index 5), so the demote walk could never
    start and every subsequent decision returned 6. Because the ladder then
    always re-selected the mode already on the hardware, `async_apply_now`
    took the no-write early return and the integration went permanently
    silent with the fan pinned at its loudest speed.
    """
    top = len(DEFAULT_SCHMITT_ZONES) - 1
    # An out-of-ladder prev index must clamp back into range...
    assert select_progressive_index(0.0, mode_count=mode_count, prev_index=6) <= top
    # ...and, at setpoint, must actually unwind to quiet.
    assert select_progressive_index(0.0, mode_count=mode_count, prev_index=top) == 0
    # Promotion can never exceed the last band we have thresholds for.
    assert select_progressive_index(99.0, mode_count=mode_count, prev_index=None) == top


def test_no_modes_returns_sentinel() -> None:
    assert select_progressive_index(5.0, mode_count=0, prev_index=None) == -1
    assert select_progressive_index(5.0, mode_count=-1, prev_index=None) == -1


# ─── select_progressive_index: defensive prev_index handling ────────────


def test_stale_prev_index_above_range_is_clamped() -> None:
    """A prev_index left over from a larger ladder must not IndexError."""
    assert select_progressive_index(0.0, mode_count=5, prev_index=99) == 0


def test_negative_prev_index_is_clamped() -> None:
    assert select_progressive_index(0.0, mode_count=5, prev_index=-3) == 0


# ─── normalize_supported_modes ──────────────────────────────────────────


def test_normalize_keeps_only_preferred_modes_in_preferred_order() -> None:
    """Vendor extras like `auto` are dropped, and our order wins.

    The underlying reports fan_modes in its own arbitrary order; the ladder
    requires quietest → strongest, which only the user's config knows.
    """
    reported = ["auto", "high", "low", "Very High", "medium", "quiet"]
    assert normalize_supported_modes(reported, FIVE_MODES) == FIVE_MODES


def test_normalize_drops_modes_the_underlying_does_not_support() -> None:
    assert normalize_supported_modes(["low", "high"], FIVE_MODES) == ["low", "high"]


def test_normalize_lowercases_and_strips() -> None:
    assert normalize_supported_modes(["  Very High "], ["very high"]) == ["very high"]


def test_normalize_dedupes_preserving_first_occurrence() -> None:
    assert normalize_supported_modes(["low", "LOW", "high"], ["low", "high"]) == [
        "low",
        "high",
    ]


@pytest.mark.parametrize("fan_modes", [None, [], ["", "  "]])
def test_normalize_returns_empty_when_nothing_is_supported(fan_modes) -> None:
    assert normalize_supported_modes(fan_modes, FIVE_MODES) == []


# ─── mode selection end to end ──────────────────────────────────────────


def _select(current: float, target: float, modes=FIVE_MODES, prev=None):
    """The ladder as the controller drives it: delta -> index -> mode name."""
    ordered = normalize_supported_modes(modes, FIVE_MODES)
    idx = select_progressive_index(abs(current - target), len(ordered), prev_index=prev)
    return idx, (ordered[idx] if idx >= 0 else None)


def test_delta_maps_to_the_expected_mode() -> None:
    assert _select(76.0, 72.0) == (3, "high")  # delta 4.0


def test_selection_uses_absolute_delta() -> None:
    """Room colder than target (heat) yields the same band as hotter (cool)."""
    assert _select(76.0, 72.0) == _select(72.0, 76.0)


def test_no_usable_modes_selects_nothing() -> None:
    """An underlying with no overlapping fan_modes must not pick a mode."""
    assert _select(80.0, 70.0, modes=[]) == (-1, None)


# ─── delta_band (log labels) ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (0.0, "quietest"),
        (1.0, "low"),
        (2.0, "medium"),
        (3.5, "high"),
        (5.0, "max"),
        (7.0, "turbo"),
    ],
)
def test_delta_band_labels(delta: float, expected: str) -> None:
    """Every zone has a distinct label — band 5 must not alias band 4."""
    assert delta_band(delta) == expected


# ─── temperature parsing (_temp_f) ──────────────────────────────────────


@pytest.mark.parametrize("value", [None, "unavailable", "unknown", "", object()])
def test_temp_rejects_non_numeric(value) -> None:
    assert _temp_f(value, "°F") is None


@pytest.mark.parametrize("value", ["nan", "NaN", float("nan"), "inf", float("-inf")])
def test_temp_rejects_non_finite(value) -> None:
    """NaN/inf must route to the sensor-loss path, not poison the delta math.

    float("nan") is a perfectly valid float, so a plain try/except lets it
    through. The range check catches it because every comparison against NaN
    is False — otherwise NaN reads as "room is exactly at setpoint" and walks
    the fan down in silence.
    """
    assert _temp_f(value, "°F") is None


@pytest.mark.parametrize("value", [0.0, -40.0, 200.0, FAHRENHEIT_TEMPERATURE_MIN - 0.1])
def test_temp_rejects_implausible(value: float) -> None:
    """0.0 is the classic startup transient — it must not read as a 72 °F delta."""
    assert _temp_f(value, "°F") is None


@pytest.mark.parametrize(
    "value", [FAHRENHEIT_TEMPERATURE_MIN, 72.0, FAHRENHEIT_TEMPERATURE_MAX]
)
def test_temp_accepts_in_range(value: float) -> None:
    assert _temp_f(value, "°F") == pytest.approx(value)


def test_temp_accepts_numeric_strings() -> None:
    assert _temp_f("72.5", "°F") == pytest.approx(72.5)


def test_temp_converts_celsius() -> None:
    assert _temp_f(0.0, "°C") is None  # 32 °F — implausible indoors
    assert _temp_f(22.0, "°C") == pytest.approx(71.6)


def test_temp_converts_kelvin() -> None:
    """Regression guard: a Kelvin sensor used to read as permanent sensor loss.

    The old code passed any non-Celsius unit through unconverted, so ~295 K
    failed the °F bounds and the fan froze with only a debug line to show it.
    """
    assert _temp_f(295.0, "K") == pytest.approx(71.33, abs=0.01)


def test_temp_falls_back_to_the_climate_unit_for_an_unknown_unit() -> None:
    """Template sensors often declare no unit; that must not mean sensor loss."""
    assert _temp_f(22.0, None, "°C") == pytest.approx(71.6)
    assert _temp_f(72.0, "", "°F") == pytest.approx(72.0)


def test_celsius_delta_scales_into_the_same_band_as_fahrenheit() -> None:
    """A 2 °C gap is 3.6 °F and must land in band 3, not band 2.

    Guards the unit-normalization contract: all ladder thresholds are °F, so
    a Celsius install that skipped conversion would under-fan badly.
    """
    cur = _temp_f(24.0, "°C")
    tgt = _temp_f(22.0, "°C")
    assert cur is not None and tgt is not None
    assert cur - tgt == pytest.approx(3.6)
    assert _select(cur, tgt) == (3, "high")
