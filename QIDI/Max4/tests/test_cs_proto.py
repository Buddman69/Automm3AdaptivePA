"""Off-printer tests for qidi_cs_proto (Stage B2b).

The point of this module is to stop guessing which MCU message each Python
wrapper is bound to, so the tests check that it reads the binding rather than
the attribute name - two wrappers on the real machine are named nothing like
the command they carry.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mock_klipper import MockReactor, MockPrinter, MockConfig
import qidi_cs_proto


class MockMsgFormat:
    def __init__(self, msgformat, msgid=0):
        self.msgformat = msgformat
        self.name = msgformat.split()[0]
        self.msgid = msgid


class MockCommandWrapper:
    def __init__(self, msgformat, response=None, fail=False):
        self._cmd = MockMsgFormat(msgformat)
        if response:
            self._response = response
        self.sends = []
        self.fail = fail
        self.reply = {'oid': 5, 'config': 60}

    def send(self, data=(), minclock=0, reqclock=0):
        self.sends.append(list(data))
        if self.fail:
            raise RuntimeError("simulated failure")
        return self.reply if hasattr(self, '_response') else None


class MockMsgParser:
    def __init__(self):
        self.messages_by_name = {
            'query_cs1237': MockMsgFormat('query_cs1237 oid=%c rest_ticks=%u'),
            'cs1237_data': MockMsgFormat('cs1237_data oid=%c data=%*s'),
            'cs1237_home_state': MockMsgFormat(
                'cs1237_home_state oid=%c homing=%c trigger_clock=%u'),
            'stepper_move': MockMsgFormat('stepper_move oid=%c'),
        }


class MockSerial:
    def __init__(self):
        # Keyed (name, oid) exactly as serialhdl does.
        self.handlers = {('cs1237_data', 5): object(),
                         ('stats', None): object()}
        self._mp = MockMsgParser()

    def get_msgparser(self):
        return self._mp


class MockMCU:
    def __init__(self):
        self._serial = MockSerial()

    def seconds_to_clock(self, t):
        return int(t * 72_000_000)


class MockBulkQueue:
    def __init__(self, pending=None):
        self.raw_samples = []
        self.pending = list(pending or [])
        self.cleared = 0

    def pull_queue(self):
        out = self.raw_samples + self.pending
        self.raw_samples = []
        self.pending = []
        return out

    def clear_queue(self):
        self.cleared += 1
        self.raw_samples = []


class FakeSensor:
    def __init__(self, pending=None, begin_fails=False):
        self.oid = 5
        self.mcu = MockMCU()
        self.bulk_queue = MockBulkQueue(pending)
        self.query_cs1237_cmd = MockCommandWrapper(
            'query_cs1237 oid=%c rest_ticks=%u')
        self.query_cs1237_begin_cmd = MockCommandWrapper(
            'query_cs1237_begin oid=%c config=%u',
            response='query_cs1237_begin_read', fail=begin_fails)
        # The two whose names do not match their command - the whole reason
        # this module reads bindings instead of trusting names.
        self.query_cs1237_update_cmd = MockCommandWrapper(
            'query_cs1237_read oid=%c reg=%u read_len=%u',
            response='query_cs1237_data')
        self.query_cs1237_end_cmd = MockCommandWrapper(
            'query_cs1237_zero oid=%c', response='query_cs1237_zero_read')


class FakeProbeAir:
    def __init__(self, sensor):
        self.sensor_helper = sensor


def build(sensor, tmpdir):
    printer = MockPrinter(MockReactor())
    printer.add('probe_air', FakeProbeAir(sensor))
    cfg = MockConfig(printer, {'out_dir': tmpdir})
    mod = qidi_cs_proto.load_config(cfg)
    return printer.lookup_object('gcode'), mod


def check(label, cond, detail=''):
    print(("  PASS  " if cond else "  FAIL  ") + label + (
        ('  <- ' + detail) if detail and not cond else ''))
    return cond


def main():
    ok = True
    tmpdir = tempfile.mkdtemp()

    print("\n== QIDI_CS_PROTO maps wrappers to real commands ==")
    s = FakeSensor()
    g, _m = build(s, tmpdir)
    out = g.run('QIDI_CS_PROTO')
    text = "\n".join(out)
    ok &= check("reads the binding, not the attribute name",
                'query_cs1237_read oid=%c reg=%u read_len=%u' in text,
                text[:600])
    ok &= check("shows the reply message for query wrappers",
                'reply: query_cs1237_data' in text, text[:600])
    ok &= check("finds the registered cs1237 handler",
                'cs1237_data  oid=5' in text, text[-600:])
    ok &= check("lists firmware cs1237 messages, excluding unrelated ones",
                'cs1237_home_state' in text and 'stepper_move' not in text,
                text[-600:])

    print("\n== QIDI_CS_TRY sends begin then query, and always stops ==")
    s2 = FakeSensor(pending=[{'oid': 5, 'data': b'\x00' * 48}])
    g2, _m = build(s2, tmpdir)
    out2 = g2.run('QIDI_CS_TRY', BEGIN=1, CONFIG=60, SECONDS=1)
    text2 = "\n".join(out2)
    bsends = s2.query_cs1237_begin_cmd.sends
    qsends = s2.query_cs1237_cmd.sends
    ok &= check("stops before starting", qsends[0] == [5, 0], str(qsends))
    ok &= check("sends begin with the config register",
                bsends == [[5, 60]], str(bsends))
    ok &= check("then starts sampling",
                qsends[1][0] == 5 and qsends[1][1] > 0, str(qsends))
    ok &= check("stops at the end", qsends[-1] == [5, 0], str(qsends))
    ok &= check("reports the captured message", '1 messages, 48 bytes' in text2,
                text2[-400:])

    print("\n== BEGIN=0 skips begin ==")
    s3 = FakeSensor()
    g3, _m = build(s3, tmpdir)
    g3.run('QIDI_CS_TRY', BEGIN=0, SECONDS=1)
    ok &= check("begin not sent", s3.query_cs1237_begin_cmd.sends == [],
                str(s3.query_cs1237_begin_cmd.sends))
    ok &= check("still stopped at the end",
                s3.query_cs1237_cmd.sends[-1] == [5, 0],
                str(s3.query_cs1237_cmd.sends))

    print("\n== a failing begin does not abort the attempt ==")
    s4 = FakeSensor(begin_fails=True)
    g4, _m = build(s4, tmpdir)
    out4 = g4.run('QIDI_CS_TRY', BEGIN=1, SECONDS=1)
    ok &= check("reports the failure", any('begin failed' in l for l in out4),
                "\n".join(out4)[:300])
    ok &= check("still tried to stream and still stopped",
                s4.query_cs1237_cmd.sends[-1] == [5, 0]
                and len(s4.query_cs1237_cmd.sends) >= 3,
                str(s4.query_cs1237_cmd.sends))

    print("\n== empty result explains itself ==")
    ok &= check("points at the setup_home possibility",
                'only during a probe' in "\n".join(g3.output)
                or 'still nothing' in "\n".join(g3.output),
                "\n".join(g3.output)[-300:])

    print("\n== report file ==")
    path = os.path.join(tmpdir, 'proto.json')
    ok &= check("proto.json written", os.path.exists(path))
    if os.path.exists(path):
        import json
        with open(path) as f:
            data = json.load(f)
        kinds = {e['kind'] for e in data}
        ok &= check("contains both result kinds", {'proto', 'try'} <= kinds,
                    str(kinds))

    print("\n== an internal error must NOT be allowed to shut the MCU down ==")
    # Klipper turns an unhandled exception in a gcode command into an INTERNAL
    # ERROR and latches every MCU into shutdown - that happened for real on
    # 2026-09-15 and took a FIRMWARE_RESTART to clear. The guard has to turn
    # anything unexpected into a clean command failure instead.

    def boom(*a, **k):
        raise NameError("corr_ms")

    for name, attr in (('QIDI_CS_PROTO', '_run_PROTO'),
                       ('QIDI_CS_TRY', '_run_TRY')):
        g, mod = build(FakeSensor(), tempfile.mkdtemp())
        setattr(mod, attr, boom)
        try:
            g.run(name)
            ok &= check("%s converts a stray exception" % name, False,
                        "nothing raised")
        except RuntimeError as e:
            ok &= check("%s converts a stray exception" % name,
                        'internal error' in str(e) and 'NameError' in str(e),
                        str(e))
        except NameError:
            ok &= check("%s converts a stray exception" % name, False,
                        "NameError escaped - this shuts both MCUs down")

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
