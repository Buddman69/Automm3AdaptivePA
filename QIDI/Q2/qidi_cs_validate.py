# qidi_cs_validate.py - can the raw read path be trusted, and does it survive
#                       a busy THR link?
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
# WHY
#   query_cs1237_read reaches ~312 Hz against read_origin_data()'s 91, with a
#   ~0 group delay instead of 0-5.1 ms. Stage 3 wants it. But
#   every safety number this project has - the 1650 gf abort, the 35 gf variance
#   abort, 201 counts/gf on this Q2 - was established through read_origin_data(). The
#   raw command skips whatever those 8 ms do, possibly including cs_fil_f = 0.9
#   and drift compensation.
#
#   Safety rule 2: our abort is not the first line of defence, it is the ONLY
#   one. So the raw path does not get to hold an abort until it is shown to read
#   the same force, on the same scale, as the path everything was calibrated on.
#
# WHY THIS IS A COLD TEST AND NOT A HOT ONE
#   Melt pressure would give 400-800 gf for free, but it is not needed. We are
#   not calibrating against an external standard - we are checking that two
#   readouts of the SAME load cell agree. Whatever the force is doing, both
#   paths see it identically, so the +/-8 gf creep that ruined the original
#   wedge calibration is COMMON MODE here and cancels exactly.
#
#   A hand press is therefore not merely adequate, it is better: it sweeps 0 to
#   ~2000 gf in one pass, where melt pressure only visits a few fixed flows.
#
# WHY lis2dw IS THE LOAD GENERATOR
#   The contention that matters is on the THR link. Checked in printer.cfg:
#
#       stepper_z   step_pin PC10       <- bare pin, MAIN mcu
#       extruder    step_pin THR:PB9    <- toolhead
#       lis2dw      cs_pin   THR:PA10   <- toolhead
#
#   So a bed level would load the wrong MCU entirely. Meanwhile lis2dw streams
#   1600 Hz x 6 bytes = 9.6 kB/s, about 19% of the 500000 baud link, while
#   extruder stepping compresses into queue_step interval/count/add and costs
#   well under 1 kB/s. lis2dw is roughly 10x the extruder's traffic, so it is a
#   deliberately CONSERVATIVE proxy - survive it and extrusion is not a concern.
#
# SAFETY
#   No motion, no heating, no extrusion, no config write, nothing near
#   cs1237_setup_home. ACCELEROMETER_MEASURE is stock Klipper and only streams;
#   it is started and stopped from a finally: block. Any unexpected exception is
#   converted to a command error rather than being allowed to become a Klipper
#   INTERNAL ERROR, which shuts down every MCU - that happened for real on
#   2026-09-15 and is not repeating.
#
# USAGE
#   [qidi_cs_validate]
#
#   QIDI_CS_PAIR SECS=25      interleave both paths - PRESS THE NOZZLE during it
#   QIDI_CS_LOAD SECS=4       torn reads and RTT, idle vs lis2dw streaming
#
#   Results go to ~/printer_data/qidi_pa/validate.json

import json
import logging
import math
import os
import time

# Q2 value - measured on one Q2 across 15 points, 8.5 gf to 2063 gf. The
# X-Max 4 cell measured 182.96.
COUNTS_PER_GF = 201.0

# A torn read is orders of magnitude out of band (-33, -9, -8113281 observed
# against a -388751 baseline). 50000 counts is 273 gf; between two samples 3 ms
# apart that is 91,000 gf/s, which no hand press approaches. So this gate
# catches tearing without ever rejecting real force.
TORN_GATE_COUNTS = 50000

DEFAULT_PAIR_SECS = 25.0
DEFAULT_LOAD_SECS = 4.0
MAX_SECS = 60.0
MIN_SPREAD_GF = 200.0       # below this the scale regression is meaningless


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


def _pct(xs, q):
    s = sorted(xs)
    if not s:
        return 0.0
    k = q * (len(s) - 1) / 100.0
    lo = int(math.floor(k))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _regress(xs, ys):
    # ys = slope*xs + intercept
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (ys[i] - my) for i, x in enumerate(xs))
    slope = sxy / sxx
    inter = my - slope * mx
    syy = sum((y - my) ** 2 for y in ys)
    resid = sum((ys[i] - (inter + slope * x)) ** 2 for i, x in enumerate(xs))
    r2 = 1.0 - resid / syy if syy > 0 else 0.0
    return {'slope': slope, 'intercept': inter, 'r2': r2,
            'rms_resid': math.sqrt(resid / n)}


def _decode24le(blob):
    if len(blob) < 3:
        return None
    v = blob[0] | (blob[1] << 8) | (blob[2] << 16)
    return v - 0x1000000 if v & 0x800000 else v


class QidiCSValidate:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.root_name = config.get('root', 'probe_air')
        self.sensor_attr = config.get('sensor_attr', 'sensor_helper')
        self.accel_cmd = config.get('accel_cmd', 'ACCELEROMETER_MEASURE')
        out_dir = config.get('out_dir', '~/printer_data/qidi_pa')
        self.out_dir = os.path.expanduser(out_dir)
        self.report_path = os.path.join(self.out_dir, 'validate.json')
        self.gcode.register_command('QIDI_CS_PAIR', self.cmd_PAIR,
                                    desc=self.cmd_PAIR_help)
        self.gcode.register_command('QIDI_CS_LOAD', self.cmd_LOAD,
                                    desc=self.cmd_LOAD_help)

    # -- plumbing ----------------------------------------------------------
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
            data.append({'kind': 'validate', 'time': time.time(),
                         'payload': payload})
            tmp = self.report_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=1, default=repr)
            os.replace(tmp, self.report_path)
            return True
        except Exception:
            logging.exception("qidi_cs_validate: could not write report")
            return False

    def _guard(self, gcmd, fn):
        # A stray exception in a gcode command is an INTERNAL ERROR to Klipper
        # and shuts down every MCU. Convert anything unexpected into a clean
        # command failure instead.
        err_cls = type(gcmd.error("probe"))
        try:
            return fn(gcmd)
        except err_cls:
            raise
        except Exception as e:
            logging.exception("qidi_cs_validate: unhandled error")
            raise gcmd.error("validate: internal error, printer left running: "
                             "%s: %s" % (type(e).__name__, str(e)[:160]))

    def _parts(self, gcmd):
        root = self.printer.lookup_object(self.root_name, None)
        if root is None:
            raise gcmd.error("validate: no object %s" % (self.root_name,))
        sensor = getattr(root, self.sensor_attr, None)
        if sensor is None:
            raise gcmd.error("validate: %s has no %s"
                             % (self.root_name, self.sensor_attr))
        read = getattr(sensor, 'read_origin_data', None)
        raw = getattr(sensor, 'query_cs1237_end_cmd', None)
        oid = getattr(sensor, 'oid', None)
        if not callable(read):
            raise gcmd.error("validate: read_origin_data is not callable")
        if raw is None or oid is None:
            raise gcmd.error("validate: no query_cs1237_end_cmd or oid")
        return sensor, read, raw, oid

    def _raw(self, raw, oid):
        """One raw sample: (value, t_mid, rtt_ms) or (None, t, rtt) if torn."""
        t0 = self.reactor.monotonic()
        resp = raw.send([oid, 0, 4])
        t1 = self.reactor.monotonic()
        rtt = (t1 - t0) * 1000.0
        blob = resp.get('data') if isinstance(resp, dict) else None
        if isinstance(blob, str):
            blob = blob.encode('latin-1', 'replace')
        v = _decode24le(blob) if blob else None
        # The calibrated timestamp from QIDI_CS_CLOCK: the MCU answers about 27%
        # of the way through the round trip, and the buffered conversion is on
        # average 0.391 ms old.
        t = t0 + 0.27 * (t1 - t0) - 0.000391
        return v, t, rtt

    # -- test A: do the two paths agree? -----------------------------------
    cmd_PAIR_help = ("Interleave read_origin_data() and the raw command while "
                     "you press the nozzle, to check they read the same force "
                     "on the same scale. Cold, read-only. [SECS=25]")

    def cmd_PAIR(self, gcmd):
        return self._guard(gcmd, self._run_pair)

    def _run_pair(self, gcmd):
        sensor, read, raw, oid = self._parts(gcmd)
        secs = gcmd.get_float('SECS', DEFAULT_PAIR_SECS, above=1.,
                              below=MAX_SECS)
        gcmd.respond_info(
            "validate: %.0f s. PRESS THE NOZZLE now - gently, and vary the "
            "force smoothly from nothing up to a firm push and back. Sweeping "
            "the range matters more than holding any value steady." % (secs,))

        rows = []
        torn = 0
        t_end = self.reactor.monotonic() + secs
        while self.reactor.monotonic() < t_end:
            # Sandwich: raw, wrapper, raw. The wrapper call takes ~11 ms, so a
            # changing force would otherwise show up as a fake offset. The two
            # raw samples bracket it and get interpolated to its midpoint.
            v1, t1, _ = self._raw(raw, oid)
            tw0 = self.reactor.monotonic()
            w = read()
            tw1 = self.reactor.monotonic()
            v2, t2, _ = self._raw(raw, oid)
            if not isinstance(w, (int, float)) or isinstance(w, bool):
                continue
            w = float(w)
            # The wrapper reading is always valid, so it re-seeds the gate every
            # triple - a torn value can never poison the reference.
            if v1 is None or abs(v1 - w) > TORN_GATE_COUNTS:
                torn += 1
                v1 = None
            if v2 is None or abs(v2 - w) > TORN_GATE_COUNTS:
                torn += 1
                v2 = None
            if v1 is None or v2 is None or t2 <= t1:
                continue
            tw = 0.5 * (tw0 + tw1)
            f = (tw - t1) / (t2 - t1)
            v_interp = v1 + (v2 - v1) * f
            rows.append({'w': w, 'r': v_interp, 'slew': (v2 - v1) / (t2 - t1),
                         't': tw})

        n = len(rows)
        if n < 20:
            raise gcmd.error("validate: only %d usable triples - was the "
                             "command interrupted?" % (n,))

        ws = [x['w'] for x in rows]
        rs = [x['r'] for x in rows]
        diffs = [(x['r'] - x['w']) / COUNTS_PER_GF for x in rows]
        spread_gf = (max(ws) - min(ws)) / COUNTS_PER_GF
        slews = [abs(x['slew']) / COUNTS_PER_GF for x in rows]

        gcmd.respond_info("  %d triples, %d torn raw reads (%.1f%%)"
                          % (n, torn, 100.0 * torn / max(2 * n + torn, 1)))
        gcmd.respond_info("  force swept %.0f gf  (%.0f to %.0f gf relative to "
                          "the start)"
                          % (spread_gf, (min(ws) - ws[0]) / COUNTS_PER_GF,
                             (max(ws) - ws[0]) / COUNTS_PER_GF))
        gcmd.respond_info("  slew rate: median %.0f gf/s, p90 %.0f gf/s"
                          % (_med(slews), _pct(slews, 90)))
        gcmd.respond_info("  raw - wrapper: median %+.3f gf   MAD %.3f gf"
                          % (_med(diffs), _mad(diffs)))

        reg = _regress(ws, rs)
        if reg is None:
            raise gcmd.error("validate: could not fit the two paths")
        gcmd.respond_info("  scale: raw = %.6f x wrapper %+.0f counts   "
                          "R2 %.6f   rms resid %.2f gf"
                          % (reg['slope'], reg['intercept'], reg['r2'],
                             reg['rms_resid'] / COUNTS_PER_GF))

        verdict = []
        if spread_gf < MIN_SPREAD_GF:
            verdict.append(
                "INCONCLUSIVE ON SCALE - only %.0f gf of range was swept. The "
                "slope needs a real spread to mean anything; press harder and "
                "over a wider range, then repeat." % (spread_gf,))
        else:
            dev = abs(reg['slope'] - 1.0) * 100.0
            if dev < 0.5:
                verdict.append(
                    "SAME SCALE to %.2f%% over %.0f gf - %.0f counts/gf and "
                    "every threshold derived from it transfer to the raw path "
                    "unchanged." % (dev, spread_gf, COUNTS_PER_GF))
            else:
                verdict.append(
                    "SCALES DIFFER by %.2f%%. The raw path needs its own "
                    "calibration before it can hold any abort." % (dev,))
        off = _med(diffs)
        if abs(off) < 1.0:
            verdict.append("Same tare: %+.3f gf apart, inside the 0.58 gf "
                           "noise floor." % (off,))
        else:
            verdict.append("TARE OFFSET %+.3f gf between the paths - a fresh "
                           "tare must be taken on whichever path is used, not "
                           "shared between them." % (off,))
        for v in verdict:
            gcmd.respond_info("validate: %s" % (v,))

        self._record({'mode': 'pair', 'n': n, 'torn': torn,
                      'spread_gf': spread_gf,
                      'diff_gf': {'median': _med(diffs), 'mad': _mad(diffs),
                                  'p05': _pct(diffs, 5), 'p95': _pct(diffs, 95)},
                      'slew_gf_s': {'median': _med(slews),
                                    'p90': _pct(slews, 90)},
                      'regression': reg, 'verdict': verdict,
                      'sample': [[round(x['w'], 1), round(x['r'], 1)]
                                 for x in rows[:256]]})
        gcmd.respond_info("validate: written to %s" % (self.report_path,))

    # -- test B: does the raw path survive a busy link? --------------------
    cmd_LOAD_help = ("Torn-read rate and round trip on the raw path, idle vs "
                     "lis2dw streaming as a THR link load. Cold, read-only, "
                     "no motion. [SECS=4]")

    def cmd_LOAD(self, gcmd):
        return self._guard(gcmd, self._run_load)

    def _run_load(self, gcmd):
        sensor, read, raw, oid = self._parts(gcmd)
        secs = gcmd.get_float('SECS', DEFAULT_LOAD_SECS, above=0.5,
                              below=MAX_SECS)

        def burst(label):
            seed = float(read())
            vals, rtts, torn = [], [], 0
            t_end = self.reactor.monotonic() + secs
            while self.reactor.monotonic() < t_end:
                v, _t, rtt = self._raw(raw, oid)
                rtts.append(rtt)
                if v is None or abs(v - seed) > TORN_GATE_COUNTS:
                    torn += 1
                    continue
                vals.append(v)
                seed = v
            n = len(vals) + torn
            res = {'label': label, 'polls': n, 'valid': len(vals),
                   'torn': torn,
                   'torn_pct': 100.0 * torn / max(n, 1),
                   'rate_hz': n / secs, 'valid_hz': len(vals) / secs,
                   'rtt_median': _med(rtts), 'rtt_p90': _pct(rtts, 90),
                   'rtt_max': max(rtts) if rtts else 0.0}
            gcmd.respond_info(
                "  %-18s %4d polls  %5.1f%% torn  %5.0f Hz valid  "
                "RTT %.2f / p90 %.2f / max %.1f ms"
                % (label, n, res['torn_pct'], res['valid_hz'],
                   res['rtt_median'], res['rtt_p90'], res['rtt_max']))
            return res

        gcmd.respond_info("validate: raw path under load. No motion, no "
                          "heating - lis2dw only streams.")
        idle = burst("idle")

        loaded, started = None, False
        try:
            try:
                self.gcode.run_script_from_command(self.accel_cmd)
                started = True
            except Exception as e:
                gcmd.respond_info("validate: could not start %s (%s) - "
                                  "reporting the idle figures only"
                                  % (self.accel_cmd, str(e)[:80]))
            if started:
                loaded = burst("lis2dw streaming")
        finally:
            if started:
                try:
                    self.gcode.run_script_from_command(self.accel_cmd)
                except Exception as e:
                    gcmd.respond_info("validate: FAILED TO STOP %s: %s - stop "
                                      "it by hand" % (self.accel_cmd,
                                                      str(e)[:80]))

        if loaded:
            d_torn = loaded['torn_pct'] - idle['torn_pct']
            d_rtt = loaded['rtt_median'] - idle['rtt_median']
            gcmd.respond_info("  delta: %+.1f%% torn, %+.2f ms round trip"
                              % (d_torn, d_rtt))
            if loaded['torn_pct'] < 15.0 and loaded['valid_hz'] > 150.0:
                v = ("the raw path survives ~19%% of the THR link being used. "
                     "Extruder stepping is roughly a tenth of that traffic, so "
                     "Stage 3 is clear to use it at %.0f Hz valid."
                     % (loaded['valid_hz'],))
            else:
                v = ("the raw path degrades badly under link load - %.1f%% "
                     "torn, %.0f Hz valid. Stage 3 should stay on "
                     "read_origin_data() or poll in gaps."
                     % (loaded['torn_pct'], loaded['valid_hz']))
            gcmd.respond_info("validate: %s" % (v,))
        else:
            v = "no loaded comparison was taken"

        self._record({'mode': 'load', 'secs': secs, 'idle': idle,
                      'loaded': loaded, 'verdict': v})
        gcmd.respond_info("validate: written to %s" % (self.report_path,))


def load_config(config):
    return QidiCSValidate(config)
