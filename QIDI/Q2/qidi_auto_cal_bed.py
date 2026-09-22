# qidi_auto_cal_bed.py - the whole calibration, run over the BED instead of
# the purge chute
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
# QIDI Q2 BUILD. Copied from the X-Max 4 sources and altered for this
# machine - the two are kept entirely separate, and nothing here feeds
# back. Q2 values verified against QIDI's own firmware, both the
# 2026-01 GitHub release and the current 01.01.02.04 (2026-08-05):
#   identical to the X-Max 4 : probe_air on THR:PB3/PB4, c_sensor,
#                              voltage 4.95, delta_v 0.08,
#                              rotation_distance 53.7, 1517:170,
#                              every extrusion guard disabled
#   DIFFERENT                : nozzle 0.4, bed 275x295x265,
#                              park X85 Y287.5, wiper X95-115,
#                              NO [gcode_macro _km_globals],
#                              load cell 201 counts/gf (Max4: 182.96)
#
#
# WHY THIS EXISTS, SEPARATELY FROM qidi_auto_cal.py
#   Some filaments do not clear the purge chute reliably. This is the same
#   calibration, with the SAME maths, SAME abort thresholds, SAME wipe
#   geometry - only WHERE the extrusion happens changes: over the bed centre,
#   with the bed lowered clear of the nozzle, instead of over the chute.
#
#   It is a SEPARATE command chain, in SEPARATE files
#   (qidi_flow_bed_search.py, qidi_pa_bed_measure.py, this file), specifically
#   so nothing here can ever change what QIDI_AUTO_CALIBRATE does. See
#   CHANGELOG/ for what was added and why.
#
# WHAT IT DOES
#   QIDI_AUTO_CALIBRATE_BED chains:
#
#     1  QIDI_BED_PREPARE   -> G28 (ALWAYS - see WHY HOMING IS MANDATORY
#                               HERE), capture the bed-centre position G28
#                               leaves the toolhead at, move to the chute,
#                               lower the bed
#     2  heat to temperature (at the chute)
#     3  Stage 1  QIDI_FLOW_BED_SEARCH  -> working max volumetric flow,
#                                          measured at bed centre
#     4  QIDI_FLOW_BED_WIPE  -> 2 strokes at the chute, then back to bed centre
#     5  Stage 2  QIDI_PA_ENVELOPE      -> UNCHANGED, pure maths
#     6  Stage 3  QIDI_PA_BED_MEASURE   -> K at each flow point, at bed centre
#     7  QIDI_FLOW_BED_WIPE
#     8  Stage 4  QIDI_PA_TABLE         -> UNCHANGED, pure maths
#     9  heater off
#    10  COOLDOWN WIPE at 170 C, at the chute - see below
#    11  print the results block
#
#   Stage 2 and Stage 4 are pure maths with no motion, so they are reused
#   completely unmodified - there is nothing in them that could differ between
#   a chute run and a bed run. Only Stage 1 and Stage 3 have positions to
#   change, so only they have bed-specific replacements.
#
# WHY HOMING IS MANDATORY HERE (unlike QIDI_AUTO_CALIBRATE, which skips G28
# when already homed)
#   The chute routine's G28 is a load-cell check with a side benefit; skipping
#   it when already homed loses nothing because the chute position it moves to
#   is a fixed macro variable (park_x), unrelated to where G28 leaves the
#   toolhead.
#
#   Here, G28's resting position IS the bed-centre reference the WHOLE run is
#   built on - QIDI_BED_PREPARE captures it live, right after homing, and
#   every later "move to bed centre" returns to exactly that point. Skipping
#   G28 would mean trusting a position captured by some EARLIER run, with no
#   guarantee the toolhead ever homed the same way since - so it is never
#   skipped.
#
# WHY THE BED STAYS DOWN THE WHOLE TIME, AND STAYS DOWN AT THE END
#   Lowering the bed 200 mm (this Q2; 265 mm is the physical max, per its own
#   printer.cfg stepper_z position_max) creates
#   clearance across the ENTIRE XY travel, not just over bed centre - so once
#   it is down, moving between the chute and bed centre needs no Z change at
#   all, and nothing in this routine raises it back up. The next real print's
#   own start gcode calls G28 anyway, which re-homes Z regardless.
#
# WHY BED-CENTRE IS CAPTURED, NOT HARDCODED
#   G28 already parks at bed centre on this machine (confirmed - not derived
#   from bed-size config, which this module does not read at all). Capturing
#   the live position after every mandatory home means this file needs no
#   printer-specific X/Y numbers, and stays correct even if that assumption
#   about where G28 parks ever changes.
#
# THE POSITION HANDOFF - bed_position.json
#   QIDI_BED_PREPARE writes {x, y, bed_z, time} to
#   ~/printer_data/qidi_pa/bed_position.json. QIDI_FLOW_BED_SEARCH and
#   QIDI_PA_BED_MEASURE each read it independently (their own copy of the
#   read logic, not a shared import - consistent with every other duplicated
#   piece in this project) and REFUSE to proceed if the toolhead's actual
#   current Z does not match bed_z: that mismatch means the bed was raised
#   back up since (a real print ran, someone re-homed) and moving to the
#   recorded X/Y at the CURRENT Z could put the nozzle into the bed. This is
#   the one new safety check this addition needed that nothing existing
#   already provided.
#
# WHY STAGE 1's AND STAGE 3's OUTPUT FILES ARE UNCHANGED
#   QIDI_FLOW_BED_SEARCH still writes flow_ramp.json; QIDI_PA_BED_MEASURE
#   still writes pa_table.json - the SAME files the chute versions write.
#   This is required, not incidental: QIDI_PA_ENVELOPE and QIDI_PA_TABLE are
#   reused UNMODIFIED, and QIDI_PA_TABLE in particular reads pa_table.json
#   from a fixed path with no override. Changing that would mean touching
#   Stage 4, which is exactly what this addition must not do.
#
#   The residual risk this accepts: nothing marks an entry in either file as
#   "bed" or "chute". QIDI_PA_ENVELOPE without an explicit QMAX= takes the
#   MOST RECENT flow_ramp.json entry, so within one orchestrated run here that
#   is always correct (Stage 1 just ran and appended), but a user running
#   stages standalone, interleaving bed and chute runs, could pick up the
#   wrong one. QIDI_PA_TABLE's run-averaging is protected in practice by its
#   existing signature check, which includes the exact flow points measured -
#   a bed run and a chute run overlapping there by coincidence is vanishingly
#   unlikely. Not a new class of risk QIDI_AUTO_CALIBRATE did not already
#   have between different chute runs; just worth knowing about.
#
# SAFETY
#   Heats and extrudes, so SAFETY.md rule 5 applies: watched, with you at the
#   machine - doubly so here, since the Z-300 bed move and bed-centre
#   extrusion position are new motion this project has not run before.
#   Every abort in the underlying stages still fires unchanged.
#
#   An unhandled exception is converted to a command error rather than being
#   allowed to become a Klipper INTERNAL ERROR, which would shut down every
#   MCU. The heater-off and cooldown-wipe steps run from a finally: block, so
#   an abort part way through still leaves the machine cold and the nozzle
#   clean.
#
# USAGE
#   [qidi_auto_cal_bed]
#
#   QIDI_BED_PREPARE                        home, capture position, chute,
#                                            lower the bed - standalone, so
#                                            this motion can be proven on its
#                                            own before anything heats
#   QIDI_AUTO_CALIBRATE_BED                  the whole thing
#   QIDI_AUTO_CALIBRATE_BED DRY=1            print the plan, no heat, no motion
#   QIDI_AUTO_CALIBRATE_BED TEMP=275 BLOCKS=12   override the defaults
#   QIDI_AUTO_CALIBRATE_BED SKIP_FLOW=1      reuse the last max flow, PA only
#
#   Results also land in ~/printer_data/qidi_pa/auto_cal_bed.json

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
# it. Identical reasoning to the chute routine - see qidi_auto_cal.py.
COOLDOWN_WIPE_C = 170.0

# The chamber air filter, same fan Stage 1 uses. Stage 1 no longer switches it
# off mid-chain, so this module does it at the very end.
FILTER_M106_P = 3

# 200 mm on this Q2: Budd's instructed value, a 65 mm margin against the real
# 265 mm max (stepper_z position_max in QIDI's own printer.cfg - Budd's own
# figure at the time was 256 mm, 9 mm short of the config's actual value;
# either way 200 mm clears it with room spare). NOT derived from config, and
# NOT ported to another model by changing this number alone - see CHANGELOG/.
BED_DOWN_Z = 200.0
# 1200 mm/min, Budd's instructed value for this Q2 too - same figure as the
# Max4's, given with the same reasoning ("just to get it out of the way"), not
# independently re-verified as a Q2-specific limit; override with BED_FEED=
# if it turns out wrong.
BED_DOWN_FEED = 1200.0     # mm/min = 20 mm/s

BANNER = "Buddman69's Auto Test Results (BED ROUTINE)"
FOOTER = ("End Results - project probably maybe on github when I get around "
          "to it possibly......")

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


class QidiAutoCalBed:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        out_dir = config.get('out_dir', '~/printer_data/qidi_pa')
        self.out_dir = os.path.expanduser(out_dir)
        self.report_path = os.path.join(self.out_dir, 'auto_cal_bed.json')
        self.position_path = os.path.join(self.out_dir, 'bed_position.json')
        self.cooldown_c = config.getfloat('cooldown_wipe_temp', COOLDOWN_WIPE_C,
                                          above=40., below=250.)
        self.bed_down_z = config.getfloat('bed_down_z', BED_DOWN_Z,
                                          above=0., below=1000.)
        self.bed_down_feed = config.getfloat('bed_down_feed', BED_DOWN_FEED,
                                             above=0.)
        self.gcode.register_command('QIDI_BED_PREPARE', self.cmd_PREPARE,
                                    desc=self.cmd_PREPARE_help)
        self.gcode.register_command('QIDI_AUTO_CALIBRATE_BED', self.cmd_AUTO,
                                    desc=self.cmd_AUTO_help)

    # -- plumbing ------------------------------------------------------
    def _script(self, s):
        self.gcode.run_script_from_command(s)

    def _guard(self, gcmd, fn, tag):
        err_cls = type(gcmd.error("probe"))
        try:
            return fn(gcmd)
        except err_cls:
            raise
        except Exception as e:
            logging.exception("qidi_auto_cal_bed: unhandled error")
            raise gcmd.error("%s: internal error, printer left running: "
                             "%s: %s" % (tag, type(e).__name__, str(e)[:160]))

    def _read_json(self, name, gcmd, what):
        path = os.path.join(self.out_dir, name)
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            raise gcmd.error("auto_cal_bed: %s did not produce %s - check "
                             "the console output above for why" % (what, name))
        if isinstance(d, list):
            if not d:
                raise gcmd.error("auto_cal_bed: %s is empty" % (name,))
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
            data.append({'kind': 'auto_cal_bed', 'time': time.time(),
                         'payload': payload})
            tmp = self.report_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=1, default=repr)
            os.replace(tmp, self.report_path)
        except Exception:
            logging.exception("qidi_auto_cal_bed: could not write report")

    def _temp(self):
        ext = self.printer.lookup_object('extruder', None)
        if ext is None:
            return 0.0
        try:
            return float(ext.get_status(self.reactor.monotonic())
                         ['temperature'])
        except Exception:
            return 0.0

    # -- QIDI_BED_PREPARE ------------------------------------------------
    cmd_PREPARE_help = ("Home (always), capture the bed-centre position, "
                        "move to the chute, lower the bed. Standalone motion "
                        "only - no heat, no extrusion - so it can be proven "
                        "before anything else runs. [BED_Z=300] [BED_FEED=1200]")

    def cmd_PREPARE(self, gcmd):
        return self._guard(gcmd, self._run_PREPARE, "bed_prepare")

    def _run_PREPARE(self, gcmd):
        bed_z = gcmd.get_float('BED_Z', self.bed_down_z, above=0., below=1000.)
        feed = gcmd.get_float('BED_FEED', self.bed_down_feed, above=0.)

        toolhead = self.printer.lookup_object('toolhead')
        gcmd.respond_info("bed_prepare: homing (mandatory - this establishes "
                          "the bed-centre reference the whole bed routine "
                          "uses)")
        self._script("G28")
        toolhead.wait_moves()

        pos = toolhead.get_position()
        cx, cy = pos[0], pos[1]
        gcmd.respond_info("bed_prepare: captured bed-centre X%.2f Y%.2f "
                          "(wherever G28 left the toolhead)" % (cx, cy))

        gcmd.respond_info("bed_prepare: moving to the purge chute (toolhead "
                          "clear before the bed moves)")
        self._script("MOVE_TO_TRASH")
        toolhead.wait_moves()

        gcmd.respond_info("bed_prepare: lowering the bed to Z%.0f at F%.0f"
                          % (bed_z, feed))
        self._script("G90")
        self._script("G1 Z%.2f F%.0f" % (bed_z, feed))
        toolhead.wait_moves()

        os.makedirs(self.out_dir, exist_ok=True)
        tmp = self.position_path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'x': cx, 'y': cy, 'bed_z': bed_z, 'time': time.time()},
                     f, indent=1)
        os.replace(tmp, self.position_path)
        gcmd.respond_info("bed_prepare: ready - bed centre X%.2f Y%.2f, bed "
                          "at Z%.0f" % (cx, cy, bed_z))

    # -- the cooldown wipe -------------------------------------------------
    def _cooldown_wipe(self, gcmd):
        """Heater off, wait for the nozzle to fall to the wipe temperature,
        then wipe off whatever sagged out while it depressurised. At the
        chute, same as the chute routine - the bed stays down, nothing here
        needs to return to bed centre since the routine is ending."""
        try:
            self._script("M104 S0")
            gcmd.respond_info(
                "auto_cal_bed: heater off, waiting for %.0f C to wipe off "
                "cooldown ooze (nozzle is %.0f C now)"
                % (self.cooldown_c, self._temp()))
            self._script("TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=%.0f"
                         % (self.cooldown_c,))
            self._script("QIDI_FLOW_BED_WIPE RETURN=0")
            self._script("M106 P%d S0" % (FILTER_M106_P,))
            gcmd.respond_info("auto_cal_bed: cooldown wipe done at %.0f C, "
                              "air filtration off. Bed stays lowered - the "
                              "next real print homes it fresh."
                              % (self._temp(),))
            return True
        except Exception as e:
            gcmd.respond_info("auto_cal_bed: cooldown wipe skipped (%s)"
                              % (str(e)[:80],))
            return False

    # -- the report ----------------------------------------------------
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

    cmd_AUTO_help = ("Run the whole calibration OVER THE BED instead of the "
                     "purge chute - for filament that will not clear the "
                     "chute reliably. HEATS AND EXTRUDES. [DRY=1] [TEMP=275] "
                     "[BLOCKS=12] [AMP=0.5] [POINTS=5] [LAYER=] [WIDTH=] "
                     "[SKIP_FLOW=0] [BED_Z=300] [BED_FEED=1200]")

    def cmd_AUTO(self, gcmd):
        return self._guard(gcmd, self._run, "auto_cal_bed")

    def _run(self, gcmd):
        dry = gcmd.get_int('DRY', 0)
        temp = gcmd.get_float('TEMP', DEFAULT_TEMP, above=150., below=350.)
        blocks = gcmd.get_int('BLOCKS', DEFAULT_BLOCKS, minval=1, maxval=50)
        amp = gcmd.get_float('AMP', DEFAULT_AMP, above=0.02, below=1.0)
        leg = gcmd.get_float('LEG_MS', DEFAULT_LEG_MS, above=20., below=2000.)
        pts = gcmd.get_int('POINTS', DEFAULT_POINTS, minval=2, maxval=12)
        skip_flow = gcmd.get_int('SKIP_FLOW', 0)
        bed_z = gcmd.get_float('BED_Z', self.bed_down_z, above=0., below=1000.)
        bed_feed = gcmd.get_float('BED_FEED', self.bed_down_feed, above=0.)
        layer = gcmd.get_float('LAYER', None)
        width = gcmd.get_float('WIDTH', None)
        geom = ""
        if layer is not None:
            geom += " LAYER=%.3f" % (layer,)
        if width is not None:
            geom += " WIDTH=%.3f" % (width,)

        gcmd.respond_info(
            "auto_cal_bed: full BED-CENTRE calibration at %.0f C - flow "
            "search, then PA at %d flow points (%d blocks, dv %.0f%%), all "
            "over the lowered bed. Wiping happens at the chute; the bed "
            "stays down the whole time and is NOT raised back up at the end."
            % (temp, pts, blocks, 100 * amp))
        if skip_flow:
            gcmd.respond_info("auto_cal_bed: SKIP_FLOW - reusing the last "
                              "QIDI_FLOW_BED_SEARCH result")
        if dry:
            gcmd.respond_info("auto_cal_bed: DRY - showing the plan only")
            for step in ("1  QIDI_BED_PREPARE  (G28 - mandatory, always - "
                         "capture bed centre, chute, bed down to Z%.0f)"
                         % bed_z,
                         "2  QIDI_FLOW_BED_SEARCH TEMP=%.0f COOLDOWN=0" % temp,
                         "3  QIDI_FLOW_BED_WIPE",
                         "4  QIDI_PA_ENVELOPE POINTS=%d AMP=%.2f%s  "
                         "(unchanged, pure maths)" % (pts, amp, geom),
                         "5  QIDI_PA_BED_MEASURE TEMP=%.0f BLOCKS=%d AMP=%.2f "
                         "LEG_MS=%.0f" % (temp, blocks, amp, leg),
                         "6  QIDI_FLOW_BED_WIPE",
                         "7  QIDI_PA_TABLE RUNS=1  (unchanged, pure maths)",
                         "8  M104 S0",
                         "9  TEMPERATURE_WAIT MAXIMUM=%.0f, QIDI_FLOW_BED_WIPE, "
                         "air filtration off. Bed stays down."
                         % self.cooldown_c):
                gcmd.respond_info("   " + step)
            gcmd.respond_info("auto_cal_bed: DRY - nothing heated, nothing "
                              "moved.")
            return

        # Outside the try/finally on purpose, same reasoning as the chute
        # routine: the cooldown wipe in that finally needs the bed already
        # down and a captured position, and nothing is hot yet, so a failed
        # prepare should abort here rather than pile a wipe error on top.
        gcmd.respond_info("auto_cal_bed: ---- preparing (home, chute, bed "
                          "down) ----")
        self._script("QIDI_BED_PREPARE BED_Z=%.2f BED_FEED=%.0f"
                     % (bed_z, bed_feed))

        t0 = time.time()
        stage = "startup"
        qmax = None
        try:
            # ---- Stage 1 : max volumetric flow, at bed centre -----------
            if not skip_flow:
                stage = "Stage 1 (QIDI_FLOW_BED_SEARCH)"
                gcmd.respond_info("auto_cal_bed: ---- Stage 1: max flow "
                                  "(bed centre) ----")
                # COOLDOWN=0 for the same reason as the chute routine: Stage 3
                # does not set temperature on its own unless told to, and this
                # keeps the nozzle hot and the melt conditioned between stages.
                self._script("QIDI_FLOW_BED_SEARCH TEMP=%.0f COOLDOWN=0"
                             % (temp,))
                self._script("QIDI_FLOW_BED_WIPE")
            flow = self._read_json('flow_ramp.json', gcmd, stage)
            qmax = flow.get('working_max')
            if not qmax:
                raise gcmd.error(
                    "auto_cal_bed: no working_max from the flow search - it "
                    "may have aborted. Nothing downstream can run without "
                    "it.")
            gcmd.respond_info("auto_cal_bed: max volumetric flow %.2f mm3/s"
                              % (qmax,))

            # ---- Stage 2 : the flow points - UNCHANGED, pure maths ------
            stage = "Stage 2 (QIDI_PA_ENVELOPE)"
            gcmd.respond_info("auto_cal_bed: ---- Stage 2: flow points ----")
            self._script("QIDI_PA_ENVELOPE QMAX=%.3f POINTS=%d AMP=%.2f%s"
                         % (qmax, pts, amp, geom))

            # ---- Stage 3 : pressure advance, at bed centre --------------
            stage = "Stage 3 (QIDI_PA_BED_MEASURE)"
            gcmd.respond_info("auto_cal_bed: ---- Stage 3: pressure advance "
                              "(bed centre) ----")
            self._script("QIDI_PA_BED_MEASURE TEMP=%.0f BLOCKS=%d AMP=%.2f "
                         "LEG_MS=%.0f" % (temp, blocks, amp, leg))
            self._script("QIDI_FLOW_BED_WIPE")

            # ---- Stage 4 : the Orca table - UNCHANGED, pure maths -------
            stage = "Stage 4 (QIDI_PA_TABLE)"
            gcmd.respond_info("auto_cal_bed: ---- Stage 4: Orca table ----")
            self._script("QIDI_PA_TABLE RUNS=1")
            table = self._read_json('orca_pa.json', gcmd, stage)
            rows = table.get('rows') or []
            centre = table.get('fallback_pressure_advance')
            if not rows or centre is None:
                raise gcmd.error("auto_cal_bed: Stage 4 produced no table")
        finally:
            self._cooldown_wipe(gcmd)

        mins = (time.time() - t0) / 60.0
        block = self._results_block(gcmd, qmax, centre, rows)
        gcmd.respond_info("auto_cal_bed: complete in %.1f min. Also written "
                          "to %s" % (mins, self.report_path))
        self._record({'qmax_mm3_s': qmax, 'centre_k': centre, 'rows': rows,
                      'temp_c': temp, 'blocks': blocks, 'amp': amp,
                      'leg_ms': leg, 'points': pts, 'bed_down_z': bed_z,
                      'minutes': mins, 'block': block,
                      'geometry': table.get('geometry')})


def load_config(config):
    return QidiAutoCalBed(config)
