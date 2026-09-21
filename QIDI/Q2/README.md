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

Download **[`QIDI-Q2-Calibration-Installer.exe`](QIDI-Q2-Calibration-Installer.exe)**
— either straight from this folder, or from the
[Releases](https://github.com/Buddman69/Automm3AdaptivePA/releases) page,
which carries the same file — and double-click it. It carries the Q2
settings; the X-Max 4 installer does not, and the two are not interchangeable.

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

The full guide — what it measures, the options, how to read the output, and how
to measure counts/gf — is in the main `README.md` alongside the modules.

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
