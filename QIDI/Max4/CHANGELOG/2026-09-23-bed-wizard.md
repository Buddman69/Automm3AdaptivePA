# 2026-09-23 — QIDI_CALIBRATE_BED, the dialog front door for the bed routine

## Why

`QIDI_AUTO_CALIBRATE_BED` (2026-09-22) only ever ran off command-line
parameters and their defaults - no dialog, unlike the chute routine, which has
had `QIDI_CALIBRATE` asking six questions in Fluidd since before the bed
routine existed. This closes that gap: `QIDI_CALIBRATE_BED` asks the same six
questions, the same way, then runs `QIDI_AUTO_CALIBRATE_BED` with the
answers.

## New file (nothing existing was touched)

| File | What |
|---|---|
| `qidi_cal_wizard_bed.py` | `QIDI_CALIBRATE_BED` and its internal `_QIDI_CAL_BED_STEP`. A full copy of `qidi_cal_wizard.py`'s dialog state machine - same six screens (temperature, nozzle, geometry, precision, flow points, review), same Fluidd verb set, same steppers-not-presets reasoning, same "nothing runs until START" guarantee. Only the command names and the text that names which routine is about to run changed. |

Registers its own command names throughout, not the chute wizard's -
`QIDI_CALIBRATE_BED` / `_QIDI_CAL_BED_STEP`, distinct from `QIDI_CALIBRATE` /
`_QIDI_CAL_STEP`. Both wizards are meant to be installed and loaded at once;
Klipper's gcode dispatcher raises if two extras register the same command
name, so this was a real constraint, not a style choice - covered by a test
that loads both wizards into one gcode dispatcher and checks neither
overwrites the other.

## What changed vs. what didn't

The review screen's warning text is the one place the wording had to differ:
it says the run "HEATS and EXTRUDES over the BED CENTRE, bed lowered clear of
the nozzle - not the purge chute" instead of naming the chute. The screen
titles say "Calibrate Bed N/6" instead of "Calibrate N/6", so it is obvious
which routine's dialog is open. Every number, clamp, stepper size, and default
(`TEMP_MIN`/`MAX`, `BLOCKS_CHOICES`, `POINTS_CHOICES`, `NOZZLE_GEOM`,
`GEOM_STEPS`) is identical to the chute wizard's, unchanged - there is no
reason for the bed routine to accept a different range of anything, since
`QIDI_AUTO_CALIBRATE_BED` takes the same `TEMP` bounds (`above=150,
below=350`) as `QIDI_AUTO_CALIBRATE`.

The command line the review screen builds and START runs is
`QIDI_AUTO_CALIBRATE_BED TEMP=... BLOCKS=... POINTS=... LAYER=... WIDTH=...`
- the bed orchestrator, never the chute one.

## Installer wiring

Added to the `CORE` / `CORE_SECTIONS` module lists in `install.sh`,
`qidi_installer.py`, and the `$Modules` list in `build_exe.ps1`, alongside the
other three bed-routine files - it needs an empty `[qidi_cal_wizard_bed]`
section in `printer.cfg`, same as every other module here, and
`printer_setup.sh` adds that automatically from the section list it is
handed. `qidi_update.py`'s `INSTALLABLE` filename pattern already matches
`qidi_cal_wizard_bed.py` with no changes needed there.

## Tests

`tests/test_cal_wizard_bed.py`. Mirrors `test_cal_wizard.py`'s coverage
(dialog well-formedness, no dead buttons, nothing runs before START, a full
click-through produces the exact expected `QIDI_AUTO_CALIBRATE_BED` command
line, dry run, pre-seeding, temperature clamping, Cancel, half-answered START
refused, bad input refused, a stray exception converted to a command error
rather than an MCU shutdown) plus one check specific to this addition: both
wizards loaded into the same gcode dispatcher register distinct commands,
neither overwriting the other.
