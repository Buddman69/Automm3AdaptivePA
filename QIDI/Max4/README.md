# QIDI automatic flow + pressure-advance calibration

**My Reddit —** <https://www.reddit.com/user/Sport_Subject/>

**Help me get things I need to make more things, anything helps :)**
<https://www.amazon.com.au/hz/wishlist/ls/2ZFL750GMOMU1?ref_=wl_share>

---

Measures **max volumetric flow** and an **adaptive pressure-advance table** using
the printer's own toolhead load cell — the one QIDI ships for bed probing and
nothing else. A few minutes, ~2 g of filament, and it prints values ready to
paste into OrcaSlicer.

No test prints. No eyeballing a PA tower.

Built and verified on a **QIDI X-Max 4**.

---

## Install

### Windows — run the installer

Download **`QIDI-Max4-Calibration-Installer.exe`** from the
[Releases](https://github.com/Buddman69/Automm3AdaptivePA/releases) page and
double-click it. Nothing else is needed: no Python, no SSH client, no
terminal.

(It is not in the repository itself — it is a ~15 MB binary rebuilt on every
change, and committing it would bloat the history. The source it is built
from is everything else in this folder.)

It will:

1. **Find your printer.** It offers to scan your network for it — the address is
   whatever your router handed out, so it is different on every machine and can
   change. You can also type it in.
2. **Ask for the login.** The QIDI defaults are filled in; press Enter to accept.
   ```
   user     qidi
   password qiditech
   ```
3. Copy the modules, edit `printer.cfg`, remove anything an older version left
   behind, and restart Klipper.

It does **not** need administrator rights and does not ask for them — it writes
only to the printer, over SSH.

To rebuild it from source: `.\build_exe.ps1`.

### macOS, Linux, or from a terminal

```bash
./install.sh 192.168.1.132
```

Needs `bash`, `ssh` and `curl`. You are asked for the SSH password **once**,
with the same defaults as above.

Options:

| Flag | Effect |
|---|---|
| `--dry-run` | Show what would change. Touches nothing. |
| `--diagnostics` | Also install the read-only sensor probes |
| `--keep-old` | Keep modules superseded by this version (they are removed by default) |
| `--clean-logs` | Also delete the printer's rotated logs. Live logs are kept. |

The installer backs up `printer.cfg` (timestamped, on the printer) before any
edit, and inserts its sections **above** the `#*# SAVE_CONFIG` marker — Klipper
rewrites everything below that line, so anything placed after it is destroyed on
the next save.

It finishes with a service restart followed by a firmware restart. Both are
needed: Klipper caches imported modules, so a plain `RESTART` will not pick up a
new or edited extra.

**That is the only time you touch the installer.** Everything after this happens
in the printer's own web interface.

---

## Running it

Open the printer's web page — in a browser at its IP, or **OrcaSlicer's Device
tab**, which is the same page — and type into the console:

```gcode
QIDI_CALIBRATE
```

A dialog appears and asks six questions: temperature, nozzle, layer height and
line width, precision, how many flow points, and a final review. **Nothing heats
or moves until you press START.**

The temperature screen **starts from whatever the printer already has set** and
steps from there in ±1, ±5 and ±20 °C. So the normal flow is: set your filament's
temperature as usual, run `QIDI_CALIBRATE`, and press *Use this*. If the printer
is cold it starts at 275 °C — settable as `default_temp` under
`[qidi_cal_wizard]`.

Layer height and line width work the same way, stepping in ±0.01 and ±0.04 mm
from the defaults for whichever nozzle you pick. (Fluidd's dialogs have no text
box — every value is a button — which is why they are steppers rather than
fields.)

### Or type it directly

The wizard is a convenience, not the only route. Everything it asks can be given
on the command line, and anything pre-seeded skips its question:

```gcode
QIDI_CALIBRATE LAYER=0.18 WIDTH=0.65
QIDI_AUTO_CALIBRATE TEMP=265 BLOCKS=20 POINTS=7
```

### What it does

```
0  G28 if not already homed
1  heat to temperature
2  max volumetric flow search
3  wipe
4  build the flow points          (pure maths, no motion)
5  measure PA at each flow point
6  wipe
7  build the Orca table
8  heater off
9  wait for 170 C, then wipe again
10 air filtration off
```

**The nozzle stays hot from step 1 to step 8.** Nothing in the middle switches
it off — each stage could shut the machine down at the end of its own run, and
one of them used to, which left the PA measurement extruding into a cooling
nozzle. Whichever routine you start owns the shutdown, and it happens last.

**Step 0 is also the load-cell check.** Z homes through the load cell, so a home
proves end-to-end that the cell responds to force: a cell stuck triggered makes
Klipper refuse before moving, and a dead one never triggers and errors out. It
is skipped when the machine is already homed, because that home already proved
it. What it does *not* prove is the scale — see below.

**Step 9** exists because the nozzle keeps oozing as the melt depressurises after
shutdown, and that sag sets hard on the tip. 170 °C is below where the melt flows
freely but while a bead is still soft enough to scrape off. Wiping hot smears it;
wiping cold will not shift it.

### Purge chute filling up?

Some filaments don't clear the chute reliably and it fills before a run
finishes. `QIDI_CALIBRATE_BED` runs the same calibration instead, positioned
at bed-centre with the bed lowered out of the way, so nothing lands in the
chute - same six-question dialog as `QIDI_CALIBRATE`, nothing heats or moves
until you press START:

```gcode
QIDI_CALIBRATE_BED
```

Or skip the dialog and type it directly, same as `QIDI_AUTO_CALIBRATE`:

```gcode
QIDI_AUTO_CALIBRATE_BED
```

Same parameters as the chute versions above.

### Options

| Parameter | Default | What it does |
|---|---|---|
| `DRY=1` | – | Print the plan, change nothing |
| `TEMP=` | 275 | Nozzle temperature |
| `BLOCKS=` | 12 | Square-wave blocks per flow point — **precision** |
| `AMP=` | 0.5 | Square-wave amplitude as a fraction of v_E |
| `POINTS=` | 5 | Number of flow points |
| `LAYER=` `WIDTH=` | from config | Line geometry the table is built for |
| `SKIP_FLOW=1` | – | Reuse the last max-flow result, measure PA only |
| `HOME=` | auto | `1` homes even if already homed, `0` skips it |

`DRY`, `TEMP`, `BLOCKS`, `POINTS`, `LAYER` and `WIDTH` work on both
`QIDI_CALIBRATE` and `QIDI_AUTO_CALIBRATE`. `AMP`, `SKIP_FLOW` and `HOME` are
`QIDI_AUTO_CALIBRATE` only.

**On `BLOCKS`:** precision comes from transition count and it averages down
cleanly. At `BLOCKS=3` two runs were 21% apart; at `BLOCKS=12` four runs had a
standard deviation of 4.9%, matching the within-run standard error of 5.5% — so
there is no hidden between-run drift and more blocks keeps helping. **Do not go
below 12.**

**On `AMP`:** raising it from 0.25 to 0.5 improved precision at every flow point
(mean MAD/K 48% → 35%) and *lowered* the measured K by up to 29%. That is not
noise — noise inflates apparent K, worst where the force step is smallest, so the
larger amplitude is the more honest measurement.

---

## Before you calibrate — read this

### Calibrating counts per gram

The load cell reports **counts**. Converting to grams-force needs a scale factor
that is specific to your cell, its mount and its preload. The shipped default is
**182.96 counts/gf**, measured on one X-Max 4. **It is not a constant of the
design.**

Homing checks that the cell *responds*. Nothing checks that it is *scaled*: a
cell reading 2× would home perfectly and then make every force number, and the
abort threshold, wrong by 2×.

This matters because of what sits underneath:

```
max_extrude_only_velocity = 5000     (Klipper default 50)
max_extrude_cross_section = 500      (default ~1.44 for a 0.6 nozzle)
```

QIDI has disabled every extrusion guard Klipper normally provides. 5000 mm/s of
filament is about 12,000 mm³/s — roughly 400× any real flow. **Klipper will raise
no error at any flow this can command.** The force abort is not a backup; it is
the only protection there is, and it means nothing numerically until counts/gf is
right for your machine.

To measure it:

1. Cold, no motion. `QIDI_CS_READ SECONDS=10` and note the resting counts.
2. Press the nozzle down onto a digital kitchen scale at a fixed Z.
3. Record the scale reading and the counts **together** — a wedge creeps tens of
   grams in under a minute, so they must be read at the same moment.
4. Repeat at four or five loads across a few hundred to ~1500 gf.
5. Fit counts against gf. Use the **loaded** points only; an unloaded zero taken
   minutes earlier will drag the fit, because the baseline wanders 1–1.6 gf/min.

Put the result in `printer.cfg`, in **both** sections — they each abort on force
independently, and a value in only one of them leaves the other running on the
default:

```ini
[qidi_flow_ramp]
counts_per_gf: 182.96      # <- yours

[qidi_pa_measure]
counts_per_gf: 182.96      # <- the same number
```

Everything is differential — the code always compares against a fresh tare, never
an absolute count. An absolute count means nothing.

### First run

Do these in order, in the console.

```gcode
QIDI_CS_READ SECONDS=10
```

Press the nozzle by hand while it runs. **The value must move.** This proves the
load cell is reachable before anything heats. If it does not move, stop —
nothing below is safe.

(`QIDI_CS_READ` comes with the diagnostic tools. The Windows installer offers
them and defaults to yes; `install.sh` needs `--diagnostics`. If the command is
not found, that is why.)

Then measure counts/gf for your machine, as above. Then:

```gcode
QIDI_CALIBRATE
```

and choose **Dry run** on the last screen — or run `QIDI_CALIBRATE DRY=1`, which
makes the dry run the default button. Either prints the plan and heats nothing.

Only then run it for real — **watched, with the machine in front of you**, with
filament loaded and the purge chute clear.

---

## The output

```
Buddman69's Auto Test Results
Copy and paste the following into Orca

max mm3 =
19.5mm3

PA Centre Value =
K=0.0236

Adaptive PA Values =
0.0349,2.92,500
0.0286,4.70,500
...
```

In Orca, **Filament → Advanced**:

| Field | Value |
|---|---|
| Pressure advance | the **PA Centre Value** |
| Enable adaptive pressure advance | on |
| Adaptive pressure advance measurements | the **Adaptive PA Values** block |

Column order is `PA, flow (mm³/s), acceleration` — read off a populated stock
Orca profile, not guessed. Each flow appears twice at two accelerations with the
same K, because that is what the measurement found: K varies with flow (1.72:1
across the range on ASA-CF) and does not vary with acceleration (bounded to
±16%, against t = 5.9 for flow).

**The table is valid only for the layer height and line width it was generated
for.** `v_E` depends only on volumetric flow, so the flow axis transfers between
profiles — but the geometry is recorded with the table, and changing layer height
or line width means regenerating it.

---

## Safety

- First run of anything that heats or moves is **watched, over the purge chute,
  with you at the machine**.
- Aborts are **magnitude** comparisons. Melt pressure reads *negative* — it pushes
  the hotend down, opposite to a bed press — and an early version compared a
  signed value against a positive limit, so its ceiling could never have fired.
  Check any new force comparison for this.
- Never touch `cs1237_setup_home`. That is the Z probe's endstop path
  (`threshold`, `trsync_oid`), and it is how bed levelling gets broken.
- Every module converts an unexpected exception into a clean command error.
  Klipper treats an unhandled exception in a G-code command as an internal error
  and shuts down **every MCU** — that happened once during development and took a
  firmware restart to clear.

---

## Licence

PolyForm Strict 1.0.0 — see [LICENSE.md](LICENSE.md).

Free for personal and noncommercial use. **Commercial use requires a paid annual
licence** — contact the author.

Strict permits use, not redistribution or derivative works. You may adapt your
own copy to your own printer — LICENSE.md grants that explicitly — but not pass
a modified copy on.

The shipped modules import only the Python standard library.

---

### A note on antivirus

Windows SmartScreen and some antivirus products flag unsigned executables on
sight, because a freshly built, uncommon, unsigned program is exactly what they
are tuned to notice. Nothing in the build can prevent that - only a code-signing
certificate would, and that is a paid, per-year thing.

If SmartScreen blocks it: **More info** then **Run anyway**.

The installer deliberately does *not* ask for administrator rights. It has no
need of them - it writes over SSH to the printer and touches nothing on the PC
outside its own temporary folder - and asking would not make any scanner happier.

The source is here to read, and `install.sh` does the same job without an
executable at all if you would rather.
