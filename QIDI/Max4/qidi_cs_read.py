# qidi_cs_read.py - Stage B: read force from the CS1237
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
# PURPOSE
#   Stage A established that the sensor does not stream while idle, so there is
#   no passive read.  Getting a number requires invoking something.  This module
#   does that, in two deliberately separated steps:
#
#   B1  QIDI_CS_READ    polls sensor_helper.read_origin_data() at a few Hz.
#                       One method call per sample, no lifecycle, nothing left
#                       running if it throws.  Enough for the counts-to-gf
#                       calibration and the noise floor, which is what actually
#                       gates progress - see README.md.
#
#   B2  QIDI_CS_STREAM  starts the firmware's periodic sampling with
#                       `query_cs1237 oid rest_ticks`, drains bulk_queue, and
#                       decodes.  Full rate, MCU-timestamped.  Only needed at
#                       the PA stage.
#
# SAFETY
#   No motion, no heating, no config writes.  Both commands are sensor reads.
#
#   B2 is the one with a lifecycle.  It sends rest_ticks=0 (stop) before it
#   starts and again in a finally block, so an exception mid-capture cannot
#   leave the chip streaming at ~12% of the THR serial budget.  If it ever does
#   get stuck, a klipper restart clears it.
#
#   This module never touches cs1237_setup_home.  That is the probe's endstop
#   path (threshold, trsync_oid, trigger_reason) and driving it wrongly is how
#   the Z probe gets broken - SAFETY.md rule 4.
#
# USAGE
#   [qidi_cs_read]
#
#   QIDI_CS_READ   [SECONDS=10] [HZ=10]     poll; press the nozzle by hand
#   QIDI_CS_STREAM [SECONDS=2] [POLL_HZ=2560] [REST_TICKS=...]
#
#   Results are appended to ~/printer_data/qidi_pa/read.json

import json
import logging
import os
import struct
import time

DEFAULT_SENSOR_PATH = 'sensor_helper'


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _stats(vals):
    n = len(vals)
    if not n:
        return {}
    lo, hi = min(vals), max(vals)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    out = {'n': n, 'min': lo, 'max': hi, 'mean': mean, 'span': hi - lo,
           'stdev': var ** 0.5, 'first': vals[0], 'last': vals[-1],
           'drift': vals[-1] - vals[0]}
    diffs = [abs(b - a) for a, b in zip(vals, vals[1:])]
    if diffs:
        out['median_abs_delta'] = sorted(diffs)[len(diffs) // 2]
    return out


# --- bulk block decoding ----------------------------------------------------
# bytes_per_block is 4 and the CS1237 is a 24-bit converter, so one block is a
# 24-bit two's-complement sample plus a byte of something - or a 32-bit word.
# Rather than guess the layout, decode every plausible reading and let the data
# choose: the correct one has small deltas between consecutive samples (sensor
# noise), the wrong ones look like noise across the full integer range.

def _s24(b0, b1, b2):
    v = (b0 << 16) | (b1 << 8) | b2
    return v - 0x1000000 if v & 0x800000 else v


def _decode_all(blob, bytes_per_block=4):
    blocks = [blob[i:i + bytes_per_block]
              for i in range(0, len(blob) - bytes_per_block + 1,
                             bytes_per_block)]
    out = {'i32le': [], 'i32be': [], 'i24be_first': [], 'i24le_first': [],
           'i24be_last': []}
    for b in blocks:
        if len(b) < 4:
            continue
        out['i32le'].append(struct.unpack('<i', b)[0])
        out['i32be'].append(struct.unpack('>i', b)[0])
        out['i24be_first'].append(_s24(b[0], b[1], b[2]))
        out['i24le_first'].append(_s24(b[2], b[1], b[0]))
        out['i24be_last'].append(_s24(b[1], b[2], b[3]))
    return out


ADC_FULL_SCALE = 1 << 23          # CS1237 is 24-bit two's complement


def _rank_decodings(decoded):
    # Two criteria, in order:
    #
    # 1. Physical plausibility.  A 24-bit converter cannot emit a value outside
    #    +/-2^23.  This matters more than it looks: reading a 24-bit sample as
    #    32-bit big-endian multiplies it by 256 and adds a pad byte, which
    #    scales step AND span equally and therefore ties on the ratio below.
    #    The range check is what separates them.
    # 2. Smallest median step relative to span - real sensor noise moves a
    #    little between consecutive samples, a wrong byte order moves randomly
    #    across the whole integer range.
    #
    # A decoding that never moves is dropped: a constant scores a perfect zero
    # while telling us nothing.
    ranked = []
    for name, vals in decoded.items():
        if len(vals) < 3:
            continue
        st = _stats(vals)
        if st.get('span', 0) == 0:
            continue
        in_range = max(abs(st['min']), abs(st['max'])) <= ADC_FULL_SCALE
        st['in_24bit_range'] = in_range
        step = st.get('median_abs_delta', 0)
        ranked.append((not in_range, step / (st['span'] or 1), name, st))
    ranked.sort()
    # Drop the sort key, keep (score, name, stats) for callers.
    return [(score, name, st) for _bad, score, name, st in ranked]


class QidiCSRead:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.root_name = config.get('root', 'probe_air')
        self.sensor_attr = config.get('sensor_attr', DEFAULT_SENSOR_PATH)
        out_dir = config.get('out_dir', '~/printer_data/qidi_pa')
        self.out_dir = os.path.expanduser(out_dir)
        self.report_path = os.path.join(self.out_dir, 'read.json')

        self.gcode.register_command('QIDI_CS_READ', self.cmd_READ,
                                    desc=self.cmd_READ_help)
        self.gcode.register_command('QIDI_CS_STREAM', self.cmd_STREAM,
                                    desc=self.cmd_STREAM_help)

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
            logging.exception("qidi_cs_read: unhandled error")
            raise gcmd.error("qidi_cs_read: internal error, printer left running: "
                             "%s: %s" % (type(e).__name__, str(e)[:160]))

    # ------------------------------------------------------------------ utils

    def _record(self, kind, payload):
        entry = {'kind': kind, 'time': time.time(), 'payload': payload}
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
            logging.exception("qidi_cs_read: could not write report")
            return False

    def _sensor(self, gcmd):
        root = self.printer.lookup_object(self.root_name, None)
        if root is None:
            raise gcmd.error("qidi_cs_read: no object %s" % (self.root_name,))
        sensor = getattr(root, self.sensor_attr, None)
        if sensor is None:
            raise gcmd.error("qidi_cs_read: %s has no attribute %s - run "
                             "QIDI_CS_LOCATE" % (self.root_name,
                                                 self.sensor_attr))
        return sensor

    # ------------------------------------------------------------- B1: poll

    cmd_READ_help = ("Poll the load cell by calling read_origin_data(). "
                     "Press the nozzle by hand to confirm it responds. "
                     "[SECONDS=10] [HZ=10]")

    def cmd_READ(self, gcmd):
        return self._guard(gcmd, self._run_READ)

    def _run_READ(self, gcmd):
        sensor = self._sensor(gcmd)
        secs = gcmd.get_float('SECONDS', 10., minval=1., maxval=120.)
        hz = gcmd.get_float('HZ', 10., minval=0.5, maxval=200.)
        interval = 1.0 / hz

        read = getattr(sensor, 'read_origin_data', None)
        if read is None:
            raise gcmd.error("qidi_cs_read: no read_origin_data on the sensor")
        if not callable(read):
            # Stage A said it is a method; if a future firmware makes it a
            # property, getattr already did the read.
            gcmd.respond_info("qidi_cs_read: read_origin_data is not callable "
                              "- treating it as a value")

        gcmd.respond_info("qidi_cs_read: polling read_origin_data() for %.0fs "
                          "at %.1f Hz - press the nozzle gently now"
                          % (secs, hz))

        rows, errors = [], 0
        start = self.reactor.monotonic()
        eventtime = start
        deadline = start + secs
        first_raw = None
        while eventtime < deadline:
            try:
                v = read() if callable(read) else getattr(sensor,
                                                          'read_origin_data')
            except Exception as e:
                errors += 1
                if errors == 1:
                    gcmd.respond_info("qidi_cs_read: read raised: %s" % (e,))
                if errors > 5:
                    raise gcmd.error("qidi_cs_read: giving up after %d read "
                                     "errors: %s" % (errors, e))
                eventtime = self.reactor.pause(eventtime + interval)
                continue
            if first_raw is None:
                first_raw = v
            rows.append((eventtime - start, v))
            eventtime = self.reactor.pause(eventtime + interval)
        elapsed = self.reactor.monotonic() - start

        gcmd.respond_info("qidi_cs_read: return type is %s, first value %s"
                          % (type(first_raw).__name__, repr(first_raw)[:80]))

        vals = [v for _t, v in rows if _is_number(v)]
        if not vals:
            # Not a failure if it returns a tuple - report the shape so the next
            # step knows what to unpack.
            self._record('read', {'elapsed': elapsed, 'errors': errors,
                                  'return_type': type(first_raw).__name__,
                                  'sample': repr(first_raw)[:400],
                                  'rows': [[round(t, 4), repr(v)[:80]]
                                           for t, v in rows[:50]]})
            raise gcmd.error("qidi_cs_read: read_origin_data did not return a "
                             "number (got %s). See read.json for its shape."
                             % (type(first_raw).__name__,))

        st = _stats(vals)
        lo, hi, span = st['min'], st['max'], st['span']
        for t, v in rows[::max(1, len(rows) // 40)]:
            if not _is_number(v):
                continue
            frac = 0. if span == 0 else (v - lo) / span
            gcmd.respond_info("  %5.1fs %14.4f %s" % (t, v, '#' * int(frac*40)))

        gcmd.respond_info("qidi_cs_read: n=%d over %.2fs (%.1f Hz effective)"
                          % (st['n'], elapsed, st['n'] / elapsed))
        gcmd.respond_info("qidi_cs_read: mean=%.6g stdev=%.6g span=%.6g "
                          "drift=%.6g" % (st['mean'], st['stdev'], span,
                                          st['drift']))
        if span == 0:
            gcmd.respond_info("qidi_cs_read: value never moved - the sensor may "
                              "need sampling started (QIDI_CS_STREAM), or this "
                              "returns a cached value")
        else:
            gcmd.respond_info("qidi_cs_read: it moves. Press harder and rerun "
                              "to check the sign and scale.")
        ok = self._record('read', {
            'elapsed': elapsed, 'hz_requested': hz, 'errors': errors,
            'return_type': type(first_raw).__name__, 'stats': st,
            'rows': [[round(t, 4), v] for t, v in rows]})
        gcmd.respond_info("qidi_cs_read: written to %s"
                          % (self.report_path if ok else "(not written)",))

    # ----------------------------------------------------------- B2: stream

    cmd_STREAM_help = ("Start firmware sampling, drain bulk_queue and decode. "
                       "Always stops sampling on exit. "
                       "[SECONDS=2] [POLL_HZ=2560] [REST_TICKS=<override>]")

    def cmd_STREAM(self, gcmd):
        return self._guard(gcmd, self._run_STREAM)

    def _run_STREAM(self, gcmd):
        sensor = self._sensor(gcmd)
        cmd = getattr(sensor, 'query_cs1237_cmd', None)
        queue = getattr(sensor, 'bulk_queue', None)
        oid = getattr(sensor, 'oid', None)
        if cmd is None or queue is None or oid is None:
            raise gcmd.error("qidi_cs_read: need query_cs1237_cmd, bulk_queue "
                             "and oid on the sensor - run QIDI_CS_LOCATE")

        secs = gcmd.get_float('SECONDS', 2., minval=0.2, maxval=20.)
        poll_hz = gcmd.get_float('POLL_HZ', 2560., minval=10., maxval=20000.)
        mcu = sensor.get_mcu() if callable(getattr(sensor, 'get_mcu', None)) \
            else getattr(sensor, 'mcu', None)
        if mcu is None:
            raise gcmd.error("qidi_cs_read: cannot reach the sensor's MCU")
        rest_ticks = gcmd.get_int('REST_TICKS',
                                  int(mcu.seconds_to_clock(1.0 / poll_hz)),
                                  minval=1)
        bpb = int(getattr(sensor, 'bytes_per_block', 4) or 4)

        gcmd.respond_info("qidi_cs_read: oid=%d rest_ticks=%d (~%.0f Hz MCU "
                          "poll), capturing %.1fs" % (oid, rest_ticks, poll_hz,
                                                      secs))

        raw = []
        started = False
        try:
            # Stop first, in case an earlier run left sampling on.
            cmd.send([oid, 0])
            queue.clear_queue()
            cmd.send([oid, rest_ticks])
            started = True
            start = self.reactor.monotonic()
            eventtime = self.reactor.pause(start + secs)
            elapsed = eventtime - start
            raw = queue.pull_queue()
        finally:
            try:
                cmd.send([oid, 0])
            except Exception:
                logging.exception("qidi_cs_read: FAILED TO STOP SAMPLING")
                gcmd.respond_info("qidi_cs_read: WARNING could not stop "
                                  "sampling - restart klipper")
            else:
                if started:
                    gcmd.respond_info("qidi_cs_read: sampling stopped")

        msgs = len(raw)
        blob = b''
        for params in raw:
            d = params.get('data') if isinstance(params, dict) else None
            if isinstance(d, (bytes, bytearray)):
                blob += bytes(d)
        nblocks = len(blob) // bpb if bpb else 0
        gcmd.respond_info("qidi_cs_read: %d messages, %d bytes, %d blocks in "
                          "%.3fs" % (msgs, len(blob), nblocks, elapsed))
        if nblocks:
            gcmd.respond_info("qidi_cs_read: -> %.1f blocks/s, %.1f msgs/s"
                              % (nblocks / elapsed, msgs / elapsed))
        if not blob:
            gcmd.respond_info(
                "qidi_cs_read: no data arrived. Either rest_ticks is wrong for "
                "this firmware, or bulk_queue is registered for a different "
                "message than the one query_cs1237 produces.")
            self._record('stream', {'elapsed': elapsed, 'messages': msgs,
                                    'bytes': 0, 'rest_ticks': rest_ticks,
                                    'oid': oid})
            return

        decoded = _decode_all(blob, bpb)
        ranked = _rank_decodings(decoded)
        gcmd.respond_info("qidi_cs_read: candidate decodings, best first "
                          "(smallest step relative to span):")
        for score, name, st in ranked[:5]:
            gcmd.respond_info("  %-13s score=%.4f mean=%.6g stdev=%.6g "
                              "span=%.6g" % (name, score, st['mean'],
                                             st['stdev'], st['span']))
        if ranked:
            best = ranked[0][1]
            gcmd.respond_info("qidi_cs_read: best guess -> %s" % (best,))
            gcmd.respond_info("qidi_cs_read: a 24-bit converter should sit "
                              "well inside +/-8388608; check that before "
                              "trusting it.")
        ok = self._record('stream', {
            'elapsed': elapsed, 'messages': msgs, 'bytes': len(blob),
            'blocks': nblocks, 'rest_ticks': rest_ticks, 'oid': oid,
            'bytes_per_block': bpb,
            'blocks_per_sec': nblocks / elapsed if elapsed else 0,
            'rankings': [{'name': n, 'score': s, 'stats': st}
                         for s, n, st in ranked],
            'first_blocks_hex': blob[:64].hex(),
            'best': ranked[0][1] if ranked else None,
            'samples': decoded.get(ranked[0][1], [])[:2000] if ranked else []})
        gcmd.respond_info("qidi_cs_read: written to %s"
                          % (self.report_path if ok else "(not written)",))


def load_config(config):
    return QidiCSRead(config)
