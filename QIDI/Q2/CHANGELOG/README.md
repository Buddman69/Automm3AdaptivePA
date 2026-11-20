# Changelog

One file per addition, named `YYYY-MM-DD-short-name.md`. Not a single growing
log — each entry stands alone so it can be linked to directly (e.g. from a
commit, a release, or when porting the same change to another model).

An entry should say: what was added, which files are new or changed, why it
needed to be a separate routine (if it did) rather than a change to an
existing one, and anything a later port to another model needs to know.

This folder's own entries are Q2-specific. See `QIDI/Max4/CHANGELOG/` for the
Max4 side of the same history — most additions here are a port of one there,
using this machine's own measured values rather than copying the X-Max 4's.

## Entries

- [2026-09-22 — the bed routine](2026-09-22-bed-routine.md) — `QIDI_AUTO_CALIBRATE_BED`,
  for filament that will not clear the purge chute reliably. Ported from
  `QIDI/Max4`'s same-day entry, using this Q2's own measured values. New files
  only; nothing existing was changed.
- [2026-09-23 — the bed wizard](2026-09-23-bed-wizard.md) — `QIDI_CALIBRATE_BED`,
  the dialog front door for the bed routine, matching `QIDI_CALIBRATE`. Ported
  from `QIDI/Max4`'s same-day entry. New file only; zero substitution needed
  in its test.
- [2026-09-23 — QIDI_UPDATE adds sections for brand-new modules](2026-09-23-update-sections.md) —
  fixes `QIDI_UPDATE` silently installing a brand-new module's files without
  ever adding its `printer.cfg` section, so the command never registered.
  Ports two Max4 entries as one, since this Q2 goes straight to the fixed
  result and never shipped the intermediate broken state Max4 hit live.
- [2026-11-20 — open the purge chute's flap](2026-11-20-chute-flap.md) — the
  toolhead holds the chute's flap shut from the park to X122, so every purge
  piled up under the nozzle on the load cell. Wipes, Stage 1 measurements
  and Stage 3 primes now end with a trip to X125 and back the
  `MOVE_TO_TRASH` way (via Y270). `qidi_flow_ramp.py`, `qidi_pa_measure.py`
  and their tests.
- [2026-11-20 — Stage 1's force abort follows counts_per_gf](2026-11-20-force-abort.md) —
  the abort was built from the module default (201), so a cell configured
  at 178 stopped at ~1863 gf instead of 1650. `qidi_flow_ramp.py`,
  `qidi_flow_bed_search.py` and their tests.
