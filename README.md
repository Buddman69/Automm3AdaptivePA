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

## Licence

[PolyForm Strict 1.0.0](LICENSE.md). Free for personal and noncommercial use.
Commercial use requires a paid licence — contact the author. Each model folder
carries its own copy of this file, since each is meant to work standalone if
extracted on its own; this one is the canonical copy GitHub shows in the
repo sidebar.
