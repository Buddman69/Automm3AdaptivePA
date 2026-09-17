# qidi_pa_envelope.py - Stage 2: turn a max flow into the PA measurement envelope
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
# WHAT THIS IS
#   Pure maths. No motion, no heating, no extrusion, no sensor. It takes the
#   working max flow from Stage 1 and produces the flow points at which Stage 3
#   will measure pressure advance: geometrically spaced from 15% of the working
#   max up to the max itself, with the top point anchored so nothing overshoots
#   it. Acceleration is not an axis - see the note on DEFAULT_FLOW_LO.
#
#   Its real job is the kinematic conversion. In a print, commanding a_XY
#   produces an extruder acceleration a_E through the line geometry. Doing that
#   conversion here is what lets Stage 3 command v_E and a_E DIRECTLY on the
#   extruder, in air over the purge chute, and still reproduce the conditions of
#   printing at (flow, accel). Nothing in the routine ever prints on the plate.
#
# GEOMETRY DEPENDENCE - the thing to remember
#   v_E depends only on volumetric flow, so nozzle, layer height and line width
#   all cancel: the flow axis transfers between profiles unchanged.
#
#   a_E does NOT. R = A_line/A_fil is 0.0567 at 0.24/0.62 and 0.0743 at
#   0.30/0.66 - 31% apart. So a PA table is only valid for the geometry it was
#   generated for, and that geometry is recorded in the output.
#
# USAGE
#   [qidi_pa_envelope]
#
#   QIDI_PA_ENVELOPE QMAX=19.5 LAYER=0.24 WIDTH=0.62
#
#   Results are written to ~/printer_data/qidi_pa/envelope.json

import json
import logging
import math
import os
import time

# Flow corners as fractions of the working max. 15% reaches overhang speeds;
# 85% stays below the flow limit so PA is never measured where the melt is
# marginal - at the cost of not covering the top 15% of the range.
DEFAULT_FLOW_LO = 0.15

# The accel axis was measured and found not to matter (t = 1.37, bounded to
# +/-16%, against t = 5.9 for flow), so it is a single representative value and
# the measurements that would have been spent on it buy flow resolution
# instead. A four-corner flow x accel layout was the original design and was
# removed on 2026-09-17, having been superseded since 2026-09-15.
DEFAULT_POINTS = 5
DEFAULT_ACCEL_MID = 5000.0

DEFAULT_FILAMENT_D = 1.75

# Below roughly this, the velocity ramp lasts longer than the PA lag it is
# hiding and the measurement degrades. Points under it are flagged so Stage 3
# and Stage 4 can weight them, rather than being silently trusted.
WEAK_ACCEL_RAMP_RATIO = 1.0
TYPICAL_K_S = 0.024          # only used to annotate expected signal quality


def line_area(h, w):
    # Extruded cross-section: a rectangle with semicircular ends.
    return h * (w - h) + math.pi * (h / 2.0) ** 2


def filament_area(d=DEFAULT_FILAMENT_D):
    return math.pi * (d / 2.0) ** 2


def build_flow_set(qmax, h, w, d=DEFAULT_FILAMENT_D,
                   flow_lo=DEFAULT_FLOW_LO, n_points=DEFAULT_POINTS,
                   accel=DEFAULT_ACCEL_MID, k=TYPICAL_K_S, amp=0.25):
    """Geometric flow points from flow_lo*qmax up to qmax itself.

    WHY THIS REPLACED THE FOUR-CORNER ENVELOPE
      Two full five-point runs (2026-09-15) found NO detectable dependence of K
      on acceleration - paired hi-accel minus lo-accel gave t = 1.37, bounding
      any effect at +/-16%. That is what tau = RC predicts: it is a melt
      property, and commanded accel changes the input waveform, not the system's
      time constant. Flow dependence, by contrast, came out at t = 5.9.

      So the accel axis was spending half the measurements on replicates. Those
      measurements buy far more as extra FLOW resolution, at identical cost.

    WHY GEOMETRIC
      K is elevated below ~10 mm3/s and flat above it, so arithmetic spacing
      wastes points in the flat region. Geometric concentrates them where K
      actually changes.

    WHY THE TOP POINT IS ANCHORED
      A symmetric wave centred at Q peaks at (1 + amp/2) * Q, so centring one at
      qmax would drive the up-leg 12.5% OVER the working max. The top point is
      therefore marked anchor='top': Stage 3 puts v_hi AT qmax and drops v_lo
      below it, so the up transition ARRIVES at max - the condition we want to
      characterise - and nothing ever exceeds it.
    """
    a_line = line_area(h, w)
    a_fil = filament_area(d)
    r = a_line / a_fil
    a_e = accel * r
    n = max(int(n_points), 2)
    q_lo = flow_lo * qmax
    ratio = (qmax / q_lo) ** (1.0 / (n - 1))

    points = []
    for i in range(n):
        q = q_lo * ratio ** i
        top = (i == n - 1)
        v_e = q / a_fil
        # dv is amp*v_E either way - the anchor only moves where the wave sits,
        # not how big it is.
        ramp_s = (amp * v_e) / a_e if a_e > 0 else float('inf')
        points.append({
            'name': ('%.2f mm3/s%s' % (q, ' (max)' if top else '')),
            'flow_mm3_s': q, 'accel_xy': accel,
            'frac_of_max': q / qmax,
            'anchor': 'top' if top else 'centre',
            'v_xy_mm_s': q / a_line, 'v_e_mm_s': v_e, 'a_e_mm_s2': a_e,
            'ramp_s': ramp_s, 'ramp_over_k': ramp_s / k if k > 0 else 0.0,
            'weak': (ramp_s / k if k > 0 else 0.0) > WEAK_ACCEL_RAMP_RATIO})
    return {
        'qmax_mm3_s': qmax, 'mode': 'flow',
        'geometry': {'layer_h': h, 'line_w': w, 'filament_d': d,
                     'a_line_mm2': a_line, 'a_fil_mm2': a_fil, 'R': r},
        'range': {'flow_lo_frac': flow_lo, 'flow_hi_frac': 1.0,
                  'accel': accel},
        'assumed_k_s': k, 'amplitude_frac': amp, 'points': points}


class QidiPAEnvelope:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.layer_h = config.getfloat('layer_height', 0.24, above=0.)
        self.line_w = config.getfloat('line_width', 0.62, above=0.)
        self.fil_d = config.getfloat('filament_diameter', DEFAULT_FILAMENT_D,
                                     above=0.)
        out_dir = config.get('out_dir', '~/printer_data/qidi_pa')
        self.out_dir = os.path.expanduser(out_dir)
        self.report_path = os.path.join(self.out_dir, 'envelope.json')
        self.gcode.register_command('QIDI_PA_ENVELOPE', self.cmd_ENVELOPE,
                                    desc=self.cmd_ENVELOPE_help)

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
            logging.exception("qidi_pa_envelope: unhandled error")
            raise gcmd.error("envelope: internal error, printer left running: "
                             "%s: %s" % (type(e).__name__, str(e)[:160]))

    def _record(self, payload):
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            tmp = self.report_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump({'kind': 'envelope', 'time': time.time(),
                           'payload': payload}, f, indent=1, default=repr)
            os.replace(tmp, self.report_path)
            return True
        except Exception:
            logging.exception("qidi_pa_envelope: could not write report")
            return False

    def _last_flow_result(self):
        # Reuse Stage 1's answer if QMAX is not given.
        path = os.path.join(self.out_dir, 'flow_ramp.json')
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = [data]
            for entry in reversed(data):
                wm = (entry.get('payload') or {}).get('working_max')
                if wm:
                    return float(wm)
        except Exception:
            pass
        return None

    cmd_ENVELOPE_help = ("Build the PA measurement envelope from a max flow. "
                         "Pure maths - no motion, heating or extrusion. "
                         "[QMAX=<mm3/s>] [LAYER=] [WIDTH=] [ACCEL_LO=] "
                         "[ACCEL_HI=] [FLOW_LO=0.15] [FLOW_HI=0.85] [K=0.024]")

    def cmd_ENVELOPE(self, gcmd):
        return self._guard(gcmd, self._run_ENVELOPE)

    def _run_ENVELOPE(self, gcmd):
        qmax = gcmd.get_float('QMAX', None)
        src = 'given'
        if qmax is None:
            qmax = self._last_flow_result()
            src = 'from the last QIDI_FLOW_SEARCH'
            if qmax is None:
                raise gcmd.error(
                    "envelope: no QMAX given and no working_max found in "
                    "flow_ramp.json - run QIDI_FLOW_SEARCH first, or pass "
                    "QMAX=<mm3/s>")
        h = gcmd.get_float('LAYER', self.layer_h, above=0.)
        w = gcmd.get_float('WIDTH', self.line_w, above=0.)
        d = gcmd.get_float('FILAMENT_D', self.fil_d, above=0.)
        f_lo = gcmd.get_float('FLOW_LO', DEFAULT_FLOW_LO, above=0., below=1.)
        k = gcmd.get_float('K', TYPICAL_K_S, above=0.)
        # Klipper ignores parameters a command does not read, so without this a
        # stale MODE=corners would silently run flow mode instead.
        if gcmd.get('MODE', None) is not None:
            raise gcmd.error(
                "envelope: MODE was removed on 2026-09-17. The four-corner "
                "flow x accel layout is gone - accel was measured and found "
                "not to affect K, so flow is the only axis and the only mode.")

        npts = gcmd.get_int('POINTS', DEFAULT_POINTS, minval=2, maxval=12)
        amp = gcmd.get_float('AMP', 0.25, above=0.02, below=1.0)
        accel = gcmd.get_float('ACCEL', DEFAULT_ACCEL_MID, above=0.)
        env = build_flow_set(qmax, h, w, d, f_lo, npts, accel, k, amp)
        g = env['geometry']
        gcmd.respond_info("envelope: max flow %.2f mm3/s (%s)" % (qmax, src))
        gcmd.respond_info("envelope: layer %.2f width %.2f -> A_line %.4f mm2, "
                          "A_fil %.4f, R = %.4f"
                          % (h, w, g['a_line_mm2'], g['a_fil_mm2'], g['R']))
        qs = [p['flow_mm3_s'] for p in env['points']]
        gcmd.respond_info(
            "envelope: %d geometric flow points, %.0f%%-%.0f%% of max "
            "= %.2f-%.2f mm3/s. Accel is fixed at %.0f and is NOT an axis: "
            "K was measured against it and no dependence was found "
            "(t=1.37, bounded +/-16%%), while flow gave t=5.9."
            % (len(qs), 100 * qs[0] / qmax, 100 * qs[-1] / qmax, qs[0],
               qs[-1], env['range']['accel']))
        gcmd.respond_info(
            "envelope: the top point is ANCHORED - Stage 3 puts v_hi at the "
            "max itself rather than centring a wave there, which would "
            "overshoot it by %.0f%%."
            % (100 * env.get('amplitude_frac', 0.25) / 2.0,))
        gcmd.respond_info("  point               Q mm3/s  v_XY mm/s   a_XY"
                          "   v_E mm/s  a_E mm/s2  ramp/K")
        for pt in env['points']:
            gcmd.respond_info(
                "  %-18s %8.2f %10.1f %6.0f %10.3f %10.1f %7.2f%s"
                % (pt['name'], pt['flow_mm3_s'], pt['v_xy_mm_s'],
                   pt['accel_xy'], pt['v_e_mm_s'], pt['a_e_mm_s2'],
                   pt['ramp_over_k'], "  WEAK" if pt['weak'] else ""))
        weak = [p['name'] for p in env['points'] if p['weak']]
        if weak:
            gcmd.respond_info(
                "envelope: %s expected to measure poorly - the velocity ramp "
                "lasts longer than the %.3fs PA lag it is hiding. Stage 3 "
                "should score these low rather than trust them."
                % (", ".join(weak), k))
        gcmd.respond_info(
            "envelope: this table is valid for %.2f layer / %.2f width ONLY - "
            "a_E scales with R, so changing geometry invalidates the accel axis"
            % (h, w))
        ok = self._record(env)
        gcmd.respond_info("envelope: written to %s"
                          % (self.report_path if ok else "(not written)",))


def load_config(config):
    return QidiPAEnvelope(config)
