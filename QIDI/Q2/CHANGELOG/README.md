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
