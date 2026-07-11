# VTherm Progressive Fan

Home Assistant custom integration that progressively steps a mini-split's
`fan_mode` based on how far the room is from the target temperature set by a
[Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat).

Larger temperature deltas call for stronger fan speeds; smaller ones let the
fan settle down to `quiet`. A Schmitt-trigger ladder with per-band hysteresis
plus temporal dwells (fast promote, slow demote) keeps the fan from flapping
between adjacent speeds when the underlying temperature signals are noisy.

## Features

- **Progressive ladder.** Configurable per-band ordering — the plugin walks
  through fan speeds you choose, quietest to strongest, based on the current
  delta between room temperature and Versatile Thermostat's target.
- **Anti-flap by design.** Signal smoothing (EMA on target), Schmitt-trigger
  band boundaries (per-band enter/exit thresholds), asymmetric dwells (30 s
  promote / 240 s demote), and a fast-lane bypass when the room is genuinely
  far off setpoint.
- **Sensor loss handling.** Freezes the current fan speed when either
  temperature reads `unavailable`; logs a warning after 15 min.
- **Startup grace.** Silently no-ops for the first 2 min after init to ride
  out Versatile Thermostat's post-reload regulation instability.
- **Per-entity paired switch** to disable the auto-control temporarily
  without deleting the integration.

## Requirements

- Home Assistant with the
  [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat)
  integration installed.
- The underlying climate entity (typically a mini-split) must expose a
  `fan_modes` attribute with modes like `quiet`, `low`, `medium`, `high`,
  `very high`.

## Installation via HACS

1. Add this repository as a custom repository (Integration type) in HACS.
2. Install "VTherm Progressive Fan".
3. Restart Home Assistant.
4. Configure via **Settings → Devices & services → Add integration → VTherm
   Progressive Fan**.

## Configuration

For each mini-split you want to auto-fan:

1. Pick the Versatile Thermostat that regulates the room.
2. Pick the underlying climate entity (the mini-split itself).
3. Optionally: pick a smoothed room-temperature sensor. Point this at
   whatever Versatile Thermostat regulates from (e.g.
   `sensor.<room>_average_temperature`) to eliminate the mini-split's
   0.5 °F quantization noise on the readback path. When left empty, the
   plugin uses the underlying climate entity's own `current_temperature`.
4. Pick the fan modes to use, in order from quietest to strongest.

A paired switch entity is created for each instance —
`switch.<room>_progressive_auto_fan` — that toggles the auto-control on
and off.

## Attribution

Original concept and initial implementation by
[@roumano](https://github.com/roumano) as
[`vtherm_auto_fan_progessif`](https://github.com/roumano/vtherm_auto_fan_progessif).
This project is a rewrite that shares the spirit but has its own architecture
(EMA-damped target, Schmitt-trigger ladder with per-band hysteresis,
promote/demote dwells, fast-lane bypass, sensor-loss handling, paired switch).

## License

GNU General Public License v3.0 or later — see [LICENSE](./LICENSE).
