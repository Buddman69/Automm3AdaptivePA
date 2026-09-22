# 2026-09-22 — the bed routine

Ported from `QIDI/Max4/CHANGELOG/2026-09-22-bed-routine.md`, same day. That
entry has the full reasoning for the design; this one records what differs
for this Q2 and one deliberate deviation made while porting it.

## Why

Some filament does not clear the purge chute reliably. This adds a second,
complete calibration routine that does the same measurement over the CENTRE
OF THE BED instead — with the bed lowered clear of the nozzle — as an
addition alongside the existing chute routine, not a replacement for it.

## New files (nothing existing was touched)

| File | What |
|---|---|
| `qidi_flow_bed_search.py` | Stage 1 bed variant. `QIDI_FLOW_BED_SEARCH`, `QIDI_FLOW_BED_RAMP`, `QIDI_FLOW_BED_WIPE`. A full copy of this Q2's `qidi_flow_ramp.py` — same settling logic, same abort thresholds, same search algorithm, same wipe geometry, same Q2-specific values (201 counts/gf, `park_x` 85, wipe offsets 10/30, `MOVE_TO_TRASH`). Only the POSITIONING changed. |
| `qidi_pa_bed_measure.py` | Stage 3 bed variant. `QIDI_PA_BED_MEASURE`. A full copy of this Q2's `qidi_pa_measure.py` — same tau-fitting maths, same Q2 values, unchanged. Only the positioning changed — see the one deviation below. |
| `qidi_auto_cal_bed.py` | The orchestrator. `QIDI_AUTO_CALIBRATE_BED` and `QIDI_BED_PREPARE`. |

Stage 2 (`qidi_pa_envelope.py` / `QIDI_PA_ENVELOPE`) and Stage 4
(`qidi_pa_table.py` / `QIDI_PA_TABLE`) are **reused completely unmodified** —
both are pure maths with no motion, so there is nothing in them that could
differ between a chute run and a bed run, or between this Q2 and the X-Max 4.

## The new console commands

Identical set to the Max4 side:

| Command | What |
|---|---|
| `QIDI_BED_PREPARE` | Home (always — see below), capture the bed-centre position, move to the chute, lower the bed. Motion only, no heat, no extrusion — standalone, so this new physical motion can be proven on its own first. |
| `QIDI_FLOW_BED_SEARCH` | Stage 1 at bed centre. Needs `QIDI_BED_PREPARE` run first. |
| `QIDI_FLOW_BED_WIPE` | Wipe at the chute, then return to bed centre. `[PASSES=2] [SHORT_PASSES=0] [RETURN=1]`. |
| `QIDI_PA_BED_MEASURE` | Stage 3 at bed centre. Needs `QIDI_BED_PREPARE` run first. |
| `QIDI_AUTO_CALIBRATE_BED` | The whole chain. |

## The routine, as specified — same order as Max4

```
1.  QIDI_BED_PREPARE — G28 (ALWAYS, never skipped — see below), capture
    the bed-centre position, move to the chute, lower the bed
2.  Heat nozzle to TEMP (at the chute)
3.  Tare the load cell (AT BED CENTRE — not at the chute)
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

Internal wipes during the search / per-block measurement keep this Q2's full
6-long + 4-short pattern from the chute routine — only the TOP-LEVEL
between-stage wipes are the lighter 2-stroke version. Both round-trip through
the chute now, since the wiper is fixed hardware that cannot move with the
bed.

Same two deliberate deviations from a literal read of the spec as Max4's
entry documents (mandatory homing here, tare at bed centre not the chute),
and the same bed-position safety mechanism (`bed_position.json`, refusing a
stale Z) — none of that is machine-specific, so it is not repeated here.

## Bed-down parameters — Q2-specific, read before porting again

```python
BED_DOWN_Z = 200.0      # Budd's instructed value
BED_DOWN_FEED = 1200.0  # mm/min - same figure Budd gave for the Max4
```

**The real max travel is 265 mm, not 256 mm.** Budd's instruction quoted
256 mm as the reason 200 mm is safe. QIDI's own `printer.cfg` on this Q2
(`[stepper_z] position_max: 265`) puts the real ceiling at 265 mm — 9 mm more
than assumed. This does not make anything less safe: 200 mm clears either
figure, and the true margin (65 mm) is larger than the one the instruction
was reasoning from (56 mm), not smaller. Recorded here so the number in the
code and the number in anyone's head agree. Compare Max4: 300 mm drop against
a 340 mm confirmed max, a 40 mm margin — this Q2's 65 mm margin is
proportionally larger.

Both are `qidi_auto_cal_bed.py` config options (`bed_down_z`, `bed_down_feed`
under `[qidi_auto_cal_bed]`) and command overrides (`BED_Z=`, `BED_FEED=` on
`QIDI_BED_PREPARE` / `QIDI_AUTO_CALIBRATE_BED`) — not hardcoded elsewhere,
same as Max4.

`_move_to_bed_centre()` reads no bed-size or nozzle config, same as Max4 — it
only returns to wherever `QIDI_BED_PREPARE`'s `G28` actually left the
toolhead, captured live. **This has not been independently confirmed on a
real Q2** — ported on the stated assumption that G28 parks at bed centre here
too, matching the X-Max 4. Confirm this by hand on first run, same as any
other new physical motion in this project.

## One deviation from a literal port: the motion-limits capture

Max4's `qidi_pa_measure.py` (the file `qidi_pa_bed_measure.py` was copied
from, there and here) captures the machine's motion limits **twice** — once
before any motion, once again after the move to the working position. That
second capture is a live, undocumented bug on the Max4 side: its
`MOVE_TO_TRASH` sets its own `M204` and never restores it, so the second
capture silently picks up the macro's acceleration instead of the machine's
real configured one. It is invisible on an X-Max 4 only because the macro's
value (10000) and the machine's real `max_accel` (also 10000) happen to
match.

This Q2's own `qidi_pa_measure.py` already fixed that exact pattern (single
capture, before anything moves) in an earlier round of work, independently of
this bed-routine addition. `qidi_pa_bed_measure.py`'s own second capture sits
after `_move_to_bed_centre()`, which is a plain `G1` with **no** macro side
effect — so it is harmless here, not dangerous the way it is on the chute
routine. Removed anyway, for one reason: carrying two different rules for
when it is safe to re-capture (never, except here, because this particular
move happens not to have a side effect) is exactly the kind of inconsistency
that produces the original bug in the first place. One rule — capture once,
before anything moves — applies to every module in this codebase now.

**Not fixed on the Max4 side, and not going to be from here.** This Q2
project does not alter `QIDI/Max4/` under any circumstances. Worth someone
picking up over there independently.

## Why Stage 1 and Stage 3's output files were NOT renamed

Same as Max4: `QIDI_FLOW_BED_SEARCH` still writes `flow_ramp.json`;
`QIDI_PA_BED_MEASURE` still writes `pa_table.json`. Required, not an
oversight — `QIDI_PA_TABLE` reads `pa_table.json` from a fixed path with no
override, and it is reused unmodified. The same residual risk Max4's entry
documents (an interleaved standalone bed/chute run picking up the wrong
entry) applies identically here.

## Tests

`tests/test_flow_bed_search.py`, `tests/test_pa_bed_measure.py`,
`tests/test_auto_cal_bed.py` — ported from the Max4 equivalents the same way
as the modules themselves (Q2 values substituted: `MOVE_TO_TRASH`, `Z200`
instead of `Z300`). Plus one Q2-only addition: `test_pa_bed_measure.py` and
its chute sibling `test_pa_measure.py` both now assert the motion-limits
capture happens exactly once, before any motion — a regression test for the
deviation above, which does not exist on the Max4 side because the bug it
guards against is still live there.

## Status

**Not yet run on a real Q2.** Every value here comes from this Q2's own
load-cell measurements and its firmware's own `printer.cfg` — but the bed
routine specifically involves new physical motion (a 200 mm bed drop, and
extrusion at bed centre) that has not been proven on this machine, or on any
Q2, the way it has on the X-Max 4. `QIDI_BED_PREPARE` on its own — no heat,
no extrusion — is the first thing to run, watched, before trusting the rest
of the chain.
