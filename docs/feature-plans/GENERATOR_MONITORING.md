# Feature Plan 11 — Generator Monitoring (DSE)

**Status: plan for review.** Part of the RAN Mines alignment
(`docs/RAN_MINES_PROPOSAL_ALIGNMENT.md`). Added after the requirements meeting surfaced
**5× Deep Sea Electronics (DSE) generators** needing fuel + run-hours monitoring (the client's
Phase 4, alongside the lab).

## Purpose

Monitor the site's standby/prime **DSE generators** — fuel consumption, run hours, and basic
health — and surface anomalies (abnormal burn, overdue service, unexpected run) as advisory
signals. Unlike a bare GNSS tracker, a genset has a **DSE controller** that already measures
these, so this is real measured data, not a fabricated sensor.

## Reuse vs new

- **Reuse:** the `fuel/` domain (measured fuel transactions/levels, measured-vs-calculated
  discipline) and the `maintenance/` domain (deterministic run-hours → service intervals,
  `Unknown` when data is thin); `event.v1` alarms; `assets` (each generator is an asset);
  device-credential model for the reader.
- **New:** a **DSE controller adapter** (Modbus TCP/RTU or DSE Gateway) that reads engine
  hours, fuel level/rate, running state, and fault flags, normalising into the existing fuel +
  maintenance inputs. No new event framework.

## Capabilities

- **Run hours → service intervals** (via `maintenance/`): each generator's engine hours drive
  the deterministic health indicator already built; a completed service resets it.
- **Fuel** (via `fuel/`): measured level/consumption where the controller exposes it; `l/h`
  burn rate is **calculated** and labelled as such; no value invented where the controller
  does not report it.
- **State & faults:** running/stopped/fault as observed; an unexpected run or a fault flag →
  `event.v1` (advisory).

## Data / contracts

- Generators are `assets` (asset_class `generator`). Readings flow into existing
  `fuel_transactions`/`fuel_tank_readings` (or a small `generator_readings` table if the shape
  differs) and the maintenance health inputs.
- Reuse `fuel.transaction.v1` / `maintenance.health.v1` where they fit; add a dedicated
  contract **only if** the DSE data genuinely doesn't map (decide when the controller spec is
  known — do not invent one speculatively).

## Phase 0 / integration dependencies (blocking)

- **DSE controller model** (e.g. DSE73xx/74xx/8xxx) and whether the gensets are on a network we
  can read (**Modbus TCP** is the usual path; DSE Gateway/cloud is an alternative but we stay
  on-prem).
- Register map / units per model; polling cadence.
- (See `QUESTIONS_FOR_DEREK.md` §E.)

## Slices

1. DSE Modbus adapter reads engine-hours + running state for one generator → maintenance
   health + an "unexpected run"/"fault" `event.v1`.
2. Fuel level/rate ingestion where exposed → fuel consumption summary (measured vs calculated).
3. Roll out to all 5; per-generator dashboard tiles.

## Safety / provenance

Advisory only — monitors and alerts, never controls a genset. Measured values labelled
`measured`; derived burn/health labelled `calculated`/`inferred`; `Unknown`/`None` where the
controller does not report, never fabricated.
