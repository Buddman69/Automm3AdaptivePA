# qidi_flow_ramp.py - Stage 1: measure melt-pressure force against volumetric flow
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
#
# SAFETY - read SAFETY.md rule 2 before touching this
#   This is the first thing in the project that heats and extrudes.
#
#   There is no firmware extrusion guard on this machine (QIDI disabled them),
#   no ADC ceiling (+/-2^23 is +/-46,900 gf, 23x the cell's rating) and nothing
#   mechanical. The aborts below are the ONLY protection that exists.
#
#   Force is enforced as counts above a FRESH tare, never as an absolute count,
#   because the baseline wanders several gf over minutes.
#
#   Extrusion is issued in short chunks and paced so at most ~0.5 s is buffered
#   in the look-ahead. That bounds abort latency: a single long queued move
#   could not be cancelled once issued.
#
#   Any exit path - normal, abort or exception - stops extrusion and retracts.
#
# USAGE
#   [qidi_flow_ramp]
#
#   QIDI_FLOW_RAMP STEPS=8 QMAX=9.98 TEMP=275
#
#   Results are appended to ~/printer_data/qidi_pa/flow_ramp.json

import json
import logging
import math
import os
import time

A_FIL = math.pi * (1.75 / 2.0) ** 2          # 2.40528 mm^2

# Measured against a kitchen scale under the nozzle; see README.md,
# "Calibrating counts per gram". It is NOT a constant of the design.
COUNTS_PER_GF = 182.96
# The cell's rating is 2000 gf (unverified, from the Q2 config). Calibration
# took it to 2175 gf with no hysteresis and perfect linearity, so this is well
# inside proven territory FOR THE CELL. The real risk at this force is the
# filament grinding in the drive gear - which is why VARIANCE_ABORT is armed.
#
# 1650 rather than 1800: on the first search run the highest passing flow
# (24 mm3/s) peaked at 1587 gf and the blob at 21 mm3/s peaked at 1667 gf. 1650
# sits between them - 4% margin above the real measurement, and it catches that
# blob 17 gf early instead of letting it grow for 19.8 s.
#
# BE AWARE this threshold largely SETS the answer rather than guarding it. On
# that run 25 mm3/s never failed the equilibrium test; it failed on force twice
# and variance once, and 23/24/25 settled at slopes of -5.3/-5.5/-6.8 gf/s
# against a 6.0 threshold the slope noise (+/-11 gf/s) cannot resolve. Lowering
# this number lowers the reported max flow.
ABORT_GF = 1650.0
DEFAULT_ABORT_COUNTS = int(ABORT_GF * COUNTS_PER_GF)     # 329,328 at 1800 gf

# Backstop against a logic error dumping filament, not a budget. The real
# budget is enforced per-step via e_left. A legitimate 12-flow up+down sweep
# worst-cases around 2400 mm; anything past 3000 is a bug, not a measurement.
DEFAULT_MAX_E_MM = 3000.0
CHUNK_S = 0.25                               # extrusion chunk length
BUFFER_S = 0.5                               # keep look-ahead this shallow
SETTLED_SLOPE_GF_S = 6.0                     # above this the step is still moving

# Hold each step until it actually settles rather than predicting how long that
# takes. The first full ramp tried to predict it and got the scaling backwards:
# dwell was set from 12/Q, giving 8 s at low flow and 3.5 s at high flow. That
# is right for hydraulic fill (slower at low flow) but the high-flow problem is
# THERMAL - the heater falling behind - which gets worse as flow rises. Step 12
# was still climbing at 159 gf/s when its 3.5 s expired.
# Settling needs TWO consecutive stable windows, so the minimum hold has to be
# long enough to contain both plus a margin.
SETTLE_WINDOW_S = 1.5     # slope and mean are both taken over this trailing window
SETTLE_MIN_S = 2 * SETTLE_WINDOW_S + 0.5
SETTLE_MAX_S = 25.0

# A prime before step 1. The melt zone is unpressurised after heating and the
# drift check. The first attempt used 5 mm3/s for 4 s and was still climbing at
# the end, which left step 1 continuing the same transient - so it is now bigger
# and faster.
PRIME_FLOW = 10.0
PRIME_S = 6.0

# Drift gate. Generous because the run is now drift-CORRECTED using the opening
# and closing tares; this only has to catch a hotend that never equilibrates.
DRIFT_LIMIT_GF_MIN = 25.0
DEFAULT_SOAK_MAX_S = 300.0

# Nozzle wipe travel, as offsets from park_x. QIDI's CLEAR_NOZZLE_PLR uses
# +8 to +27; that turned out not to sweep far enough to clean properly, so this
# is wider and adjustable per run via LO= / HI=.
WIPE_LO_OFFSET = 0.0
WIPE_HI_OFFSET = 45.0
# 6, raised from 5 on 2026-09-21 to match qidi_pa_measure.py. The two stages had
# drifted apart (5 here, 6 there) for no reason anyone could reconstruct, which
# made "how many wipes does it do?" unanswerable without reading both files.
WIPE_PASSES = 6

# Then four SHORTER strokes, added 2026-09-21 because the chute was not clearing
# on some runs. The long passes carry material out along the full travel; these
# work the near end, where what the long strokes drag back was being left.
#
# The short travel is a FRACTION of whatever stroke is actually in use, not a
# second fixed offset - so LO= / HI= overrides still scale it correctly instead
# of silently producing a "short" pass longer than the long one.
WIPE_SHORT_PASSES = 4
WIPE_SHORT_FRAC = 0.7

# Feedrates. NOT to be raised: doubled to 20000/12000 on 2026-09-15 and reverted
# the same day, because at 333 mm/s the wiper left waste on the nozzle, and
# anything measured after a bad wipe is corrupted. More passes are cheap; faster
# passes are not.
WIPE_FEED_FAST = 10000
WIPE_FEED_SLOW = 6000

# Slip detection, now armed by default. Baselines from the first two full ramps:
# settled steps ran 5-13 gf of within-step sd, 22 gf at 19.87 mm3/s and 63 gf at
# 25.00 mm3/s when the melt stopped keeping up. 35 gf catches a slipping drive
# gear without tripping on genuine high-flow variance.
DEFAULT_VARIANCE_GF = 35.0

# A flow that aborts is a data point, not a run-ender: every remaining flow in
# the sweep is lower, so lower force, and safe to continue. This counts DISTINCT
# flows that failed, not abort events - the top flow failing on every leg is one
# finding, not several. More than a couple of distinct flows failing means
# something is wrong rather than that we found the limit.
DEFAULT_MAX_ABORTS = 2

# Phase 2: having bracketed the limit between the highest flow that equilibrated
# and the lowest that did not, subdivide to find where it actually sits.
DEFAULT_REFINE = 4
DEFAULT_REFINE_REPEATS = 2
# Working max = limit x margin, rounded DOWN to the nearest 0.5. Rounding down
# never eats into the margin. 0.85 rather than 0.90: this is a safety
# buffer below a real measured limit, not an error bar, and the number sets
# print speeds.
WORKING_MARGIN = 0.85
WORKING_ROUND = 0.5

# ---- adaptive search (QIDI_FLOW_SEARCH) ---------------------------------
# Coarse stride, bisect, then +1 mm3/s.
SEARCH_START = 5.0             # 0 counts as an automatic pass, so a bracket
SEARCH_COARSE_STEP = 10.0      #   always exists even for a flexible
SEARCH_FINE_STEP = 1.0
SEARCH_MAX_Q = 60.0            # sanity check, not a safety limit: a 0.6 nozzle
                               # reaching this means the measurement is wrong
SEARCH_CONFIRM_FAILS = 2       # failure is reproducible to 0.4% (1809/1807/1815)

# Wipe on VOLUME extruded, not on a count of measurements. Build-up scales with
# material through the nozzle, and a measurement is 37 mm3 at 5 mm3/s but 416 at
# 21 - eleven times the material for the same "one measurement". Counting
# measurements let 1014 mm3 pass unwiped between the wipe before the bisect and
# the blob three measurements later.
#
# 200 rather than 300: at 300, one run had 20 mm3/s measured with 186 mm3
# already unwiped and accumulating to 488 before a wipe triggered. It read
# 1244 gf while the very next flow UP (21 mm3/s), measured clean straight after
# a wipe, read 1236 - lower force at higher flow, which cannot be right. At 200
# nothing runs carrying the previous measurement's material.
SEARCH_WIPE_VOLUME_MM3 = 200.0

# Chamber exhaust / air filtration. The touchscreen's exhaust button sends
# M106 P3 S254, which QIDI's own M106 macro routes to chamber_circulation_fan -
# and ALSO forces the chamber heater target to 0, since venting and heating the
# chamber are mutually exclusive. We go through M106 rather than SET_FAN_SPEED
# so that coupling is preserved instead of bypassed.
#
# It runs for the whole routine including the soak. Five minutes at 275 C with
# no extrusion is the peak outgassing period and the part you are least likely
# to be standing next to.
FILTER_M106_P = 3
FILTER_SPEED = 254

# Priming is merged into the measurement - it runs at the test flow, so the fill
# IS the start of the measurement and the settle detector ignores it. The cap has
# to allow for that fill, which genuinely does scale as 1/Q.
#
# NOT the earlier mistake: that scaled the whole dwell as 12/Q including the
# thermal part, which gets worse at high flow, so the scaling was backwards
# exactly where it mattered. Fill time does scale as 1/Q.
SEARCH_FILL_MM3 = 100.0
SEARCH_BASE_CAP_S = 15.0


def _mean(v):
    return sum(v) / len(v) if v else 0.0


def _slope(pts):
    # Least-squares d(value)/dt over (t, value) pairs.
    n = len(pts)
    if n < 2:
        return 0.0
    sx = sum(t for t, _ in pts); sy = sum(v for _, v in pts)
    sxx = sum(t * t for t, _ in pts); sxy = sum(t * v for t, v in pts)
    den = n * sxx - sx * sx
    return 0.0 if abs(den) < 1e-12 else (n * sxy - sx * sy) / den


def _stdev(v):
    if len(v) < 2:
        return 0.0
    m = _mean(v)
    return (sum((x - m) ** 2 for x in v) / len(v)) ** 0.5


def _working_max(limit):
    # Working max = limit x margin, rounded DOWN to WORKING_ROUND. Rounding
    # down never eats into the safety buffer.
    if not limit:
        return None
    return math.floor(limit * WORKING_MARGIN / WORKING_ROUND) * WORKING_ROUND


def _classify_flows(results):
    # (equilibrated, failed) flow lists.
    #
    # A flow counts as equilibrated only if EVERY measurement of it settled and
    # none aborted. Taking "any measurement settled" is what broke the 48-point
    # sweep: 25 mm3/s aborted on both up-legs but appeared to settle on both
    # down-legs (the abort's retract left a flat-looking re-pressurisation
    # shoulder), so it landed in the good set, max() picked it, and the run
    # reported a working max of 22.5 that nothing supported.
    per = {}
    for r in results:
        if str(r.get('leg', '')) == 'prime' or r.get('step') == 0:
            continue
        q = round(r['flow'], 3)
        good = (r.get('abort') is None) and bool(r.get('settled'))
        per[q] = per.get(q, True) and good
    ok = sorted(q for q, v in per.items() if v)
    bad = sorted(q for q, v in per.items() if not v)
    return ok, bad


def _loglog_fit(qs, fs):
    # F = a * Q^n  ->  ln F = ln a + n ln Q
    pts = [(math.log(q), math.log(f)) for q, f in zip(qs, fs) if q > 0 and f > 0]
    if len(pts) < 3:
        return None
    n = len(pts)
    sx = sum(x for x, _ in pts); sy = sum(y for _, y in pts)
    sxx = sum(x * x for x, _ in pts); sxy = sum(x * y for x, y in pts)
    den = n * sxx - sx * sx
    if abs(den) < 1e-12:
        return None
    slope = (n * sxy - sx * sy) / den
    inter = (sy - slope * sx) / n
    resid = [y - (slope * x + inter) for x, y in pts]
    return {'n': slope, 'ln_a': inter, 'a': math.exp(inter),
            'resid_rms': (sum(r * r for r in resid) / len(resid)) ** 0.5,
            'points': len(pts)}


class QidiFlowRamp:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.root_name = config.get('root', 'probe_air')
        self.sensor_attr = config.get('sensor_attr', 'sensor_helper')
        self.counts_per_gf = config.getfloat('counts_per_gf', COUNTS_PER_GF,
                                             above=1.)
        out_dir = config.get('out_dir', '~/printer_data/qidi_pa')
        self.out_dir = os.path.expanduser(out_dir)
        self.report_path = os.path.join(self.out_dir, 'flow_ramp.json')
        self.gcode.register_command('QIDI_FLOW_RAMP', self.cmd_RAMP,
                                    desc=self.cmd_RAMP_help)
        self.gcode.register_command('QIDI_FLOW_WIPE', self.cmd_WIPE,
                                    desc=self.cmd_WIPE_help)
        self.gcode.register_command('QIDI_FLOW_SEARCH', self.cmd_SEARCH,
                                    desc=self.cmd_SEARCH_help)

    def _guard(self, gcmd, fn):
        # Klipper treats an unhandled exception in a gcode command as an
        # INTERNAL ERROR and latches every MCU into shutdown - a stray
        # NameError did exactly that on 2026-09-15, and it took a
        # FIRMWARE_RESTART to clear. A gcode.error is by contrast a clean
        # command failure that leaves the printer running, so anything
        # unexpected is converted into one here.
        # Derive the clean-failure class from gcmd rather than importing it,
        # so this works against the mock harness too.
        err_cls = type(gcmd.error("probe"))
        try:
            return fn(gcmd)
        except err_cls:
            raise
        except Exception as e:
            logging.exception("qidi_flow_ramp: unhandled error")
            raise gcmd.error("flow_ramp: internal error, printer left running: "
                             "%s: %s" % (type(e).__name__, str(e)[:160]))

    # ------------------------------------------------------------------ utils

    def _record(self, payload):
        entry = {'kind': 'flow_ramp', 'time': time.time(), 'payload': payload}
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            data = []
            if os.path.exists(self.report_path):
                try:
                    with open(self.report_path, 'r') as f:
                        data = json.load(f)
                    if not isinstance(data, list):
                        data = [data]
                except Exception:
                    data = []
            data.append(entry)
            tmp = self.report_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=1, default=repr)
            os.replace(tmp, self.report_path)
            return True
        except Exception:
            logging.exception("qidi_flow_ramp: could not write report")
            return False

    def _sensor(self, gcmd):
        root = self.printer.lookup_object(self.root_name, None)
        if root is None:
            raise gcmd.error("flow_ramp: no object %s" % (self.root_name,))
        s = getattr(root, self.sensor_attr, None)
        if s is None:
            raise gcmd.error("flow_ramp: %s has no %s - run QIDI_CS_LOCATE"
                             % (self.root_name, self.sensor_attr))
        read = getattr(s, 'read_origin_data', None)
        if not callable(read):
            raise gcmd.error("flow_ramp: read_origin_data is not callable")
        return read

    def _sample(self, read, n, interval, gcmd):
        vals, eventtime = [], self.reactor.monotonic()
        for _ in range(n):
            try:
                v = read()
            except Exception as e:
                raise gcmd.error("flow_ramp: force read failed: %s" % (e,))
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(float(v))
            eventtime = self.reactor.pause(eventtime + interval)
        if not vals:
            raise gcmd.error("flow_ramp: no numeric force readings")
        return vals

    def _wait_temp_stable(self, gcmd, target, tol, window, timeout):
        # Klipper's own wait returns inside a band; the MAX6675 is slow, so
        # require the reading to actually sit still before taring.
        heater = self.printer.lookup_object('extruder').get_heater()
        pheaters = self.printer.lookup_object('heaters')
        gcmd.respond_info("flow_ramp: heating to %.0fC" % (target,))
        pheaters.set_temperature(heater, target, True)
        gcmd.respond_info("flow_ramp: at target, waiting for a stable reading")
        deadline = self.reactor.monotonic() + timeout
        eventtime = self.reactor.monotonic()
        hist = []
        while self.reactor.monotonic() < deadline:
            cur = heater.get_temp(eventtime)[0]
            hist.append(cur)
            hist = hist[-int(window / 0.5):] or hist
            if len(hist) >= 4 and max(hist) - min(hist) <= tol \
                    and abs(_mean(hist) - target) <= tol:
                gcmd.respond_info("flow_ramp: stable at %.1fC (spread %.2f over "
                                  "%.0fs)" % (_mean(hist), max(hist) - min(hist),
                                              window))
                return _mean(hist)
            eventtime = self.reactor.pause(eventtime + 0.5)
        raise gcmd.error("flow_ramp: hotend did not settle within %.0fs"
                         % (timeout,))

    # --------------------------------------------------------------- the ramp

    cmd_RAMP_help = ("Flow ramp: measure melt force vs volumetric flow over the "
                     "purge chute. [STEPS=8] [QMIN=2] [QMAX=9.98] [TEMP=275] "
                     "[STEP_TIME=3.5] [HZ=30] [VARIANCE_ABORT=0] [DRY=0]")

    def cmd_RAMP(self, gcmd):
        return self._guard(gcmd, self._run_RAMP)

    def _run_RAMP(self, gcmd):
        read = self._sensor(gcmd)
        steps = gcmd.get_int('STEPS', 8, minval=3, maxval=24)
        qmin = gcmd.get_float('QMIN', 2.0, above=0.)
        qmax = gcmd.get_float('QMAX', 9.98, above=0.)
        temp = gcmd.get_float('TEMP', 275., minval=170., maxval=300.)
        step_time = gcmd.get_float('STEP_TIME', 3.5, minval=1., maxval=15.)
        hz = gcmd.get_float('HZ', 30., minval=5., maxval=77.)
        var_abort = gcmd.get_float('VARIANCE_ABORT', DEFAULT_VARIANCE_GF,
                                   minval=0.)
        abort_counts = gcmd.get_int('ABORT_COUNTS', DEFAULT_ABORT_COUNTS,
                                    minval=1000)
        max_e = gcmd.get_float('MAX_E', DEFAULT_MAX_E_MM, above=0.)
        retract = gcmd.get_float('RETRACT', 2.0, minval=0., maxval=10.)
        wipe = gcmd.get_int('WIPE', 1, minval=0, maxval=1)
        cooldown = gcmd.get_int('COOLDOWN', 1, minval=0, maxval=1)
        prime = gcmd.get_int('PRIME', 1, minval=0, maxval=1)
        soak_max = gcmd.get_float('SOAK_MAX', DEFAULT_SOAK_MAX_S, minval=30.,
                                  maxval=1800.)
        repeats = gcmd.get_int('REPEATS', 1, minval=1, maxval=6)
        # Up-legs only by default. Descending from high flow leaves the melt
        # zone over-pressurised for each new lower flow, so it decompresses by
        # pushing out surplus material - carried away at high flow, but it
        # oozes and sticks below ~8 mm3/s. That is what produced the blob
        # halfway down the descending leg, and it also biased the down-leg
        # +21% at 2 mm3/s falling to -3% at 19.87. Ascending never does this:
        # the melt absorbs material to reach the higher pressure.
        updown = gcmd.get_int('UPDOWN', 0, minval=0, maxval=1)
        max_aborts = gcmd.get_int('MAX_ABORTS', DEFAULT_MAX_ABORTS, minval=0,
                                  maxval=10)
        refine = gcmd.get_int('REFINE', DEFAULT_REFINE, minval=0, maxval=10)
        refine_reps = gcmd.get_int('REFINE_REPEATS', DEFAULT_REFINE_REPEATS,
                                   minval=1, maxval=6)
        wipe_lo = gcmd.get_float('WIPE_LO', None)
        wipe_hi = gcmd.get_float('WIPE_HI', None)
        wipe_passes = gcmd.get_int('WIPE_PASSES', WIPE_PASSES, minval=1,
                                   maxval=20)
        filt = gcmd.get_int('FILTER', 1, minval=0, maxval=1)
        dry = gcmd.get_int('DRY', 0, minval=0, maxval=1)
        if qmax <= qmin:
            raise gcmd.error("flow_ramp: QMAX must exceed QMIN")

        # Log spacing, not linear: the interesting region is the low end, where
        # the force curve is steepest.
        ratio = (qmax / qmin) ** (1.0 / (steps - 1))
        flows = [qmin * ratio ** i for i in range(steps)]
        # Build the measurement sequence: up, then down, repeated. The
        # down-leg is the highest-value addition - it gives a duplicate at every
        # flow (so step-to-step reproducibility is measured, not guessed), it
        # reveals hysteresis (which is what slip looks like), and it passes each
        # flow on a block that has been at high load, which tests the thermal
        # explanation for free.
        # Grouped into LEGS, because each leg now starts with a wipe and a
        # prime. Without the wipe, material builds up on the nozzle across the
        # run and raises the effective restriction: the 48-point sweep measured
        # the same flow 17-58% higher by the last leg than the first, which made
        # every leg after up1 worthless. The prime is needed because wiping
        # means stopping extrusion, which depressurises the melt zone.
        legs = []
        for rep in range(repeats):
            legs.append(('up%d' % (rep + 1,),
                         [(q, i + 1) for i, q in enumerate(flows)]))
            if updown:
                legs.append(('down%d' % (rep + 1,),
                             [(q, steps - i)
                              for i, q in enumerate(reversed(flows))]))
        seq = [(q, lab, i) for lab, items in legs for q, i in items]
        # Worst case: every point runs to the cap. Typically far less.
        worst_mm = sum(q / A_FIL * SETTLE_MAX_S for q, _l, _i in seq)
        if prime:
            worst_mm += len(legs) * PRIME_FLOW / A_FIL * PRIME_S
        if worst_mm > max_e:
            raise gcmd.error(
                "flow_ramp: worst case needs %.0f mm of filament, over the "
                "%.0f mm cap. Raise MAX_E or lower STEPS/REPEATS."
                % (worst_mm, max_e))

        gcmd.respond_info(
            "flow_ramp: %d flows %.2f-%.2f mm3/s, %d measurements (%s), held "
            "%.0f-%.0fs each to settling"
            % (steps, flows[0], flows[-1], len(seq),
               "up+down x%d" % repeats if updown else "up x%d" % repeats,
               SETTLE_MIN_S, SETTLE_MAX_S))
        # Most steps settle in a few seconds; only the top flows approached the
        # cap on the first full ramp. Show both so the worst case does not look
        # like the expected cost.
        likely_mm = sum(q / A_FIL * min(SETTLE_MAX_S, max(SETTLE_MIN_S, 6.0))
                        for q, _l, _i in seq)
        gcmd.respond_info(
            "flow_ramp: likely ~%.0f mm (%.2f g), worst case %.0f mm (%.2f g) "
            "if every point runs to the %.0fs cap"
            % (likely_mm, likely_mm * A_FIL * 1.09e-3,
               worst_mm, worst_mm * A_FIL * 1.09e-3, SETTLE_MAX_S))
        gcmd.respond_info("flow_ramp: abort at %d counts above tare (%.0f gf, "
                          "on magnitude - melt pressure reads negative)"
                          % (abort_counts, abort_counts / self.counts_per_gf))
        if dry:
            if prime:
                gcmd.respond_info("   prime  Q=%6.2f mm3/s  %.1fs" % (PRIME_FLOW,
                                                                     PRIME_S))
            for q, lab, i in seq:
                gcmd.respond_info("   %-7s step %2d  Q=%6.2f mm3/s  v_E=%5.2f"
                                  % (lab, i, q, q / A_FIL))
            gcmd.respond_info("flow_ramp: DRY=1, nothing executed")
            return

        toolhead = self.printer.lookup_object('toolhead')
        if 'xyz' not in (toolhead.get_status(self.reactor.monotonic())
                         .get('homed_axes', '')):
            raise gcmd.error("flow_ramp: home the printer first (G28)")

        results, aborted, abort_reason = [], False, None
        tare = tare_end = drift_rate = None
        ramp_t0 = None
        try:
            if filt:
                self._filter(gcmd, True)
            gcmd.respond_info("flow_ramp: moving to the purge chute")
            self.gcode.run_script_from_command("OPTIMIZED_MOVE_TO_TRASH")
            toolhead.wait_moves()

            self._wait_temp_stable(gcmd, temp, tol=1.0, window=5.0, timeout=420.)

            # Tare AT TEMPERATURE - the cold baseline does not apply once the
            # hotend is soaking the toolhead.
            tare_v = self._sample(read, 60, 1.0 / hz, gcmd)
            tare = _mean(tare_v)
            gcmd.respond_info("flow_ramp: tare %.0f counts (stdev %.0f = %.2f gf)"
                              % (tare, _stdev(tare_v),
                                 _stdev(tare_v) / self.counts_per_gf))

            # Drift check at temperature. Retry with a longer soak rather than
            # failing: the drift IS the hotend still soaking into the toolhead,
            # and the previous behaviour (fail, then cool down) guaranteed the
            # next attempt started colder and failed again.
            tare, rate = self._settle_drift(gcmd, read, hz, tare, soak_max)
            drift_rate = rate

            self.gcode.run_script_from_command("M83")
            ramp_t0 = self.reactor.monotonic()
            used_mm = 0.0
            aborts = set()          # distinct flows, not abort events
            for lab, items in legs:
                # Every leg starts from the same state: clean nozzle, primed
                # melt zone. See _leg_prep.
                pr_mm = self._leg_prep(gcmd, read, toolhead, lab, prime, hz,
                                       tare, abort_counts, wipe, wipe_lo,
                                       wipe_hi, wipe_passes, max_e - used_mm)
                used_mm += pr_mm
                for q, i in items:
                    t_at = self.reactor.monotonic() - ramp_t0
                    r = self._run_step(gcmd, read, toolhead, i, q, hz, tare,
                                       abort_counts, var_abort,
                                       label="%s step %d" % (lab, i),
                                       e_left=max_e - used_mm)
                    used_mm += r['e_mm']
                    r['t_mid'] = t_at + r['held_s'] * 0.9
                    r['leg'] = lab
                    results.append(r)
                    if r.get('abort'):
                        stop = self._handle_abort(gcmd, toolhead, r, q, wipe,
                                                  wipe_lo, wipe_hi, wipe_passes)
                        if stop:
                            aborted, abort_reason = True, r['abort']
                            break
                        # Count DISTINCT flows, not abort events. A flow at the
                        # top of the range is expected to fail, and failing on
                        # every leg is the measurement working - not three
                        # separate problems. Counting events is what stopped
                        # run 4 before refinement: 25 mm3/s failed on up1,
                        # down1 and up2, which is one finding reported 3 times.
                        aborts.add(round(q, 3))
                        if len(aborts) > max_aborts:
                            aborted = True
                            abort_reason = (
                                "%d distinct flows failed to equilibrate (%s) "
                                "- stopping"
                                % (len(aborts), ", ".join("%.2f" % f for f
                                                          in sorted(aborts))))
                            gcmd.respond_info("flow_ramp: %s" % (abort_reason,))
                            break
                if aborted:
                    break

            # ---- phase 2: refine the bracket -----------------------------
            if refine and not aborted:
                used_mm = self._refine(gcmd, read, toolhead, results, refine,
                                       refine_reps, hz, tare, abort_counts,
                                       var_abort, max_e, used_mm, ramp_t0,
                                       prime, wipe, wipe_lo, wipe_hi,
                                       wipe_passes, updown)
        finally:
            self._finish(gcmd, toolhead, retract, wipe, cooldown,
                         wipe_lo, wipe_hi, wipe_passes, filt)

        ramp_len = None
        try:
            # Let residual melt pressure decay before the closing tare, or it
            # reads as drift. The first run's closing tare was ~67 gf out,
            # more than drift alone accounted for.
            self.reactor.pause(self.reactor.monotonic() + 10.)
            ramp_len = (self.reactor.monotonic() - ramp_t0) if ramp_t0 else None
            tare_end = _mean(self._sample(read, 30, 1.0 / hz, gcmd))
            gcmd.respond_info("flow_ramp: closing tare %.0f, drift over the run "
                              "%.2f gf" % (tare_end,
                                           (tare_end - tare) / self.counts_per_gf))
        except Exception:
            pass

        self._report(gcmd, results, flows, tare, tare_end, aborted,
                     abort_reason, temp, step_time, hz, abort_counts,
                     drift_rate, ramp_len)

    def _settle_drift(self, gcmd, read, hz, tare, soak_max):
        # Measure drift over successive 30 s windows until it is small enough,
        # keeping the heater on throughout. Returns (fresh tare, gf/min).
        window = 30.0
        waited = 0.0
        rate = None
        while True:
            t0 = self.reactor.monotonic()
            self.reactor.pause(t0 + window)
            v = self._sample(read, 30, 1.0 / hz, gcmd)
            elapsed = (self.reactor.monotonic() - t0) / 60.0
            rate = ((_mean(v) - tare) / self.counts_per_gf) / max(elapsed, 1e-6)
            tare = _mean(v)
            waited += window
            gcmd.respond_info("flow_ramp: drift %.1f gf/min after %.0fs soak"
                              % (rate, waited))
            if abs(rate) <= DRIFT_LIMIT_GF_MIN:
                return tare, rate
            if waited >= soak_max:
                raise gcmd.error(
                    "flow_ramp: drift still %.1f gf/min after %.0fs of soaking. "
                    "The hotend is not reaching thermal equilibrium - check the "
                    "part cooling fan is off and nothing is blowing on the "
                    "toolhead." % (rate, waited))

    def _filter(self, gcmd, on):
        try:
            self.gcode.run_script_from_command(
                "M106 P%d S%d" % (FILTER_M106_P, FILTER_SPEED if on else 0))
            gcmd.respond_info("flow_ramp: air filtration %s"
                              % ("ON" if on else "off",))
        except Exception:
            logging.exception("qidi_flow_ramp: filter control failed")
            gcmd.respond_info("flow_ramp: WARNING could not switch the "
                              "filtration %s" % ("on" if on else "off",))

    def _park_x(self):
        px = 135.0
        km = self.printer.lookup_object('gcode_macro _km_globals', None)
        if km is not None:
            try:
                px = float(km.get_status(self.reactor.monotonic())
                           .get('park_x', px))
            except Exception:
                pass
        return px

    def _wipe(self, gcmd, toolhead, lo, hi, passes, short_passes=None):
        # The silicone wiper beside the chute. Defaults match the X oscillation
        # QIDI's own CLEAR_NOZZLE_PLR uses (park_x + 8 to park_x + 27), but the
        # travel is adjustable - that default does not sweep far enough to
        # clean the nozzle properly.
        #
        # Two phases: the long strokes sweep the full travel, then a few shorter
        # ones work the near end. See WIPE_SHORT_PASSES.
        px = self._park_x()
        lo = px + WIPE_LO_OFFSET if lo is None else lo
        hi = px + WIPE_HI_OFFSET if hi is None else hi
        if hi < lo:
            lo, hi = hi, lo
        if short_passes is None:
            short_passes = WIPE_SHORT_PASSES
        short_hi = lo + WIPE_SHORT_FRAC * (hi - lo)
        lines = ["G90"]
        for _ in range(int(passes)):
            lines.append("G1 X%.2f F%d" % (hi, WIPE_FEED_FAST))
            lines.append("G1 X%.2f F%d" % (lo, WIPE_FEED_SLOW))
        for _ in range(int(short_passes)):
            lines.append("G1 X%.2f F%d" % (short_hi, WIPE_FEED_FAST))
            lines.append("G1 X%.2f F%d" % (lo, WIPE_FEED_SLOW))
        lines.append("G1 X%.2f F%d" % (px, WIPE_FEED_SLOW))
        self.gcode.run_script_from_command("\n".join(lines))
        toolhead.wait_moves()
        gcmd.respond_info("flow_ramp: wiped X%.1f-%.1f x%d then X%.1f-%.1f x%d,"
                          " parked at X%.1f"
                          % (lo, hi, passes, lo, short_hi, short_passes, px))

    cmd_WIPE_help = ("Wipe the nozzle on the silicone wiper. Standalone, so the "
                     "travel can be tuned without running a ramp. Long strokes "
                     "then shorter ones at the near end. "
                     "[LO=<x>] [HI=<x>] [PASSES=5] [SHORT_PASSES=4]")

    def cmd_WIPE(self, gcmd):
        return self._guard(gcmd, self._run_WIPE)

    def _run_WIPE(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        if 'xyz' not in (toolhead.get_status(self.reactor.monotonic())
                         .get('homed_axes', '')):
            raise gcmd.error("flow_ramp: home the printer first (G28)")
        lo = gcmd.get_float('LO', None)
        hi = gcmd.get_float('HI', None)
        passes = gcmd.get_int('PASSES', WIPE_PASSES, minval=1, maxval=20)
        short = gcmd.get_int('SHORT_PASSES', WIPE_SHORT_PASSES, minval=0,
                             maxval=20)
        move = gcmd.get_int('MOVE', 1, minval=0, maxval=1)
        if move:
            self.gcode.run_script_from_command("OPTIMIZED_MOVE_TO_TRASH")
            toolhead.wait_moves()
        self._wipe(gcmd, toolhead, lo, hi, passes, short)

    def _finish(self, gcmd, toolhead, retract, wipe, cooldown,
                wipe_lo=None, wipe_hi=None, wipe_passes=None, filt=0):
        wipe_passes = WIPE_PASSES if wipe_passes is None else wipe_passes
        # Runs on every exit path - normal, abort or exception. Each part is
        # guarded separately so a failure in one still lets the others run;
        # cooling down matters more than tidiness.
        try:
            toolhead.wait_moves()
            if retract:
                self.gcode.run_script_from_command(
                    "M83\nG1 E-%.3f F1800" % (retract,))
                toolhead.wait_moves()
                gcmd.respond_info("flow_ramp: retracted %.2f mm" % (retract,))
        except Exception:
            logging.exception("qidi_flow_ramp: retract failed")
            gcmd.respond_info("flow_ramp: WARNING retract failed")

        if wipe:
            try:
                self._wipe(gcmd, toolhead, wipe_lo, wipe_hi, wipe_passes)
            except Exception:
                logging.exception("qidi_flow_ramp: wipe failed")
                gcmd.respond_info("flow_ramp: WARNING wipe failed")

        if cooldown:
            try:
                heater = self.printer.lookup_object('extruder').get_heater()
                self.printer.lookup_object('heaters').set_temperature(heater, 0.)
                gcmd.respond_info("flow_ramp: hotend off")
            except Exception:
                logging.exception("qidi_flow_ramp: cooldown failed")
                gcmd.respond_info("flow_ramp: WARNING could not turn the "
                                  "hotend off - do it manually")
        # Filtration goes off only when the hotend does. COOLDOWN=0 means "I am
        # not finished with the machine" - another stage is about to extrude -
        # and a hot nozzle keeps outgassing whether or not this module is the
        # one using it. Turning the filter off while the next stage runs would
        # be the wrong half of a shutdown.
        if filt and cooldown:
            self._filter(gcmd, False)

    def _hotend_temp(self):
        try:
            h = self.printer.lookup_object('extruder').get_heater()
            return float(h.get_temp(self.reactor.monotonic())[0])
        except Exception:
            return None

    def _run_step(self, gcmd, read, toolhead, idx, q, hz, tare, abort_counts,
                  var_abort, label='', min_t=None, max_t=None, e_left=None):
        # Extrude continuously and hold until the force actually settles, up to
        # max_t. Step length is not predicted - the first full ramp predicted it
        # and got the scaling backwards at high flow.
        min_t = SETTLE_MIN_S if min_t is None else min_t
        max_t = SETTLE_MAX_S if max_t is None else max_t
        v_e = q / A_FIL                       # mm/s of filament
        feed = v_e * 60.0                     # G1 F is mm/min
        gcmd.respond_info("flow_ramp: %s Q=%.2f mm3/s  v_E=%.2f mm/s"
                          % (label or ("step %d" % idx), q, v_e))

        samples, issued, abort = [], 0.0, None
        settled = False
        interval = 1.0 / hz
        t_start_temp = self._hotend_temp()
        temps = []
        start = self.reactor.monotonic()
        eventtime = start
        while abort is None:
            elapsed = eventtime - start
            if elapsed >= max_t:
                break
            # Keep the look-ahead shallow so an abort takes effect quickly.
            try:
                buffered = toolhead.get_last_move_time() -                     toolhead.mcu.estimated_print_time(eventtime)
            except Exception:
                buffered = 0.0
            if buffered < BUFFER_S:
                chunk = v_e * CHUNK_S
                if e_left is not None and issued + chunk > e_left:
                    abort = "extrusion budget exhausted"
                    break
                self.gcode.run_script_from_command(
                    "G1 E%.5f F%.2f" % (chunk, feed))
                issued += chunk
            try:
                raw = read()
            except Exception as e:
                abort = "force read failed: %s" % (e,)
                break
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                rel = float(raw) - tare
                samples.append((eventtime - start, rel))
                # MAGNITUDE, not signed. Melt pressure pushes the hotend DOWN,
                # the opposite sign to a bed press, so a signed test against a
                # positive limit would never fire - and this abort is the only
                # overload protection that exists on this machine.
                if abs(rel) > abort_counts:
                    abort = ("force %.0f gf over the %.0f gf limit"
                             % (abs(rel) / self.counts_per_gf,
                                abort_counts / self.counts_per_gf))
                    break
            if len(samples) % 10 == 0:
                t = self._hotend_temp()
                if t is not None:
                    temps.append(t)
            # Settled? Require TWO consecutive stable windows, not one.
            #
            # One is not enough: after an abort's pressure relief the
            # re-pressurisation has a flat shoulder that a single window reads
            # as equilibrium. That is how 25 mm3/s - which aborted at 1879 gf on
            # the up-leg - came back "settled" at 1062 gf on the down-leg, put
            # itself in the equilibrated set, and produced a working max of 22.5
            # that nothing in the data supported.
            if elapsed >= min_t:
                t_now = samples[-1][0]
                w1 = [(t, v) for t, v in samples
                      if t >= t_now - SETTLE_WINDOW_S]
                w2 = [(t, v) for t, v in samples
                      if t_now - 2 * SETTLE_WINDOW_S <= t < t_now - SETTLE_WINDOW_S]
                lim = SETTLED_SLOPE_GF_S * self.counts_per_gf
                if (len(w1) > 5 and abs(_slope(w1)) <= lim
                        and len(w2) > 5 and abs(_slope(w2)) <= lim):
                    settled = True
                    break
            eventtime = self.reactor.pause(eventtime + interval)

        toolhead.wait_moves()
        held = samples[-1][0] if samples else 0.0
        window = [(t, v) for t, v in samples if t >= held - SETTLE_WINDOW_S]
        if len(window) < 3:
            window = samples
        steady = [v for _t, v in window]
        mean_c, sd_c = _mean(steady), _stdev(steady)
        gf, gf_sd = mean_c / self.counts_per_gf, sd_c / self.counts_per_gf
        slope = _slope(window) / self.counts_per_gf if len(window) > 3 else 0.0
        t_end_temp = self._hotend_temp()
        tmin = min(temps) if temps else None

        gcmd.respond_info(
            "   -> %.1f gf  sd %.2f  slope %+.1f gf/s  held %.1fs  %s%s"
            % (gf, gf_sd, slope, held, "settled" if settled else "NOT SETTLED",
               ("  temp %.1f->%.1f min %.1f" % (t_start_temp, t_end_temp, tmin))
               if (t_start_temp and t_end_temp and tmin) else ""))
        if var_abort and gf_sd > var_abort and abort is None:
            abort = "variance %.2f gf over the %.2f gf limit" % (gf_sd, var_abort)
        return {'step': idx, 'label': label, 'flow': q, 'v_e': v_e,
                'e_mm': issued, 'held_s': held, 'settled': settled,
                'force_gf': gf, 'stdev_gf': gf_sd, 'slope_gf_s': slope,
                'temp_start': t_start_temp, 'temp_end': t_end_temp,
                'temp_min': tmin, 'n_steady': len(steady),
                'n_total': len(samples), 'abort': abort,
                'trace': [[round(t, 4), round(v, 1)] for t, v in samples]}

    def _leg_prep(self, gcmd, read, toolhead, lab, prime, hz, tare,
                  abort_counts, wipe, wipe_lo, wipe_hi, wipe_passes, e_left):
        # Wipe, then prime. Returns filament used.
        #
        # The wipe is not cosmetic. Across the 48-point sweep, material built up
        # on the nozzle and raised the effective restriction so much that the
        # same flow read 17-58% higher by the last leg than the first - only the
        # first leg was usable. Wiping between legs is what makes leg-to-leg
        # comparison mean anything.
        #
        # The prime follows because wiping means stopping extrusion, which
        # depressurises the melt zone; without it the first measurement of each
        # leg would sit on a re-pressurisation transient.
        used = 0.0
        gcmd.respond_info("flow_ramp: --- %s: wipe and prime ---" % (lab,))
        if wipe:
            try:
                self.gcode.run_script_from_command("M83\nG1 E-1.000 F1800")
                toolhead.wait_moves()
                self._wipe(gcmd, toolhead, wipe_lo, wipe_hi, wipe_passes)
            except Exception:
                logging.exception("qidi_flow_ramp: inter-leg wipe failed")
                gcmd.respond_info("flow_ramp: WARNING inter-leg wipe failed")
        if prime:
            pr = self._run_step(gcmd, read, toolhead, 0, PRIME_FLOW, hz, tare,
                                abort_counts, 0., label="%s prime" % (lab,),
                                min_t=PRIME_S, max_t=PRIME_S + 8.0,
                                e_left=e_left)
            used += pr['e_mm']
            if pr.get('abort'):
                raise gcmd.error("flow_ramp: aborted priming %s - %s"
                                 % (lab, pr['abort']))
        return used

    def _handle_abort(self, gcmd, toolhead, r, q, wipe=0, wipe_lo=None,
                      wipe_hi=None, wipe_passes=None):
        # Returns True to stop the whole run, False to skip this flow and go on.
        #
        # A force or variance abort at one flow is a DATA POINT: it says the melt
        # could not keep up there. Every remaining flow in the sweep is lower, so
        # lower force, and safe to continue. Ending the run instead throws away
        # the measurements that would locate the limit - which is exactly what
        # happened on the 48-point sweep, where one unreachable flow cost the
        # other 36 measurements.
        reason = r.get('abort') or ''
        recoverable = ('force ' in reason) or ('variance ' in reason)
        if not recoverable:
            gcmd.respond_info("flow_ramp: ABORT - %s" % (reason,))
            return True
        gcmd.respond_info("flow_ramp: %.2f mm3/s did not equilibrate (%s) - "
                          "skipping this flow and continuing" % (q, reason))
        try:
            # Relieve melt pressure before the next flow so it does not start
            # loaded, and give it a moment to decay.
            self.gcode.run_script_from_command("M83\nG1 E-1.000 F1800")
            toolhead.wait_moves()
            self.reactor.pause(self.reactor.monotonic() + 2.0)
            # An aborted flow is where the worst build-up forms - it is the one
            # that could not clear the material it was fed. Wipe before moving
            # on, or the rest of the leg inherits the blob.
            if wipe:
                self._wipe(gcmd, toolhead, wipe_lo, wipe_hi,
                           wipe_passes or WIPE_PASSES)
        except Exception:
            logging.exception("qidi_flow_ramp: pressure relief failed")
        return False

    def _refine(self, gcmd, read, toolhead, results, refine, reps, hz, tare,
                abort_counts, var_abort, max_e, used_mm, ramp_t0,
                prime=1, wipe=1, wipe_lo=None, wipe_hi=None, wipe_passes=None,
                updown=1):
        # Phase 2. The coarse sweep brackets the limit between the highest flow
        # that reached equilibrium and the lowest that did not. Subdivide that
        # bracket to find where the transition actually sits, then the working
        # max is that x WORKING_MARGIN.
        ok_q, bad_q = _classify_flows(results)
        if not ok_q:
            gcmd.respond_info("flow_ramp: nothing equilibrated - no bracket to "
                              "refine")
            return used_mm
        lo = max(ok_q)
        hi = min([q for q in bad_q if q > lo], default=None)
        if hi is None:
            gcmd.respond_info(
                "flow_ramp: every flow tested equilibrated (highest %.2f mm3/s)"
                " - the limit is ABOVE the tested range, so raise QMAX to find "
                "it" % (lo,))
            return used_mm

        ratio = (hi / lo) ** (1.0 / (refine + 1))
        pts = [lo * ratio ** k for k in range(1, refine + 1)]
        gcmd.respond_info(
            "flow_ramp: --- refining between %.2f (equilibrated) and %.2f "
            "(did not) ---" % (lo, hi))
        gcmd.respond_info("flow_ramp: %d points x%d: %s"
                          % (len(pts), reps,
                             ", ".join("%.2f" % q for q in pts)))
        # Same leg structure as the coarse sweep - ascending then descending,
        # each wiped and primed - so the refinement points are measured under
        # the same conditions as the points that bracketed them. Without that,
        # the refinement would sit on a nozzle that had been extruding since the
        # last wipe and would read high.
        rlegs = [('refup%d' % (rep + 1,), list(enumerate(pts)))
                 for rep in range(reps)]
        if updown:
            rlegs = []
            for rep in range(reps):
                rlegs.append(('refup%d' % (rep + 1,), list(enumerate(pts))))
                rlegs.append(('refdn%d' % (rep + 1,),
                              list(reversed(list(enumerate(pts))))))
        for lab, items in rlegs:
            try:
                used_mm += self._leg_prep(gcmd, read, toolhead, lab, prime, hz,
                                          tare, abort_counts, wipe, wipe_lo,
                                          wipe_hi, wipe_passes,
                                          max_e - used_mm)
            except Exception as e:
                gcmd.respond_info("flow_ramp: %s prep failed - %s" % (lab, e))
                return used_mm
            for k, q in items:
                t_at = self.reactor.monotonic() - ramp_t0
                r = self._run_step(gcmd, read, toolhead, 100 + k, q, hz, tare,
                                   abort_counts, var_abort,
                                   label="%s pt%d" % (lab, k + 1),
                                   e_left=max_e - used_mm)
                used_mm += r['e_mm']
                r['t_mid'] = t_at + r['held_s'] * 0.9
                r['leg'] = lab
                results.append(r)
                if r.get('abort'):
                    if self._handle_abort(gcmd, toolhead, r, q, wipe, wipe_lo,
                                          wipe_hi, wipe_passes):
                        return used_mm
        return used_mm

    cmd_SEARCH_help = ("Find max volumetric flow by adaptive search: coarse "
                       "stride, bisect, then +1 mm3/s. Finds its own range - no "
                       "QMIN/QMAX needed. [TEMP=275] [START=5] [STEP=10] "
                       "[MAXQ=60] [DRY=0]")

    def cmd_SEARCH(self, gcmd):
        return self._guard(gcmd, self._run_SEARCH)

    def _run_SEARCH(self, gcmd):
        read = self._sensor(gcmd)
        temp = gcmd.get_float('TEMP', 275., minval=170., maxval=300.)
        start = gcmd.get_float('START', SEARCH_START, above=0.)
        step = gcmd.get_float('STEP', SEARCH_COARSE_STEP, above=0.)
        fine = gcmd.get_float('FINE', SEARCH_FINE_STEP, above=0.)
        maxq = gcmd.get_float('MAXQ', SEARCH_MAX_Q, above=1.)
        hz = gcmd.get_float('HZ', 30., minval=5., maxval=77.)
        var_abort = gcmd.get_float('VARIANCE_ABORT', DEFAULT_VARIANCE_GF,
                                   minval=0.)
        abort_counts = gcmd.get_int('ABORT_COUNTS', DEFAULT_ABORT_COUNTS,
                                    minval=1000)
        max_e = gcmd.get_float('MAX_E', DEFAULT_MAX_E_MM, above=0.)
        retract = gcmd.get_float('RETRACT', 2.0, minval=0., maxval=10.)
        wipe = gcmd.get_int('WIPE', 1, minval=0, maxval=1)
        cooldown = gcmd.get_int('COOLDOWN', 1, minval=0, maxval=1)
        soak_max = gcmd.get_float('SOAK_MAX', DEFAULT_SOAK_MAX_S, minval=30.,
                                  maxval=1800.)
        wipe_lo = gcmd.get_float('WIPE_LO', None)
        wipe_hi = gcmd.get_float('WIPE_HI', None)
        wipe_passes = gcmd.get_int('WIPE_PASSES', WIPE_PASSES, minval=1,
                                   maxval=20)
        filt = gcmd.get_int('FILTER', 1, minval=0, maxval=1)
        dry = gcmd.get_int('DRY', 0, minval=0, maxval=1)

        coarse = []
        q = start
        while q <= maxq:
            coarse.append(q)
            q += step
        gcmd.respond_info(
            "flow_ramp: adaptive search - coarse %s ... (stride %.0f, cap "
            "%.0f), then bisect, then +%.0f"
            % (", ".join("%.0f" % c for c in coarse[:5]), step, maxq, fine))
        gcmd.respond_info("flow_ramp: each flow held up to %.0f-%.0fs "
                          "(cap scales with fill time), %d failures confirm"
                          % (self._search_cap(coarse[-1]),
                             self._search_cap(coarse[0]), SEARCH_CONFIRM_FAILS))
        if dry:
            gcmd.respond_info("flow_ramp: DRY=1, nothing executed")
            return

        toolhead = self.printer.lookup_object('toolhead')
        if 'xyz' not in (toolhead.get_status(self.reactor.monotonic())
                         .get('homed_axes', '')):
            raise gcmd.error("flow_ramp: home the printer first (G28)")

        results = []
        state = {'used_mm': 0.0, 'since_wipe': 0, 'wipe_now': False,
                 'fails': {}, 'passes': {}, 'why': {}, 'stage': 'coarse',
                 'wipe_lo': wipe_lo, 'wipe_hi': wipe_hi,
                 'wipe_passes': wipe_passes}
        tare = tare_end = None
        limit = None
        marginal = False
        ramp_t0 = None
        try:
            if filt:
                self._filter(gcmd, True)
            gcmd.respond_info("flow_ramp: moving to the purge chute")
            self.gcode.run_script_from_command("OPTIMIZED_MOVE_TO_TRASH")
            toolhead.wait_moves()
            self._wait_temp_stable(gcmd, temp, tol=1.0, window=5.0, timeout=420.)
            tare = _mean(self._sample(read, 60, 1.0 / hz, gcmd))
            tare, drift_rate = self._settle_drift(gcmd, read, hz, tare, soak_max)
            self.gcode.run_script_from_command("M83")
            ramp_t0 = self.reactor.monotonic()

            # ---- stage 1: coarse ----------------------------------------
            gcmd.respond_info("flow_ramp: --- coarse ---")
            last_pass, first_fail = 0.0, None   # 0 is an automatic pass
            for c in coarse:
                ok = self._test_flow(gcmd, read, toolhead, c, hz, tare,
                                     abort_counts, var_abort, results, state,
                                     ramp_t0, max_e)
                if ok:
                    last_pass = c
                    continue
                # Confirm before believing it.
                ok2 = self._test_flow(gcmd, read, toolhead, c, hz, tare,
                                      abort_counts, var_abort, results, state,
                                      ramp_t0, max_e)
                if ok2:
                    last_pass = c      # first failure was a fluke
                    continue
                first_fail = c
                break
            if first_fail is None:
                gcmd.respond_info(
                    "flow_ramp: nothing failed up to %.0f mm3/s - the limit is "
                    "ABOVE the tested range. For a 0.6 nozzle that means the "
                    "measurement is wrong, not the filament." % (maxq,))
                limit = None
            elif last_pass <= 0.0:
                gcmd.respond_info(
                    "flow_ramp: %.0f mm3/s already fails - the limit is BELOW "
                    "the coarse floor. Re-run with a lower START."
                    % (coarse[0],))
                limit = None
            else:
                # ---- stage 2: bisect -----------------------------------
                state['stage'] = 'bisect'
                mid = round((last_pass + first_fail) / 2.0, 1)
                gcmd.respond_info("flow_ramp: --- bisect %.1f (between %.0f "
                                  "pass and %.0f fail) ---"
                                  % (mid, last_pass, first_fail))
                m1 = self._test_flow(gcmd, read, toolhead, mid, hz, tare,
                                     abort_counts, var_abort, results, state,
                                     ramp_t0, max_e)
                m2 = self._test_flow(gcmd, read, toolhead, mid, hz, tare,
                                     abort_counts, var_abort, results, state,
                                     ramp_t0, max_e)
                base = mid if (m1 and m2) else last_pass
                if not (m1 and m2):
                    gcmd.respond_info("flow_ramp: bisect failed - stepping up "
                                      "from %.0f instead" % (base,))
                    self._test_flow(gcmd, read, toolhead, base, hz, tare,
                                    abort_counts, var_abort, results, state,
                                    ramp_t0, max_e)

                # ---- stage 3: fine -------------------------------------
                state['stage'] = 'fine'
                gcmd.respond_info("flow_ramp: --- fine, +%.0f from %.1f ---"
                                  % (fine, base))
                cur, last_good = base, base
                while cur + fine < first_fail + 1e-9:
                    cur = round(cur + fine, 3)
                    ok = self._test_flow(gcmd, read, toolhead, cur, hz, tare,
                                         abort_counts, var_abort, results,
                                         state, ramp_t0, max_e)
                    if ok:
                        last_good = cur
                        continue
                    hit = self._confirmed_fail(state, cur, fine)
                    if hit is None:
                        ok2 = self._test_flow(gcmd, read, toolhead, cur, hz,
                                              tare, abort_counts, var_abort,
                                              results, state, ramp_t0, max_e)
                        if ok2:
                            last_good = cur
                            marginal = True
                            continue
                        hit = self._confirmed_fail(state, cur, fine)
                    if hit is not None:
                        limit = round(hit - fine, 3)
                        break
                if limit is None:
                    limit = last_good
        finally:
            self._finish(gcmd, toolhead, retract, wipe, cooldown, wipe_lo,
                         wipe_hi, wipe_passes, filt)
        try:
            self.reactor.pause(self.reactor.monotonic() + 10.)
            tare_end = _mean(self._sample(read, 30, 1.0 / hz, gcmd))
        except Exception:
            pass

        wmax = _working_max(limit) if limit else None
        gcmd.respond_info("flow_ramp: ===================================")
        if limit:
            gcmd.respond_info("flow_ramp: highest flow that equilibrated: "
                              "%.1f mm3/s" % (limit,))
            gcmd.respond_info("flow_ramp: WORKING MAX = %.1f mm3/s  (%.1f x "
                              "%.2f = %.2f, rounded down to %.1f)"
                              % (wmax, limit, WORKING_MARGIN,
                                 limit * WORKING_MARGIN, WORKING_ROUND))
        else:
            gcmd.respond_info("flow_ramp: no limit determined - see above")
        if marginal:
            gcmd.respond_info("flow_ramp: NOTE the transition was marginal - a "
                              "flow failed then passed. Taking the lower, "
                              "conservative value.")
        fails = ", ".join("%.1f(%dx,%s)" % (q, n, state['why'].get(q, '?'))
                          for q, n in sorted(state['fails'].items()))
        gcmd.respond_info("flow_ramp: failures: %s" % (fails or "none",))
        gcmd.respond_info("flow_ramp: %d measurements, %.0f mm filament "
                          "(%.2f g)" % (len(results), state['used_mm'],
                                        state['used_mm'] * A_FIL * 1.09e-3))
        gcmd.respond_info("flow_ramp: ===================================")
        ok = self._record({'mode': 'search', 'temp': temp, 'tare': tare,
                           'tare_end': tare_end, 'limit': limit,
                           'working_max': _working_max(limit),
                           'limit_raw': limit * WORKING_MARGIN if limit
                           else None, 'marginal': marginal,
                           'fails': {str(k): v for k, v
                                     in state['fails'].items()},
                           'why': {str(k): v for k, v in state['why'].items()},
                           'used_mm': state['used_mm'], 'steps': results})
        gcmd.respond_info("flow_ramp: written to %s"
                          % (self.report_path if ok else "(not written)",))

    # ------------------------------------------------- adaptive search ----

    def _search_cap(self, q):
        return SEARCH_BASE_CAP_S + SEARCH_FILL_MM3 / max(q, 0.1)

    def _search_min(self, q):
        # The MINIMUM hold has to clear the fill as well, not just the cap.
        #
        # With a flat 3.5 s minimum, 20 mm3/s settled at 3.6 s and read 790 gf
        # where its repeat held 8.6 s and read 1116 - a 41% error, because the
        # fill at 20 mm3/s takes 5.0 s and the two-window stability test can
        # pass on a momentarily flat stretch of the rising curve.
        #
        # A premature settle is a FALSE PASS, which near the limit would report
        # a max flow the filament cannot sustain. That is the dangerous
        # direction, so the minimum scales with fill exactly as the cap does.
        return SETTLE_MIN_S + SEARCH_FILL_MM3 / max(q, 0.1)

    def _test_flow(self, gcmd, read, toolhead, q, hz, tare, abort_counts,
                   var_abort, results, state, ramp_t0, max_e):
        # One pass/fail trial at flow q. Primes at the test flow (the fill is
        # the start of the measurement), wipes per policy, records the result.
        if state['wipe_now']:
            try:
                self.gcode.run_script_from_command("M83\nG1 E-1.000 F1800")
                toolhead.wait_moves()
                self._wipe(gcmd, toolhead, state['wipe_lo'], state['wipe_hi'],
                           state['wipe_passes'])
            except Exception:
                logging.exception("qidi_flow_ramp: search wipe failed")
            state['wipe_now'] = False
            state['since_wipe'] = 0.0      # mm3 extruded since the last wipe

        t_at = self.reactor.monotonic() - ramp_t0
        cap = self._search_cap(q)
        r = self._run_step(gcmd, read, toolhead, 0, q, hz, tare, abort_counts,
                           var_abort, label="%s %.1f" % (state['stage'], q),
                           min_t=self._search_min(q), max_t=cap,
                           e_left=max_e - state['used_mm'])
        state['used_mm'] += r['e_mm']
        r['t_mid'] = t_at + r['held_s'] * 0.9
        r['leg'] = state['stage']
        results.append(r)

        ok = (r.get('abort') is None) and r['settled']
        # Volume since the last wipe, not a count of measurements - build-up
        # tracks material through the nozzle, and one measurement is 37 mm3 at
        # 5 mm3/s against 416 at 21.
        state['since_wipe'] += r['e_mm'] * A_FIL
        if ok:
            if state['since_wipe'] >= SEARCH_WIPE_VOLUME_MM3:
                state['wipe_now'] = True
        else:
            # A failing flow could not clear what it was fed, so it is where
            # build-up concentrates. Always wipe before the next trial.
            state['wipe_now'] = True
            state['fails'][q] = state['fails'].get(q, 0) + 1
            why = 'force' if 'force ' in (r.get('abort') or '') else (
                'variance' if 'variance ' in (r.get('abort') or '')
                else 'no equilibrium')
            state['why'][q] = why
            try:
                self.gcode.run_script_from_command("M83\nG1 E-1.000 F1800")
                toolhead.wait_moves()
                self.reactor.pause(self.reactor.monotonic() + 2.0)
            except Exception:
                logging.exception("qidi_flow_ramp: relief failed")
        state['passes'].setdefault(q, 0)
        if ok:
            state['passes'][q] += 1
        gcmd.respond_info("flow_ramp:   %.1f mm3/s -> %s%s"
                          % (q, "PASS" if ok else "FAIL",
                             "" if ok else " (%s)" % state['why'].get(q, '')))
        return ok

    def _confirmed_fail(self, state, q, step):
        # A flow is the limit if it failed twice, OR if it failed once and the
        # next step up also failed once - two failures across adjacent steps.
        # Taking the LOWER of the pair is conservative, which is the right way
        # to be wrong about a max flow.
        f = state['fails']
        if f.get(q, 0) >= SEARCH_CONFIRM_FAILS:
            return q
        for a in sorted(f):
            b = round(a + step, 3)
            if f.get(a, 0) >= 1 and f.get(b, 0) >= 1:
                return a
        return None

    def _report(self, gcmd, results, flows, tare, tare_end, aborted, reason,
                temp, step_time, hz, abort_counts, drift_rate=None,
                ramp_len=None):
        # Drift correction. The opening and closing tares bracket the run, so
        # assume the baseline moved linearly between them and subtract each
        # step's share. This is what makes the drift gate a sanity check rather
        # than a hard requirement.
        if tare is not None and tare_end is not None and ramp_len:
            total_gf = (tare_end - tare) / self.counts_per_gf
            gcmd.respond_info("flow_ramp: correcting for %.1f gf of baseline "
                              "drift across %.0fs" % (total_gf, ramp_len))
            for r in results:
                t_mid = r.get('t_mid')
                if t_mid is None:
                    continue
                corr = total_gf * (t_mid / ramp_len)
                r['force_gf_raw'] = r['force_gf']
                r['force_gf'] = r['force_gf'] - corr
                r['drift_correction_gf'] = -corr
        # Fit on MAGNITUDE - melt pressure reads negative because it pushes the
        # hotend down, the opposite sign to a bed press. And only on steps that
        # actually reached steady state: a still-climbing step is not a
        # steady-state point and would drag the exponent down.
        good = [r for r in results if r.get('abort') is None
                and r.get('settled', True) and abs(r['force_gf']) > 0]
        skipped = [r['step'] for r in results
                   if r.get('abort') is None and not r.get('settled', True)]
        if skipped:
            gcmd.respond_info("flow_ramp: excluded steps %s from the fit - not "
                              "settled" % (", ".join(str(s) for s in skipped),))
        fit = _loglog_fit([r['flow'] for r in good],
                          [abs(r['force_gf']) for r in good])

        # Aggregate duplicates per flow. This is what the down-leg buys: the
        # spread between repeats at one flow IS the step-to-step reproducibility,
        # which was the limiting uncertainty on the first full ramp (local
        # exponent scattering +-0.08 against a +-0.013 formal error).
        byq = {}
        for r in results:
            if r.get('abort') is not None:
                continue
            byq.setdefault(round(r['flow'], 3), []).append(r)
        agg = []
        for q in sorted(byq):
            rs = byq[q]
            fs = [abs(r['force_gf']) for r in rs]
            ups = [abs(r['force_gf']) for r in rs if str(r.get('leg', '')).startswith('up')]
            dns = [abs(r['force_gf']) for r in rs if str(r.get('leg', '')).startswith('down')]
            agg.append({'flow': q, 'n': len(fs), 'mean_gf': _mean(fs),
                        'spread_gf': (max(fs) - min(fs)) if len(fs) > 1 else 0.0,
                        'up_gf': _mean(ups) if ups else None,
                        'down_gf': _mean(dns) if dns else None,
                        'all_settled': all(r['settled'] for r in rs),
                        'temp_min': min([r['temp_min'] for r in rs
                                         if r.get('temp_min') is not None] or [None])
                        if any(r.get('temp_min') is not None for r in rs) else None})
        payload_agg = agg
        if agg:
            gcmd.respond_info("flow_ramp: per-flow summary")
            gcmd.respond_info("     Q     force gf   spread   up      down    hyst   Tmin")
            for a in agg:
                up = a['up_gf']; dn = a['down_gf']
                hy = (dn - up) if (up and dn) else None
                gcmd.respond_info(
                    "  %6.2f  %8.1f  %7.1f  %7s %7s %6s %6s%s"
                    % (a['flow'], a['mean_gf'], a['spread_gf'],
                       ("%.1f" % up) if up else "-",
                       ("%.1f" % dn) if dn else "-",
                       ("%+.1f" % hy) if hy is not None else "-",
                       ("%.1f" % a['temp_min']) if a['temp_min'] else "-",
                       "" if a['all_settled'] else "  NOT SETTLED"))

        # Local exponent n = dlnF/dlnQ between adjacent flows. The knee shows as
        # n rising off its low-flow plateau; n = 1 is where shear thinning stops
        # helping at all. Neither needs a baseline fit or a threshold.
        if len(agg) > 1:
            gcmd.respond_info("flow_ramp: local exponent n = dlnF/dlnQ")
            locs = []
            for x, y in zip(agg, agg[1:]):
                if x['mean_gf'] <= 0 or y['mean_gf'] <= 0:
                    continue
                nloc = (math.log(y['mean_gf'] / x['mean_gf'])
                        / math.log(y['flow'] / x['flow']))
                qgm = (x['flow'] * y['flow']) ** 0.5
                locs.append({'q': qgm, 'n': nloc,
                             'from': x['flow'], 'to': y['flow']})
                gcmd.respond_info("  %6.2f->%6.2f  (gm %6.2f)   n = %.3f"
                                  % (x['flow'], y['flow'], qgm, nloc))
            payload_locs = locs
            cross = None
            for u, v in zip(locs, locs[1:]):
                if u['n'] <= 1.0 < v['n']:
                    f = (1.0 - u['n']) / (v['n'] - u['n'])
                    cross = math.exp(math.log(u['q'])
                                     + f * math.log(v['q'] / u['q']))
                    break
            if cross:
                gcmd.respond_info("flow_ramp: n crosses 1.0 at %.2f mm3/s"
                                  % (cross,))
            base = [l['n'] for l in locs[:max(2, len(locs) // 3)]]
            if base:
                nb = _mean(base)
                gcmd.respond_info("flow_ramp: low-flow plateau n = %.3f" % (nb,))
                c2 = None
                for u, v in zip(locs, locs[1:]):
                    if u['n'] <= 2 * nb < v['n']:
                        f = (2 * nb - u['n']) / (v['n'] - u['n'])
                        c2 = math.exp(math.log(u['q'])
                                      + f * math.log(v['q'] / u['q']))
                        break
                if c2:
                    gcmd.respond_info("flow_ramp: n reaches 2x plateau (%.3f) "
                                      "at %.2f mm3/s" % (2 * nb, c2))
        else:
            payload_locs = []

        payload = {'temp': temp, 'step_time': step_time, 'hz': hz,
                   'counts_per_gf': self.counts_per_gf,
                   'abort_counts': abort_counts, 'tare': tare,
                   'tare_end': tare_end, 'drift_rate_gf_min': drift_rate,
                   'ramp_len_s': ramp_len,
                   'aborted': aborted, 'reason': reason,
                   'planned_flows': flows, 'steps': results, 'fit': fit,
                   'per_flow': agg,
                   'local_n': payload_locs}
        if fit:
            gcmd.respond_info("flow_ramp: power law F = %.4g * Q^%.3f "
                              "(rms resid %.4f in ln F, %d points)"
                              % (fit['a'], fit['n'], fit['resid_rms'],
                                 fit['points']))
            for qp in (20.0, 25.0):
                fp = fit['a'] * qp ** fit['n']
                gcmd.respond_info("   extrapolated: %.0f mm3/s -> %.0f gf  "
                                  "(%.0f%% of the %.0f gf abort)"
                                  % (qp, fp,
                                     100. * fp * self.counts_per_gf / abort_counts,
                                     abort_counts / self.counts_per_gf))
            payload['extrapolated'] = {str(qp): fit['a'] * qp ** fit['n']
                                       for qp in (15., 20., 25.)}
        else:
            gcmd.respond_info("flow_ramp: not enough clean steps to fit")
        # ---- the headline: highest flow that reached equilibrium ----------
        #
        # This is the criterion that survived contact with the reference
        # standard. A flow that reaches steady melt pressure is one the melt can
        # sustain indefinitely; one whose pressure climbs without bound is not.
        # No baseline fit, no departure threshold, no arbitrary percentage - the
        # three things that made the earlier "knee" wobble between 9.3 and
        # 11.6 mm3/s.
        eq, noeq = _classify_flows(results)
        limit = max(eq) if eq else None
        first_bad = min([q for q in noeq if limit and q > limit], default=None)
        payload['equilibrium_limit'] = limit
        payload['first_non_equilibrium'] = first_bad
        if limit:
            wmax = limit * WORKING_MARGIN
            payload['working_max'] = wmax
            gcmd.respond_info("flow_ramp: ===================================")
            gcmd.respond_info("flow_ramp: highest flow that equilibrated: "
                              "%.2f mm3/s" % (limit,))
            if first_bad:
                gcmd.respond_info("flow_ramp: lowest that did not:          "
                                  "%.2f mm3/s" % (first_bad,))
                gcmd.respond_info("flow_ramp: limit is bracketed to %.2f-%.2f "
                                  "(%.1f%% wide)"
                                  % (limit, first_bad,
                                     100.0 * (first_bad / limit - 1.0)))
            else:
                gcmd.respond_info("flow_ramp: nothing failed - the limit is "
                                  "ABOVE the tested range, raise QMAX")
            gcmd.respond_info("flow_ramp: WORKING MAX = %.2f mm3/s  "
                              "(%.2f x %.2f)" % (wmax, limit, WORKING_MARGIN))
            gcmd.respond_info("flow_ramp: ===================================")
        else:
            gcmd.respond_info("flow_ramp: no flow reached equilibrium - no "
                              "working max can be reported")

        ok = self._record(payload)
        gcmd.respond_info("flow_ramp: written to %s"
                          % (self.report_path if ok else "(not written)",))


def load_config(config):
    return QidiFlowRamp(config)
