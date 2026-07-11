"""Constants for the VTherm Progressive Fan integration."""

from __future__ import annotations

DOMAIN = "vtherm_progressive_fan"
CONF_VTHERM_ENTITY_ID = "vtherm_entity_id"
CONF_CLIMATE_ENTITY_ID = "climate_entity_id"
CONF_FAN_MODE_ORDER = "fan_mode_order"
# Optional override — a sensor whose state is the room's smooth current
# temperature. When set, the snapshot uses it for `current_temperature`.
# When empty, the snapshot falls through: VTherm.current_temperature →
# mini-split.current_temperature. Point this at whatever VTherm itself
# uses (typically sensor.<room>_average_temperature) to eliminate the
# mini-split's 0.5°F quantization noise.
CONF_CURRENT_TEMPERATURE_SENSOR = "current_temperature_sensor_entity_id"
DEFAULT_FAN_MODE_ORDER = ["quiet", "auto", "low", "middle", "medium", "high", "turbo"]

# Schmitt-trigger ladder: per-band (enter, exit) thresholds on |delta| in °F.
# Rising delta crosses enter[k] to promote INTO band k. Falling delta crosses
# below exit[k] to demote OUT OF band k. Zone 0 is the quietest band; its
# enter/exit are placeholders since band 0 has no entrance from below
# (you can't demote below quiet) and no lower exit. Hysteresis is 0.35-0.50°F
# per boundary. A 6th zone is included for users with a "turbo" mode above
# very_high; setups with fewer modes only consult the leading zones.
DEFAULT_SCHMITT_ZONES = (
    (0.00, 0.00),   # band 0 — quiet (no entry-from-below, no lower exit)
    (0.75, 0.40),   # band 1 — low
    (1.75, 1.30),   # band 2 — medium
    (3.00, 2.50),   # band 3 — high
    (4.50, 4.00),   # band 4 — very_high
    (6.00, 5.50),   # band 5 — turbo (present for 6-mode setups; unused otherwise)
)

# Target temperature EMA time constant. VTherm nudges its target by 0.5°F
# every 5-10 min as it converges. A 90s EMA damps a single 0.5°F step to
# ~0.16°F peak amplitude — comfortably inside the smallest ladder deadband
# (0.35°F). The room sensor is expected to already be a smoothed multi-
# sensor average, so it doesn't need additional EMA.
DEFAULT_TARGET_EMA_TAU_SECONDS = 90.0

# When |raw target − ema| exceeds this, snap the EMA to the new target
# instead of damping. A preset change (Eco → Boost, ±4°F) or manual
# target edit is a user-intent step, not regulation noise — the fan
# should respond right away instead of crawling up over minutes.
DEFAULT_TARGET_EMA_JUMP_THRESHOLD = 1.5

# Minimum seconds between two consecutive set_fan_mode commands. Bounds
# same-second event storms (integration reload, coordinator refresh, listener
# fan-out). Applies to both promote and demote decisions.
DEFAULT_MIN_APPLY_INTERVAL_SECONDS = 10.0

# Seconds after plugin init during which async_apply_now silently no-ops.
# Rides out VTherm's post-reload regulation instability — auto_regulation
# accumulators / EMA state reset to zero at reload and can drive the
# underlying target 2°F either direction for the first few minutes before
# converging.
DEFAULT_STARTUP_DELAY_SECONDS = 120.0

# Promote-dwell: minimum seconds between entering a band and allowing a
# further promote to a higher band. Short — a real warming room should get
# response quickly. The fast-lane below can bypass this entirely when the
# room is way off setpoint.
DEFAULT_PROMOTE_DWELL_SECONDS = 30.0

# Demote-dwell: minimum seconds between entering a band and allowing a
# demote to a lower band. Long enough to smooth out VTherm's regulation-
# cycle noise (0.5°F target nudges every 5-10 min) without holding a
# needlessly-loud fan too long after a real cool-down completes.
DEFAULT_DEMOTE_DWELL_SECONDS = 240.0

# Fast-lane margin: if delta exceeds the next band's enter threshold by
# this margin, bypass the promote dwell entirely and jump straight to the
# best-matching band. Prevents artificial cool-down lag when the room is
# clearly a lot hotter than a boundary-graze case.
DEFAULT_FAST_LANE_MARGIN = 1.0

# Sensor-unavailable warn threshold: log a warning when either the current
# or target sensor has been None / unavailable this long. The plugin
# silently holds the current fan speed during unavailability; the warn is
# just an operational nudge.
DEFAULT_SENSOR_UNAVAIL_WARN_SECONDS = 900.0
