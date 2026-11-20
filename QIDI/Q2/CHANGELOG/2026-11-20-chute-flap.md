# 2026-11-20 — open the purge chute's flap between measurements

## Why

The Q2's purge chute has a spring-loaded flap. The toolhead holds it
**shut** whenever it is at the park (X85 Y287.5, `MOVE_TO_TRASH`) and stays
holding it shut until X122. That was measured on a Q2 by jogging right from
`MOVE_TO_TRASH` one millimetre at a time until the flap released. The wiper
strokes (X95–115) never reach X122, and every wipe ended back at X85. So
the flap stayed shut for the whole routine, and every purge (each Stage 1
measurement, each Stage 3 prime and block) landed on the closed flap under
the nozzle.

That is not just untidy. The pile builds up under the nozzle and presses
back on it, and the nozzle sits on the load cell that is doing the
measuring. On a real Q2 run the blobs had to be pushed away with tweezers
mid-run (which in turn shows up as torn blocks).

QIDI's own macros deal with this by moving sideways after a purge:
`CLEAR_NOZZLE` and `CLEAR_NOZZLE_PLR` both end at X125.

## What changed

`qidi_flow_ramp.py` and `qidi_pa_measure.py`, plus their tests. New
constants `FLAP_RELEASE_X = 125`, `FLAP_DWELL_S = 1.0`, `FLAP_PARK_Y`,
`FLAP_CLEAR_Y = 270` and `FLAP_APPROACH_FEED = 1500`, in both modules.

- **Every wipe** now ends with the flap release: X125, a 1 s dwell, then
  back to the park.
- **Stage 1:** after a passing measurement that does not trigger a wipe,
  retract 1 mm and do the same release (`_release_flap`). A failed
  measurement, or one past `SEARCH_WIPE_VOLUME_MM3`, was already followed by
  a wipe.
- **Stage 3:** after each point's 100 mm³ prime, retract 1 mm and release
  (at the machine's accel, as the inter-block wipe does), then re-prime
  `REPRIME_MM3` and tare, exactly as after an inter-block wipe. The
  measured blocks are unchanged.

## The way back matters

The release goes **out** along X at Y287.5, which is what lets the flap
open. It must come **back** the way `MOVE_TO_TRASH` does: out to Y270,
across to X85, then in to Y287.5 at F1500. `MOVE_TO_TRASH` never crosses
X112 at Y > 270, and this is why.

A first version came straight back along X at Y287.5. On its first live
run the toolhead hit the chute and X skipped steps. Klipper still reported
X85 while the nozzle sat beside the chute, so the next measurement extruded
into the printer, and the run had to be emergency-stopped. After the fix:
- the new path was run once cold and watched, and the toolhead re-entered
  cleanly;
- `GET_POSITION` before and after a fresh `G28 X Y` differed by 6
  microsteps on X and 0 on Y, which is normal homing scatter;
- a full `QIDI_AUTO_CALIBRATE` then ran to completion with every purge
  dropping into the bin.

The tests pin the order: `test_flow_ramp.py` and `test_pa_measure.py` check
X125 → dwell → Y270 → X85 → Y287.5 F1500 for the wipe, the standalone
release and the Stage 3 prime. All of those checks fail against the
previous modules.

## Not changed

- The bed routine (`qidi_flow_bed_search.py`, `qidi_pa_bed_measure.py`)
  measures over the bed, so its purges never meet the flap.
- `QIDI/Max4`: whether the Max4's chute has the same flap is unknown here.
  If it does, the same release applies, with its own measured release X.
