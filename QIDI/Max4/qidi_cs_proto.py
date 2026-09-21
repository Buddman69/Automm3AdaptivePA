# qidi_cs_proto.py - Stage B2b: map the CS1237 wire protocol, then try to
#                    actually trigger a bulk stream
#
# Copyright (C) 2026  Budd
# Licensed under PolyForm Strict 1.0.0 - see LICENSE.md.
#
# WHY
#   QIDI_CS_STREAM sent `query_cs1237 oid rest_ticks` and no data arrived.
#   Two guesses were possible - wrong command, or wrong queue - and guessing
#   again is not a method.  Klipper's own objects hold the answer:
#
#     * mcu.CommandWrapper / CommandQueryWrapper keep the msgformat they were
#       built from, so every `query_cs1237_*_cmd` attribute can be mapped to the
#       exact MCU command and response it is bound to.  Two of those wrapper
#       names have no matching format string in the .so, so the mapping is not
#       what the names suggest.
#     * serialhdl registers response handlers in a dict keyed by (name, oid),
#       so we can read which message bulk_queue is actually waiting for instead
#       of assuming it is `cs1237_data`.
#
#   All of that is stock Klipper Python - plain attributes on objects whose
#   source is on the machine.  Reading them invokes no vendor code.
#
# SAFETY
#   QIDI_CS_PROTO is read-only and calls nothing.
#
#   QIDI_CS_TRY sends commands.  It sends stop (rest_ticks=0) before it starts
#   and again in a finally block, exactly as QIDI_CS_STREAM does.  It never
#   touches cs1237_setup_home - that is the probe's endstop path and driving it
#   is how the Z probe gets broken (SAFETY.md rule 4).
#
# USAGE
#   [qidi_cs_proto]
#
#   QIDI_CS_PROTO                       map wrappers, handlers and MCU commands
#   QIDI_CS_TRY [BEGIN=1] [CONFIG=60] [REST_TICKS=...] [SECONDS=2]
#
#   Results are appended to ~/printer_data/qidi_pa/proto.json

import json
import logging
import os
import time

DEFAULT_CONFIG_REG = 0x3c          # 60 = channel A, gain 128, 1280 SPS


def _describe_wrapper(w):
    # CommandWrapper keeps _cmd (a MessageFormat) and CommandQueryWrapper also
    # keeps _response (a message name).  Both are stock Klipper attributes.
    out = {'type': type(w).__name__}
    cmd = getattr(w, '_cmd', None)
    if cmd is not None:
        for attr in ('msgformat', 'name', 'msgid'):
            v = getattr(cmd, attr, None)
            if v is not None:
                out[attr] = v
    resp = getattr(w, '_response', None)
    if resp is not None:
        out['response'] = resp
    oid = getattr(w, '_oid', None)
    if oid is not None:
        out['oid'] = oid
    return out


class QidiCSProto:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.root_name = config.get('root', 'probe_air')
        self.sensor_attr = config.get('sensor_attr', 'sensor_helper')
        out_dir = config.get('out_dir', '~/printer_data/qidi_pa')
        self.out_dir = os.path.expanduser(out_dir)
        self.report_path = os.path.join(self.out_dir, 'proto.json')

        self.gcode.register_command('QIDI_CS_PROTO', self.cmd_PROTO,
                                    desc=self.cmd_PROTO_help)
        self.gcode.register_command('QIDI_CS_TRY', self.cmd_TRY,
                                    desc=self.cmd_TRY_help)

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
            logging.exception("qidi_cs_proto: unhandled error")
            raise gcmd.error("qidi_cs_proto: internal error, printer left running: "
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
            logging.exception("qidi_cs_proto: could not write report")
            return False

    def _sensor(self, gcmd):
        root = self.printer.lookup_object(self.root_name, None)
        if root is None:
            raise gcmd.error("qidi_cs_proto: no object %s" % (self.root_name,))
        s = getattr(root, self.sensor_attr, None)
        if s is None:
            raise gcmd.error("qidi_cs_proto: %s has no %s"
                             % (self.root_name, self.sensor_attr))
        return s

    def _serial(self, sensor):
        mcu = getattr(sensor, 'mcu', None)
        if mcu is None:
            return None
        return getattr(mcu, '_serial', None)

    # ------------------------------------------------------------ read-only

    cmd_PROTO_help = ("Map every cs1237 command wrapper to its MCU message, "
                      "list registered response handlers, and dump the "
                      "firmware's cs1237 commands. Read-only.")

    def cmd_PROTO(self, gcmd):
        return self._guard(gcmd, self._run_PROTO)

    def _run_PROTO(self, gcmd):
        sensor = self._sensor(gcmd)

        # 1. Which MCU command is each Python wrapper actually bound to?
        wrappers = {}
        for name in sorted(dir(sensor)):
            if not name.endswith('_cmd'):
                continue
            try:
                w = getattr(sensor, name)
            except Exception:
                continue
            wrappers[name] = _describe_wrapper(w)
        gcmd.respond_info("qidi_cs_proto: %d command wrappers" % (len(wrappers),))
        for name, d in wrappers.items():
            gcmd.respond_info("  %-30s %s" % (name, d.get('msgformat',
                                                          d.get('name', '?'))))
            if 'response' in d:
                gcmd.respond_info("  %-30s   -> reply: %s"
                                  % ('', d['response']))

        # 2. What is bulk_queue actually listening for?  This is the question
        #    that sank QIDI_CS_STREAM.
        handlers = []
        serial = self._serial(sensor)
        hdict = getattr(serial, 'handlers', None) if serial else None
        if isinstance(hdict, dict):
            for key in hdict:
                try:
                    nm, oid = key
                except Exception:
                    nm, oid = repr(key), None
                if 'cs1237' in str(nm).lower():
                    handlers.append({'message': str(nm), 'oid': oid})
        gcmd.respond_info("qidi_cs_proto: cs1237 response handlers registered:")
        if handlers:
            for h in handlers:
                gcmd.respond_info("    %s  oid=%s" % (h['message'], h['oid']))
        else:
            gcmd.respond_info("    (none found - bulk_queue may be listening "
                              "on a name without 'cs1237' in it)")

        # 3. Everything the firmware advertises, so we can see commands the
        #    driver never wires up.
        mcu_cmds = []
        msgparser = None
        try:
            msgparser = serial.get_msgparser() if serial else None
        except Exception:
            msgparser = None
        for attr in ('messages_by_name',):
            d = getattr(msgparser, attr, None) if msgparser else None
            if isinstance(d, dict):
                for nm, fmt in d.items():
                    if 'cs1237' in str(nm).lower():
                        mcu_cmds.append(getattr(fmt, 'msgformat', str(nm)))
        mcu_cmds = sorted(set(mcu_cmds))
        gcmd.respond_info("qidi_cs_proto: firmware advertises %d cs1237 "
                          "messages:" % (len(mcu_cmds),))
        for m in mcu_cmds:
            gcmd.respond_info("    %s" % (m,))

        bq = getattr(sensor, 'bulk_queue', None)
        qlen = len(getattr(bq, 'raw_samples', []) or []) if bq else None
        gcmd.respond_info("qidi_cs_proto: bulk_queue currently holds %s messages"
                          % (qlen,))

        ok = self._record('proto', {'wrappers': wrappers, 'handlers': handlers,
                                    'mcu_cs1237_messages': mcu_cmds,
                                    'bulk_queue_len': qlen,
                                    'oid': getattr(sensor, 'oid', None)})
        gcmd.respond_info("qidi_cs_proto: written to %s"
                          % (self.report_path if ok else "(not written)",))

    # --------------------------------------------------------------- attempt

    cmd_TRY_help = ("Try to trigger a bulk stream: optionally send begin first, "
                    "then query_cs1237 with rest_ticks, then drain. Always "
                    "stops. [BEGIN=1] [CONFIG=60] [REST_TICKS=n] [SECONDS=2]")

    def cmd_TRY(self, gcmd):
        return self._guard(gcmd, self._run_TRY)

    def _run_TRY(self, gcmd):
        sensor = self._sensor(gcmd)
        oid = getattr(sensor, 'oid', None)
        qcmd = getattr(sensor, 'query_cs1237_cmd', None)
        bq = getattr(sensor, 'bulk_queue', None)
        if oid is None or qcmd is None or bq is None:
            raise gcmd.error("qidi_cs_proto: sensor is missing oid, "
                             "query_cs1237_cmd or bulk_queue")

        do_begin = gcmd.get_int('BEGIN', 1, minval=0, maxval=1)
        cfg = gcmd.get_int('CONFIG', DEFAULT_CONFIG_REG, minval=0, maxval=255)
        secs = gcmd.get_float('SECONDS', 2., minval=0.2, maxval=20.)
        mcu = getattr(sensor, 'mcu', None)
        default_ticks = 28125
        try:
            default_ticks = int(mcu.seconds_to_clock(1.0 / 2560.))
        except Exception:
            pass
        rest_ticks = gcmd.get_int('REST_TICKS', default_ticks, minval=1)

        notes = []
        begin_reply = None
        try:
            qcmd.send([oid, 0])
            bq.clear_queue()
            if do_begin:
                bcmd = getattr(sensor, 'query_cs1237_begin_cmd', None)
                if bcmd is None:
                    notes.append("no query_cs1237_begin_cmd")
                else:
                    try:
                        begin_reply = bcmd.send([oid, cfg])
                        gcmd.respond_info("qidi_cs_proto: begin(config=%d) -> %s"
                                          % (cfg, repr(begin_reply)[:120]))
                    except Exception as e:
                        notes.append("begin failed: %s" % (e,))
                        gcmd.respond_info("qidi_cs_proto: begin failed: %s"
                                          % (e,))
            qcmd.send([oid, rest_ticks])
            gcmd.respond_info("qidi_cs_proto: query_cs1237 oid=%d rest_ticks=%d"
                              % (oid, rest_ticks))
            start = self.reactor.monotonic()
            now = self.reactor.pause(start + secs)
            elapsed = now - start
            raw = bq.pull_queue()
        finally:
            try:
                qcmd.send([oid, 0])
            except Exception:
                logging.exception("qidi_cs_proto: FAILED TO STOP SAMPLING")
                gcmd.respond_info("qidi_cs_proto: WARNING could not stop "
                                  "sampling - restart klipper")
            else:
                gcmd.respond_info("qidi_cs_proto: sampling stopped")

        nbytes = 0
        for p in raw:
            d = p.get('data') if isinstance(p, dict) else None
            if isinstance(d, (bytes, bytearray)):
                nbytes += len(d)
        gcmd.respond_info("qidi_cs_proto: %d messages, %d bytes in %.3fs"
                          % (len(raw), nbytes, elapsed))
        if raw:
            gcmd.respond_info("qidi_cs_proto: first message keys: %s"
                              % (sorted(raw[0].keys())
                                 if isinstance(raw[0], dict) else type(raw[0])))
        else:
            gcmd.respond_info(
                "qidi_cs_proto: still nothing. Run QIDI_CS_PROTO and compare "
                "the handler's message name against what query_cs1237 emits - "
                "if the only push path is cs1237_setup_home, bulk streaming "
                "exists only during a probe and we stop here.")
        ok = self._record('try', {
            'begin': bool(do_begin), 'config': cfg, 'rest_ticks': rest_ticks,
            'elapsed': elapsed, 'messages': len(raw), 'bytes': nbytes,
            'begin_reply': repr(begin_reply)[:300], 'notes': notes,
            'first': repr(raw[0])[:300] if raw else None})
        gcmd.respond_info("qidi_cs_proto: written to %s"
                          % (self.report_path if ok else "(not written)",))


def load_config(config):
    return QidiCSProto(config)
