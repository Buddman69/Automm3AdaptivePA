# qidi_cs_bulk.py - does query_cs1237 push sensor_bulk_data?
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
# WHY
#   The THR firmware dictionary (extracted offline with qidi_fw_dict.py from
#   QD_MAX4_THR_02.02.01.08_20260303.bin) advertises:
#
#       sensor_bulk_data    oid=%c sequence=%hu data=%*s
#       sensor_bulk_status  oid=%c clock=%u query_ticks=%u next_sequence=%hu
#                           buffered=%u possible_overflows=%hu
#
#   and query_cs1237 oid rest_ticks has the SAME signature as the three known
#   bulk samplers (adxl345, lis2dw, hx71x). cs1237.so also carries the stock
#   adxl345.py bulk constants - BYTES_PER_SAMPLE=4, MAX_SAMPLES_PER_BLOCK=13.
#
#   QIDI's own bulk_queue listens for `cs1237_data`, which the firmware NEVER
#   sends - confirmed against the dictionary. That is why every previous capture
#   attempt returned nothing: right command, wrong handler name.
#
#   Evidence against, stated honestly: there is no query_cs1237_status (all three
#   siblings have one), and cs1237.so contains no `sensor_bulk_data` string at
#   all. So this is a 50/50 test, not a formality.
#
#   Prize if it works: ~87 Hz polled -> up to 1280 Hz. About 15x.
#
# SAFETY
#   LISTEN mode (the default) sends NOTHING. It only registers a response
#   handler and waits. There is no way for it to change machine state.
#
#   START mode sends exactly one command, query_cs1237 oid rest_ticks:
#     - It is NOT the endstop path. No threshold, no trsync_oid, no
#       trigger_reason. SAFETY.md rule 4 is about cs1237_setup_home,
#       which this module never touches.
#     - Stock Klipper semantics: rest_ticks=0 stops periodic sampling. Stopping
#       is one command, and it is issued from a finally: block so no exit path
#       can leave the sampler running.
#     - Default 100 Hz, not 1280, so the MCU buffer cannot overflow even if it
#       does stream.
#     - Refuses to run while a print is active.
#     - Re-reads read_origin_data() afterwards to prove the polled path survived.
#
#   Worst realistic case is a Klipper shutdown needing FIRMWARE_RESTART - which
#   also re-runs config_cs1237 from scratch and wipes anything we set. Nothing
#   heats, nothing moves, no force is applied to the cell.
#
# USAGE
#   [qidi_cs_bulk]
#
#   QIDI_CS_BULK                 LISTEN only - sends nothing, 3 s
#   QIDI_CS_BULK SECS=5          listen longer
#   QIDI_CS_BULK HZ=100          START the sampler at 100 Hz, then stop it
#   QIDI_CS_BULK HZ=1280 SECS=2  full rate, only after 100 Hz has worked
#
#   Results are appended to ~/printer_data/qidi_pa/bulk.json

import json
import logging
import os
import time

# Q2 value - measured on one Q2 across 15 points, 8.5 gf to 2063 gf. The
# X-Max 4 cell measured 182.96; this is display-only in this diagnostic tool,
# but a mismatched value here would still print a wrong gf figure.
COUNTS_PER_GF = 201.0
BYTES_PER_SAMPLE = 4
SAMPLE_PERIOD_MS = 1000.0 / 1280.0      # 0.78125 ms per conversion

DEFAULT_SECS = 3.0
MAX_SECS = 15.0
DEFAULT_HZ = 100.0
MAX_HZ = 1280.0

BULK_DATA_MSG = 'sensor_bulk_data'
BULK_STATUS_MSG = 'sensor_bulk_status'


def _s24le(b):
    # Established by QIDI_CS_BATCH: 24-bit little-endian in bytes 0-2, byte 3
    # is a 0x00 pad. Nine readings agreed with read_origin_data() to 0.29 gf.
    v = b[0] | (b[1] << 8) | (b[2] << 16)
    return v - 0x1000000 if v & 0x800000 else v


def decode_block(blob):
    out = []
    for i in range(0, len(blob) - BYTES_PER_SAMPLE + 1, BYTES_PER_SAMPLE):
        out.append(_s24le(blob[i:i + BYTES_PER_SAMPLE]))
    return out


def _as_bytes(v):
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    if isinstance(v, str):
        return v.encode('latin-1', 'replace')
    return None


class QidiCSBulk:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.root_name = config.get('root', 'probe_air')
        self.sensor_attr = config.get('sensor_attr', 'sensor_helper')
        out_dir = config.get('out_dir', '~/printer_data/qidi_pa')
        self.out_dir = os.path.expanduser(out_dir)
        self.report_path = os.path.join(self.out_dir, 'bulk.json')
        self._msgs = []
        self._status = []
        self._registered = False
        self.gcode.register_command('QIDI_CS_BULK', self.cmd_BULK,
                                    desc=self.cmd_BULK_help)

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
            logging.exception("qidi_cs_bulk: unhandled error")
            raise gcmd.error("bulk: internal error, printer left running: "
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
            data.append({'kind': 'bulk', 'time': time.time(),
                         'payload': payload})
            tmp = self.report_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=1, default=repr)
            os.replace(tmp, self.report_path)
            return True
        except Exception:
            logging.exception("qidi_cs_bulk: could not write report")
            return False

    def _sensor(self, gcmd):
        root = self.printer.lookup_object(self.root_name, None)
        if root is None:
            raise gcmd.error("bulk: no object %s" % (self.root_name,))
        s = getattr(root, self.sensor_attr, None)
        if s is None:
            raise gcmd.error("bulk: %s has no %s" % (self.root_name,
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
            raise gcmd.error("bulk: cannot reach the sensor's MCU object")
        return mcu

    def _refuse_if_printing(self, gcmd):
        ps = self.printer.lookup_object('print_stats', None)
        if ps is not None:
            try:
                state = ps.get_status(self.reactor.monotonic()).get('state')
            except Exception:
                state = None
            if state in ('printing', 'paused'):
                raise gcmd.error("bulk: refusing to touch the sensor while a "
                                 "print is %s" % (state,))

    def _register(self, mcu, oid, gcmd):
        if self._registered:
            return
        # Register ONLY for this oid. A global (oid=None) handler could displace
        # whatever lis2dw or adxl345 have registered for their own oids.
        def on_data(params):
            self._msgs.append((self.reactor.monotonic(), params))

        def on_status(params):
            self._status.append((self.reactor.monotonic(), params))

        try:
            mcu.register_response(on_data, BULK_DATA_MSG, oid)
            mcu.register_response(on_status, BULK_STATUS_MSG, oid)
        except Exception as e:
            raise gcmd.error("bulk: could not register a handler for %s oid=%d"
                             ": %s" % (BULK_DATA_MSG, oid, e))
        self._registered = True

    def _collect(self, secs):
        end = self.reactor.monotonic() + secs
        while True:
            now = self.reactor.monotonic()
            if now >= end:
                break
            self.reactor.pause(min(now + 0.05, end))

    def _summarise(self, gcmd, elapsed):
        n_msg = len(self._msgs)
        gcmd.respond_info("  %s: %d message(s) in %.2f s"
                          % (BULK_DATA_MSG, n_msg, elapsed))
        for t, p in self._status[:4]:
            gcmd.respond_info("  %s: %s" % (BULK_STATUS_MSG,
                                            {k: v for k, v in p.items()
                                             if k != '#name'}))
        if not n_msg:
            return {'messages': 0, 'samples': 0}

        samples, seqs, lens = [], [], []
        for t, p in self._msgs:
            blob = _as_bytes(p.get('data'))
            if blob is None:
                continue
            lens.append(len(blob))
            if 'sequence' in p:
                seqs.append(p['sequence'])
            samples.extend(decode_block(blob))

        rate = len(samples) / elapsed if elapsed > 0 else 0.0
        gcmd.respond_info("  payload %d-%d bytes per message, %d samples total "
                          "-> %.0f Hz effective"
                          % (min(lens) if lens else 0, max(lens) if lens else 0,
                             len(samples), rate))
        if seqs:
            gaps = sum(1 for a, b in zip(seqs, seqs[1:])
                       if (b - a) & 0xffff != 1)
            gcmd.respond_info("  sequence %d..%d, %d discontinuit%s"
                              % (seqs[0], seqs[-1], gaps,
                                 "y" if gaps == 1 else "ies"))
        if samples:
            lo, hi = min(samples), max(samples)
            gcmd.respond_info("  counts %d..%d (span %.1f gf), first %d last %d"
                              % (lo, hi, (hi - lo) / COUNTS_PER_GF,
                                 samples[0], samples[-1]))
        return {'messages': n_msg, 'samples': len(samples),
                'rate_hz': rate,
                'payload_len': [min(lens), max(lens)] if lens else None,
                'sequences': seqs[:64],
                'first_samples': samples[:64],
                'span_counts': (max(samples) - min(samples)) if samples else None}

    def _poll_loop(self, cmd, args, n):
        vals, durs = [], []
        t_start = self.reactor.monotonic()
        for _ in range(n):
            t0 = self.reactor.monotonic()
            resp = cmd.send(list(args))
            durs.append((self.reactor.monotonic() - t0) * 1000.0)
            blob = _as_bytes(resp.get('data')) if isinstance(resp, dict) else None
            if blob:
                d = decode_block(blob)
                if d:
                    vals.append(d[0])
        return vals, durs, self.reactor.monotonic() - t_start

    def _compare(self, gcmd, sensor, n):
        # How many conversions does a burst read actually perform?
        #
        # QIDI_CS_TIMING measured a 10.315 ms floor for read_origin_data() and
        # I attributed it to 13 x 0.78125 ms + 0.16 ms round trip - fitting
        # MAX_SAMPLES_PER_BLOCK = 13 because I already expected it. Then the
        # cache read came back with a 2.47 ms FLOOR, which says a round trip on
        # this link costs ~2.5 ms, and 10 conversions + 2.5 ms fits just as
        # well.
        #
        # The cache read provably performs no conversion - 600 polls returned
        # one distinct value. So it measures the round trip alone, and
        #
        #     burst_floor - cache_floor = conversions x 0.78125 ms
        #
        # Both loops here call a CommandQueryWrapper the same way from Python,
        # so host-side overhead cancels instead of being assumed away.
        cache_cmd = getattr(sensor, 'query_cs1237_zero_read_cmd', None)
        burst_cmd = getattr(sensor, 'query_cs1237_end_cmd', None)
        if cache_cmd is None or burst_cmd is None:
            raise gcmd.error("bulk: need both query_cs1237_zero_read_cmd and "
                             "query_cs1237_end_cmd")
        oid = sensor.oid

        # Three loops, identical call style, same session, same conditions.
        c_vals, c_durs, _ = self._poll_loop(cache_cmd, [oid], n)
        b_vals, b_durs, _ = self._poll_loop(burst_cmd, [oid, 0, 4], n)

        read = getattr(sensor, 'read_origin_data', None)
        r_vals, r_durs = [], []
        if callable(read):
            t_start = self.reactor.monotonic()
            for _ in range(n):
                t0 = self.reactor.monotonic()
                v = read()
                r_durs.append((self.reactor.monotonic() - t0) * 1000.0)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    r_vals.append(float(v))
        if len(c_durs) < 8 or len(b_durs) < 8:
            raise gcmd.error("bulk: not enough samples to compare")

        def summary(durs, vals):
            s = sorted(durs)
            return {'min': s[0], 'median': s[len(s) // 2], 'n': len(s),
                    'distinct': len(set(vals)), 'vals': len(vals),
                    'rate_hz': 1000.0 / s[len(s) // 2] if s else 0.0}

        C, Bq, R = (summary(c_durs, c_vals), summary(b_durs, b_vals),
                    summary(r_durs, r_vals) if r_durs else None)

        for label, s in (("cache read       ", C), ("burst read (raw) ", Bq),
                         ("read_origin_data ", R)):
            if s is None:
                continue
            gcmd.respond_info("  %s min %6.3f  median %6.3f ms  -> %5.0f Hz   "
                              "%d/%d distinct"
                              % (label, s['min'], s['median'], s['rate_hz'],
                                 s['distinct'], s['vals']))

        d_min = Bq['min'] - C['min']
        gcmd.respond_info("  burst - cache at the floor: %.3f ms = %.2f "
                          "conversions of %.3f ms"
                          % (d_min, d_min / SAMPLE_PERIOD_MS, SAMPLE_PERIOD_MS))

        notes = []
        if C['distinct'] <= 2:
            notes.append("the cache read is confirmed static, so its %.2f ms "
                         "is pure round trip" % (C['min'],))
        if Bq['distinct'] > n // 4 and Bq['median'] < 0.6 * (R or Bq)['median']:
            notes.append(
                "THE RAW COMMAND IS THE FAST PATH: %.0f Hz against %.0f Hz "
                "for read_origin_data(), returning fresh values. That is "
                "%.1fx, available with no firmware change and no streaming - "
                "read_origin_data() is adding the delay, not the MCU"
                % (Bq['rate_hz'], (R or Bq)['rate_hz'],
                   (R or Bq)['median'] / Bq['median']))
        elif R and abs(Bq['median'] - R['median']) < 1.0:
            notes.append("the raw command and read_origin_data() cost the same, "
                         "so the wrapper adds nothing and the MCU is the cost")
        for msg in notes:
            gcmd.respond_info("bulk: %s" % (msg,))
        return {'n': n, 'cache': C, 'burst_raw': Bq, 'read_origin_data': R,
                'delta_min_ms': d_min,
                'conversions_from_floor': d_min / SAMPLE_PERIOD_MS,
                'burst_values': b_vals[:128], 'cache_values': c_vals[:16],
                'notes': notes}

    def _zero_fast(self, gcmd, sensor, n):
        # query_cs1237_zero_read_only came back in ~3.2 ms against
        # read_origin_data()'s 10.9, and returned EXACTLY the value the
        # reference read had just produced. Two readings of that:
        #
        #   (a) it returns a cached last-conversion result without running a
        #       burst. If the MCU keeps converting at 1280 SPS in the
        #       background, polling this gives ~310 Hz of FRESH samples - 3.5x
        #       the polled path.
        #   (b) it returns a stale cache that only updates when something else
        #       triggers a conversion, in which case back-to-back polls repeat
        #       the same number and it is worthless on its own.
        #
        # The discriminator is simply to poll it back-to-back with no
        # read_origin_data() in between and count how many values repeat.
        cmd = getattr(sensor, 'query_cs1237_zero_read_cmd', None)
        if cmd is None:
            raise gcmd.error("bulk: sensor has no query_cs1237_zero_read_cmd")
        oid = sensor.oid
        vals, durs = [], []
        t_start = self.reactor.monotonic()
        for _ in range(n):
            t0 = self.reactor.monotonic()
            try:
                resp = cmd.send([oid])
            except Exception as e:
                raise gcmd.error("bulk: zero read failed: %s" % (str(e)[:120],))
            durs.append((self.reactor.monotonic() - t0) * 1000.0)
            blob = _as_bytes(resp.get('data')) if isinstance(resp, dict) else None
            if blob:
                d = decode_block(blob)
                if d:
                    vals.append(d[0])
        elapsed = self.reactor.monotonic() - t_start
        if len(vals) < 4:
            raise gcmd.error("bulk: only %d values decoded" % (len(vals),))

        durs_sorted = sorted(durs)
        med = durs_sorted[len(durs_sorted) // 2]
        repeats = sum(1 for a, b in zip(vals, vals[1:]) if a == b)
        distinct = len(set(vals))
        rate = len(vals) / elapsed if elapsed > 0 else 0.0
        gcmd.respond_info("  %d polls in %.2f s -> %.0f Hz, median %.2f ms "
                          "(min %.2f, max %.2f)"
                          % (len(vals), elapsed, rate, med, durs_sorted[0],
                             durs_sorted[-1]))
        gcmd.respond_info("  %d distinct values, %d consecutive repeats (%.1f%%)"
                          % (distinct, repeats, 100.0 * repeats
                             / max(len(vals) - 1, 1)))
        spread = max(vals) - min(vals)
        gcmd.respond_info("  counts %d..%d = %.2f gf of movement"
                          % (min(vals), max(vals), spread / COUNTS_PER_GF))

        frac = repeats / float(max(len(vals) - 1, 1))
        if frac < 0.25 and rate > 150.0:
            verdict = ("FRESH AND FAST. %.0f Hz with only %.0f%% repeats - this "
                       "is a live sample per call at %.2f ms, against 10.9 ms "
                       "for read_origin_data(). That is %.1fx the polled rate "
                       "and Stage 3 should use this path."
                       % (rate, 100 * frac, med, rate / 87.3))
        elif frac > 0.6:
            verdict = ("STALE CACHE. %.0f%% of consecutive polls returned an "
                       "identical value, so it is not converting per call - it "
                       "hands back whatever was last computed. Useless without "
                       "something else driving conversions." % (100 * frac,))
        else:
            verdict = ("MIXED - %.0f Hz, %.0f%% repeats. Partly fresh; needs a "
                       "longer run before trusting it." % (rate, 100 * frac))
        gcmd.respond_info("bulk: %s" % (verdict,))
        return {'n': len(vals), 'elapsed': elapsed, 'rate_hz': rate,
                'median_ms': med, 'min_ms': durs_sorted[0],
                'max_ms': durs_sorted[-1], 'distinct': distinct,
                'repeats': repeats, 'repeat_frac': frac,
                'spread_counts': spread, 'verdict': verdict,
                'values': vals[:256], 'durations_ms': [round(d, 4)
                                                       for d in durs[:256]]}

    def _zero_read(self, gcmd, sensor, n):
        # query_cs1237_zero_read_only oid -> query_cs1237_zero_read_o data=%*s
        #
        # The last data-bearing reply never exercised. It matters because a tare
        # has to average SOMETHING, MAX_SAMPLES_PER_BLOCK is 13, and the timing
        # test proved one read spans exactly 13 conversions. If this hands back
        # the block rather than the mean, that is 13 samples at a known 0.78 ms
        # spacing per call - the transient resolved, not integrated.
        #
        # Named "read_only", and its sibling query_cs1237_zero is the one that
        # would actually re-tare. We call the read-only one.
        cmd = getattr(sensor, 'query_cs1237_zero_read_cmd', None)
        if cmd is None:
            raise gcmd.error("bulk: sensor has no query_cs1237_zero_read_cmd")
        oid = sensor.oid
        read = getattr(sensor, 'read_origin_data', None)
        rows = []
        for i in range(n):
            ref = None
            try:
                if callable(read):
                    ref = float(read())
            except Exception:
                pass
            t0 = self.reactor.monotonic()
            try:
                resp = cmd.send([oid])
            except Exception as e:
                gcmd.respond_info("  attempt %d -> error: %s" % (i, str(e)[:80]))
                rows.append({'i': i, 'error': str(e)[:200]})
                continue
            dt = (self.reactor.monotonic() - t0) * 1000.0
            blob = None
            if isinstance(resp, dict):
                blob = _as_bytes(resp.get('data'))
            if blob is None:
                gcmd.respond_info("  attempt %d -> reply with no data field: %s"
                                  % (i, sorted(resp.keys())
                                     if isinstance(resp, dict) else resp))
                rows.append({'i': i, 'keys': sorted(resp.keys())
                             if isinstance(resp, dict) else None})
                continue
            vals = decode_block(blob)
            gcmd.respond_info("  attempt %d -> %d bytes = %d sample(s), %.2f ms"
                              "  %s" % (i, len(blob), len(vals), dt,
                                        blob[:16].hex()))
            if vals:
                gcmd.respond_info("      %s%s   ref %s"
                                  % (vals[:6], " ..." if len(vals) > 6 else "",
                                     "%.0f" % ref if ref is not None else "n/a"))
            rows.append({'i': i, 'bytes': len(blob), 'samples': len(vals),
                         'ms': dt, 'hex': blob.hex()[:256], 'values': vals[:32],
                         'ref': ref})
        best = max((r.get('samples') or 0) for r in rows) if rows else 0
        if best > 1:
            gcmd.respond_info(
                "bulk: THIS RETURNS A BLOCK - %d samples per call. At 0.78 ms "
                "spacing that is the transient resolved rather than integrated. "
                "Stage 3 should be redesigned around it." % (best,))
        else:
            gcmd.respond_info(
                "bulk: one sample per call here too. Every data-bearing message "
                "this firmware has is now exercised and none returns a block. "
                "The polled path at ~87 Hz is the ceiling - Stage 3 as designed.")
        return rows

    cmd_BULK_help = ("Test whether query_cs1237 pushes sensor_bulk_data. No "
                     "HZ = listen only, sends nothing. HZ= starts the sampler "
                     "and always stops it. ZERO=n probes the zero-read reply. "
                     "[SECS=3] [HZ=] [ZERO=]")

    def cmd_BULK(self, gcmd):
        return self._guard(gcmd, self._run_BULK)

    def _run_BULK(self, gcmd):
        sensor = self._sensor(gcmd)
        mcu = self._mcu(sensor, gcmd)
        oid = getattr(sensor, 'oid', None)
        if oid is None:
            raise gcmd.error("bulk: sensor has no oid - run QIDI_CS_LOCATE")
        secs = gcmd.get_float('SECS', DEFAULT_SECS, above=0., below=MAX_SECS)
        hz = gcmd.get_float('HZ', None)
        zero_n = gcmd.get_int('ZERO', None)

        cmp_n = gcmd.get_int('CMP', None)
        if cmp_n is not None:
            if cmp_n < 8 or cmp_n > 2000:
                raise gcmd.error("bulk: CMP must be 8..2000")
            self._refuse_if_printing(gcmd)
            gcmd.respond_info("bulk: timing a cache read against a burst read, "
                              "%d each, to count the conversions in a burst"
                              % (cmp_n,))
            res = self._compare(gcmd, sensor, cmp_n)
            self._record({'mode': 'compare', 'oid': oid, 'result': res})
            gcmd.respond_info("bulk: written to %s" % (self.report_path,))
            return

        fast_n = gcmd.get_int('FAST', None)
        if fast_n is not None:
            if fast_n < 8 or fast_n > 2000:
                raise gcmd.error("bulk: FAST must be 8..2000")
            self._refuse_if_printing(gcmd)
            gcmd.respond_info("bulk: polling query_cs1237_zero_read_only x%d "
                              "back-to-back, no reference reads - is it fresh "
                              "or a stale cache?" % (fast_n,))
            res = self._zero_fast(gcmd, sensor, fast_n)
            self._record({'mode': 'zero_fast', 'oid': oid, 'result': res})
            gcmd.respond_info("bulk: written to %s" % (self.report_path,))
            return

        if zero_n is not None:
            if zero_n < 1 or zero_n > 20:
                raise gcmd.error("bulk: ZERO must be 1..20")
            self._refuse_if_printing(gcmd)
            gcmd.respond_info("bulk: probing query_cs1237_zero_read_only x%d - "
                              "the read-only sibling of the tare command, oid=%d"
                              % (zero_n, oid))
            rows = self._zero_read(gcmd, sensor, zero_n)
            self._record({'mode': 'zero_read', 'oid': oid, 'attempts': rows})
            gcmd.respond_info("bulk: written to %s" % (self.report_path,))
            return

        read = getattr(sensor, 'read_origin_data', None)
        ref_before = None
        try:
            if callable(read):
                ref_before = float(read())
        except Exception:
            pass

        self._msgs, self._status = [], []
        self._register(mcu, oid, gcmd)

        if hz is None:
            gcmd.respond_info("bulk: LISTEN only for %.1f s - sending nothing. "
                              "oid=%d, read_origin_data() = %s"
                              % (secs, oid, "%.0f" % ref_before
                                 if ref_before is not None else "n/a"))
            t0 = self.reactor.monotonic()
            self._collect(secs)
            elapsed = self.reactor.monotonic() - t0
            summary = self._summarise(gcmd, elapsed)
            if not summary['messages']:
                gcmd.respond_info(
                    "bulk: silent, as expected - nothing is streaming on its "
                    "own. The handler is registered for %s oid=%d. Next: "
                    "QIDI_CS_BULK HZ=100 to start the sampler."
                    % (BULK_DATA_MSG, oid))
            else:
                gcmd.respond_info(
                    "bulk: UNEXPECTED - data arrived without us asking. "
                    "Something already has the sampler running.")
            self._record({'mode': 'listen', 'oid': oid, 'secs': secs,
                          'ref_before': ref_before, 'summary': summary})
            return

        if hz <= 0 or hz > MAX_HZ:
            raise gcmd.error("bulk: HZ must be between 0 and %.0f" % (MAX_HZ,))
        self._refuse_if_printing(gcmd)

        cmd = getattr(sensor, 'query_cs1237_cmd', None)
        if cmd is None:
            raise gcmd.error("bulk: sensor has no query_cs1237_cmd")
        rest_ticks = int(mcu.seconds_to_clock(1.0 / hz))

        gcmd.respond_info("bulk: starting the sampler at %.0f Hz "
                          "(rest_ticks=%d), collecting %.1f s, then stopping."
                          % (hz, rest_ticks, secs))
        t0 = self.reactor.monotonic()
        elapsed = 0.0
        started = False
        try:
            cmd.send([oid, rest_ticks])
            started = True
            self._collect(secs)
            elapsed = self.reactor.monotonic() - t0
        finally:
            # Must happen on every path, including an exception mid-collect.
            try:
                cmd.send([oid, 0])
                gcmd.respond_info("bulk: sampler stopped (rest_ticks=0)")
            except Exception as e:
                gcmd.respond_info("bulk: FAILED TO STOP THE SAMPLER: %s - run "
                                  "FIRMWARE_RESTART now" % (e,))

        summary = self._summarise(gcmd, elapsed or secs)

        ref_after, read_ok = None, False
        try:
            if callable(read):
                ref_after = float(read())
                read_ok = True
        except Exception as e:
            gcmd.respond_info("bulk: read_origin_data() raised after the test: "
                              "%s" % (e,))
        if read_ok:
            gcmd.respond_info("  polled path still works: %.0f -> %.0f counts "
                              "(%.1f gf apart)"
                              % (ref_before if ref_before is not None else 0,
                                 ref_after,
                                 abs((ref_after - (ref_before or ref_after)))
                                 / COUNTS_PER_GF))

        if summary['messages']:
            gcmd.respond_info(
                "bulk: IT STREAMS. %d samples in %.2f s = %.0f Hz against 87 Hz "
                "polled. query_cs1237 is a real bulk sampler and QIDI's driver "
                "was simply listening on the wrong message name."
                % (summary['samples'], elapsed, summary.get('rate_hz') or 0.0))
        else:
            gcmd.respond_info(
                "bulk: no %s arrived. query_cs1237 sets something internal and "
                "does not push to the host. That closes the streaming question "
                "for this firmware - Stage 3 stays on the polled path at 87 Hz."
                % (BULK_DATA_MSG,))

        self._record({'mode': 'start', 'oid': oid, 'hz': hz,
                      'rest_ticks': rest_ticks, 'secs': secs,
                      'started': started, 'elapsed': elapsed,
                      'ref_before': ref_before, 'ref_after': ref_after,
                      'summary': summary})
        gcmd.respond_info("bulk: written to %s" % (self.report_path,))


def load_config(config):
    return QidiCSBulk(config)
