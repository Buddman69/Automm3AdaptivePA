# 2026-11-20 — qidi_calibration_q2.cfg no longer stops Klipper starting

## Why

`qidi_calibration_q2.cfg` set `park_x`, `wipe_lo_offset` and
`wipe_hi_offset` in `[qidi_flow_ramp]`, `[qidi_pa_measure]`,
`[qidi_flow_bed_search]` and `[qidi_pa_bed_measure]`. None of the Q2
modules reads them. `_park_x()` returns a literal 85.0, and the wipe
offsets are module constants. Klipper rejects any option that no section
consumed (`configfile.py`, `check_unused_options`), so including the file
as shipped stops Klipper starting:

    Option 'park_x' is not valid in section 'qidi_flow_ramp'

Seen on a real Q2 when the file was included from `printer.cfg`.

## What changed

- `qidi_calibration_q2.cfg`: those twelve lines are now comments, with the
  values kept so the numbers are still documented. The header explains
  why.
- `README.md`: the paragraph that said `park_x` is set in the `.cfg` now
  says the modules hardcode it.
- New `tests/test_cfg_options.py`: it loads each `[qidi_*]` section's module
  with a config that records which options the module asks for, and fails
  on any option in the `.cfg` that nothing reads. Against the previous
  `.cfg` it fails on exactly those four sections.

## Later

If `park_x` or the wipe offsets ever become real options, uncomment them.
The test then confirms the module reads them.
