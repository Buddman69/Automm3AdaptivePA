# Changelog

One file per addition, named `YYYY-MM-DD-short-name.md`. Not a single growing
log — each entry stands alone so it can be linked to directly (e.g. from a
commit, a release, or when porting the same change to another model).

An entry should say: what was added, which files are new or changed, why it
needed to be a separate routine (if it did) rather than a change to an
existing one, and anything a later port to another model needs to know.

## Entries

- [2026-09-22 — the bed routine](2026-09-22-bed-routine.md) — `QIDI_AUTO_CALIBRATE_BED`,
  for filament that will not clear the purge chute reliably. New files only;
  nothing existing was changed.
- [2026-09-23 — the bed wizard](2026-09-23-bed-wizard.md) — `QIDI_CALIBRATE_BED`,
  the six-question dialog front door for the bed routine, matching
  `QIDI_CALIBRATE`. One new file; nothing existing was changed.
- [2026-09-23 — QIDI_UPDATE adds missing printer.cfg sections](2026-09-23-update-missing-sections.md) —
  a brand-new module's `.py` file installed but its `[section]` was never
  added, so Klipper never loaded it. Fixed in `qidi_update.py`; needs
  `QIDI_UPDATE` run twice to take effect on a printer that already hit it.
- [2026-09-23 — QIDI_UPDATE halted a real printer; fixed](2026-09-23-update-halted-printer.md) —
  the fix above added a section for `qidi_installer.py` too (not a Klipper
  extra), and Klipper refused to start. Fixed by only adding a section for
  files that actually define `load_config`.
