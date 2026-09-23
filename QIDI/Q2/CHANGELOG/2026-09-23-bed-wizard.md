# 2026-09-23 — QIDI_CALIBRATE_BED, the dialog front door for the bed routine

Ported from `QIDI/Max4/CHANGELOG/2026-09-23-bed-wizard.md`, same day. That
entry has the full reasoning; this Q2 copy carries this machine's own values
(0.4 mm nozzle default), same as every other file in this folder.

## Why

`QIDI_AUTO_CALIBRATE_BED` (2026-09-22) only ever ran off command-line
parameters and their defaults — no dialog, unlike the chute routine, which
has had `QIDI_CALIBRATE` asking six questions in Fluidd since before the bed
routine existed. This closes that gap: `QIDI_CALIBRATE_BED` asks the same six
questions, the same way, then runs `QIDI_AUTO_CALIBRATE_BED` with the
answers.

## New file (nothing existing was touched)

| File | What |
|---|---|
| `qidi_cal_wizard_bed.py` | `QIDI_CALIBRATE_BED` and its internal `_QIDI_CAL_BED_STEP`. A full copy of this Q2's `qidi_cal_wizard.py` dialog state machine — same six screens, same Fluidd verb set, same steppers-not-presets reasoning, same "nothing runs until START" guarantee, same 0.4 mm nozzle default. Only the command names and the text naming which routine is about to run changed. |

Registers its own command names throughout, not the chute wizard's —
`QIDI_CALIBRATE_BED` / `_QIDI_CAL_BED_STEP`, distinct from `QIDI_CALIBRATE` /
`_QIDI_CAL_STEP`. Both wizards are meant to be installed and loaded at once;
a test loads both into one gcode dispatcher and checks neither overwrites the
other.

## What changed vs. what didn't — same as Max4's port

The review screen's warning text names the bed centre and the lowered bed
instead of the chute; the screen titles say "Calibrate Bed N/6" instead of
"Calibrate N/6". Every number, clamp, stepper size, and default
(`TEMP_MIN`/`MAX`, `BLOCKS_CHOICES`, `POINTS_CHOICES`, `NOZZLE_GEOM`,
`GEOM_STEPS`) is identical to this Q2's own chute wizard, unchanged — there
is no reason for the bed routine to accept a different range of anything.

The command line the review screen builds and START runs is
`QIDI_AUTO_CALIBRATE_BED TEMP=... BLOCKS=... POINTS=... LAYER=... WIDTH=...`
— this Q2's bed orchestrator, never the chute one.

## Installer wiring

Added to `CORE` / `CORE_SECTIONS` in `install.sh` and `qidi_installer.py`,
and to the `$Modules` list in `build_exe.ps1` — same treatment as the other
three bed-routine files. `qidi_calibration_q2.cfg` gets an empty
`[qidi_cal_wizard_bed]` section with `nozzle_diameter: 0.4`, matching the
chute wizard's own section.

## Tests

`tests/test_cal_wizard_bed.py` — a verbatim copy of Max4's test file. Checked
against this Q2's own chute wizard test (`test_cal_wizard.py`) first: the two
are byte-identical between the two model folders, because every assertion
either supplies an explicit `NOZZLE=` value or exercises the shared
`NOZZLE_GEOM` lookup table across all four standard sizes — none of it
depends on which nozzle happens to be a given printer's own default. The bed
wizard test has the same shape, so it needed zero substitution to port.

## Status

Same as the rest of the bed routine: not yet run on a real Q2.
