# Safety

**Read this before running anything that heats or moves.**

This routine drives the extruder hard, on purpose, to find the flow limit - and
on this machine there is nothing underneath it. QIDI has disabled every
extrusion guard Klipper normally provides, so the aborts in these modules are
not a backup layer. They are the only layer.

The source files refer to these by number ("safety rule 4"), which is why they
are numbered here.

## The rules

1. **Step 1 is read-only.** No motion, no heating, no state writes. Keep it that
   way; it is the first thing ever to touch this sensor.
2. **Never drive the extruder to filament grind.** This machine has already had
   a grind event from a spool snag. The flow ramp must detect slip *onset* from
   force variance going sawtooth and abort — never push through to find the
   limit the conventional way. Hard ceiling at the load cell's 2000 gf safety
   limit as a backstop.

   **There is no firmware backstop underneath us.** QIDI has disabled every
   extrusion guard Klipper normally provides:

   ```
   max_extrude_only_velocity = 5000.0     (Klipper default 50)
   max_extrude_cross_section = 500.0      (default ~1.44 for a 0.6 nozzle)
   max_extrude_only_distance = 1000.0
   ```

   5000 mm/s of filament is 12,026 mm³/s — about 400× any real flow for this
   nozzle. **Klipper will raise no error at any flow we can command.** Our abort
   logic is not the first line of defence, it is the only one. Treat the
   `lis2dw` accelerometer as a second, independent slip indicator so the abort
   does not rest on force variance alone.

   **Three protections that do not exist**, all measured 2026-09-14:

   - **No firmware extrusion guard** — the limits above are disabled.
   - **No ADC ceiling.** At this Q2's 201 counts/gf the ADC's ±2²³ range is
     ±41,734 gf, ~21× the cell's 2000 gf rating (calibration on this cell
     reached 2063 gf with no hysteresis or knee, so 2000 gf is a safe floor,
     not an optimistic one). It cannot saturate before the cell breaks, so
     there is no clipping or rail to detect.
   - **No mechanical limit on Z.** Two Z motors at 1.07 A on a 2 mm lead make
     51,000–128,000 gf — 25–65× the cell's rating — reachable in microns at
     0.078 µm/microstep. Any bed-contact routine must go through the probe's own
     trigger or carry a hard force abort.

   Enforce the ceiling as **counts above a fresh tare**, never as an absolute
   count. The limit is **1650 gf (301,884 counts)**: calibration took the cell to
   2175 gf with no hysteresis and perfect linearity, so that is inside proven
   territory *for the cell*. The real risk at that force is the filament grinding
   in the drive gear, which is why the variance abort is armed by default at
   35 gf.
3. **Never invoke a callable during introspection.** The discovery walk reads
   non-callable attributes only, and skips `set_ do_ run_ cmd_ home_ probe_
   start_ stop_ calibrate_ move_ reset_ write_ send_ clear_ init_ load_ save_
   delete_ update_` prefixes even when non-callable, in case a Cython property
   has side effects. Do not relax this.
4. **Do not reconfigure QIDI's `[probe_air]`** or the chip's sample rate without
   an explicit plan to restore it. It is the Z probe; breaking it breaks bed
   levelling.
5. First run of anything that heats or moves is **watched**, over the purge
   chute, with you at the machine.
