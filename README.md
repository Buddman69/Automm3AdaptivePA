# QIDI automatic flow + pressure-advance calibration

**My Reddit —** <https://www.reddit.com/user/Sport_Subject/>

**Help me get things I need to make more things, anything helps :)**
<https://www.amazon.com.au/hz/wishlist/ls/2ZFL750GMOMU1?ref_=wl_share>

---

Measures **max volumetric flow** and an **adaptive pressure-advance table**
using a QIDI printer's own toolhead load cell — the one QIDI ships for bed
probing and nothing else. A few minutes, a few grams of filament, and it
prints values ready to paste into OrcaSlicer. No test prints, no eyeballing a
PA tower.

## Pick your printer

Each model is its own complete, self-contained codebase — nothing is shared
between them, deliberately, so a change made for one can never silently affect
another:

| Model | Status |
|---|---|
| **[QIDI/Max4](QIDI/Max4/)** | Built and verified on a QIDI X-Max 4. |
| **[QIDI/Q2](QIDI/Q2/)** | Ported from the Max4 build as a starting point. **Not yet verified on a Q2** — see the status note at the top of that folder's README. |

Open the model's own `README.md` for install instructions — everything from
here down (updates, licence, support links) is common ground, not shared code.

## Releases

Every build, for every model, is published on the
[Releases page](https://github.com/Buddman69/Automm3AdaptivePA/releases) —
installers, release notes, and version history all in one place.

## Updating

Once installed, `QIDI_UPDATE` in the printer's own web console fetches new
releases from here directly — no terminal, no re-running the installer. It
asks brand → model → release → confirm as a dialog, the same way the
calibration wizard does, and it only ever installs files that live under
`QIDI/<Model>/` for the model you picked, so one model's release can never
land on the other's printer. See `qidi_update.py` in either model folder for
exactly how.

Releases are tagged `qidi-<model>-vX.Y.Z` (e.g. `qidi-max4-v1.1.0`) — one
GitHub Release per tag is what makes it show up in the picker.

**After any update, restart Klipper from the machine/power menu ("Restart
Klipper"), not just `FIRMWARE_RESTART` from the console.** Klipper never
actually exits and restarts its own process on `RESTART` or
`FIRMWARE_RESTART` — both just reinitialize objects inside the same running
Python process, which keeps every already-loaded module's code exactly as it
was. A module that has never been loaded before (a command this update adds
for the first time) still picks up correctly either way. But a module that
was already running - which, after your first install, is every existing
file, including `qidi_update.py` itself - keeps running its OLD code no
matter how many times you `FIRMWARE_RESTART`, because the file on disk
changed but Python's copy in memory did not. Only a real "Restart Klipper"
(from Fluidd's machine panel, or Moonraker's own service restart) actually
respawns the process and picks the new code up.

**The very first time you ever run `QIDI_UPDATE` on a printer installed
before this note was added, run it twice**, with a real Klipper restart
(not just `FIRMWARE_RESTART`) in between. The first run installs whatever
release you pick using the update script already on the printer; if that
script is an old one, it may not do everything a newer one does — and per the
note above, it will keep being the old script until Klipper actually
restarts, not merely reinitializes. Once it has, running `QIDI_UPDATE` a
second time installs the same release again, this time using the script that
first run just put in place, which is what actually ensures every file the
release ships is fully applied. A fresh install already gets the current
script from the start, so this is a one-time step, not an every-update habit.

## Licence

[PolyForm Strict 1.0.0](LICENSE.md). Free for personal and noncommercial use.
Commercial use requires a paid licence — contact the author. Each model folder
carries its own copy of this file, since each is meant to work standalone if
extracted on its own; this one is the canonical copy GitHub shows in the
repo sidebar.

## Troubleshooting

**A new command doesn't show up after `QIDI_UPDATE` says it installed**
(Max4 v1.2.3 onwards) — power cycle the printer, the actual switch off and
on. `FIRMWARE_RESTART` and Fluidd's "Restart Klipper" button both just
reinitialize objects inside the same already-running Python process; they do
not reload code that was already in memory, which after your first install
is everything, including `QIDI_UPDATE` itself — see the note on this under
Updating above. A power cycle is the one thing guaranteed to actually
respawn it. Then run `QIDI_UPDATE` again, and restart Klipper once more
afterwards to load whatever the now-correctly-running update just added.

**`update: download is larger than 25 MB - refusing`** — a release's
zipball is too large. This is a repo-side problem, not something you did;
let me know. In the meantime, install with the standalone installer
instead — it never downloads a release zipball, so it never hits this
limit: run the model's `.exe` from the [Releases
page](https://github.com/Buddman69/Automm3AdaptivePA/releases) (Windows), or
`./install.sh <printer-ip>` (macOS/Linux). See the model's own README's
Install section for detail.

**Your printer's model doesn't appear in the `QIDI_UPDATE` picker** — no
release has been tagged for it yet. Let me know (Reddit or the wishlist
link at the top of this page) and I'll add it.

**An update went wrong** — `QIDI_UPDATE ROLLBACK=1` restores the files from
before the last install.
