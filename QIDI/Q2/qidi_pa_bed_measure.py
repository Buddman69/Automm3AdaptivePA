# qidi_pa_bed_measure.py - Stage 3, run over the BED instead of the chute
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
# ALSO: the second _motion_limits() capture after the move to bed centre -
# present in the Max4 source this was copied from - is removed here. It is
# harmless there and here (_move_to_bed_centre issues a plain G1, no macro,
# no M204 side effect to pick up), but Q2's own qidi_pa_measure.py already
# fixed the SAME pattern where it IS dangerous (MOVE_TO_TRASH sets its own
# M204 and never restores it), and carrying two different rules for when to
# re-capture invites exactly the mistake that fix was for. One rule: capture
# once, before anything moves.
#
#
# A COPY of qidi_pa_measure.py, kept as a separate file on purpose - see
# CHANGELOG/ and qidi_auto_cal_bed.py's header for why. The tau-fitting maths
# below is UNCHANGED. What changed is WHERE the square waves run: at bed
# centre instead of the chute, needing QIDI_BED_PREPARE run first (the
# orchestrator does this automatically). Heating (M109) does not care about
# position, so unlike Stage 1 this moves straight to bed centre at its own
# start rather than visiting the chute first - see _run().
#
# The wiper is still only reachable at the chute, so every wipe here - the
# per-block round-trip wipes and the final cleanup wipe - now means
# travelling there and back. See _wipe_at_chute() in qidi_flow_bed_search.py
# for the fuller explanation; the same pattern is duplicated here rather than
# imported, consistent with everything else in this project.
#
# This implements the CORRECTED design - read the next
# section before changing anything.
#
# WHY THE ORIGINAL DESIGN COULD NOT WORK
#   The original design specified an `area` primitive: apply a candidate K,
#   measure the integrated residual against an ideal force step, and search for
#   the K where it vanishes. That requires Klipper to actually apply K. It does not.
#   From this machine's own klippy/kinematics/extruder.py:
#
#       can_pressure_advance = False
#       if axis_r > 0. and (move.axes_d[0] or move.axes_d[1]):
#           can_pressure_advance = True
#
#   Pressure advance is applied ONLY when a move carries X or Y motion. Stage 3
#   extrudes in air over the purge chute with no XY motion, so every candidate K
#   would have produced identical motion, area(K) would have been flat, and the
#   search would have returned a number that meant nothing.
#
# WHAT IS MEASURED INSTEAD - and why it is the same quantity
#   Melt pressure obeys a first-order lag on commanded flow. With melt
#   compliance C and flow resistance R:
#
#       C dP/dt = Q_in - Q_out,  Q_out = P/R   ->   RC dP/dt + P = R Q_in
#
#   so tau = RC. The volume stored at steady flow Q is C*P = C*R*Q = tau*Q, and
#   pressure advance exists precisely to push that stored volume in ahead of
#   time: it advances the extruder by K*v_e. Therefore
#
#       K = tau
#
#   and since force is pressure times filament area, the FORCE RESPONSE TO A
#   VELOCITY STEP IS A FIRST-ORDER LAG WHOSE TIME CONSTANT IS THE PA VALUE.
#   Command the step, fit tau, done. No K search, no reliance on Klipper's PA,
#   and one square wave per point instead of six to eight evaluations.
#
# HOW tau IS EXTRACTED - linear, not iterative
#   Integrating dF/dt = -(F - F_inf)/tau from 0 to t gives
#
#       F(t) = F(0) - (1/tau) * INT[0..t] F ds + (F_inf/tau) * t
#
#   which is LINEAR in the unknowns. Regress F on [1, cumulative integral of F,
#   t, t^2] and tau = -1/(coefficient on the integral). The t^2 term absorbs the
#   slow melt-equilibration drift, which is near-linear inside one 200 ms leg.
#
#   No scipy, no iteration, no initial guess to get wrong - and the integration
#   is exactly the noise smoothing the original design wanted in arguing for an
#   integral over a fit. This is that argument, carried through to tau directly.
#
# THE TWO TIME CONSTANTS
#       PA lag                24 ms      what we want
#       melt equilibration    1-15 s     absorbed by the t^2 drift term
#   A factor of ~100 apart, which is what makes the separation clean. The square
#   wave must stay fast enough that the slow process cannot move appreciably
#   within a leg.
#
# SAFETY
#   Heats and extrudes, so SAFETY.md rule 5 applies: watched, over the
#   purge chute, with you at the machine. Same envelope as Stage 1 -
#   force abort on MAGNITUDE at 1650 gf above a fresh tare (melt pressure is
#   negative; a signed comparison here is the bug that made Stage 1's first
#   ceiling unfirable), variance abort, volume-based wiping, and retract/wipe on
#   every exit path including exceptions.
#
#   It does NOT switch the air filter or the hotend off - an earlier version of
#   this comment claimed it did. Whoever runs the chain owns the shutdown, so
#   that a stage finishing does not cool the nozzle out from under the next one.
#
#   An unhandled exception in a gcode command is a Klipper INTERNAL ERROR and
#   shuts down every MCU. Everything is wrapped so that cannot happen, and the
#   guard sits OUTSIDE the cleanup so cleanup still runs.
#
# USAGE
#   [qidi_pa_bed_measure]
#
#   QIDI_PA_BED_MEASURE TEMP=275         needs QIDI_BED_PREPARE run first
#   QIDI_PA_BED_MEASURE DRY=1            print the plan, no heat, no motion
#   QIDI_PA_BED_MEASURE POINT=centre     one point only
#   QIDI_PA_BED_MEASURE FLOW=9.75 ACCEL=5000   an explicit point
#
#   Results go to ~/printer_data/qidi_pa/pa_table.json - the SAME file the
#   chute version writes. Deliberate: QIDI_PA_TABLE is reused unmodified and
#   reads a fixed path - see qidi_auto_cal_bed.py's header for why, and the
#   residual risk that accepts.

import json
import logging
import math
import os
import time

# Measured on one Q2 over 15 points, 8.5 gf to 2063 gf. The X-Max 4 cell
# measured 182.96 - an 11% difference between two machines of the same
# family, which is why this can never be a shared constant.
#
# 201 rather than the 205 a full-range fit gives, deliberately: the slope
# drifts from ~208 mid-range to ~200 at the top, and the abort lives at the
# top. Low makes the abort fire EARLY, which is the safe direction.
COUNTS_PER_GF = 201.0
ABORT_GF = 1650.0
VARIANCE_ABORT_GF = 35.0

A_FIL_MM2 = 2.4053

# Square wave. dv = 0.25 * v_E halves the worst corner's ramp/K from 1.27 to
# 0.64, which is what makes hi-flow/lo-accel measurable. At
# 0.99 gf noise the smallest resulting force step is still ~70 gf, so SNR was
# never the binding constraint - ramp time against the lag was.
AMPLITUDE_FRAC = 0.25
DEFAULT_LEG_MS = 200.0
DEFAULT_CYCLES = 5
DEFAULT_BLOCKS = 3

# toolhead.py: BUFFER_TIME_HIGH = 3.0 on this machine. A block is queued in one
# go and only then sampled, so if the block is longer than the buffer, Klipper
# blocks the gcode thread mid-queue, motion starts before sampling does, and the
# first transitions are simply missed. 5 cycles x 2 legs x 200 ms = 2.0 s fits.
# Not a correctness problem - the windowing skips transitions with no samples -
# but it wastes filament, so warn rather than let it happen quietly.
BUFFER_TIME_HIGH_S = 3.0
BLOCK_SAFE_FRAC = 0.7

DEFAULT_PRIME_MM3 = 100.0
# Volume extruded at one point before the nozzle is wiped again. Stage 1
# established this threshold over several hot runs; below it no blob forms,
# above it one does and everything measured afterwards is corrupted.
WIPE_VOLUME_MM3 = 200.0
REPRIME_MM3 = 50.0      # re-pressurise the melt after an inter-block wipe
# QIDI's own CLEAR_NOZZLE_PLR oscillates between X95 and X115, i.e.
# park_x + 10 to park_x + 30. The X-Max 4 build uses 0..45 from a park of
# 135; the same span here would reach X130 on a 275 mm bed.
WIPE_LO_OFFSET = 10.0
WIPE_HI_OFFSET = 30.0
# 6 passes, raised from 5 on 2026-09-15 after waste was left on the nozzle.
# Costs ~0.5 s per wipe at the proven feedrate - cheap insurance against the
# failure mode that corrupts everything measured after it.
WIPE_PASSES = 6

# Then four SHORTER strokes, added 2026-09-21 because the chute was not clearing
# on some runs. The long passes carry material out along the full travel; these
# work the near end, where what the long strokes drag back was being left.
# The short travel is a fraction of the full stroke rather than a second fixed
# offset, so it stays proportional if the travel is ever retuned.
#
# These values are duplicated in qidi_flow_ramp.py, which has its own copy of
# the wipe. Change both or the two stages wipe differently. They were 5 there
# and 6 here until 2026-09-21, for no reason anyone could reconstruct; both are
# 6 now, so the two stages wipe identically.
WIPE_SHORT_PASSES = 4
WIPE_SHORT_FRAC = 0.7

# Tolerance for the Z-matches-what-was-recorded check in
# _move_to_bed_centre() - motion settling and rounding, not a real margin.
BED_Z_TOLERANCE_MM = 1.0
BED_TRAVEL_FEED = 6000
# Doubled to 20000/12000 on 2026-09-15 and REVERTED the same day: at 333 mm/s
# the wiper left waste on the nozzle. The whole saving was ~1.4 s per wipe,
# about 14 s across a five-point run, against a cleaning failure that corrupts
# every measurement after it. These are the values Stage 1 proved over several
# hot runs - do not raise them without a wipe-quality test to justify it.
WIPE_FEED_FAST = 10000
WIPE_FEED_SLOW = 6000
FILTER_M106_P = 3
FILTER_SPEED = 254

MIN_EXTRUDE_TEMP = 200.0
FIT_WINDOW_TAU = 5.0        # fit out to 5 tau past a transition
ASSUMED_K_S = 0.024         # only to size the fit window before tau is known
MIN_FIT_SAMPLES = 8

# Raw-path timestamp correction, measured by QIDI_CS_CLOCK: the MCU answers
# about 27% of the way through the round trip, and the buffered conversion is
# on average 0.391 ms old. Applied per sample - never as a fixed offset,
# because RTT moves with host load.
RTT_FRAC = 0.27
BUFFER_STALE_S = 0.000391
TORN_GATE_COUNTS = 50000


# --------------------------------------------------------------------------
# the maths, kept as free functions so the tests can drive them directly
# --------------------------------------------------------------------------

def cumtrapz(ts, fs):
    """Cumulative trapezoidal integral of f over irregularly spaced t."""
    out = [0.0]
    for i in range(1, len(ts)):
        out.append(out[-1] + 0.5 * (fs[i] + fs[i - 1]) * (ts[i] - ts[i - 1]))
    return out


def _solve(a, b):
    """Gaussian elimination with partial pivoting. n is 4, so this is fine."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r == c:
                continue
            f = m[r][c] / m[c][c]
            for k in range(c, n + 1):
                m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_tau(ts, fs):
    """Recover the first-order time constant from a step response.

    F(t) = F(0) - (1/tau) INT F + (F_inf/tau) t, plus a t^2 term to absorb the
    slow melt drift. Linear in the unknowns, so one least-squares solve.

    ts must be relative to the transition and increasing. Returns None if the
    fit is degenerate or gives a non-physical tau.
    """
    n = len(ts)
    if n < MIN_FIT_SAMPLES:
        return None
    I = cumtrapz(ts, fs)
    # design matrix columns: 1, I(t), t, t^2
    X = [[1.0, I[i], ts[i], ts[i] * ts[i]] for i in range(n)]
    ata = [[sum(X[i][r] * X[i][c] for i in range(n)) for c in range(4)]
           for r in range(4)]
    atb = [sum(X[i][r] * fs[i] for i in range(n)) for r in range(4)]
    coef = _solve(ata, atb)
    if coef is None:
        return None
    b = coef[1]
    if b >= 0 or not (b == b):          # b must be negative for a decay
        return None
    tau = -1.0 / b

    # Trapezoidal discretisation bias. The trapezoid rule overestimates the
    # integral of a convex function, and an exponential decay is convex, so the
    # cumulative integral comes out large and tau with it. The relative error is
    # (dt/tau)^2 / 12, verified against synthetic data:
    #
    #     tau 24 ms, dt 4.5 ms -> 0.29% predicted, <1% measured
    #     tau 12 ms, dt 4.5 ms -> 1.2%  predicted, 1.2% measured
    #
    # It matters because an unknown filament's PA can be anywhere from 0.01 to
    # 0.1, and at the short end the sampling is only ~2.7 points per tau. One
    # correction pass is enough; the residual is third order.
    dts = sorted(ts[i] - ts[i - 1] for i in range(1, n))
    dt = dts[len(dts) // 2]
    if tau > 0:
        tau = tau / (1.0 + (dt / tau) ** 2 / 12.0)
    f_inf = coef[2] * tau
    pred = [coef[0] + coef[1] * I[i] + coef[2] * ts[i]
            + coef[3] * ts[i] * ts[i] for i in range(n)]
    mf = sum(fs) / n
    sst = sum((f - mf) ** 2 for f in fs)
    sse = sum((fs[i] - pred[i]) ** 2 for i in range(n))
    return {'tau': tau, 'f_inf': f_inf, 'n': n,
            'r2': (1.0 - sse / sst) if sst > 0 else 0.0,
            'rms': math.sqrt(sse / n)}


def wave(point, amp):
    """(v_lo, v_hi) for a point's square wave. dv is amp*v_E either way.

    A CENTRED wave peaks at (1 + amp/2) * v_E, so centring one on the working
    max would drive the up-leg 12.5% OVER it. A point marked anchor='top' puts
    v_hi AT its own v_E and drops v_lo below - so the up transition ARRIVES at
    max, which is the condition worth characterising, and nothing exceeds it.
    Up is the half Stage 3 keeps, so the anchored form loses nothing.
    """
    v = point['v_e_mm_s']
    if point.get('anchor') == 'top':
        return v * (1.0 - amp), v
    return v * (1.0 - amp / 2.0), v * (1.0 + amp / 2.0)


def _med(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _mad(xs):
    if not xs:
        return 0.0
    m = _med(xs)
    return 1.4826 * _med([abs(x - m) for x in xs])


def confidence(taus, r2s, step_gf, noise_gf=0.99):
    """A 0-100 score, so a weak point is visible as weak rather than being
    silently averaged into the table.

    30 base, plus signal-to-noise, plus consistency, plus fit quality.

    NOT djsplice's scale, though an earlier version of this docstring said it
    was. Only the shape is theirs: a base of 30, four
    terms totalling 100, and the grade bands below. Every term is different,
    because theirs reads quantities this method does not produce - their score
    works on the `area` residual of a ZERO-CROSSING ROOT FIND, and this fits the
    melt time constant directly and never brackets a sign flip:

      theirs  SNR in five tiers on peak |area|, max 40; balance = how
              symmetrically the bracketing steps straddle zero; flat +10 when
              the end residuals are small
      here    SNR continuous in log10(force step / cell noise), max 25;
              consistency = MAD/median of the fitted taus, max 25; fit quality
              linear in the regression R^2, max 20
    """
    if not taus:
        return 0
    score = 30.0
    snr = step_gf / max(noise_gf, 1e-6)
    score += min(25.0, 25.0 * math.log10(max(snr, 1.0)) / 2.0)
    spread = _mad(taus) / max(_med(taus), 1e-9)
    score += max(0.0, 25.0 * (1.0 - spread / 0.25))
    score += max(0.0, 20.0 * (_med(r2s) - 0.8) / 0.2) if r2s else 0.0
    return int(max(0, min(100, round(score))))


def grade(score):
    if score >= 80:
        return "excellent"
    if score >= 65:
        return "good"
    if score >= 45:
        return "fair"
    if score >= 30:
        return "weak"
    return "unusable"


# --------------------------------------------------------------------------

class QidiPABedMeasure:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.root_name = config.get('root', 'probe_air')
        self.sensor_attr = config.get('sensor_attr', 'sensor_helper')
        # MUST be configurable, and must match [qidi_flow_bed_search]'s value.
        # See that file, and qidi_pa_measure.py, for the full history of why
        # this is a config option and not a module constant.
        self.counts_per_gf = config.getfloat('counts_per_gf', COUNTS_PER_GF,
                                             above=1.)
        out_dir = config.get('out_dir', '~/printer_data/qidi_pa')
        self.out_dir = os.path.expanduser(out_dir)
        # SAME file the chute version writes - required so the unmodified
        # QIDI_PA_TABLE can find it. See this file's header.
        self.report_path = os.path.join(self.out_dir, 'pa_table.json')
        self.position_path = os.path.join(self.out_dir, 'bed_position.json')
        self.gcode.register_command('QIDI_PA_BED_MEASURE', self.cmd_MEASURE,
                                    desc=self.cmd_MEASURE_help)

    # -- bed-centre positioning ------------------------------------------
    # Duplicated from qidi_flow_bed_search.py rather than imported - see this
    # project's "no shared code between files" convention.
    def _read_bed_position(self, gcmd):
        try:
            with open(self.position_path) as f:
                d = json.load(f)
            return float(d['x']), float(d['y']), float(d['bed_z'])
        except Exception:
            raise gcmd.error(
                "pa_bed: no bed_position.json - run QIDI_BED_PREPARE first "
                "(or QIDI_AUTO_CALIBRATE_BED, which does it for you)")

    def _move_to_bed_centre(self, gcmd, toolhead):
        bx, by, bed_z = self._read_bed_position(gcmd)
        cur = toolhead.get_position()
        if abs(cur[2] - bed_z) > BED_Z_TOLERANCE_MM:
            raise gcmd.error(
                "pa_bed: expected the bed at Z%.1f (from the last "
                "QIDI_BED_PREPARE) but the toolhead is at Z%.1f now - the "
                "bed may have been raised since. Run QIDI_BED_PREPARE again "
                "before trusting this position." % (bed_z, cur[2]))
        self.gcode.run_script_from_command(
            "G90\nG1 X%.2f Y%.2f F%d" % (bx, by, BED_TRAVEL_FEED))
        toolhead.wait_moves()

    # -- plumbing ----------------------------------------------------------
    def _record(self, payload):
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            # APPEND, like every other module here. This overwrote until
            # 2026-09-15, which threw away three runs deliberately taken to
            # compare against each other - and run-to-run spread turned out to
            # be the most important thing in the data.
            data = []
            if os.path.exists(self.report_path):
                try:
                    with open(self.report_path) as f:
                        data = json.load(f)
                    if not isinstance(data, list):
                        data = [data]
                except Exception:
                    data = []
            data.append({'kind': 'pa_table', 'time': time.time(),
                         'payload': payload})
            tmp = self.report_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=1, default=repr)
            os.replace(tmp, self.report_path)
            return True
        except Exception:
            logging.exception("qidi_pa_bed_measure: could not write report")
            return False

    def _parts(self, gcmd):
        root = self.printer.lookup_object(self.root_name, None)
        if root is None:
            raise gcmd.error("pa_bed: no object %s" % (self.root_name,))
        sensor = getattr(root, self.sensor_attr, None)
        raw = getattr(sensor, 'query_cs1237_end_cmd', None) if sensor else None
        oid = getattr(sensor, 'oid', None) if sensor else None
        read = getattr(sensor, 'read_origin_data', None) if sensor else None
        if raw is None or oid is None or not callable(read):
            raise gcmd.error("pa_bed: sensor not usable - run QIDI_CS_LOCATE")
        return sensor, read, raw, oid

    def _script(self, s):
        self.gcode.run_script_from_command(s)

    def _envelope(self, gcmd):
        path = os.path.join(self.out_dir, 'envelope.json')
        try:
            with open(path) as f:
                d = json.load(f)
            return d['payload']
        except Exception:
            raise gcmd.error("pa_bed: no usable envelope.json - run "
                             "QIDI_PA_ENVELOPE first, or pass FLOW= and ACCEL=")

    # -- sampling ----------------------------------------------------------
    def _sample(self, raw, oid, mcu):
        """One raw sample: (print_time, counts) or (print_time, None) if torn."""
        t0 = self.reactor.monotonic()
        resp = raw.send([oid, 0, 4])
        t1 = self.reactor.monotonic()
        host = t0 + RTT_FRAC * (t1 - t0) - BUFFER_STALE_S
        blob = resp.get('data') if isinstance(resp, dict) else None
        if isinstance(blob, str):
            blob = blob.encode('latin-1', 'replace')
        v = None
        if blob and len(blob) >= 3:
            u = blob[0] | (blob[1] << 8) | (blob[2] << 16)
            v = u - 0x1000000 if u & 0x800000 else u
        return mcu.estimated_print_time(host), v, (t1 - t0) * 1000.0

    def _collect(self, gcmd, raw, oid, mcu, until_pt, tare, rtt_cap):
        """Poll the raw path until print time passes until_pt, aborting on
        force. Returns (samples, torn, rejected)."""
        rows, torn, rej = [], 0, 0
        abort_counts = ABORT_GF * self.counts_per_gf
        last_good = tare
        while True:
            pt, v, rtt = self._sample(raw, oid, mcu)
            if pt >= until_pt:
                break
            if v is None or abs(v - last_good) > TORN_GATE_COUNTS:
                torn += 1
                continue
            last_good = v
            if rtt > rtt_cap:
                rej += 1
                continue
            rel = v - tare
            # Melt pressure is NEGATIVE. Compare MAGNITUDE - a signed test
            # against a positive limit is the bug that made Stage 1's first
            # ceiling unfirable.
            if abs(rel) > abort_counts:
                raise gcmd.error("pa_bed: FORCE ABORT %.0f gf exceeds %.0f gf"
                                 % (abs(rel) / self.counts_per_gf,
                                    ABORT_GF))
            rows.append((pt, rel))
        return rows, torn, rej

    # -- motion ------------------------------------------------------------
    def _park_x(self):
        """The wiper sits beside the purge chute, so its X is relative to the
        park position - NOT absolute.

        Fixed at the Q2's park - it has no [gcode_macro _km_globals] to read
        from, and the X-Max 4's 135 would be mid-bed here. See
        qidi_flow_bed_search.py._park_x.
        """
        return 85.0

    def _wipe(self, toolhead=None):
        # Long strokes over the full travel, then shorter ones working the near
        # end - see WIPE_SHORT_PASSES. Kept deliberately identical in shape to
        # qidi_flow_ramp._wipe; the two are separate copies, so a change to the
        # wipe has to be made in BOTH.
        px = self._park_x()
        lo, hi = px + WIPE_LO_OFFSET, px + WIPE_HI_OFFSET
        short_hi = lo + WIPE_SHORT_FRAC * (hi - lo)
        lines = ["SAVE_GCODE_STATE NAME=_qidi_pa_wipe", "G90"]
        for _ in range(WIPE_PASSES):
            lines.append("G1 X%.2f F%d" % (hi, WIPE_FEED_FAST))
            lines.append("G1 X%.2f F%d" % (lo, WIPE_FEED_SLOW))
        for _ in range(WIPE_SHORT_PASSES):
            lines.append("G1 X%.2f F%d" % (short_hi, WIPE_FEED_FAST))
            lines.append("G1 X%.2f F%d" % (lo, WIPE_FEED_SLOW))
        lines.append("G1 X%.2f F%d" % (px, WIPE_FEED_SLOW))
        lines.append("RESTORE_GCODE_STATE NAME=_qidi_pa_wipe")
        self._script("\n".join(lines))
        if toolhead is not None:
            toolhead.wait_moves()

    def _wipe_at_chute(self, gcmd, toolhead, return_to_bed=True):
        """Same wipe as the chute routine, unchanged - but over the bed the
        wiper is only reachable at the chute, so every wipe now means
        travelling there and back rather than wiping in place. return_to_bed
        is False only for the very last wipe in the finally: block, where the
        routine is ending and there is nothing to return FOR."""
        self._script("MOVE_TO_TRASH")
        toolhead.wait_moves()
        self._wipe(toolhead)
        if return_to_bed:
            self._move_to_bed_centre(gcmd, toolhead)

    def _square_wave(self, toolhead, v_lo, v_hi, leg_s, cycles):
        """Queue one block and return (t_start, t_end) in print time.

        Both get_last_move_time() calls flush the lookahead, which forces a
        stop - so they bracket a BLOCK, never individual legs. The first
        transition of each block is discarded because its leg starts from rest.
        """
        t_start = toolhead.get_last_move_time()
        d_lo = v_lo * leg_s
        d_hi = v_hi * leg_s
        self._script("M83")
        for _ in range(cycles):
            self._script("G1 E%.5f F%.1f" % (d_lo, v_lo * 60.0))
            self._script("G1 E%.5f F%.1f" % (d_hi, v_hi * 60.0))
        t_end = toolhead.get_last_move_time()
        return t_start, t_end

    # -- analysis ----------------------------------------------------------
    def _transitions(self, rows, t_start, t_end, cycles, leg_s):
        """Split samples into per-transition windows.

        The block's actual duration is taken from Klipper rather than assumed,
        so the leg length used here is the one the planner really produced.
        """
        if not rows or cycles < 1:
            return []
        actual = (t_end - t_start) / (2.0 * cycles)
        win = FIT_WINDOW_TAU * ASSUMED_K_S
        out = []
        # skip transition 0: that leg starts from rest, so its response is the
        # machine starting up rather than the melt answering a velocity step
        for k in range(1, 2 * cycles):
            t_tr = t_start + k * actual
            seg = [(t - t_tr, f) for t, f in rows
                   if 0.0 <= t - t_tr <= min(win, actual)]
            if len(seg) >= MIN_FIT_SAMPLES:
                out.append({'k': k, 'up': (k % 2 == 1),
                            'ts': [s[0] for s in seg],
                            'fs': [s[1] for s in seg]})
        return out

    def _analyse(self, transitions):
        ups, downs, r2s, steps = [], [], [], []
        r2_up, steps_up = [], []
        detail = []
        for tr in transitions:
            fit = fit_tau(tr['ts'], tr['fs'])
            if fit is None:
                continue
            # A tau outside 1-200 ms is not a PA lag - it is a bad fit or the
            # slow melt process leaking through.
            if not (0.001 <= fit['tau'] <= 0.200):
                continue
            step = abs(fit['f_inf'] - tr['fs'][0]) / self.counts_per_gf
            (ups if tr['up'] else downs).append(fit['tau'])
            if tr['up']:
                r2_up.append(fit['r2'])
                steps_up.append(step)
            r2s.append(fit['r2'])
            steps.append(step)
            detail.append({'k': tr['k'], 'up': tr['up'],
                           'tau': fit['tau'], 'r2': fit['r2'], 'step_gf': step})
        allt = ups + downs
        return {'up': ups, 'down': downs, 'all': allt, 'r2': r2s,
                'r2_up': r2_up, 'steps_up': steps_up,
                'steps_gf': steps, 'detail': detail}

    cmd_MEASURE_help = ("Stage 3 - measure pressure advance at each envelope "
                        "point over BED CENTRE instead of the chute - needs "
                        "QIDI_BED_PREPARE run first. Fits the melt time "
                        "constant, same as the chute version. HEATS AND "
                        "EXTRUDES. [DRY=1] [POINT=] [FLOW=] [ACCEL=] "
                        "[LEG_MS=200] [CYCLES=5] [BLOCKS=3]")

    def cmd_MEASURE(self, gcmd):
        # The guard sits OUTSIDE the cleanup so cleanup still runs, and turns a
        # stray exception into a command error rather than a Klipper internal
        # error, which would shut down every MCU.
        err_cls = type(gcmd.error("probe"))
        try:
            return self._run(gcmd)
        except err_cls:
            raise
        except Exception as e:
            logging.exception("qidi_pa_bed_measure: unhandled error")
            raise gcmd.error("pa_bed: internal error, printer left running: "
                             "%s: %s" % (type(e).__name__, str(e)[:160]))

    def _plan(self, gcmd):
        leg_s = gcmd.get_float('LEG_MS', DEFAULT_LEG_MS, above=20.,
                               below=2000.) / 1000.0
        cycles = gcmd.get_int('CYCLES', DEFAULT_CYCLES, minval=2, maxval=20)
        blocks = gcmd.get_int('BLOCKS', DEFAULT_BLOCKS, minval=1, maxval=50)
        amp = gcmd.get_float('AMP', AMPLITUDE_FRAC, above=0.02, below=1.0)
        flow = gcmd.get_float('FLOW', None)
        accel = gcmd.get_float('ACCEL', None)
        want = gcmd.get('POINT', None)

        if flow is not None and accel is not None:
            pts = [{'name': 'explicit', 'flow_mm3_s': flow, 'accel_xy': accel,
                    'v_e_mm_s': flow / A_FIL_MM2, 'a_e_mm_s2': None,
                    'weak': False}]
            geom = None
        else:
            env = self._envelope(gcmd)
            geom = env.get('geometry')
            pts = env['points']
            if want:
                pts = [p for p in pts if p['name'].replace(' ', '')
                       .replace(',', '') == want.replace(' ', '')
                       .replace(',', '')]
                if not pts:
                    raise gcmd.error("pa_bed: no envelope point matching %r"
                                     % (want,))
        for p in pts:
            if p.get('a_e_mm_s2') is None:
                r = (geom or {}).get('R')
                p['a_e_mm_s2'] = p['accel_xy'] * r if r else 300.0
        return pts, geom, leg_s, cycles, blocks, amp

    def _run(self, gcmd):
        pts, geom, leg_s, cycles, blocks, amp = self._plan(gcmd)
        dry = gcmd.get_int('DRY', 0)

        gcmd.respond_info(
            "pa_bed: Stage 3 - fitting the melt time constant directly. Klipper "
            "does not apply pressure advance to extrude-only moves "
            "(can_pressure_advance needs X or Y), so a K search would have "
            "been measuring nothing. tau = K is measured instead.")
        gcmd.respond_info("  %d point(s), %.0f ms legs, %d cycles x %d blocks, "
                          "dv = %.0f%% of v_E"
                          % (len(pts), leg_s * 1000.0, cycles, blocks,
                             100.0 * amp))
        gcmd.respond_info("  point               Q mm3/s   v_E    v_lo   v_hi"
                          "   a_E   E per block")
        for p in pts:
            v = p['v_e_mm_s']
            lo, hi = wave(p, amp)
            e_blk = (lo + hi) * leg_s * cycles
            gcmd.respond_info("  %-18s %7.2f %6.3f %6.3f %6.3f %6.1f %8.2f mm%s"
                              % (p['name'], p['flow_mm3_s'], v, lo, hi,
                                 p['a_e_mm_s2'], e_blk,
                                 "  TOP-ANCHORED" if p.get('anchor') == 'top'
                                 else ""))
        block_s = 2.0 * leg_s * cycles
        limit = BUFFER_TIME_HIGH_S * BLOCK_SAFE_FRAC
        if block_s > limit:
            gcmd.respond_info(
                "  WARNING: a block is %.2f s of motion against a %.1f s move "
                "buffer. Klipper will block mid-queue, motion will start before "
                "sampling does, and the first transitions of every block will "
                "be missed - wasting filament for nothing. Reduce CYCLES to %d "
                "or LEG_MS accordingly."
                % (block_s, BUFFER_TIME_HIGH_S, max(2, int(limit / (2 * leg_s)))))
        total_mm = 0.0
        total_wipes = 0
        for p in pts:
            lo, hi = wave(p, amp)
            run_mm = (lo + hi) * leg_s * cycles * blocks
            nwipe = int((run_mm * A_FIL_MM2) // WIPE_VOLUME_MM3)
            total_wipes += nwipe
            total_mm += (run_mm + DEFAULT_PRIME_MM3 / A_FIL_MM2
                         + nwipe * REPRIME_MM3 / A_FIL_MM2)
        if total_wipes:
            gcmd.respond_info("  %d inter-block wipe(s) at %.0f mm3, each "
                              "re-priming %.0f mm3"
                              % (total_wipes, WIPE_VOLUME_MM3, REPRIME_MM3))
        gcmd.respond_info("  total filament about %.0f mm = %.1f g at 1.09 g/cm3"
                          % (total_mm, total_mm * A_FIL_MM2 * 1.09 / 1000.0))

        if dry:
            gcmd.respond_info("pa_bed: DRY - nothing heated, nothing moved.")
            return

        sensor, read, raw, oid = self._parts(gcmd)
        mcu = sensor.get_mcu() if callable(getattr(sensor, 'get_mcu', None)) \
            else getattr(sensor, 'mcu', None)
        if mcu is None:
            raise gcmd.error("pa_bed: cannot reach the sensor MCU")
        toolhead = self.printer.lookup_object('toolhead')

        # TEMP=, if given, is SET AND WAITED FOR rather than merely checked.
        #
        # This used to only check, and refuse when cold. That made the whole
        # stage depend on an invisible precondition - "somebody left the nozzle
        # hot" - and on 2026-09-17 the chain broke it: Stage 1 shut the hotend
        # down at the end of its own run, Stage 3 started 18 s later while the
        # check still passed at ~270 C, and the nozzle kept falling underneath
        # the measurement. The lowest flow point, 2.92 mm3/s, hit the force
        # abort at 1662 gf - more force cold than 23 mm3/s made hot.
        #
        # Checking a temperature at the start says nothing about what it will be
        # a minute later. Setting it is what makes the stage correct on its own.
        target = gcmd.get_float('TEMP', None, above=150., below=350.)
        if target is not None:
            gcmd.respond_info("pa_bed: heating to %.0f C and waiting" % (target,))
            self.gcode.run_script_from_command("M109 S%.0f" % (target,))

        extruder = self.printer.lookup_object('extruder', None)
        temp = 0.0
        if extruder is not None:
            try:
                temp = extruder.get_status(self.reactor.monotonic()
                                           )['temperature']
            except Exception:
                temp = 0.0
        if temp < MIN_EXTRUDE_TEMP:
            raise gcmd.error("pa_bed: nozzle is %.0f C - pass TEMP= to have this "
                             "heat it, or heat it to printing temperature "
                             "first" % (temp,))
        # The wipe, the chute move and the bed-centre move are all absolute
        # moves, and extruding
        # anywhere other than the chute is safety rule 5 territory.
        if 'xyz' not in (toolhead.get_status(self.reactor.monotonic())
                         .get('homed_axes', '')):
            raise gcmd.error("pa_bed: home the printer first (G28)")

        results = []
        started = False
        limits = self._motion_limits(toolhead)
        try:
            self._script("M106 P%d S%d" % (FILTER_M106_P, FILTER_SPEED))
            started = True
            # Straight to bed centre - unlike Stage 1, heating (M109, above)
            # does not care about position, so there is no reason to visit
            # the chute first. This is the direct equivalent of the chute
            # version moving itself to the chute at its own start: each stage
            # positions itself to its OWN working position, regardless of
            # where the toolhead happened to be before.
            gcmd.respond_info("pa_bed: moving to bed centre")
            self._move_to_bed_centre(gcmd, toolhead)
            # NOT re-captured here - see the header note. _move_to_bed_centre
            # is a plain G1, so the limits captured before anything moved are
            # still correct.
            for p in pts:
                results.append(self._measure_point(
                    gcmd, p, leg_s, cycles, blocks, read, raw, oid, mcu,
                    toolhead, limits, amp))
        finally:
            try:
                self._script("SET_VELOCITY_LIMIT ACCEL=%.0f "
                             "MINIMUM_CRUISE_RATIO=%.3f" % limits)
            except Exception:
                logging.exception("qidi_pa_bed_measure: cleanup motion limits")
            try:
                self._script("M83")
                self._script("G1 E-1.0 F1800")
                # return_to_bed=False: the routine is ending here on a
                # standalone run, or the orchestrator's own QIDI_FLOW_BED_WIPE
                # will reposition next regardless - nothing needs this wipe to
                # come back to bed centre.
                self._wipe_at_chute(gcmd, toolhead, return_to_bed=False)
            except Exception:
                logging.exception("qidi_pa_bed_measure: cleanup retract/wipe")
            if started:
                try:
                    self._script("M106 P%d S0" % (FILTER_M106_P,))
                except Exception:
                    logging.exception("qidi_pa_bed_measure: cleanup "
                                      "filtration")

        self._summary(gcmd, results, geom, leg_s, cycles, blocks, amp)

    def _motion_limits(self, toolhead):
        """The machine's own accel and cruise ratio, read ONCE before anything
        is changed.

        Reading toolhead.max_accel inside the per-point finally: read back the
        value that same point had just lowered it to, so each point "restored"
        to a_E and the next point's wipe ran at 113-454 mm/s^2 - and the run
        left the printer that way. Capture first, restore to the capture."""
        try:
            st = toolhead.get_status(self.reactor.monotonic())
            return (float(st.get('max_accel', 10000.0)),
                    float(st.get('minimum_cruise_ratio', 0.5)))
        except Exception:
            return 10000.0, 0.5

    def _measure_point(self, gcmd, p, leg_s, cycles, blocks, read, raw, oid,
                       mcu, toolhead, limits, amp=AMPLITUDE_FRAC):
        v = p['v_e_mm_s']
        v_lo, v_hi = wave(p, amp)
        gcmd.respond_info("pa_bed: %s - Q %.2f mm3/s, v_E %.3f (%.3f..%.3f), "
                          "a_E %.1f, dv %.0f%%%s"
                          % (p['name'], p['flow_mm3_s'], v, v_lo, v_hi,
                             p['a_e_mm_s2'], 100.0 * amp,
                             ", TOP-ANCHORED (v_hi is the max, never exceeded)"
                             if p.get('anchor') == 'top' else ""))
        self._wipe_at_chute(gcmd, toolhead)
        self._script("SET_VELOCITY_LIMIT ACCEL=%.0f MINIMUM_CRUISE_RATIO=0"
                     % (p['a_e_mm_s2'],))
        try:
            # prime at the point's own flow so the melt is at its working state
            self._script("M83")
            self._script("G1 E%.3f F%.1f"
                         % (DEFAULT_PRIME_MM3 / A_FIL_MM2, v * 60.0))
            toolhead.wait_moves()
            tare = float(read())

            transitions, torn_t, rej_t = [], 0, 0
            since_wipe = 0.0
            wipes = 0
            block_mm3 = (v_lo + v_hi) * leg_s * cycles * A_FIL_MM2
            for _b in range(blocks):
                if since_wipe >= WIPE_VOLUME_MM3:
                    # Stage 1 learned this the hard way over several runs: past
                    # roughly 200 mm3 a blob forms on the nozzle and corrupts
                    # everything measured after it. The wipe runs at the
                    # MACHINE's accel, not the point's a_E - at 113 mm/s^2 a
                    # 45 mm wipe peaks at 71 mm/s and crawls.
                    self._script("SET_VELOCITY_LIMIT ACCEL=%.0f "
                                 "MINIMUM_CRUISE_RATIO=%.3f" % limits)
                    self._wipe_at_chute(gcmd, toolhead)
                    self._script("SET_VELOCITY_LIMIT ACCEL=%.0f "
                                 "MINIMUM_CRUISE_RATIO=0" % (p['a_e_mm_s2'],))
                    # The melt depressurises during the wipe and needs to come
                    # back up before the next block means anything.
                    self._script("M83")
                    self._script("G1 E%.3f F%.1f"
                                 % (REPRIME_MM3 / A_FIL_MM2, v * 60.0))
                    toolhead.wait_moves()
                    tare = float(read())
                    since_wipe = 0.0
                    wipes += 1
                t0, t1 = self._square_wave(toolhead, v_lo, v_hi, leg_s, cycles)
                rows, torn, rej = self._collect(gcmd, raw, oid, mcu, t1, tare,
                                                rtt_cap=20.0)
                torn_t += torn
                rej_t += rej
                transitions.extend(self._transitions(rows, t0, t1, cycles,
                                                     leg_s))
                toolhead.wait_moves()
                since_wipe += block_mm3
        finally:
            self._script("SET_VELOCITY_LIMIT ACCEL=%.0f "
                         "MINIMUM_CRUISE_RATIO=%.3f" % limits)

        a = self._analyse(transitions)
        n = len(a['all'])
        if not n:
            gcmd.respond_info("  no usable transitions - %d torn, %d RTT "
                              "rejected" % (torn_t, rej_t))
            return {'point': p, 'k_s': None, 'n': 0, 'confidence': 0,
                    'torn': torn_t, 'rtt_rejected': rej_t}
        # K IS THE UP-TRANSITIONS ONLY, and this is not a tuning choice.
        #
        # Pressure advance compensates flow INCREASES - it pushes extra filament
        # to fill melt compliance when the extruder accelerates. Decompression
        # is a different mechanism (melt relaxing rather than being pumped) and
        # has no reason to share a time constant. Measured on ASA-CF against its
        # calibrated 0.024, over seven runs at two leg lengths:
        #
        #     up    0.0230 +/-5.0%  and  0.0245 +/-5.9%   CONTAINS 0.024
        #     down  0.0183 +/-2.3%  and  0.0177 +/-5.1%   excludes it
        #     pool  0.0211 +/-2.5%  and  0.0222 +/-3.1%   excludes it
        #
        # The obvious objection is that the fit window might treat rising and
        # falling edges differently, in which case keeping the half that matches
        # a known answer would be tuning the method to a filament we already
        # know. Tested by doubling LEG_MS, which pushes the velocity ramp
        # proportionally further from the window: the gap GREW, 25.6% -> 38.4%.
        # Windowing is ruled out.
        #
        # Down is still measured and reported - those transitions happen whether
        # or not they are used, and a divergence between them is a signal.
        use_up = bool(a['up'])
        src = a['up'] if use_up else a['all']
        k = _med(src)
        spread = _mad(src)
        step = _med(a['steps_up'] if use_up else a['steps_gf'])
        r2s = a['r2_up'] if use_up else a['r2']
        score = confidence(src, r2s, step)
        gcmd.respond_info("  K = %.4f s  (MAD %.4f, n=%d up, step %.0f gf, "
                          "R2 %.3f)%s"
                          % (k, spread, len(src), step, _med(r2s),
                             "" if use_up else "  [NO UP TRANSITIONS - this is "
                             "the pooled value and is biased low]"))
        if a['up'] and a['down']:
            gcmd.respond_info("    up %.4f (n=%d)  <- K   down %.4f (n=%d) "
                              "diagnostic only, %.1f%% lower"
                              % (_med(a['up']), len(a['up']), _med(a['down']),
                                 len(a['down']),
                                 100.0 * (1.0 - _med(a['down'])
                                          / max(_med(a['up']), 1e-9))))
        gcmd.respond_info("    confidence %d (%s), %d torn, %d RTT rejected"
                          "%s"
                          % (score, grade(score), torn_t, rej_t,
                             ", %d inter-block wipe(s)" % wipes if wipes
                             else ""))
        return {'point': p, 'k_s': k, 'k_mad': spread, 'n': len(src),
                'k_source': 'up' if use_up else 'pooled',
                'n_all': n, 'wipes': wipes,
                'k_up': _med(a['up']) if a['up'] else None,
                'k_down': _med(a['down']) if a['down'] else None,
                'n_up': len(a['up']), 'n_down': len(a['down']),
                'step_gf': step, 'r2': _med(a['r2']), 'confidence': score,
                'torn': torn_t, 'rtt_rejected': rej_t,
                'detail': a['detail'][:64]}

    def _summary(self, gcmd, results, geom, leg_s, cycles, blocks,
                 amp=AMPLITUDE_FRAC):
        gcmd.respond_info("pa_bed: ---- results ----")
        gcmd.respond_info("  point               Q mm3/s  accel      K s   "
                          "MAD     n  conf")
        good = []
        for r in results:
            p = r['point']
            if r['k_s'] is None:
                gcmd.respond_info("  %-18s %7.2f %6.0f        -     -     -"
                                  "     0" % (p['name'], p['flow_mm3_s'],
                                              p['accel_xy']))
                continue
            gcmd.respond_info("  %-18s %7.2f %6.0f  %7.4f %7.4f %5d %5d"
                              % (p['name'], p['flow_mm3_s'], p['accel_xy'],
                                 r['k_s'], r['k_mad'], r['n'],
                                 r['confidence']))
            if r['confidence'] >= 45:
                good.append(r)
        if good:
            ks = [r['k_s'] for r in good]
            gcmd.respond_info("  fallback single K (median of %d usable "
                              "points): %.4f" % (len(good), _med(ks)))
            gcmd.respond_info(
                "  ASA-CF's calibrated PA is 0.024 - if this run is that "
                "filament, compare. A constant offset points at the read "
                "delay; a proportional one at the estimator bias.")
        else:
            gcmd.respond_info("  nothing scored high enough to trust.")
        self._record({'results': results, 'geometry': geom,
                      'leg_s': leg_s, 'cycles': cycles, 'blocks': blocks,
                      'amplitude_frac': amp,
                      'method': 'direct tau fit (integral method); Klipper '
                                'does not apply PA to extrude-only moves'})
        gcmd.respond_info("pa_bed: written to %s" % (self.report_path,))


def load_config(config):
    return QidiPABedMeasure(config)
