"""Progressive auto-fan selection helpers.

Pure functions — no Home Assistant dependencies. Temporal reasoning (dwell,
throttle) lives in the controller; this module is just the delta → band-index
Schmitt-trigger ladder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .const import DEFAULT_FAN_MODE_ORDER, DEFAULT_SCHMITT_ZONES


@dataclass(frozen=True)
class ProgressiveFanDecision:
    """Result returned by the progressive selector."""

    delta: float
    ordered_modes: tuple[str, ...]
    selected_index: int
    selected_mode: str | None


def _dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = str(value).strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def normalize_supported_modes(
    fan_modes: Sequence[str] | None,
    preferred_order: Sequence[str] | None = None,
) -> list[str]:
    """Return fan modes ordered from quietest to strongest.

    Only modes in preferred_order are used — vendor extras (``auto``, etc.)
    are dropped so the ladder can't fall back to them.
    """
    supported = _dedupe_preserve_order(fan_modes or [])
    if not supported:
        return []
    order = _dedupe_preserve_order(preferred_order or DEFAULT_FAN_MODE_ORDER)
    return [mode for mode in order if mode in supported]


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
    """
    if mode_count <= 0:
        return -1
    abs_delta = abs(float(delta))
    clamped_zones = list(zones)[:mode_count]

    # Highest band whose enter threshold is met — the promote target.
    promote_target = 0
    for k, (enter, _exit) in enumerate(clamped_zones):
        if k == 0:
            continue  # band 0 has no enter (implicit floor)
        if abs_delta >= enter:
            promote_target = k

    if prev_index is None:
        return max(0, min(promote_target, mode_count - 1))

    prev = max(0, min(prev_index, mode_count - 1))

    # Promote path: delta reaches a band above where we are.
    if promote_target > prev:
        return promote_target

    # Demote path: walk down as long as delta stays below each band's exit.
    if prev > 0 and prev < len(clamped_zones) and abs_delta < clamped_zones[prev][1]:
        target = prev - 1
        while target > 0 and abs_delta < clamped_zones[target][1]:
            target -= 1
        return target

    return prev


def choose_fan_mode(
    current_temperature: float,
    target_temperature: float,
    fan_modes: Sequence[str] | None,
    preferred_order: Sequence[str] | None = None,
    prev_index: int | None = None,
    zones: Sequence[tuple[float, float]] = DEFAULT_SCHMITT_ZONES,
) -> "ProgressiveFanDecision":
    """Choose the best fan mode for the current delta via Schmitt ladder."""
    ordered_modes = normalize_supported_modes(fan_modes, preferred_order)
    delta = abs(float(current_temperature) - float(target_temperature))
    selected_index = select_progressive_index(
        delta, len(ordered_modes), prev_index=prev_index, zones=zones,
    )
    return ProgressiveFanDecision(
        delta=delta,
        ordered_modes=tuple(ordered_modes),
        selected_index=selected_index,
        selected_mode=None if selected_index < 0 else ordered_modes[selected_index],
    )


def delta_band(delta: float, zones: Sequence[tuple[float, float]] = DEFAULT_SCHMITT_ZONES) -> str:
    """Human-friendly band name for logs — based on raw enter thresholds."""
    abs_delta = abs(float(delta))
    names = ("quietest", "low", "medium", "high", "max")
    idx = 0
    for k, (enter, _exit) in enumerate(zones):
        if k == 0:
            continue
        if abs_delta >= enter:
            idx = k
    return names[min(idx, len(names) - 1)]


def next_enter_threshold(
    current_index: int, zones: Sequence[tuple[float, float]] = DEFAULT_SCHMITT_ZONES
) -> float | None:
    """Enter threshold of the band above `current_index`, or None if top."""
    next_idx = current_index + 1
    if 0 < next_idx < len(zones):
        return zones[next_idx][0]
    return None
