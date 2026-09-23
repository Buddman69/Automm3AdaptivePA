# 2026-09-23 — QIDI_UPDATE halted a real printer; fixed

## What happened

The previous fix that day (`_ensure_sections()`, see
`2026-09-23-update-missing-sections.md`) handed EVERY name `_stage()`'s
`INSTALLABLE` regex matched - literally any `qidi_*.py` in the release -
straight to the code that adds `printer.cfg` sections. That set includes
`qidi_installer.py`, the Windows PC-side installer. It is valid, importable
Python (so `py_compile` and the existing import guard both pass it clean),
but it defines no `load_config` - it isn't a Klipper extra and was never
meant to be one.

`_ensure_sections()` added `[qidi_installer]` to `printer.cfg`. Klipper
tried to load it as an extra, found nothing behind the name, and halted:

```
Section 'qidi_installer' is not a valid config section
```

Correctly, too - it genuinely is not one. This is a real production
incident: Budd's printer would not start (no motion, no console commands,
nothing) until the bad section was removed from `printer.cfg` by hand.

## The fix

`_validate(gcmd, staging, names)` now returns the subset of `names` that
actually define `load_config` - checked by inspecting the real imported
module object in the same subprocess that already tests importing it, not
by guessing from the filename or maintaining a list of "known non-extra
files" that would need updating by hand every time a new companion script is
added. `_install()` passes that subset, not the full file list, to
`_ensure_sections()`. `qidi_installer.py` (and any future file like it)
still gets copied into `klippy/extras` exactly as before - that part was
always harmless, since Klipper does not care about a stray file nothing
references - it just never gets a `[section]` it has no `load_config` to
answer for.

## Why this wasn't caught by the compile/import guards

Those guards only ask "does this file crash before or during import." A
Python file that imports perfectly cleanly but simply is not a Klipper
extra passes both with no complaint - the failure mode here is entirely
about what Klipper's config loader does with the SECTION, not the file.
Nothing before this fix ever asked "does this module actually behave like a
Klipper extra."

## Tests

`tests/test_update.py`:

- `GOOD` (the fixture used everywhere else in this file) now defines
  `load_config`, matching what a real extra actually looks like - it did
  not before, which is exactly how this class of bug shipped in the first
  place without any existing test catching it.
- A new fixture, `NOT_AN_EXTRA`, is `qidi_installer.py`'s shape exactly:
  imports cleanly, no `load_config`.
- New case: a release containing one real extra and one `NOT_AN_EXTRA`
  file - both get copied into `klippy/extras`, but only the real extra gets
  a `printer.cfg` section; the console output names only the real extra.

## For the other printer

Q2's `qidi_update.py` was forked from an earlier copy of this same file and
almost certainly ships its own copy of `qidi_installer.py` in its releases
too (each model's installer is separately built - see
`2026-09-23-update-missing-sections.md`'s note on this). If Q2 gets this
same `_ensure_sections()` addition ported across, it needs this
`load_config` filter ported at the same time, not after - porting the first
half alone would reproduce this exact halt on a Q2 printer.
