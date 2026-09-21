# qidi_cs_clock.py - where inside a query's round trip does the MCU answer?
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
# WHY
#   query_cs1237_data carries NO timestamp - confirmed against the firmware
#   dictionary, it is `oid=%c data=%*s` and nothing else. Almost every other
#   sensor reply in this firmware has a next_clock; this one does not. So a
#   sample's time has to be derived on the host from t0 (sent) and t1 (reply),
#   and the obvious guess is the midpoint.
#
#   The midpoint is only right if the round trip is symmetric. If the MCU
#   actually answers at 30% or 70% of the way through, the midpoint carries a
#   fixed offset - and a FIXED offset is exactly what Stage 3 cannot tolerate,
#   because it is indistinguishable from extra pressure-advance lag and survives
#   averaging across transitions. Random jitter averages away; this does not.
#
#   get_uptime returns timer_read_time() - the MCU's clock at the instant it
#   handled the command. Klipper's clocksync already maps MCU clock to print
#   time accurately, so comparing that against the host's own t0/t1 measures the
#   asymmetry directly instead of assuming it away.
#
# WHY get_uptime AND NOT get_clock
#   get_clock is the obvious choice and it is a trap. Klipper's ClockSync
#   registers its own handler for the `clock` response and drives the entire
#   host-to-MCU clock estimate from it. serialhdl keys handlers by
#   (name, oid), so creating a second query wrapper for `clock` would REPLACE
#   ClockSync's handler and break clock synchronisation for the whole printer.
#
#   get_uptime returns the same timer_read_time() value. Its handler slot is
#   used once during connect and released, so it is free at runtime. This module
#   still refuses to run if it finds that slot occupied, rather than clobbering
#   whatever is there.
#
# SAFETY
#   Read-only. get_uptime and query_cs1237_read are both pure reads with no
#   side effects - no motion, no heating, no extrusion, no config write, nothing
#   near cs1237_setup_home. The only hazard was the handler collision above, and
#   it is checked for rather than assumed.
#
# USAGE
#   [qidi_cs_clock]
#
#   QIDI_CS_CLOCK            200 get_uptime probes plus an RTT comparison
#   QIDI_CS_CLOCK N=500
#
#   Results are appended to ~/printer_data/qidi_pa/clock.json

import json
import logging
import math
import os
import time

DEFAULT_N = 200
MAX_N = 2000

UPTIME_MSG = 'get_uptime'
UPTIME_RESP = 'uptime high=%u clock=%u'


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
    m = pct(50)
    # A delayed reply puts an enormous value in this distribution - one probe
    # came back 85 ms late. sd is then meaningless (4.56 ms against a p10-p90
    # width of 0.47), so report MAD as the spread and keep sd only to show how
    # far apart the two are.
    amd = sorted(abs(x - m) for x in s)
    mad = 1.4826 * (amd[n // 2] if n % 2 else
                    0.5 * (amd[n // 2 - 1] + amd[n // 2]))
    return {'n': n, 'min': s[0], 'p10': pct(10), 'median': m,
            'p90': pct(90), 'max': s[-1], 'mean': mean, 'sd': math.sqrt(var),
            'mad': mad,
            'outliers_5mad': sum(1 for x in s if mad > 0
                                 and abs(x - m) > 5 * mad)}


class QidiCSClock:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.root_name = config.get('root', 'probe_air')
        self.sensor_attr = config.get('sensor_attr', 'sensor_helper')
        out_dir = config.get('out_dir', '~/printer_data/qidi_pa')
        self.out_dir = os.path.expanduser(out_dir)
        self.report_path = os.path.join(self.out_dir, 'clock.json')
        self._uptime_cmd = None
        self.gcode.register_command('QIDI_CS_CLOCK', self.cmd_CLOCK,
                                    desc=self.cmd_CLOCK_help)

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
            data.append({'kind': 'clock', 'time': time.time(),
                         'payload': payload})
            tmp = self.report_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=1, default=repr)
            os.replace(tmp, self.report_path)
            return True
        except Exception:
            logging.exception("qidi_cs_clock: could not write report")
            return False

    def _sensor(self, gcmd):
        root = self.printer.lookup_object(self.root_name, None)
        if root is None:
            raise gcmd.error("clock: no object %s" % (self.root_name,))
        s = getattr(root, self.sensor_attr, None)
        if s is None:
            raise gcmd.error("clock: %s has no %s" % (self.root_name,
                                                      self.sensor_attr))
        return s

    def _mcu(self, sensor, gcmd):
        get = getattr(sensor, 'get_mcu', None)
        mcu = None
        if callable(get):
            try:
                mcu = get()
            except Exception:
                mcu = None
        if mcu is None:
            mcu = getattr(sensor, 'mcu', None)
        if mcu is None:
            raise gcmd.error("clock: cannot reach the sensor's MCU object")
        return mcu

    def _check_slot_free(self, mcu, gcmd):
        # Never clobber another subsystem's response handler. If anything holds
        # the uptime slot, stop rather than break it.
        serial = getattr(mcu, '_serial', None)
        handlers = getattr(serial, 'handlers', None) if serial else None
        if handlers is None:
            gcmd.respond_info("clock: cannot inspect the handler table - "
                              "proceeding, but see the get_uptime note in the "
                              "module header")
            return
        held = [k for k in handlers if k[0] == 'uptime']
        if held:
            raise gcmd.error(
                "clock: something already handles the 'uptime' response (%s). "
                "Registering over it would break that subsystem. Refusing."
                % (held,))
        if not any(k[0] == 'clock' for k in handlers):
            gcmd.respond_info("clock: note - nothing holds the 'clock' slot, "
                              "which is unexpected; ClockSync normally does")

    def _uptime(self, mcu, gcmd):
        if self._uptime_cmd is None:
            try:
                self._uptime_cmd = mcu.lookup_query_command(UPTIME_MSG,
                                                            UPTIME_RESP)
            except Exception as e:
                raise gcmd.error("clock: could not build the get_uptime query: "
                                 "%s" % (e,))
        return self._uptime_cmd

    cmd_CLOCK_help = ("Measure where inside a query's round trip the MCU "
                      "answers, so a host-derived sample timestamp can be "
                      "corrected. Read-only. [N=200]")

    def cmd_CLOCK(self, gcmd):
        # Klipper treats an unhandled exception in a gcode command as an
        # INTERNAL ERROR and shuts down every MCU. A stray NameError in this
        # module did exactly that on 2026-09-15. A gcode.error, by contrast, is
        # a clean command failure that leaves the printer running - so anything
        # unexpected is converted into one here rather than being allowed to
        # take the machine down.
        # Derive the clean-failure class from gcmd itself rather than importing
        # it, so this works against the mock harness too.
        err_cls = type(gcmd.error("probe"))
        try:
            return self._run(gcmd)
        except err_cls:
            raise
        except Exception as e:
            logging.exception("qidi_cs_clock: unhandled error")
            raise gcmd.error("clock: internal error, printer left running: "
                             "%s: %s" % (type(e).__name__, str(e)[:160]))

    def _run(self, gcmd):
        sensor = self._sensor(gcmd)
        mcu = self._mcu(sensor, gcmd)
        n = gcmd.get_int('N', DEFAULT_N, minval=20, maxval=MAX_N)
        self._check_slot_free(mcu, gcmd)
        cmd = self._uptime(mcu, gcmd)

        gcmd.respond_info("clock: %d get_uptime probes - read-only, nothing "
                          "moves or heats" % (n,))

        fracs, offs, rtts, errs = [], [], [], 0
        for _ in range(n):
            t0 = self.reactor.monotonic()
            try:
                resp = cmd.send()
            except Exception as e:
                errs += 1
                if errs > 5:
                    raise gcmd.error("clock: get_uptime failed: %s"
                                     % (str(e)[:120],))
                continue
            t1 = self.reactor.monotonic()
            try:
                c64 = mcu.clock32_to_clock64(resp['clock'])
                pt_mcu = mcu.clock_to_print_time(c64)
                pt0 = mcu.estimated_print_time(t0)
                pt1 = mcu.estimated_print_time(t1)
            except Exception as e:
                raise gcmd.error("clock: clock conversion failed: %s"
                                 % (str(e)[:120],))
            rtt = pt1 - pt0
            if rtt <= 0:
                continue
            rtts.append(rtt * 1000.0)
            fracs.append((pt_mcu - pt0) / rtt)
            offs.append((pt_mcu - 0.5 * (pt0 + pt1)) * 1000.0)

        if len(fracs) < 8:
            raise gcmd.error("clock: only %d usable probes" % (len(fracs),))

        F, O, R = _stats(fracs), _stats(offs), _stats(rtts)
        gcmd.respond_info("  round trip  : min %.3f  median %.3f  p90 %.3f ms"
                          % (R['min'], R['median'], R['p90']))
        gcmd.respond_info("  MCU answers at fraction f of the round trip:")
        gcmd.respond_info("    median %.3f   p10 %.3f   p90 %.3f   sd %.3f"
                          % (F['median'], F['p10'], F['p90'], F['sd']))
        gcmd.respond_info("  offset from the midpoint guess:")
        gcmd.respond_info("    median %+.3f ms   sd %.3f ms   (p10 %+.3f, "
                          "p90 %+.3f)"
                          % (O['median'], O['sd'], O['p10'], O['p90']))

        # How comparable is the cs1237 read? f only transfers if the two
        # commands cost the MCU similar work.
        burst = getattr(sensor, 'query_cs1237_end_cmd', None)
        b_rtts = []
        if burst is not None:
            oid = getattr(sensor, 'oid', None)
            for _ in range(min(n, 200)):
                t0 = self.reactor.monotonic()
                try:
                    burst.send([oid, 0, 4])
                except Exception:
                    break
                b_rtts.append((self.reactor.monotonic() - t0) * 1000.0)
        B = _stats(b_rtts) if len(b_rtts) >= 8 else None
        if B:
            gcmd.respond_info("  query_cs1237_read round trip: min %.3f  "
                              "median %.3f ms" % (B['min'], B['median']))
            ratio = B['median'] / R['median'] if R['median'] > 0 else 0.0
            gcmd.respond_info("    %.2fx the get_uptime round trip - %s"
                              % (ratio,
                                 "close enough that f should transfer"
                                 if 0.8 <= ratio <= 1.25 else
                                 "different enough that f may NOT transfer; "
                                 "treat the correction as approximate"))

        # Which estimator: t0 + f*RTT, or t0 + a fixed outbound delay? Decide on
        # ABSOLUTE timestamp scatter in ms, not on relative spread - the units
        # differ between the two and relative comparison picks the wrong one.
        outs = []
        for f, o in zip(fracs, offs):
            if abs(f - 0.5) < 1e-9:
                continue
            rtt = o / (f - 0.5)
            if 0 < rtt < 20.0:
                outs.append(f * rtt)
        prop_ms = F['mad'] * R['median']          # f scatter, in ms
        fixed_ms = _stats(outs)['mad'] if len(outs) >= 8 else float('inf')
        c_fixed = _stats(outs)['median'] if len(outs) >= 8 else 0.0
        gcmd.respond_info("  estimator scatter: proportional (t0 + f*RTT) "
                          "%.3f ms   vs fixed (t0 + c) %.3f ms"
                          % (prop_ms, fixed_ms))
        if prop_ms <= fixed_ms:
            rule = "t0 + %.4f x RTT" % (F['median'],)
            jit = prop_ms
        else:
            rule = "t0 + %.3f ms" % (c_fixed,)
            jit = fixed_ms

        # The buffer holds the last completed conversion: uniform 0-0.781 ms
        # old, so mean 0.391 ms and sd 0.781/sqrt(12) = 0.226 ms. That term is
        # irreducible - it is the price of an unsynchronised read.
        stale, stale_sd = 0.391, 0.781 / math.sqrt(12.0)
        total_jit = math.sqrt(jit ** 2 + stale_sd ** 2)
        gcmd.respond_info("clock: timestamp a raw sample at  %s - %.3f ms"
                          % (rule, stale))
        gcmd.respond_info("  systematic : the midpoint guess was %+.3f ms out; "
                          "now measured and removed" % (O['median'],))
        gcmd.respond_info("  random     : %.3f ms estimator + %.3f ms buffer "
                          "phase = %.3f ms, or %.2f%% of a 24 ms K - and it "
                          "averages down across transitions"
                          % (jit, stale_sd, total_jit, 100 * total_jit / 24.0))
        if O['outliers_5mad']:
            gcmd.respond_info(
                "  reject     : %d of %d probes (%.1f%%) had a delayed reply, "
                "worst %+.1f ms. Stage 3 must drop samples whose RTT is far "
                "above the median rather than trusting the correction on them."
                % (O['outliers_5mad'], O['n'],
                   100.0 * O['outliers_5mad'] / O['n'], O['min']))

        ok = self._record({'n': n, 'errors': errs, 'frac': F, 'offset_ms': O,
                           'rtt_ms': R, 'cs1237_rtt_ms': B,
                           'correction_ms': O['median'],
                           'estimator': rule,
                           'estimator_jitter_ms': jit,
                           'buffer_stale_ms': stale,
                           'total_random_ms': total_jit,
                           'prop_scatter_ms': prop_ms,
                           'fixed_scatter_ms': fixed_ms,
                           'fixed_c_ms': c_fixed,
                           'fracs': [round(x, 5) for x in fracs[:256]],
                           'offsets_ms': [round(x, 5) for x in offs[:256]]})
        gcmd.respond_info("clock: written to %s"
                          % (self.report_path if ok else "(not written)",))


def load_config(config):
    return QidiCSClock(config)
