# 2026-09-22 — the bed routine

## Why

Some filament does not clear the purge chute reliably. This adds a second,
complete calibration routine that does the same measurement over the CENTRE
OF THE BED instead — with the bed lowered clear of the nozzle — as an
addition alongside the existing chute routine, not a replacement for it.

## New files (nothing existing was touched)

| File | What |
|---|---|
| `qidi_flow_bed_search.py` | Stage 1 bed variant. `QIDI_FLOW_BED_SEARCH`, `QIDI_FLOW_BED_RAMP`, `QIDI_FLOW_BED_WIPE`. A full copy of `qidi_flow_ramp.py` — same settling logic, same abort thresholds, same search algorithm, same wipe geometry. Only the POSITIONING changed. |
| `qidi_pa_bed_measure.py` | Stage 3 bed variant. `QIDI_PA_BED_MEASURE`. A full copy of `qidi_pa_measure.py` — same tau-fitting maths, unchanged. Only the positioning changed. |
| `qidi_auto_cal_bed.py` | The orchestrator. `QIDI_AUTO_CALIBRATE_BED` and `QIDI_BED_PREPARE`. |

Stage 2 (`qidi_pa_envelope.py` / `QIDI_PA_ENVELOPE`) and Stage 4
(`qidi_pa_table.py` / `QIDI_PA_TABLE`) are **reused completely unmodified** —
both are pure maths with no motion, so there is nothing in them that could
differ between a chute run and a bed run.

## The new console commands

| Command | What |
|---|---|
| `QIDI_BED_PREPARE` | Home (always — see below), capture the bed-centre position, move to the chute, lower the bed. Motion only, no heat, no extrusion — standalone, so this new physical motion can be proven on its own first. |
| `QIDI_FLOW_BED_SEARCH` | Stage 1 at bed centre. Needs `QIDI_BED_PREPARE` run first. |
| `QIDI_FLOW_BED_WIPE` | Wipe at the chute, then return to bed centre. `[PASSES=2] [SHORT_PASSES=0] [RETURN=1]`. |
| `QIDI_PA_BED_MEASURE` | Stage 3 at bed centre. Needs `QIDI_BED_PREPARE` run first. |
| `QIDI_AUTO_CALIBRATE_BED` | The whole chain. |

## The routine, as specified

```
1.  QIDI_BED_PREPARE — G28 (ALWAYS, never skipped — see below), capture
    the bed-centre position, move to the chute, lower the bed
2.  Heat nozzle to TEMP (at the chute)
3.  Tare the load cell (AT BED CENTRE — see below, not at the chute)
4.  Wipe (2 long strokes) at the chute, then move to bed centre
5.  QIDI_FLOW_BED_SEARCH — Stage 1, at bed centre
6.  Wipe (2 long strokes) at the chute, then move to bed centre
7.  QIDI_PA_ENVELOPE — Stage 2, unchanged, pure maths
8.  QIDI_PA_BED_MEASURE — Stage 3, at bed centre
9.  Wipe (2 long strokes) at the chute, then move to bed centre
10. QIDI_PA_TABLE — Stage 4, unchanged, pure maths
11. Cooldown: heater off, wait <=170 C, wipe once more at the chute
    (does not return to bed centre), filtration off. Bed stays LOWERED.
```

Internal wipes during the search / per-block measurement (triggered every
~200 mm³, unchanged threshold) keep the full 6-long + 4-short pattern from
the chute routine — only the TOP-LEVEL between-stage wipes are the lighter
2-stroke version. Both round-trip through the chute now, since the wiper is
fixed hardware that cannot move with the bed.

## Two deliberate deviations from a literal read of the spec

**Homing is mandatory here, never skipped.** The chute routine skips G28 when
already homed, because the chute position it uses (`park_x`) is a fixed
config value unrelated to where G28 leaves the toolhead. Here, G28's resting
position IS the bed-centre reference the whole run is built on — captured
live, not hardcoded — so skipping it would mean trusting a position from
some earlier, unrelated home.

**Tare happens at bed centre, not at the chute**, even though "tare" reads as
a step before "move to bed centre" in the plain-English walkthrough. Every
existing stage in this project tares exactly where it is about to measure,
never somewhere else on the assumption it transfers — there is no evidence
the load cell's baseline is position-independent (cable strain could differ).
Since Stage 1 and Stage 3 both already tare internally, right where they are
about to extrude, this fell out naturally rather than needing a special case.

## The one new safety mechanism this needed

Bed centre is a **live position**, not a fixed macro constant like the
chute's `park_x`. `QIDI_BED_PREPARE` writes it to `bed_position.json`
(`{x, y, bed_z, time}`), and every later move there **verifies the toolhead's
actual current Z matches the recorded `bed_z`** before moving — refusing if
it does not. That mismatch means the bed was raised since (a real print ran,
someone re-homed), and moving to the recorded X/Y at today's Z could drive
the nozzle into the bed. Nothing else in this project needed this check,
because nothing else depended on a position that could go stale.

## Why Stage 1 and Stage 3's output files were NOT renamed

`QIDI_FLOW_BED_SEARCH` still writes `flow_ramp.json`; `QIDI_PA_BED_MEASURE`
still writes `pa_table.json` — the exact files the chute versions write.
Required, not an oversight: `QIDI_PA_TABLE` reads `pa_table.json` from a
fixed path with no override, and it is reused unmodified. Renaming the bed
output would have meant touching Stage 4, which is exactly what this
addition must not do.

**Residual risk accepted knowingly:** neither file marks an entry as "bed"
or "chute". Within one `QIDI_AUTO_CALIBRATE_BED` run this is always correct
(Stage 1 just ran and appended, so it is the most recent entry). A user
interleaving standalone chute and bed runs by hand could pick up the wrong
one for `QIDI_PA_ENVELOPE` without an explicit `QMAX=`. `QIDI_PA_TABLE`'s
run-averaging is protected in practice by its existing signature check
(exact flow points measured) — a bed run and a chute run coinciding there by
chance is vanishingly unlikely. Not a new category of risk; this already
existed between different chute runs.

## Bed-down parameters (Max4-specific — read before porting to another model)

```python
BED_DOWN_Z = 300.0      # confirmed safe by hand; 340 mm is the Max4's max
BED_DOWN_FEED = 1200.0  # mm/min - a starting point, not a verified limit
```

Both are `qidi_auto_cal_bed.py` config options (`bed_down_z`, `bed_down_feed`
under `[qidi_auto_cal_bed]`) and command overrides (`BED_Z=`, `BED_FEED=` on
`QIDI_BED_PREPARE` / `QIDI_AUTO_CALIBRATE_BED`) — **not hardcoded**
elsewhere. A port to another model needs, at minimum, its own safe Z and
travel clearance re-verified by hand the same way; nothing here assumes the
Max4's numbers.

`_move_to_bed_centre()` itself reads NO bed-size or nozzle config — it only
ever returns to whatever position `QIDI_BED_PREPARE`'s `G28` actually left
the toolhead at, captured live. That part should port to another model
without changes, provided the same fact holds there: **confirm G28 parks at
bed centre on that machine too before trusting this.**

## Tests

`tests/test_flow_bed_search.py`, `tests/test_pa_bed_measure.py`,
`tests/test_auto_cal_bed.py`. These do not re-prove the settling/abort/
tau-fitting maths — that logic is a byte-identical copy already covered by
the chute test suites. They cover what is actually new: command registration
under the `_BED` names, the Z-safety guard (including that a stale/mismatched
Z is refused and a small settling tolerance is not), every wipe round-tripping
through the chute, results still landing in the shared `flow_ramp.json` /
`pa_table.json`, mandatory (never skipped) homing, and the full chain order.
