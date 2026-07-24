# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Progressive auto-fan selection helpers.

Pure functions — no Home Assistant dependencies. Temporal reasoning (the
demote dwell) lives in the controller; this module is just the delta →
band-index Schmitt-trigger ladder.
"""

from __future__ import annotations

from collections.abc import Sequence

from .const import DEFAULT_FAN_MODE_ORDER, DEFAULT_SCHMITT_ZONES


def normalize_supported_modes(
    fan_modes: Sequence[str] | None,
    preferred_order: Sequence[str] | None = None,
) -> list[str]:
    """Return fan modes ordered from quietest to strongest.

    Only modes in preferred_order are used — vendor extras (``auto``, etc.)
    are dropped so the ladder can't fall back to them. Walking the preferred
    order and testing membership dedupes the result for free: a mode repeated
    in either input can be emitted at most once.
    """
    supported = {str(m).strip().lower() for m in (fan_modes or []) if str(m).strip()}
    if not supported:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for mode in preferred_order or DEFAULT_FAN_MODE_ORDER:
        normalized = str(mode).strip().lower()
        if normalized in supported and normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


def _band_for(abs_delta: float, zones: Sequence[tuple[float, float]]) -> int:
    """Highest band whose enter threshold is met. Band 0 is the implicit floor."""
    band = 0
    for k, (enter, _exit) in enumerate(zones):
        if k and abs_delta >= enter:
            band = k
    return band


def select_progressive_index(
    delta: float,
    mode_count: int,
    prev_index: int | None = None,
    zones: Sequence[tuple[float, float]] = DEFAULT_SCHMITT_ZONES,
) -> int:
    """Map a delta to a band index using a Schmitt-trigger ladder.

    Promote: jump straight to the highest band whose enter threshold is met.
    Demote: walk down while `delta < exit[k]` — a slow cool-down hits each
    exit sequentially (one band per dwell window); a fast cool-down unwinds
    all the way to the matching band once the demote dwell expires.

    prev_index=None indicates a cold start; enter thresholds pick the initial
    band directly.

    When the caller configures more modes than we have zones for, the ladder
    is capped at `len(zones)` bands and the surplus modes are unreachable.
    That cap MUST also clamp `prev_index`: an index at or past `len(zones)`
    has no exit threshold, so the demote walk can never start and the band
    becomes absorbing — the fan would sit at that speed forever with no
    further service calls. `usable` below is what makes that unreachable.
    """
    if mode_count <= 0:
        return -1
    abs_delta = abs(float(delta))
    usable = min(mode_count, len(zones))
    clamped_zones = list(zones)[:usable]

    promote_target = _band_for(abs_delta, clamped_zones)

    if prev_index is None:
        return max(0, min(promote_target, usable - 1))

    prev = max(0, min(prev_index, usable - 1))

    # Promote path: delta reaches a band above where we are.
    if promote_target > prev:
        return promote_target

    # Demote path: walk down as long as delta stays below each band's exit.
    # `prev` is clamped into range above, so indexing clamped_zones is safe.
    if prev > 0 and abs_delta < clamped_zones[prev][1]:
        target = prev - 1
        while target > 0 and abs_delta < clamped_zones[target][1]:
            target -= 1
        return target

    return prev


def delta_band(
    delta: float, zones: Sequence[tuple[float, float]] = DEFAULT_SCHMITT_ZONES
) -> str:
    """Human-friendly band name for logs — based on raw enter thresholds.

    Deliberately the *raw* band, not the Schmitt-held one: comparing this
    against the selected index in a log line shows how much hysteresis is
    holding a decision back, which is the quantity worth watching when
    diagnosing flap.
    """
    # One name per zone in DEFAULT_SCHMITT_ZONES; the trailing name is reused
    # if a caller passes more zones than we have names for.
    names = ("quietest", "low", "medium", "high", "max", "turbo")
    return names[min(_band_for(abs(float(delta)), zones), len(names) - 1)]
