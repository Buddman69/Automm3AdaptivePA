# qidi_cs_timing.py - what actually costs the ~13 ms per load-cell read?
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
# WHY
#   The "~77 Hz" figure everywhere in this project is the LOOP rate of
#   QIDI_CS_READ, which calls reactor.pause() every iteration. The bare
#   read_origin_data() call has never been timed on its own. Three candidates,
#   with very different ceilings:
#
#     MCU burst of 12 conversions   12 x 0.781 ms = 9.375 ms   -> ~105 Hz, no win
#     Klipper query round trip      ~1-2 ms                    -> ~500 Hz
#     our own reactor overhead      removable                  -> free win
#
#   The 9.375 ms arithmetic lands suspiciously close to the observed 12.9 ms
#   period, and would also explain why read_len does nothing (see
#   the MCU always runs a fixed burst and returns one filtered
#   number regardless of what is asked for.
#
#   Timing the bare call separates them.
#
# ANSWER - AND THE FIRST VERSION OF IT WAS WRONG. READ THE CORRECTION BELOW.
#
# CORRECTION 2026-09-15
#   The decomposition below (13 conversions + 0.159 ms round trip) assumed the
#   round trip rather than measuring it. QIDI_CS_BULK CMP measured it directly:
#   a Klipper query round trip on this link is 2.50 ms, and the raw
#   query_cs1237_read returns a fresh sample in 2.98 ms - 0.17 conversions of
#   waiting, not 13. read_origin_data() adds ~8 ms of its own on top.
#
#   So this module measures what read_origin_data() costs, which is real and
#   worth knowing, but its verdict text attributes that cost to the MCU and that
#   attribution is wrong. The raw command reaches ~312 Hz of valid samples.
#   The chip free-runs into a sample buffer; a read takes whatever is there.
#
#   The lag-1 independence result and the DRDY grid-lock result both stand.
#
# The original (superseded) answer, over 600 calls, cold and idle:
#
#     floor        10.315 ms = 13 x 0.78125 (10.156) + 0.159 ms round trip
#     median       10.931 ms - about one conversion period above the floor,
#                             which is the phase wait for the first ready edge
#     sustained    87.3 Hz unpaced, against 77 Hz paced
#     Rayleigh p   3.8e-29 - durations really are locked to the 0.78125 ms grid
#     lag-1 r      -0.076 - consecutive reads are INDEPENDENT
#
#   13 is MAX_SAMPLES_PER_BLOCK from cs1237.so, not blocks_per_msg = 12.
#   Round-trip latency is 1.5% of the call, so there is nothing for an async or
#   pipelined path to recover. The sample-rate question is closed at ~87-97 Hz.
#
# TWO FREE EXTRAS
#   1. DRDY quantisation. If the MCU waits on conversion-ready edges, call
#      durations land on a 0.78125 ms grid. Measured as circular concentration
#      R over the fractional part of duration/0.78125. R high is strong evidence
#      of a DRDY-locked read; R low proves nothing, because host jitter alone
#      can smear the grid.
#   2. Where cs_fil_f = 0.9 lives - still an open question, never
#      answered. If the 0.9 IIR carries state ACROSS calls, consecutive readings
#      are correlated at about r = 0.9. If it is applied WITHIN a burst and
#      reset each call, consecutive readings are independent, r near 0.
#      Lag-1 autocorrelation of the linearly detrended series settles it, and
#      decides whether averaging polled samples gains sqrt(n) or almost nothing.
#
# SAFETY
#   Read-only. No motion, no heating, no extrusion, no config write, nothing
#   near cs1237_setup_home. It calls read_origin_data() in a loop - the same
#   call a flow run already makes thousands of times - while the machine sits
#   cold and idle. The call yields to the reactor internally while waiting for
#   the MCU reply, so a tight loop does not starve Klipper.
#
# USAGE
#   [qidi_cs_timing]
#
#   QIDI_CS_TIMING                 default 400 calls, 3 phases
#   QIDI_CS_TIMING N=1000          more calls
#   QIDI_CS_TIMING PACED_HZ=200    what the paced loop is asked for in phase 2
#
#   Results are appended to ~/printer_data/qidi_pa/timing.json

import json
import logging
import math
import os
import time

SAMPLE_PERIOD_MS = 1000.0 / 1280.0      # 0.78125 ms at the configured rate

DEFAULT_N = 400
MAX_N = 4000
DEFAULT_MAX_S = 12.0
DEFAULT_PACED_HZ = 200.0
PAUSE_PROBE_N = 200

# Verdict bands on the median bare-call duration, in ms.
LATENCY_BOUND_MS = 4.0                  # below this, the MCU is not the cost
BURST_LO_MS = 7.5                       # a 12-conversion burst is 9.375
BURST_HI_MS = 11.5


def _stats(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)

    def pct(p):
        if n == 1:
            return s[0]
        k = p * (n - 1) / 100.0
        lo = int(math.floor(k))
        hi = min(lo + 1, n - 1)
        return s[lo] + (s[hi] - s[lo]) * (k - lo)

    mean = sum(s) / n
    var = sum((x - mean) ** 2 for x in s) / n
    return {'n': n, 'min': s[0], 'p10': pct(10), 'median': pct(50),
            'p90': pct(90), 'max': s[-1], 'mean': mean,
            'sd': math.sqrt(var)}


def _grid_concentration(durations_ms, period_ms=SAMPLE_PERIOD_MS):
    # Circular concentration of duration modulo the conversion period.
    # 1.0 = perfectly locked to the grid, 0.0 = uniformly smeared.
    if len(durations_ms) < 8:
        return None
    c = s = 0.0
    for d in durations_ms:
        ang = 2.0 * math.pi * ((d / period_ms) % 1.0)
        c += math.cos(ang)
        s += math.sin(ang)
    n = float(len(durations_ms))
    return math.sqrt((c / n) ** 2 + (s / n) ** 2)


def _rayleigh(r, n):
    # A fixed threshold on R is wrong: R falls as 1/sqrt(n) under pure chance,
    # so what counts as "locked" depends on how many calls were timed. Rayleigh
    # gives the probability of seeing this much concentration by chance, and
    # E[R] under uniform is the yardstick to quote alongside it.
    if r is None or n < 8:
        return None, None
    return math.exp(-n * r * r), 0.886 / math.sqrt(n)


def _burst_fit(min_ms, period_ms=SAMPLE_PERIOD_MS):
    # The FLOOR is the clean estimator of the burst length: the fastest call is
    # the one that waited least for the first conversion-ready edge. Everything
    # above it is phase wait plus host jitter.
    k = int(round(min_ms / period_ms))
    return k, min_ms - k * period_ms


def _detrend(vals):
    # Baseline wander is ~1-1.6 gf/min and would otherwise
    # show up as correlation that has nothing to do with the filter.
    n = len(vals)
    if n < 3:
        return list(vals)
    mx = (n - 1) / 2.0
    my = sum(vals) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    if sxx <= 0:
        return [v - my for v in vals]
    sxy = sum((i - mx) * (vals[i] - my) for i in range(n))
    b = sxy / sxx
    return [vals[i] - (my + b * (i - mx)) for i in range(n)]


def _lag1(vals):
    r = _detrend(vals)
    n = len(r)
    if n < 8:
        return None
    denom = sum(x * x for x in r)
    if denom <= 0:
        return None
    num = sum(r[i] * r[i + 1] for i in range(n - 1))
    return num / denom


class QidiCSTiming:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.root_name = config.get('root', 'probe_air')
        self.sensor_attr = config.get('sensor_attr', 'sensor_helper')
        out_dir = config.get('out_dir', '~/printer_data/qidi_pa')
        self.out_dir = os.path.expanduser(out_dir)
        self.report_path = os.path.join(self.out_dir, 'timing.json')
        # Injectable so the off-printer tests can drive a deterministic clock.
        self.clock = time.monotonic
        self.gcode.register_command('QIDI_CS_TIMING', self.cmd_TIMING,
                                    desc=self.cmd_TIMING_help)

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
            logging.exception("qidi_cs_timing: unhandled error")
            raise gcmd.error("timing: internal error, printer left running: "
                             "%s: %s" % (type(e).__name__, str(e)[:160]))

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
            data.append({'kind': 'timing', 'time': time.time(),
                         'payload': payload})
            tmp = self.report_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=1, default=repr)
            os.replace(tmp, self.report_path)
            return True
        except Exception:
            logging.exception("qidi_cs_timing: could not write report")
            return False

    def _reader(self, gcmd):
        root = self.printer.lookup_object(self.root_name, None)
        if root is None:
            raise gcmd.error("timing: no object %s" % (self.root_name,))
        sensor = getattr(root, self.sensor_attr, None)
        if sensor is None:
            raise gcmd.error("timing: %s has no %s"
                             % (self.root_name, self.sensor_attr))
        read = getattr(sensor, 'read_origin_data', None)
        if read is None:
            raise gcmd.error("timing: no read_origin_data on the sensor")
        if not callable(read):
            raise gcmd.error("timing: read_origin_data is not callable here - "
                             "cannot time an attribute access")
        return read

    # -- phase 1: the bare call, no pacing at all ---------------------------
    def _phase_bare(self, gcmd, read, n, max_s):
        durs, vals = [], []
        errors = 0
        t_start = self.clock()
        while len(durs) < n:
            t0 = self.clock()
            if t0 - t_start > max_s:
                break
            try:
                v = read()
            except Exception as e:
                errors += 1
                if errors > 5:
                    raise gcmd.error("timing: giving up after %d read errors: "
                                     "%s" % (errors, e))
                continue
            t1 = self.clock()
            durs.append((t1 - t0) * 1000.0)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(float(v))
        elapsed = self.clock() - t_start
        return durs, vals, errors, elapsed

    # -- phase 2: paced the way QIDI_CS_READ paces it ----------------------
    def _phase_paced(self, read, hz, n, max_s):
        interval = 1.0 / hz
        durs = []
        t_start = self.clock()
        eventtime = self.reactor.monotonic()
        while len(durs) < n:
            if self.clock() - t_start > max_s:
                break
            t0 = self.clock()
            try:
                read()
            except Exception:
                pass
            t1 = self.clock()
            durs.append((t1 - t0) * 1000.0)
            eventtime = self.reactor.pause(eventtime + interval)
        elapsed = self.clock() - t_start
        return durs, elapsed

    # -- phase 3: what reactor.pause alone costs ---------------------------
    def _phase_pause(self, hz, n):
        interval = 1.0 / hz
        t_start = self.clock()
        eventtime = self.reactor.monotonic()
        for _ in range(n):
            eventtime = self.reactor.pause(eventtime + interval)
        return self.clock() - t_start

    cmd_TIMING_help = ("Time the bare read_origin_data() call to find the real "
                       "sample-rate ceiling. Read-only: no motion, heating or "
                       "extrusion. [N=400] [PACED_HZ=200] [MAX_S=12]")

    def cmd_TIMING(self, gcmd):
        return self._guard(gcmd, self._run_TIMING)

    def _run_TIMING(self, gcmd):
        read = self._reader(gcmd)
        n = gcmd.get_int('N', DEFAULT_N, minval=20, maxval=MAX_N)
        paced_hz = gcmd.get_float('PACED_HZ', DEFAULT_PACED_HZ, above=0.)
        max_s = gcmd.get_float('MAX_S', DEFAULT_MAX_S, above=0.)

        gcmd.respond_info("timing: %d calls, no pacing. Read-only - nothing "
                          "moves or heats. Keep hands off the nozzle." % (n,))

        durs, vals, errors, elapsed = self._phase_bare(gcmd, read, n, max_s)
        if len(durs) < 8:
            raise gcmd.error("timing: only %d calls completed - cannot analyse"
                             % (len(durs),))
        st = _stats(durs)
        rate = len(durs) / elapsed if elapsed > 0 else 0.0

        gcmd.respond_info("  bare call ms:  min %.2f  p10 %.2f  median %.2f  "
                          "p90 %.2f  max %.2f"
                          % (st['min'], st['p10'], st['median'], st['p90'],
                             st['max']))
        gcmd.respond_info("  mean %.2f ms  sd %.2f ms  -> %.1f Hz sustained "
                          "over %.2f s (%d errors)"
                          % (st['mean'], st['sd'], rate, elapsed, errors))

        conv = st['median'] / SAMPLE_PERIOD_MS
        gcmd.respond_info("  median = %.2f conversion periods of %.3f ms"
                          % (conv, SAMPLE_PERIOD_MS))

        k_burst, rt_ms = _burst_fit(st['min'])
        gcmd.respond_info("  floor %.3f ms = %d conversions (%.3f) + %.3f ms "
                          "round trip" % (st['min'], k_burst,
                                          k_burst * SAMPLE_PERIOD_MS, rt_ms))

        grid = _grid_concentration(durs)
        p_ray, r_unif = _rayleigh(grid, len(durs))
        if grid is not None:
            locked = p_ray is not None and p_ray < 1e-3
            gcmd.respond_info("  DRDY grid R = %.3f vs %.3f expected by chance, "
                              "Rayleigh p = %.2g - %s"
                              % (grid, r_unif, p_ray,
                                 "durations are locked to the conversion grid"
                                 if locked else
                                 "no lock - inconclusive, host jitter alone "
                                 "can smear it"))

        r1 = _lag1(vals) if len(vals) >= 8 else None
        if r1 is not None:
            if r1 > 0.6:
                filt = ("the 0.9 IIR carries ACROSS calls - consecutive reads "
                        "are not independent, so averaging them gains far less "
                        "than sqrt(n)")
            elif r1 < 0.25:
                filt = ("consecutive reads are independent - the filter is "
                        "reset or applied within a burst, so averaging works")
            else:
                filt = "partial correlation - inconclusive"
            gcmd.respond_info("  lag-1 autocorrelation %.3f: %s" % (r1, filt))

        paced_durs, paced_elapsed = self._phase_paced(read, paced_hz,
                                                      min(n, 200), max_s)
        paced_rate = (len(paced_durs) / paced_elapsed
                      if paced_elapsed > 0 else 0.0)
        pause_only = self._phase_pause(paced_hz, PAUSE_PROBE_N)
        pause_ms = pause_only * 1000.0 / PAUSE_PROBE_N
        gcmd.respond_info("  paced loop asked for %.0f Hz, achieved %.1f Hz "
                          "(QIDI_CS_READ measured ~77 Hz this way)"
                          % (paced_hz, paced_rate))
        gcmd.respond_info("  reactor.pause alone at %.0f Hz: %.2f ms per "
                          "iteration" % (paced_hz, pause_ms))

        med = st['median']
        if med < LATENCY_BOUND_MS:
            verdict = ("LATENCY-BOUND. The MCU is not the cost - %.2f ms per "
                       "call is ~%.0f Hz. Worth building the async path, and "
                       "sample placement uncertainty drops from +/-13 ms to "
                       "about +/-%.0f ms, which matters more than the extra "
                       "samples." % (med, 1000.0 / med, med))
        elif BURST_LO_MS <= med <= BURST_HI_MS:
            verdict = ("MCU BURST of %d conversions. The floor is %.2f ms = "
                       "%d x %.3f ms + %.2f ms round trip, and the median %.2f "
                       "ms sits about one conversion period above it - the "
                       "phase wait for the first ready edge. Round-trip latency "
                       "is only %.2f ms, so pipelining or an async path would "
                       "recover %.1f%% and is not worth building. Ceiling is "
                       "~%.0f Hz. Stage 3 proceeds as designed and the "
                       "sample-rate question is closed."
                       % (k_burst, st['min'], k_burst, SAMPLE_PERIOD_MS, rt_ms,
                          med, rt_ms, 100.0 * rt_ms / med, 1000.0 / st['min']))
        else:
            verdict = ("NEITHER BAND. %.2f ms = %.1f conversion periods. Not "
                       "the 9.375 ms burst and not round-trip latency - read "
                       "the percentiles above before drawing a conclusion."
                       % (med, conv))
        gcmd.respond_info("timing: %s" % (verdict,))

        overhead = 0.0
        if paced_rate > 0 and rate > 0:
            overhead = (1000.0 / paced_rate) - (1000.0 / rate)
        if overhead > 0.5:
            gcmd.respond_info("timing: pacing costs %.2f ms per iteration on "
                              "top of the call itself - that much of the 77 Hz "
                              "was our own loop, not the sensor." % (overhead,))

        ok = self._record({
            'n_requested': n, 'paced_hz': paced_hz,
            'bare': {'stats': st, 'rate_hz': rate, 'elapsed_s': elapsed,
                     'errors': errors,
                     'conversions_per_call': conv,
                     'burst_conversions': k_burst,
                     'round_trip_ms': rt_ms,
                     'grid_concentration': grid,
                     'rayleigh_p': p_ray,
                     'lag1_autocorr': r1,
                     'durations_ms': [round(d, 4) for d in durs[:512]],
                     'values': [round(v, 1) for v in vals[:512]]},
            'paced': {'stats': _stats(paced_durs), 'rate_hz': paced_rate,
                      'elapsed_s': paced_elapsed},
            'pause_only_ms_per_iter': pause_ms,
            'sample_period_ms': SAMPLE_PERIOD_MS,
            'verdict': verdict})
        gcmd.respond_info("timing: written to %s"
                          % (self.report_path if ok else "(not written)",))


def load_config(config):
    return QidiCSTiming(config)
