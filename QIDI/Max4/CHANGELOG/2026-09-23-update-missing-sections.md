# 2026-09-23 — QIDI_UPDATE now adds printer.cfg sections for brand-new modules

## The bug, found live

After `qidi-max4-v1.2.1` shipped `qidi_cal_wizard_bed.py`
(`QIDI_CALIBRATE_BED`), Budd ran `QIDI_UPDATE`, it reported success, Klipper
restarted - and `QIDI_CALIBRATE_BED` did not exist. No error either: Klipper
only imports a klippy extra whose `[section]` exists in `printer.cfg`, and
nothing had ever added `[qidi_cal_wizard_bed]`. The `.py` file was sitting in
`klippy/extras`, correctly copied, never loaded.

`qidi_update.py`'s `_install()` copies files into `klippy/extras` and never
touched `printer.cfg` at all. That gap was invisible until now because every
module `QIDI_UPDATE` had ever shipped already had its section from the
original `install.sh` / `.exe` run - `printer_setup.sh` (the fresh-install
path) has always added missing sections; `QIDI_UPDATE` (the ongoing-update
path) never did.

## The fix

`_install()` now calls a new `_ensure_sections()` after copying files, before
recording the install. For each just-installed module whose `[section]` is
not already in `printer.cfg`, it inserts an empty one above the
`#*# SAVE_CONFIG` marker - same anchor logic as `printer_setup.sh`'s sed
command (`^#\*# <-* SAVE_CONFIG`, insert before the first match, because
Klipper rewrites everything below that line on every save). `printer.cfg` is
backed up first (`printer.cfg.bak-upd-<timestamp>`), same backup-before-touch
discipline as the module files themselves.

**Best-effort, not a gate.** By the time this runs the files are already
validated (compiled + test-imported) and copied, so a `printer.cfg` that
cannot be found or written is reported to the console but does not undo an
otherwise-good install - the user can add the section by hand, same as a
`--dry-run` `install.sh` would have shown them to.

## How printer.cfg's path is found

`self.printer.get_start_args()['config_file']` - the same call Klipper's own
`configfile.py` uses internally to find the file it is about to implement
`SAVE_CONFIG` on. An override, `cfg_path` under `[qidi_update]`, exists as an
escape hatch but should not be needed.

## What this means for anyone who already hit this

Fixing `qidi_update.py` does not retroactively fix an install that already
ran the OLD version of it. `QIDI_UPDATE` has to be run **twice**:

1. First run installs the fixed `qidi_update.py` itself - using the
   currently-loaded (old, buggy) code, which still only copies files. This
   run does NOT add the missing section, because the code doing the copying
   is still the old code.
2. **Power cycle the printer** - the actual switch, off and on - to load the
   new `qidi_update.py`. See the correction below for why.
3. Second `QIDI_UPDATE` run (installing the same release again, or the next
   one) now executes the FIXED `_install()`, which checks every module in
   that release against `printer.cfg` and adds whatever is missing -
   including `[qidi_cal_wizard_bed]`, left over from the first run.

**CORRECTED 2026-09-23 - step 2 above originally said `FIRMWARE_RESTART`.
That is wrong.** Verified live: `FIRMWARE_RESTART`, `RESTART`, and Fluidd's
"Restart Klipper" button all just reinitialize objects inside the same
already-running Python process - checked directly on the affected printer,
where the klippy process's own PID and start time were unchanged after
three separate uses of that button. None of them clear Python's module
cache, so a module that was already imported (which, after the first
install, includes `qidi_update.py` itself) keeps running its old in-memory
code regardless of how many times any of those are used. Only an actual
power cycle is guaranteed to respawn the process. Full detail and the root
README's matching guidance: `2026-09-23-update-halted-printer.md` and the
root README's Updating / Troubleshooting sections.

## Tests

`tests/test_update.py` gained three cases: a release containing one already-
installed module and one brand-new one gets exactly the new module's section
added, above the marker, with the existing section and everything below the
marker untouched, and a backup taken; a release where every module's section
already exists changes `printer.cfg` not at all (byte-identical, no backup);
and a run with no discoverable `printer.cfg` (the common case for every
OTHER existing test in this file, and the honest fallback on a real host that
cannot find one) still completes the file install and records it as
installed - the missing config step degrades gracefully rather than aborting
a good install.

`tests/mock_klipper.py`'s `MockPrinter` gained `get_start_args()` (returning
`self._start_args`, empty by default) to support this - additive only,
every existing test that never sets it continues to exercise the
already-covered "printer.cfg not found" fallback path.
