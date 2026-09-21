# qidi_cs_batch.py - probe: can we get more than one sample per round trip?
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
# WHY
#   read_origin_data() polls at ~77 Hz, which is well under one sample per PA
#   transition. But the firmware advertises
#
#       query_cs1237_read oid=%c reg=%u read_len=%u
#           -> query_cs1237_data oid=%c data=%*s        (VARIABLE length)
#
#   and the chip object carries blocks_per_msg=12, bytes_per_block=4 - 48 bytes,
#   i.e. twelve samples in one reply. If that is what this command returns, then
#   read_origin_data() is already pulling twelve samples per round trip and
#   discarding eleven of them.
#
#   That would be ~930 Hz effective instead of 77, and - more importantly -
#   samples WITHIN a batch are consecutive at 1280 SPS, so their relative spacing
#   is exactly 0.78 ms even though the batch's absolute placement is not.
#
#   That is the difference between integrating a residual because the transient
#   cannot be seen, and resolving its shape.
#
#   Confusingly the wrapper bound to this command is named
#   `query_cs1237_end_cmd`. The names on this driver do not describe what the
#   commands do.
#
# SAFETY
#   No motion, no heating, no extrusion. It sends one synchronous query per
#   attempt - the same class of call as read_origin_data(), which a flow run
#   already makes thousands of times.
#
#   It IS an undocumented vendor command called with guessed parameters. It is
#   named "read", its reply is named "data", and the CS1237 has no destructive
#   register operations - but that is inference, not proof. Run it cold and
#   idle, and stop if the printer does anything unexpected.
#
# USAGE
#   [qidi_cs_batch]
#
#   QIDI_CS_BATCH                 sweep a small set of (reg, len) candidates
#   QIDI_CS_BATCH REG=0 LEN=48    try one combination
#
#   Results are appended to ~/printer_data/qidi_pa/batch.json

import json
import logging
import os
import struct
import time

# blocks_per_msg x bytes_per_block on the live chip object.
EXPECTED_LEN = 48
CANDIDATE_REGS = (0, 1, 2)
CANDIDATE_LENS = (4, 12, 48)


def _s24(b0, b1, b2):
    v = (b0 << 16) | (b1 << 8) | b2
    return v - 0x1000000 if v & 0x800000 else v


def _decode(blob, bpb=4):
    out = {'i32le': [], 'i32be': [], 'i24be_first': [], 'i24le_first': [],
           'i24be_last': []}
    for i in range(0, len(blob) - bpb + 1, bpb):
        b = blob[i:i + bpb]
        if len(b) < 4:
            continue
        out['i32le'].append(struct.unpack('<i', b)[0])
        out['i32be'].append(struct.unpack('>i', b)[0])
        out['i24be_first'].append(_s24(b[0], b[1], b[2]))
        out['i24le_first'].append(_s24(b[2], b[1], b[0]))
        out['i24be_last'].append(_s24(b[1], b[2], b[3]))
    return out


def _plausible(vals, ref=None):
    # A real batch of consecutive 24-bit samples: inside +/-2^23, and adjacent
    # values close together because 0.78 ms apart on a quiet sensor.
    if len(vals) < 2:
        return None
    lo, hi = min(vals), max(vals)
    if max(abs(lo), abs(hi)) > (1 << 23):
        return None
    steps = [abs(b - a) for a, b in zip(vals, vals[1:])]
    med = sorted(steps)[len(steps) // 2]
    span = hi - lo
    score = {'n': len(vals), 'min': lo, 'max': hi, 'span': span,
             'median_step': med}
    if ref is not None and span >= 0:
        # Does the batch bracket the value read_origin_data() returns?
        score['brackets_ref'] = (lo - max(1000, span)) <= ref <= (hi + max(1000, span))
    return score


class QidiCSBatch:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.root_name = config.get('root', 'probe_air')
        self.sensor_attr = config.get('sensor_attr', 'sensor_helper')
        out_dir = config.get('out_dir', '~/printer_data/qidi_pa')
        self.out_dir = os.path.expanduser(out_dir)
        self.report_path = os.path.join(self.out_dir, 'batch.json')
        self.gcode.register_command('QIDI_CS_BATCH', self.cmd_BATCH,
                                    desc=self.cmd_BATCH_help)

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
            logging.exception("qidi_cs_batch: unhandled error")
            raise gcmd.error("batch: internal error, printer left running: "
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
            data.append({'kind': 'batch', 'time': time.time(),
                         'payload': payload})
            tmp = self.report_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=1, default=repr)
            os.replace(tmp, self.report_path)
            return True
        except Exception:
            logging.exception("qidi_cs_batch: could not write report")
            return False

    def _sensor(self, gcmd):
        root = self.printer.lookup_object(self.root_name, None)
        if root is None:
            raise gcmd.error("batch: no object %s" % (self.root_name,))
        s = getattr(root, self.sensor_attr, None)
        if s is None:
            raise gcmd.error("batch: %s has no %s" % (self.root_name,
                                                      self.sensor_attr))
        return s

    def _attempt(self, gcmd, sensor, cmd, oid, reg, ln, ref):
        try:
            resp = cmd.send([oid, reg, ln])
        except Exception as e:
            gcmd.respond_info("  reg=%-3d len=%-3d  -> error: %s"
                              % (reg, ln, str(e)[:70]))
            return {'reg': reg, 'len': ln, 'error': str(e)[:200]}
        blob = None
        if isinstance(resp, dict):
            for k in ('data', 'response', 'value'):
                v = resp.get(k)
                if isinstance(v, (bytes, bytearray)):
                    blob = bytes(v)
                    break
        if blob is None:
            gcmd.respond_info("  reg=%-3d len=%-3d  -> reply with no byte "
                              "field: %s" % (reg, ln, sorted(resp.keys())
                                             if isinstance(resp, dict)
                                             else type(resp).__name__))
            return {'reg': reg, 'len': ln, 'reply_keys':
                    sorted(resp.keys()) if isinstance(resp, dict) else None}
        dec = _decode(blob)
        best, best_s = None, None
        for name, vals in dec.items():
            sc = _plausible(vals, ref)
            if sc is None:
                continue
            if best_s is None or sc['median_step'] < best_s['median_step']:
                best, best_s = name, sc
        gcmd.respond_info("  reg=%-3d len=%-3d  -> %d bytes  %s"
                          % (reg, ln, len(blob), blob[:16].hex()))
        if best:
            gcmd.respond_info("      best layout %-12s n=%d  span=%d  "
                              "median step=%d%s"
                              % (best, best_s['n'], best_s['span'],
                                 best_s['median_step'],
                                 "  BRACKETS read_origin_data"
                                 if best_s.get('brackets_ref') else ""))
        return {'reg': reg, 'len': ln, 'bytes': len(blob),
                'hex': blob.hex()[:256], 'best_layout': best,
                'best_score': best_s,
                'samples': dec.get(best, [])[:64] if best else []}

    cmd_BATCH_help = ("Probe whether query_cs1237_read returns a BATCH of "
                      "samples rather than one. No motion, no heating. "
                      "[REG=] [LEN=]")

    def cmd_BATCH(self, gcmd):
        return self._guard(gcmd, self._run_BATCH)

    def _run_BATCH(self, gcmd):
        sensor = self._sensor(gcmd)
        oid = getattr(sensor, 'oid', None)
        cmd = getattr(sensor, 'query_cs1237_end_cmd', None)
        read = getattr(sensor, 'read_origin_data', None)
        if oid is None or cmd is None:
            raise gcmd.error("batch: sensor has no oid or "
                             "query_cs1237_end_cmd - run QIDI_CS_LOCATE")
        # A reference single reading taken now, to compare batches against.
        ref = None
        try:
            v = read() if callable(read) else None
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                ref = float(v)
        except Exception:
            pass
        gcmd.respond_info("batch: oid=%d, read_origin_data() = %s"
                          % (oid, "%.0f" % ref if ref is not None else "n/a"))
        gcmd.respond_info("batch: expecting %d bytes = 12 samples if this is "
                          "the batched read" % (EXPECTED_LEN,))

        reg = gcmd.get_int('REG', None)
        ln = gcmd.get_int('LEN', None)
        if reg is not None or ln is not None:
            pairs = [(reg if reg is not None else 0,
                      ln if ln is not None else EXPECTED_LEN)]
        else:
            pairs = [(r, l) for l in CANDIDATE_LENS for r in CANDIDATE_REGS]
        results = []
        for r, l in pairs:
            results.append(self._attempt(gcmd, sensor, cmd, oid, r, l, ref))

        hits = [x for x in results if x.get('best_layout')
                and (x.get('best_score') or {}).get('n', 0) > 1]
        if hits:
            gcmd.respond_info("batch: %d attempt(s) returned multi-sample "
                              "data - this is the batched read, and Stage 3 "
                              "can resolve transients rather than integrate "
                              "them" % (len(hits),))
        else:
            gcmd.respond_info("batch: nothing returned a plausible multi-sample "
                              "batch. Stage 3 stays with its integrating "
                              "design - which is expected to work, just "
                              "with less margin.")
        ok = self._record({'oid': oid, 'ref': ref, 'attempts': results})
        gcmd.respond_info("batch: written to %s"
                          % (self.report_path if ok else "(not written)",))


def load_config(config):
    return QidiCSBatch(config)
