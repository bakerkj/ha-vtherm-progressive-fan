# VTherm Progressive Fan

Home Assistant custom integration that progressively steps a climate entity's
`fan_mode` based on how far the room is from the target temperature set by a
[Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat).

Larger temperature deltas call for stronger fan speeds; smaller ones let the fan
settle down to `quiet`. Per-band hysteresis plus a demote dwell (promote fast,
demote slowly) keep the fan from flapping between adjacent speeds when the
underlying temperature signals are noisy.

## Features

- **Progressive ladder.** Configurable per-band ordering — the plugin walks
  through fan speeds you choose, quietest to strongest, based on the current
  delta between room temperature and Versatile Thermostat's target.
- **Anti-flap by design.** The target is smoothed with an EMA (so Versatile
  Thermostat's ~0.5 °F regulation nudges don't cross a band boundary), and a
  Schmitt-trigger ladder with per-band enter/exit thresholds keeps a delta
  hovering at a boundary from oscillating. Promotion is immediate — a room
  drifting off setpoint is answered at once — while demotion is held by a 240 s
  dwell, so the fan doesn't drop the instant the room brushes setpoint.
- **Stale read-back guard.** Polled climate integrations (e.g. mUART) publish a
  `fan_mode` optimistically on write and then republish the stale pre-write
  value seconds later, before the hardware acknowledges. Two guards handle it: a
  `state_changed` whose _only_ difference is `fan_mode` never triggers a
  re-decision, and the controller refuses to re-issue a mode it has already
  commanded while the band is unchanged — so one real transition produces one
  `set_fan_mode` call, not several.
- **Unit-aware.** Readings in °C, °F or K are normalized before the delta math,
  and a plausibility range rejects the `0.0`/`NaN`/absurd values some climate
  integrations emit at startup, treating them as sensor loss rather than a real
  temperature.
- **Sensor loss handling.** Freezes the current fan speed when a temperature
  reads `unavailable`, `unknown`, or implausible; logs a warning after 15 min.
- **Startup grace.** Silently no-ops for the first 5 min after init to ride out
  Versatile Thermostat's post-reload regulation instability. Toggling the paired
  switch off then on starts the ladder cold, so a re-enable re-evaluates from
  scratch rather than inheriting stale state.
- **Per-entity paired switch** to disable the auto-control temporarily without
  deleting the integration.

## Requirements

- Home Assistant with the
  [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat)
  integration installed.
- An underlying climate entity that exposes a `fan_modes` attribute with modes
  like `quiet`, `low`, `medium`, `high`, `very high`.

## Installation via HACS

1. Add this repository as a custom repository (Integration type) in HACS.
2. Install "VTherm Progressive Fan".
3. Restart Home Assistant.
4. Configure via **Settings → Devices & services → Add integration → VTherm
   Progressive Fan**.

## Configuration

For each underlying climate you want to auto-fan:

1. Pick the Versatile Thermostat that regulates the room.
2. Pick the underlying climate entity.
3. Optionally: pick a smoothed room-temperature sensor. Point this at whatever
   Versatile Thermostat regulates from (e.g.
   `sensor.<room>_average_temperature`) to eliminate the climate entity's
   quantization noise on the readback path (many climates report the current
   temperature rounded to 0.5 °F, which drives boundary-graze band flap). When
   left empty, the plugin uses the underlying climate entity's own
   `current_temperature`.
4. Pick the fan modes to use, in order from quietest to strongest.

A paired switch entity named **Progressive Fan** is created for each instance
and attached to the Versatile Thermostat's device, so it appears alongside the
thermostat's own entities (displayed as "&lt;VTherm name&gt; Progressive Fan").
Toggling it off stops the integration from writing `fan_mode` without deleting
the config entry. The switch is a `config` entity and its state is restored
across restarts.

## Attribution

Original concept and initial implementation by
[@roumano](https://github.com/roumano) as
[`vtherm_auto_fan_progessif`](https://github.com/roumano/vtherm_auto_fan_progessif).
This project is a rewrite that shares the spirit but has its own architecture
(EMA-damped target, per-band enter/exit thresholds, a demote dwell, stale
read-back suppression, sensor-loss handling, paired switch).

## License

GNU General Public License v3.0 or later — see [LICENSE](./LICENSE).
