# 2026-09-23 — QIDI_UPDATE now adds printer.cfg sections for brand-new modules

Ports two Max4 entries as one, because this Q2 goes straight to the fixed
result and never shipped the intermediate broken state that Max4 hit live.
Both `QIDI/Max4/CHANGELOG/2026-09-23-update-missing-sections.md` and
`QIDI/Max4/CHANGELOG/2026-09-23-update-halted-printer.md` have the full
incident detail; read those for the "why", not just the "what", before
touching this code again.

## The gap, and the bug the first fix would have introduced here too

`qidi_update.py`'s `_install()` copies files into `klippy/extras` and never
touched `printer.cfg`. That was invisible until a brand-new module shipped in
a release for the first time — the ordinary install path (`printer_setup.sh`,
shared by `install.sh` and the `.exe`) has always added missing sections; the
ongoing-update path never did. On Max4, this was found live: `qidi_update.py`
reported success installing `qidi_cal_wizard_bed.py`, but `QIDI_CALIBRATE_BED`
never registered, because Klipper only loads an extra whose `[section]`
exists.

The first attempt at fixing it added a section for **every** name the
release's own `qidi_*.py` filter matched — which includes `qidi_installer.py`,
a real installed file, importable and clean, but not a Klipper extra and
defining no `load_config`. On Max4 that halted a real printer:
`Section 'qidi_installer' is not a valid config section`. **This Q2 never ran
that intermediate version** — the fix landed here already carrying the
`load_config` filter, so the failure mode described in Max4's second entry is
not a risk this Q2 was ever exposed to. It is documented here only because
the next person touching this file needs to know why the filter exists and
must never be removed on its own.

## What actually changed

`_install()` now calls `_ensure_sections()` after copying files, before
recording the install — but only for the subset of just-installed modules
that `_validate()` confirms actually define `load_config`, checked by
inspecting the real imported module object in the same subprocess that
already tests importing it. For each of those whose `[section]` is missing,
an empty one is inserted above the `#*# SAVE_CONFIG` marker — same anchor
logic as `printer_setup.sh`. `printer.cfg` is backed up first
(`printer.cfg.bak-upd-<timestamp>`).

**Best-effort, not a gate.** A `printer.cfg` that cannot be found or written
is reported to the console but does not undo an otherwise-good install.

`printer.cfg`'s path comes from
`self.printer.get_start_args()['config_file']` — the same call Klipper's own
`configfile.py` uses. An override, `cfg_path` under `[qidi_update]`, exists
as an escape hatch.

## For anyone on a Q2 who already hit the ORIGINAL gap

If `QIDI_UPDATE` was ever used on this Q2 before this update landed, to
install a brand-new module (as opposed to updating an existing one), the same
two-step recovery Max4's entry describes applies:

1. Run `QIDI_UPDATE` once — installs this fixed `qidi_update.py` itself,
   using the still-old, already-running code, which only copies files.
2. **Power cycle the printer** — the actual switch, off and on. `RESTART`,
   `FIRMWARE_RESTART`, and Fluidd's "Restart Klipper" button do not clear
   Python's module cache; verified directly on the affected Max4 printer,
   where the klippy process's own PID and start time were unchanged after
   all three. Only a real power cycle respawns the process and loads the new
   code.
3. Run `QIDI_UPDATE` again — now executes the fixed `_install()`, which
   checks every module in the release against `printer.cfg` and adds
   whatever section is missing.

## Files ported unchanged — zero Q2-specific substitution needed

Checked before porting, not assumed: `qidi_update.py`,
`tests/test_update.py`, and `tests/mock_klipper.py` on this Q2 were, before
this change, byte-for-byte identical to Max4's own copies as they stood the
day before this fix (`qidi_update.py`'s brand/model dispatch is generic —
`PRETTY = {'qidi': 'QIDI', 'max4': 'Max4', 'q2': 'Q2', ...}` — there is no
per-model value anywhere in it to substitute). All three are therefore
carried across verbatim from Max4's current, fixed versions.

`mock_klipper.py`'s change is purely additive — `MockPrinter` gained
`get_start_args()` returning an empty dict by default — confirmed by diffing
before porting, so nothing already relying on the old shape could break.

## Tests

`tests/test_update.py` carries Max4's three new cases, all passing here
unchanged: a release with one already-installed module and one brand-new one
gets exactly the new module's section added, above the marker, with
everything else untouched and a backup taken; a release where every section
already exists changes `printer.cfg` not at all; and a run with no
discoverable `printer.cfg` still completes the file install and records it —
the missing-config case degrades gracefully.

The specific regression test for the printer-halting bug —
`qidi_installer.py` gets copied but never gets a section, while a genuine
extra in the same release does — is present and passing here.
