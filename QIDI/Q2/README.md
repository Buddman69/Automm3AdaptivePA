# QIDI Q2 — automatic flow + pressure-advance calibration

**My Reddit —** <https://www.reddit.com/user/Sport_Subject/>

**Help me get things I need to make more things, anything helps :)**
<https://www.amazon.com.au/hz/wishlist/ls/2ZFL750GMOMU1?ref_=wl_share>

---

> ## ⚠ UNTESTED ON A Q2
>
> Every Q2 value here was read out of QIDI's own firmware and verified against
> two releases — but **nothing in this folder has ever run on a Q2.** It has
> only been run on an X-Max 4.
>
> The load cell, extruder and toolhead firmware are identical between the two
> machines. The bed, the park position and the wiper are not, and those are
> what move a hot nozzle around.
>
> **First run watched, with your hand near the power switch.** Do the dry run
> first — it heats nothing and moves nothing.
>
> ## ⚠ REQUIRES FIRMWARE `01.01.02.04` OR NEWER
>
> Verified only against QIDI's `01.01.02.04` (2026-08-05) release and the
> matching 2026-01 GitHub source. **Older firmware is not supported and there
> is no plan to support it.** On at least one printer running `1.1.1`, the
> load cell's driver exposes `read_origin_data` differently — the calibration
> refuses to run rather than guess, with `flow_ramp: read_origin_data is not
> callable` in the console. That refusal is the safety design working
> correctly, not a crash; nothing was damaged. **Update the printer's
> firmware to `01.01.02.04` or newer before installing.**

---

## Why a Q2 build at all

The toolhead is the same. The machine around it is not.

Verified against QIDI's 2026-01 GitHub release **and** the current
`01.01.02.04` (2026-08-05) firmware package — the values below are identical in
both, so they are stable across releases:

| | Q2 | X-Max 4 |
|---|---|---|
| `[probe_air]` | `c_sensor`, `THR:PB3`/`PB4`, `voltage 4.95`, `delta_v 0.08` | **identical** |
| Extruder | `rotation_distance 53.7`, `gear_ratio 1517:170` | **identical** |
| Extrusion guards | 5000 / 500 / 1000 — all disabled | **identical** |
| Toolhead firmware | `THR_02.02.01.08` | **byte-identical** |
| **Nozzle** | **0.4** | 0.6 |
| **Bed** | **275 × 295 × 265** | 390 × 390 |
| **Park / trash** | **X85 Y287.5** | X135 Y403 |
| **Wiper** | **X95 – X115** | X143 – X162 |
| **`_km_globals`** | **absent** | present |

That last row is the one that matters most. On the X-Max 4 the modules read
`park_x` from `[gcode_macro _km_globals]`. The Q2 has no such macro, so
`park_x` is set explicitly in `qidi_calibration_q2.cfg`. The modules **refuse
to run** rather than fall back to a guess — a hardcoded 135 would put the wipe
most of the way across a 275 mm bed, dragging a hot, extruding nozzle over the
build surface.

---

## Install

Download **`QIDI-Q2-Calibration-Installer.exe`** from the
[Releases](https://github.com/Buddman69/Automm3AdaptivePA/releases) page and
double-click it. Nothing else is needed: no Python, no SSH client, no
terminal.

(It is not in the repository itself — it is a ~15 MB binary rebuilt on every
change, and committing it would bloat the history. The source it is built
from is everything else in this folder.)

It carries the Q2 settings; the X-Max 4 installer does not, and the two are
not interchangeable.

It finds the printer on your network, asks for the login (defaults `qidi` /
`qiditech`), copies the modules, installs `qidi_calibration_q2.cfg` and adds one
`[include]` line to `printer.cfg`. No Python, no SSH client, no terminal.

From a terminal instead:

```bash
./install.sh <printer-ip>
```

It picks up the `.cfg` sitting beside it automatically.

---

## The one number you must measure yourself

`counts_per_gf` in `qidi_calibration_q2.cfg` is set to **201**, measured on one
Q2 across 15 points from 8.5 gf to 2063 gf. **It is not a constant of the
design** — the X-Max 4 cell measured **182.96**, an 11% difference between two
machines of the same family.

QIDI has disabled every extrusion guard Klipper provides, so the force abort is
the only overload protection this printer has, and it means nothing numerically
until this figure is right for *your* cell. Measure it as the main README
describes, and set it in **both** sections — they abort independently.

**Why 201 and not the 205 a full-range fit gives:** the measured slope drifts
from ~208 mid-range to ~200 at the top, and the abort lives at the top.
Configuring low makes the abort fire early, which is the safe direction.
Configuring high makes it fire late.

If you leave it at the shipped 201 and your cell is really 205, the 1650 gf
abort fires at about 1618 gf — early, harmless. The dangerous direction is a
cell reading *lower* than configured.

---

## Running it

Everything happens in the printer's web console — a browser at its address, or
OrcaSlicer's Device tab, which is the same page.

```gcode
QIDI_CALIBRATE DRY=1
```

Click through, and the dry run prints the plan without heating. Then the real
thing, watched:

```gcode
QIDI_CALIBRATE
```

See "Updating" and "Troubleshooting" below for what to do after an update,
and for what to check if a run behaves oddly.

### Filament filling the purge chute instead of clearing it?

Some filaments don't clear the chute reliably — they fill it instead. For
those, use the bed routine: the same calibration, run over the centre of the
bed with the bed lowered clear of the nozzle, so there's nothing to clog.

```gcode
QIDI_CALIBRATE_BED DRY=1
```

Same six-question dialog as `QIDI_CALIBRATE`, same buttons-only Fluidd
constraint, same typed-parameter workaround (see Troubleshooting). The
non-dialog command it runs underneath, if you'd rather script it directly, is
`QIDI_AUTO_CALIBRATE_BED` — same parameters as `QIDI_AUTO_CALIBRATE`. Same
measurements, same output, just measured over the lowered bed instead of the
chute. First run `QIDI_BED_PREPARE` on its own (motion only, no heat) to
prove the bed drop before trusting the rest of the chain. See
`CHANGELOG/2026-09-22-bed-routine.md` and `CHANGELOG/2026-09-23-bed-wizard.md`
for the detail.

---

## Updating

`QIDI_UPDATE` in the printer's own web console fetches new releases from this
repo directly — no terminal, no re-running the installer:

```gcode
QIDI_UPDATE
```

It asks release → confirm as a dialog, the same mechanism as `QIDI_CALIBRATE`.
Nothing is swapped onto the live files until the new version has downloaded,
compiled, and test-imported in a subprocess — a file that would stop Klipper
starting is refused before it ever reaches `klippy/extras`. `QIDI_UPDATE
ROLLBACK=1` restores the files from before the last install if something
still needs undoing.

**After any update, restart Klipper with a real power cycle — the actual
switch, off and on — not `FIRMWARE_RESTART` or Fluidd's "Restart Klipper"
button.** Neither of those ever exits and restarts Klipper's own process;
both just reinitialize objects inside the same already-running Python
program, so every module that was already loaded — which after your first
install is everything, including `qidi_update.py` itself — keeps running its
OLD code no matter how many times you press them, because the file on disk
changed but Python's copy in memory did not. Only a genuine process respawn
picks up the new code. A command this update adds for the very first time
(never loaded before) can still register on a plain restart; the risk is
specifically for files that already existed.

**The first time you ever run `QIDI_UPDATE` on this Q2, run it twice**, with
a real power cycle in between:

1. Run `QIDI_UPDATE` — installs the chosen release using whatever
   `qidi_update.py` is already on the printer.
2. **Power cycle** — off and on at the switch.
3. Run `QIDI_UPDATE` again — this time the *newly installed* update script
   runs, which is what actually guarantees every file and `printer.cfg`
   section the release ships gets applied, including any brand-new command.

Once that first round trip is done, later updates that only change existing
commands need just one `QIDI_UPDATE` plus one power cycle. An update whose
release notes mention a brand-new command name is worth treating as "first
time" again, for the reason above.

---

## What is in this folder

| | |
|---|---|
| `QIDI-Q2-Calibration-Installer.exe` | the installer, Q2 settings bundled in |
| `qidi_calibration_q2.cfg` | those settings, readable, with the reasoning |
| `Stock firmware/QIDI_Q2-main` | QIDI's 2026-01 GPL source release |
| `Stock firmware/01.01.02.04 (current…)` | configs and MCU binaries from the current firmware |

The stock firmware is reference material — it is where every Q2 number above
came from, and it is worth keeping so the next person can check them rather
than trust them.

---

## Licence

PolyForm Strict 1.0.0 — see `LICENSE.md` alongside the modules. Free for
personal and noncommercial use; commercial use requires a paid annual licence.

---

## Troubleshooting

**Console shows `flow_ramp: read_origin_data is not callable`** — your
firmware is older than `01.01.02.04` (see the warning box at the top). Older
firmware exposes the load cell's read call differently, and the calibration
refuses to guess at an unfamiliar driver rather than run blind. Nothing heats
or moves when it hits this check — it's the safety design working, not a
crash. Update the printer's firmware and try again; older firmware is not
currently supported.

**Want to type numbers instead of clicking through the dialog** — Fluidd's
prompts are buttons only; there's no text-input verb a Klipper macro can use,
so this isn't fixable short of a full slicer-side integration. The closest
equivalent is typing values as parameters on the command itself, which skips
straight to the review screen with nothing left to click but START:

```gcode
QIDI_CALIBRATE TEMP=275 NOZZLE=0.4 LAYER=0.20 WIDTH=0.42 BLOCKS=12 POINTS=5
```

Any parameter you omit still shows up as a normal dialog screen. Giving
`NOZZLE=` alone seeds the geometry screen's numbers but still shows it for
confirmation; giving `LAYER=` and `WIDTH=` together is what actually skips
it. Same parameters work on `QIDI_CALIBRATE_BED`.

**`QIDI_UPDATE` says it installed, but the new command isn't there** — see
"Updating" above. This is almost always the module-cache gap: run
`QIDI_UPDATE` once more, power cycle (not `FIRMWARE_RESTART`), then run it a
second time.

**Purge chute fills up / gets blobby before a run finishes** — use
`QIDI_CALIBRATE_BED` instead; see "Filament filling the purge chute instead
of clearing it?" above.

**Run aborts citing force variance / possible slip** — the deliberate
slip-detection safety abort (`SAFETY.md`, rule 2), not a bug. Check the
filament isn't snagged at the spool, the drive gear isn't slipping, and the
nozzle isn't partially clogged before retrying.

**Load cell doesn't move enough during `QIDI_CS_READ`** — run it twice: once
without touching the nozzle, once pressing it by hand. The two readings
should differ by at least 500 gf. If they don't, stop — something is wrong
with the sensor path, not the calibration.

**PA / max-flow numbers look implausible** — check `counts_per_gf` is
calibrated for *your* cell, not just left at the shipped **201** (see "The
one number you must measure yourself" above — that figure came from one
specific Q2 and machines of the same family have measured 11% apart), that
`LAYER`/`WIDTH`/`NOZZLE` match what you actually slice with, and that the
calibration temperature matches the filament.
