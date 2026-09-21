# qidi_pa_table.py - Stage 4: turn measured K values into an OrcaSlicer table
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
# WHAT ORCA EXPECTS - read off a real profile, not guessed
#   The filament setting is `adaptive_pressure_advance_model`: one string,
#   newline-separated rows, each "PA,flow,accel". From the stock
#   "FILL3D PLA Turbo @base.json" in the Orca filament library:
#
#       0.03,23.43,5000
#       0.028,25.31,5000
#       0.025,28.12,5000
#       0.025,23.43,12000
#       0.020,25.31,12000
#       0.018,28.12,12000
#
#   Three flows at each of two accelerations. Column order is therefore
#   PA, then volumetric flow in mm3/s, then acceleration in mm/s2. The zeroed
#   default ("0,0,0\n0,0,0") could not have told us the order - a populated one
#   could, so that is what was used.
#
#   `adaptive_pressure_advance` must also be set to 1 for the model to be used.
#
# WHY EVERY FLOW IS EMITTED AT TWO IDENTICAL ACCELERATIONS
#   Stage 3 measured K against acceleration and found no dependence - paired
#   hi-minus-lo gave t = 1.37, bounding any effect at +/-16%, while flow gave
#   t = 5.9. So the honest table is "the same K at every acceleration".
#
#   Orca interpolates over both axes, so giving it a single accel row would
#   leave it nothing to interpolate across. Emitting each flow twice, at a low
#   and a high accel with the SAME K, states the measured result exactly: flat
#   in accel, varying in flow.
#
# WHAT IS NOT DONE, DELIBERATELY
#   No curve fitting. Orca interpolates linearly between rows, and the measured
#   points are already the shape - fitting a power law would add a model the
#   data does not need and hide the plateau above ~12 mm3/s.
#
#   No extrapolation beyond the measured flow range. Below the lowest point
#   Orca holds the lowest K, which is the right behaviour for slow features.
#
# USAGE
#   [qidi_pa_table]
#
#   QIDI_PA_TABLE               the most recent complete run
#   QIDI_PA_TABLE RUNS=3        average the last 3 runs that share a config
#   QIDI_PA_TABLE MIN_CONF=45   drop points scoring below this
#
#   Prints a block ready to paste into Orca, and writes orca_pa.json.

import json
import logging
import math
import os
import time

try:
    # qidi_auto_cal holds the one copy of these, so the links printed here and
    # the ones printed under a full run can never drift apart. Both modules are
    # installed together; degrade quietly if only this one is present.
    from qidi_auto_cal import SUPPORT_LINES
except Exception:
    SUPPORT_LINES = []

DEFAULT_MIN_CONF = 45          # the "fair" band; below this is not trusted
DEFAULT_ACCEL_LO = 500.0       # first layer
DEFAULT_ACCEL_HI = 10000.0     # the machine's max_accel
MAX_RUNS = 20


def _med(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def run_signature(payload):
    """What has to match for two runs to be averaged together.

    Stage 4 must never merge across entries - written when the
    file held runs at different leg lengths, amplitudes and block counts, where
    merging really would be wrong. Runs with an IDENTICAL config measuring the
    SAME flows are a different case: averaging them is just more samples, and
    three of them cut the per-point standard error from 1.7-5.9% to 0.9-2.9%.
    The signature is what enforces that distinction.
    """
    pts = payload.get('results') or []
    flows = tuple(round(r['point']['flow_mm3_s'], 4) for r in pts)
    return (round(payload.get('leg_s', 0.0), 5),
            payload.get('cycles'), payload.get('blocks'),
            round(payload.get('amplitude_frac', 0.25), 4), flows)


def collect(entries, n_runs):
    """Newest-first, take runs matching the newest one's signature."""
    usable = [e for e in entries
              if isinstance(e, dict) and (e.get('payload') or {}).get('results')]
    if not usable:
        return [], None
    newest = usable[-1]['payload']
    sig = run_signature(newest)
    picked = []
    for e in reversed(usable):
        if run_signature(e['payload']) == sig:
            picked.append(e['payload'])
        if len(picked) >= n_runs:
            break
    return picked, sig


def merge_points(runs, min_conf):
    """One row per flow, averaged across runs, with the spread kept."""
    by_flow = {}
    for p in runs:
        for r in p['results']:
            if r.get('k_s') is None:
                continue
            q = round(r['point']['flow_mm3_s'], 4)
            by_flow.setdefault(q, {'k': [], 'conf': [], 'n': 0,
                                   'point': r['point']})
            by_flow[q]['k'].append(r['k_s'])
            by_flow[q]['conf'].append(r['confidence'])
            by_flow[q]['n'] += r.get('n') or 0
    out = []
    for q in sorted(by_flow):
        d = by_flow[q]
        ks = d['k']
        mean = sum(ks) / len(ks)
        sd = (math.sqrt(sum((v - mean) ** 2 for v in ks) / (len(ks) - 1))
              if len(ks) > 1 else 0.0)
        conf = _med(d['conf'])
        out.append({'flow_mm3_s': q, 'k_s': mean, 'sd': sd,
                    'se': sd / math.sqrt(len(ks)) if len(ks) > 1 else 0.0,
                    'runs': len(ks), 'n': d['n'], 'confidence': conf,
                    'used': conf >= min_conf,
                    'v_xy_mm_s': d['point'].get('v_xy_mm_s')})
    return out


def orca_model(points, accel_lo, accel_hi):
    """The adaptive_pressure_advance_model string: PA,flow,accel per row."""
    rows = []
    for a in (accel_lo, accel_hi):
        for p in points:
            rows.append("%.4f,%.2f,%.0f" % (p['k_s'], p['flow_mm3_s'], a))
    return "\n".join(rows)


class QidiPATable:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        out_dir = config.get('out_dir', '~/printer_data/qidi_pa')
        self.out_dir = os.path.expanduser(out_dir)
        self.src_path = os.path.join(self.out_dir, 'pa_table.json')
        self.report_path = os.path.join(self.out_dir, 'orca_pa.json')
        self.gcode.register_command('QIDI_PA_TABLE', self.cmd_TABLE,
                                    desc=self.cmd_TABLE_help)

    def _record(self, payload):
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            tmp = self.report_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump({'kind': 'orca_pa', 'time': time.time(),
                           'payload': payload}, f, indent=1, default=repr)
            os.replace(tmp, self.report_path)
            return True
        except Exception:
            logging.exception("qidi_pa_table: could not write report")
            return False

    cmd_TABLE_help = ("Stage 4 - turn measured K values into an OrcaSlicer "
                      "adaptive_pressure_advance_model. Pure maths, no motion. "
                      "[RUNS=1] [MIN_CONF=45] [ACCEL_LO=500] [ACCEL_HI=10000]")

    def cmd_TABLE(self, gcmd):
        err_cls = type(gcmd.error("probe"))
        try:
            return self._run(gcmd)
        except err_cls:
            raise
        except Exception as e:
            logging.exception("qidi_pa_table: unhandled error")
            raise gcmd.error("pa_table: internal error, printer left running: "
                             "%s: %s" % (type(e).__name__, str(e)[:160]))

    def _run(self, gcmd):
        n_runs = gcmd.get_int('RUNS', 1, minval=1, maxval=MAX_RUNS)
        min_conf = gcmd.get_int('MIN_CONF', DEFAULT_MIN_CONF, minval=0,
                                maxval=100)
        a_lo = gcmd.get_float('ACCEL_LO', DEFAULT_ACCEL_LO, above=0.)
        a_hi = gcmd.get_float('ACCEL_HI', DEFAULT_ACCEL_HI, above=0.)
        if a_hi <= a_lo:
            raise gcmd.error("pa_table: ACCEL_HI must exceed ACCEL_LO")

        try:
            with open(self.src_path) as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = [data]
        except Exception:
            raise gcmd.error("pa_table: cannot read %s - run QIDI_PA_MEASURE "
                             "first" % (self.src_path,))

        runs, sig = collect(data, n_runs)
        if not runs:
            raise gcmd.error("pa_table: no run in %s has any usable result"
                             % (self.src_path,))
        pts = merge_points(runs, min_conf)
        if not pts:
            raise gcmd.error("pa_table: no points survived")

        geom = runs[0].get('geometry') or {}
        gcmd.respond_info("pa_table: %d run(s) averaged, %.0f ms legs, %d "
                          "cycles x %d blocks, dv %.0f%%"
                          % (len(runs), runs[0].get('leg_s', 0) * 1000.0,
                             runs[0].get('cycles') or 0,
                             runs[0].get('blocks') or 0,
                             100.0 * runs[0].get('amplitude_frac', 0.25)))
        if len(runs) < n_runs:
            gcmd.respond_info("pa_table: only %d of the %d requested runs share "
                              "this configuration - the rest were measured "
                              "differently and averaging them would be wrong"
                              % (len(runs), n_runs))

        gcmd.respond_info("  Q mm3/s   v_XY mm/s        K       sd       SE  "
                          "runs  conf")
        for p in pts:
            gcmd.respond_info("  %7.2f %10s   %.4f  %7.4f  %7.4f  %4d  %4d%s"
                              % (p['flow_mm3_s'],
                                 ("%.1f" % p['v_xy_mm_s']) if p['v_xy_mm_s']
                                 else "-", p['k_s'], p['sd'], p['se'],
                                 p['runs'], p['confidence'],
                                 "" if p['used'] else "   DROPPED"))

        used = [p for p in pts if p['used']]
        dropped = [p for p in pts if not p['used']]
        if dropped:
            gcmd.respond_info("pa_table: %d point(s) below confidence %d were "
                              "dropped rather than trusted"
                              % (len(dropped), min_conf))
        if len(used) < 2:
            raise gcmd.error("pa_table: only %d point(s) scored above %d - not "
                             "enough for a table. Re-measure with more BLOCKS."
                             % (len(used), min_conf))

        model = orca_model(used, a_lo, a_hi)
        ks = [p['k_s'] for p in used]
        fallback = _med(ks)
        span = max(ks) / min(ks) if min(ks) > 0 else 0.0

        gcmd.respond_info("pa_table: K spans %.4f to %.4f = %.2f:1 across the "
                          "flow range" % (min(ks), max(ks), span))
        gcmd.respond_info("pa_table: a single value of %.4f would be %+.0f%% "
                          "wrong at the lowest flow and %+.0f%% at the highest"
                          % (fallback, 100.0 * (fallback / max(ks) - 1),
                             100.0 * (fallback / min(ks) - 1)))
        if geom:
            gcmd.respond_info(
                "pa_table: VALID ONLY for %.2f layer / %.2f width. The flow "
                "axis transfers between geometries but was measured at this "
                "one; regenerate if either changes."
                % (geom.get('layer_h', 0), geom.get('line_w', 0)))

        gcmd.respond_info("---- paste into Orca: Filament > Advanced ----")
        gcmd.respond_info("adaptive_pressure_advance = 1")
        gcmd.respond_info("pressure_advance = %.4f" % (fallback,))
        gcmd.respond_info("adaptive_pressure_advance_model =")
        for line in model.split("\n"):
            gcmd.respond_info("  " + line)
        gcmd.respond_info("---- end ----")

        ok = self._record({
            'model': model, 'rows': model.split("\n"),
            'fallback_pressure_advance': fallback,
            'accel_lo': a_lo, 'accel_hi': a_hi,
            'min_confidence': min_conf, 'runs_averaged': len(runs),
            'span_ratio': span, 'geometry': geom, 'points': pts,
            'note': 'accel columns carry the same K at both values because '
                    'Stage 3 found no dependence of K on acceleration'})
        gcmd.respond_info("pa_table: written to %s"
                          % (self.report_path if ok else "(not written)",))
        # Stage 4 prints results on its own when run outside QIDI_AUTO_CALIBRATE,
        # so the links belong here too. Imported rather than copied so the two
        # cannot drift; absent if only this module was installed.
        for line in SUPPORT_LINES:
            gcmd.respond_info(line)


def load_config(config):
    return QidiPATable(config)
