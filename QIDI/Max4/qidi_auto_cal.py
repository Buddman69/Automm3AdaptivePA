# qidi_auto_cal.py - one command that runs the whole calibration
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
# WHAT IT DOES
#   QIDI_AUTO_CALIBRATE chains every stage and prints a block ready to paste
#   into OrcaSlicer:
#
#     0  G28 if not already homed   -> see WHY HOMING IS THE PROBE CHECK
#     1  heat to temperature
#     2  Stage 1  QIDI_FLOW_SEARCH   -> working max volumetric flow
#     3  wipe
#     4  Stage 2  QIDI_PA_ENVELOPE   -> geometric flow points (pure maths)
#     5  Stage 3  QIDI_PA_MEASURE    -> K at each flow point
#     6  wipe
#     7  Stage 4  QIDI_PA_TABLE      -> the Orca model string
#     8  heater off
#     9  COOLDOWN WIPE at 170 C      -> see below
#    10  print the results block
#
#   Each stage already writes its own JSON, so this reads those rather than
#   duplicating any measurement logic. If a stage aborts, the chain stops and
#   says which one - it never proceeds on a half-result.
#
# WHY THE COOLDOWN WIPE
#   After the heater is switched off the nozzle keeps oozing as the melt
#   depressurises, and that sag sets hard on the tip. Wiping once the nozzle has
#   dropped to ~170 C - below where the melt flows freely but while any bead is
#   still soft enough to scrape - clears it. Wiping hot just smears it, and
#   wiping cold cannot shift it.
#
#   Uses Klipper's own TEMPERATURE_WAIT, which blocks until the sensor reads at
#   or below the target. With the heater off and the chamber at 60 C this always
#   converges; it is not a poll loop of our own.
#
# WHY HOMING IS THE PROBE CHECK
#   The routine needs a homed machine anyway - every wipe and every extrusion
#   refuses to run otherwise. Z homing on this printer goes through the load
#   cell, so G28 is also an end-to-end proof that the cell responds to force
#   through the machine's OWN path, at no extra cost:
#
#     * cell stuck triggered  -> Klipper refuses before moving, "Probe
#                                triggered prior to movement"
#     * cell dead or unwired  -> no trigger, Z runs to the travel limit and
#                                errors out
#
#   It runs only when the machine is not already homed: a homed machine has
#   already demonstrated a working probe, so re-homing would buy nothing and
#   cost 40 s.
#
#   WHAT IT DOES NOT PROVE is the SCALE. A cell reading 2x would home perfectly
#   and then make every force number - and the abort threshold - wrong by 2x.
#   counts_per_gf still has to be measured by hand; see README.
#
#   PROBE_ACCURACY was considered and deliberately left out of the chain. What
#   it adds over G28 is trigger repeatability in microns, which is a
#   bed-levelling property - this routine extrudes in air over the chute and
#   measures force, so it does not depend on it. It stays a manual diagnostic.
#
#   Homing happens BEFORE the try/finally, not inside it. The cooldown wipe in
#   that finally: needs a homed machine, so a failed home must abort before it
#   is armed - and nothing is hot yet at that point, so there is nothing to
#   cool down.
#
# SAFETY
#   Heats and extrudes, so SAFETY.md rule 5 applies: watched, over the
#   purge chute, with you at the machine. Every abort in the underlying stages
#   still fires - this module adds no new motion of its own beyond the wipes,
#   and those go through Stage 1's proven QIDI_FLOW_WIPE.
#
#   An unhandled exception is converted to a command error rather than being
#   allowed to become a Klipper INTERNAL ERROR, which would shut down every MCU.
#   The heater-off and cooldown-wipe steps run from a finally: block, so an
#   abort part way through still leaves the machine cold and the nozzle clean.
#
# USAGE
#   [qidi_auto_cal]
#
#   QIDI_AUTO_CALIBRATE                     the whole thing
#   QIDI_AUTO_CALIBRATE DRY=1               print the plan, no heat, no motion
#   QIDI_AUTO_CALIBRATE TEMP=275 BLOCKS=12  override the defaults
#   QIDI_AUTO_CALIBRATE SKIP_FLOW=1         reuse the last max flow, PA only
#   QIDI_AUTO_CALIBRATE HOME=1              home even if already homed
#
#   Or QIDI_CALIBRATE, which asks for all of this as dialogs in the printer's
#   web UI and then calls this - see qidi_cal_wizard.py.
#
#   Results also land in ~/printer_data/qidi_pa/auto_cal.json

import json
import logging
import os
import time

DEFAULT_TEMP = 275.0
DEFAULT_BLOCKS = 12
DEFAULT_AMP = 0.5
DEFAULT_LEG_MS = 200.0
DEFAULT_POINTS = 5

# Below where the melt flows freely, but while any sagging bead is still soft
# enough for the silicone wiper to take off. Hot smears it; cold will not shift
# it.
COOLDOWN_WIPE_C = 170.0
COOLDOWN_TIMEOUT_S = 600.0

# The chamber air filter, same fan Stage 1 uses. Stage 1 no longer switches it
# off mid-chain, so this module does it at the very end.
FILTER_M106_P = 3

BANNER = "Buddman69's Auto Test Results"
FOOTER = ("End Results - project probably maybe on github when I get around "
          "to it possibly......")

# Printed under every results block. Kept as one list so the console output,
# the installer's closing message and the README all say the same thing.
REDDIT_URL = "https://www.reddit.com/user/Sport_Subject/"
WISHLIST_URL = ("https://www.amazon.com.au/hz/wishlist/ls/"
                "2ZFL750GMOMU1?ref_=wl_share")
SUPPORT_LINES = [
    "My Reddit",
    REDDIT_URL,
    "",
    "Help me get things I need to make more things, anything helps :)",
    WISHLIST_URL,
]


class QidiAutoCal:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        out_dir = config.get('out_dir', '~/printer_data/qidi_pa')
        self.out_dir = os.path.expanduser(out_dir)
        self.report_path = os.path.join(self.out_dir, 'auto_cal.json')
        self.cooldown_c = config.getfloat('cooldown_wipe_temp', COOLDOWN_WIPE_C,
                                          above=40., below=250.)
        self.gcode.register_command('QIDI_AUTO_CALIBRATE', self.cmd_AUTO,
                                    desc=self.cmd_AUTO_help)

    # -- plumbing ----------------------------------------------------------
    def _script(self, s):
        self.gcode.run_script_from_command(s)

    def _read_json(self, name, gcmd, what):
        path = os.path.join(self.out_dir, name)
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            raise gcmd.error("auto_cal: %s did not produce %s - check the "
                             "console output above for why" % (what, name))
        if isinstance(d, list):
            if not d:
                raise gcmd.error("auto_cal: %s is empty" % (name,))
            d = d[-1]
        return d.get('payload') or {}

    def _record(self, payload):
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            data = []
            if os.path.exists(self.report_path):
                try:
                    with open(self.report_path) as f:
                        data = json.load(f)
                    if not isinstance(data, list):
                        data = [data]
                except Exception:
                    data = []
            data.append({'kind': 'auto_cal', 'time': time.time(),
                         'payload': payload})
            tmp = self.report_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=1, default=repr)
            os.replace(tmp, self.report_path)
        except Exception:
            logging.exception("qidi_auto_cal: could not write report")

    def _temp(self):
        ext = self.printer.lookup_object('extruder', None)
        if ext is None:
            return 0.0
        try:
            return float(ext.get_status(self.reactor.monotonic())
                         ['temperature'])
        except Exception:
            return 0.0

    def _homed(self):
        th = self.printer.lookup_object('toolhead', None)
        if th is None:
            return ''
        try:
            return th.get_status(self.reactor.monotonic()).get('homed_axes', '')
        except Exception:
            return ''

    def _home_if_needed(self, gcmd, force):
        """G28 doubles as the load-cell check - see WHY HOMING IS THE PROBE
        CHECK at the top. Skipped when the machine is already homed, because
        that already proves the probe works."""
        if force == 0:
            gcmd.respond_info("auto_cal: HOME=0 - skipping the home. Every "
                              "wipe and extrusion will refuse if any axis is "
                              "unhomed.")
            return
        axes = self._homed()
        if force != 1 and all(a in axes for a in 'xyz'):
            gcmd.respond_info("auto_cal: already homed (%s) - skipping G28. "
                              "That home already proved the load cell "
                              "triggers." % (axes or 'none',))
            return
        gcmd.respond_info("auto_cal: homing. Z homes through the load cell, so "
                          "this also proves the cell responds to force.")
        self._script("G28")

    # -- the cooldown wipe -------------------------------------------------
    def _cooldown_wipe(self, gcmd):
        """Heater off, wait for the nozzle to fall to the wipe temperature,
        then wipe off whatever sagged out while it depressurised."""
        try:
            self._script("M104 S0")
            gcmd.respond_info("auto_cal: heater off, waiting for %.0f C to wipe "
                              "off cooldown ooze (nozzle is %.0f C now)"
                              % (self.cooldown_c, self._temp()))
            # Klipper's own wait - blocks until the sensor reads at or below
            # MAXIMUM. With the heater off this always converges.
            self._script("TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=%.0f"
                         % (self.cooldown_c,))
            self._script("QIDI_FLOW_WIPE")
            # Stage 1 is told COOLDOWN=0 so the nozzle stays hot for Stage 3,
            # which also leaves its air filter running. Switching it off is
            # therefore this module's job, and it happens last - after the
            # nozzle is down to wipe temperature and has stopped outgassing.
            self._script("M106 P%d S0" % (FILTER_M106_P,))
            gcmd.respond_info("auto_cal: cooldown wipe done at %.0f C, "
                              "air filtration off" % (self._temp(),))
            return True
        except Exception as e:
            gcmd.respond_info("auto_cal: cooldown wipe skipped (%s)"
                              % (str(e)[:80],))
            return False

    # -- the report --------------------------------------------------------
    def _results_block(self, gcmd, qmax, centre_k, rows):
        out = []
        out.append("")
        out.append(BANNER)
        out.append("Copy and paste the following into Orca")
        out.append("")
        out.append("max mm3 =")
        out.append("%.1fmm3" % (qmax,))
        out.append("")
        out.append("PA Centre Value =")
        out.append("K=%.4f" % (centre_k,))
        out.append("")
        out.append("Adaptive PA Values =")
        for r in rows:
            out.append(r)
        out.append("")
        out.append(FOOTER)
        out.append("")
        out.extend(SUPPORT_LINES)
        out.append("")
        for line in out:
            gcmd.respond_info(line)
        return "\n".join(out)

    cmd_AUTO_help = ("Run the whole calibration - max flow, then adaptive "
                     "pressure advance - and print values ready to paste into "
                     "Orca. HEATS AND EXTRUDES. [DRY=1] [TEMP=275] [BLOCKS=12] "
                     "[AMP=0.5] [POINTS=5] [LAYER=] [WIDTH=] [SKIP_FLOW=0] "
                     "[HOME=]")

    def cmd_AUTO(self, gcmd):
        err_cls = type(gcmd.error("probe"))
        try:
            return self._run(gcmd)
        except err_cls:
            raise
        except Exception as e:
            logging.exception("qidi_auto_cal: unhandled error")
            raise gcmd.error("auto_cal: internal error, printer left running: "
                             "%s: %s" % (type(e).__name__, str(e)[:160]))

    def _run(self, gcmd):
        dry = gcmd.get_int('DRY', 0)
        temp = gcmd.get_float('TEMP', DEFAULT_TEMP, above=150., below=350.)
        blocks = gcmd.get_int('BLOCKS', DEFAULT_BLOCKS, minval=1, maxval=50)
        amp = gcmd.get_float('AMP', DEFAULT_AMP, above=0.02, below=1.0)
        leg = gcmd.get_float('LEG_MS', DEFAULT_LEG_MS, above=20., below=2000.)
        pts = gcmd.get_int('POINTS', DEFAULT_POINTS, minval=2, maxval=12)
        skip_flow = gcmd.get_int('SKIP_FLOW', 0)
        home = gcmd.get_int('HOME', -1, minval=-1, maxval=1)
        layer = gcmd.get_float('LAYER', None)
        width = gcmd.get_float('WIDTH', None)
        geom = ""
        if layer is not None:
            geom += " LAYER=%.3f" % (layer,)
        if width is not None:
            geom += " WIDTH=%.3f" % (width,)

        gcmd.respond_info(
            "auto_cal: full calibration at %.0f C - flow search, then PA at %d "
            "flow points (%d blocks, dv %.0f%%), wiping between and again at "
            "%.0f C on the way down."
            % (temp, pts, blocks, 100 * amp, self.cooldown_c))
        if skip_flow:
            gcmd.respond_info("auto_cal: SKIP_FLOW - reusing the last "
                              "QIDI_FLOW_SEARCH result")
        if dry:
            gcmd.respond_info("auto_cal: DRY - showing the plan only")
            for step in ("0  G28  (skipped if already homed; HOME=1 forces "
                         "it, HOME=0 skips it)",
                         "1  QIDI_FLOW_SEARCH TEMP=%.0f COOLDOWN=0  (stays hot for Stage 3)" % temp,
                         "2  QIDI_FLOW_WIPE",
                         "3  QIDI_PA_ENVELOPE POINTS=%d AMP=%.2f%s"
                         % (pts, amp, geom),
                         "4  QIDI_PA_MEASURE TEMP=%.0f BLOCKS=%d AMP=%.2f "
                         "LEG_MS=%.0f" % (temp, blocks, amp, leg),
                         "5  QIDI_FLOW_WIPE",
                         "6  QIDI_PA_TABLE RUNS=1",
                         "7  M104 S0",
                         "8  TEMPERATURE_WAIT MAXIMUM=%.0f, QIDI_FLOW_WIPE, "
                         "air filtration off" % self.cooldown_c):
                gcmd.respond_info("   " + step)
            gcmd.respond_info("auto_cal: DRY - nothing heated, nothing moved.")
            return

        # Outside the try/finally on purpose: the cooldown wipe in that finally
        # needs a homed machine, and nothing is hot yet, so a failed home should
        # abort here rather than pile a wipe error on top of a homing error.
        self._home_if_needed(gcmd, home)

        t0 = time.time()
        stage = "startup"
        qmax = None
        try:
            # ---- Stage 1 : max volumetric flow --------------------------
            if not skip_flow:
                stage = "Stage 1 (QIDI_FLOW_SEARCH)"
                gcmd.respond_info("auto_cal: ---- Stage 1: max flow ----")
                # COOLDOWN=0 is not optional here. Stage 1 shuts the hotend and
                # the air filter down at the end of its own run, which is right
                # when it is run on its own and WRONG in a chain: Stage 3 does
                # not set temperature, so it would extrude into a nozzle that
                # had been cooling for a minute. That happened on 2026-09-17 -
                # the lowest flow point, 2.92 mm3/s, hit the 1650 gf abort at
                # 1662 gf, having made more force cold than 23 mm3/s made hot.
                # This module owns the shutdown; see _cooldown_wipe.
                self._script("QIDI_FLOW_SEARCH TEMP=%.0f COOLDOWN=0" % (temp,))
                self._script("QIDI_FLOW_WIPE")
            flow = self._read_json('flow_ramp.json', gcmd, stage)
            qmax = flow.get('working_max')
            if not qmax:
                raise gcmd.error(
                    "auto_cal: no working_max from the flow search - it may "
                    "have aborted. Nothing downstream can run without it.")
            gcmd.respond_info("auto_cal: max volumetric flow %.2f mm3/s"
                              % (qmax,))

            # ---- Stage 2 : the flow points ------------------------------
            stage = "Stage 2 (QIDI_PA_ENVELOPE)"
            gcmd.respond_info("auto_cal: ---- Stage 2: flow points ----")
            self._script("QIDI_PA_ENVELOPE QMAX=%.3f POINTS=%d AMP=%.2f%s"
                         % (qmax, pts, amp, geom))

            # ---- Stage 3 : pressure advance -----------------------------
            stage = "Stage 3 (QIDI_PA_MEASURE)"
            gcmd.respond_info("auto_cal: ---- Stage 3: pressure advance ----")
            # TEMP is passed even though Stage 1 has already left the nozzle
            # hot. It costs nothing when the temperature is already there, and
            # it means Stage 3 no longer depends on that being true.
            self._script("QIDI_PA_MEASURE TEMP=%.0f BLOCKS=%d AMP=%.2f "
                         "LEG_MS=%.0f" % (temp, blocks, amp, leg))
            self._script("QIDI_FLOW_WIPE")

            # ---- Stage 4 : the Orca table -------------------------------
            stage = "Stage 4 (QIDI_PA_TABLE)"
            gcmd.respond_info("auto_cal: ---- Stage 4: Orca table ----")
            self._script("QIDI_PA_TABLE RUNS=1")
            table = self._read_json('orca_pa.json', gcmd, stage)
            rows = table.get('rows') or []
            centre = table.get('fallback_pressure_advance')
            if not rows or centre is None:
                raise gcmd.error("auto_cal: Stage 4 produced no table")
        finally:
            # Always leave the machine cold and the nozzle clean, however we
            # got here.
            self._cooldown_wipe(gcmd)

        mins = (time.time() - t0) / 60.0
        block = self._results_block(gcmd, qmax, centre, rows)
        gcmd.respond_info("auto_cal: complete in %.1f min. Also written to %s"
                          % (mins, self.report_path))
        self._record({'qmax_mm3_s': qmax, 'centre_k': centre, 'rows': rows,
                      'temp_c': temp, 'blocks': blocks, 'amp': amp,
                      'leg_ms': leg, 'points': pts,
                      'minutes': mins, 'block': block,
                      'geometry': table.get('geometry')})


def load_config(config):
    return QidiAutoCal(config)
