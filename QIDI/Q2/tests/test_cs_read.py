"""Off-printer tests for qidi_cs_read (Stage B).

Two things matter here beyond "does it compute the right numbers":

  * B2 must ALWAYS stop firmware sampling, including when the capture throws.
    Leaving the chip streaming costs ~12% of the toolhead serial link.
  * The block decoder must pick the right byte layout from the data alone,
    since nobody has published the CS1237 bulk format for this machine.
"""
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mock_klipper import MockReactor, MockPrinter, MockConfig
import qidi_cs_read


BASELINE = 4_200_000        # inside +/-2^23, as a real 24-bit reading must be


class MockMCU:
    def __init__(self, freq=72_000_000.):
        self.freq = freq

    def seconds_to_clock(self, t):
        return int(t * self.freq)


class MockCommand:
    """Stands in for Klipper's CommandWrapper - records every send."""

    def __init__(self, fail=False):
        self.sends = []
        self.fail = fail

    def send(self, data=(), minclock=0, reqclock=0):
        self.sends.append(list(data))
        if self.fail:
            raise RuntimeError("simulated serial failure")


class MockBulkQueue:
    """`pending` models messages that arrive from the MCU *during* the capture
    window.  They must survive clear_queue(), which only drops what was already
    stale - getting this wrong made the module look broken when it was right."""

    def __init__(self, messages=None):
        self.raw_samples = []
        self.pending = list(messages or [])
        self.cleared = 0

    def pull_queue(self):
        out = self.raw_samples + self.pending
        self.raw_samples = []
        self.pending = []
        return out

    def clear_queue(self):
        self.cleared += 1
        self.raw_samples = []


def encode_i24be_pad(vals, pad=0x00):
    """The layout we expect: 24-bit big-endian sample + one pad byte."""
    out = bytearray()
    for v in vals:
        u = v & 0xFFFFFF
        out += bytes([(u >> 16) & 0xFF, (u >> 8) & 0xFF, u & 0xFF, pad])
    return bytes(out)


class FakeSensor:
    def __init__(self, reactor, messages=None, fail_cmd=False,
                 read_returns=None):
        self._reactor = reactor
        self.oid = 5
        self.bytes_per_block = 4
        self.blocks_per_msg = 12
        self.cs_fil_f = 0.9
        self.mcu = MockMCU()
        self.query_cs1237_cmd = MockCommand(fail=fail_cmd)
        self.bulk_queue = MockBulkQueue(messages)
        self.press = 0.
        self._read_returns = read_returns

    def get_mcu(self):
        return self.mcu

    def read_origin_data(self):
        if self._read_returns is not None:
            return self._read_returns
        t = self._reactor.monotonic()
        return BASELINE + math.sin(t * 3.1) * 150. + self.press


class FakeProbeAir:
    def __init__(self, sensor):
        self.sensor_helper = sensor


def build(sensor, tmpdir):
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    printer.add('probe_air', FakeProbeAir(sensor))
    cfg = MockConfig(printer, {'out_dir': tmpdir})
    mod = qidi_cs_read.load_config(cfg)
    return printer.lookup_object('gcode'), mod, reactor


def check(label, cond, detail=''):
    print(("  PASS  " if cond else "  FAIL  ") + label + (
        ('  <- ' + detail) if detail and not cond else ''))
    return cond


def main():
    ok = True
    tmpdir = tempfile.mkdtemp()

    print("\n== B1: QIDI_CS_READ polls and reports ==")
    reactor = MockReactor()
    sensor = FakeSensor(reactor)
    g, mod, reactor2 = build(sensor, tmpdir)
    sensor._reactor = reactor2
    out = g.run('QIDI_CS_READ', SECONDS=5, HZ=10)
    text = "\n".join(out)
    ok &= check("reports the return type", 'return type is float' in text,
                text[:300])
    ok &= check("reports mean/stdev/span/drift",
                all(k in text for k in ('mean=', 'stdev=', 'span=', 'drift=')),
                text[-300:])
    ok &= check("detects that the value moves", 'it moves' in text,
                text[-300:])

    print("\n== B1: a non-numeric return is reported, not swallowed ==")
    g2, _m, r2 = build(FakeSensor(MockReactor(), read_returns=(1, 2, 3)),
                       tmpdir)
    try:
        g2.run('QIDI_CS_READ', SECONDS=2, HZ=5)
        ok &= check("raises with the shape of the return value", False,
                    "no error raised")
    except RuntimeError as e:
        ok &= check("raises with the shape of the return value",
                    'did not return a number' in str(e) and 'tuple' in str(e),
                    str(e))

    print("\n== B2: starts, captures, and stops ==")
    vals = [BASELINE + int(math.sin(i * 0.11) * 400) for i in range(240)]
    msgs = [{'data': encode_i24be_pad(vals[i:i + 12])}
            for i in range(0, 240, 12)]
    sensor = FakeSensor(MockReactor(), messages=msgs)
    g3, _m, r3 = build(sensor, tmpdir)
    out = g3.run('QIDI_CS_STREAM', SECONDS=2, POLL_HZ=2560)
    text = "\n".join(out)
    sends = sensor.query_cs1237_cmd.sends
    ok &= check("stops before starting (clears a stuck stream)",
                sends[0] == [5, 0], str(sends))
    ok &= check("starts with a non-zero rest_ticks",
                len(sends) > 1 and sends[1][0] == 5 and sends[1][1] > 0,
                str(sends))
    ok &= check("stops again at the end", sends[-1] == [5, 0], str(sends))
    ok &= check("rest_ticks matches the MCU clock",
                sends[1][1] == int(72_000_000 / 2560.), str(sends[1]))
    ok &= check("cleared the queue before capturing",
                sensor.bulk_queue.cleared == 1)
    ok &= check("counts messages and blocks",
                '20 messages' in text and '240 blocks' in text, text[:400])

    print("\n== B2: picks the right byte layout from the data ==")
    ok &= check("best guess is i24be_first", 'best guess -> i24be_first' in text,
                text[-500:])

    print("\n== B2: stops sampling even when the capture fails ==")
    bad = FakeSensor(MockReactor(), messages=[])
    orig = bad.bulk_queue.pull_queue

    def boom():
        raise RuntimeError("capture blew up")
    bad.bulk_queue.pull_queue = boom
    g4, _m, r4 = build(bad, tmpdir)
    try:
        g4.run('QIDI_CS_STREAM', SECONDS=1)
    except RuntimeError:
        pass
    ok &= check("stop was still sent after an exception",
                bad.query_cs1237_cmd.sends[-1] == [5, 0],
                str(bad.query_cs1237_cmd.sends))

    print("\n== B2: warns loudly if the stop itself fails ==")
    stuck = FakeSensor(MockReactor(), messages=[], fail_cmd=True)
    g5, _m, r5 = build(stuck, tmpdir)
    try:
        out5 = g5.run('QIDI_CS_STREAM', SECONDS=1)
    except Exception:
        out5 = g5.output
    ok &= check("tells the user to restart klipper",
                any('could not stop' in l for l in out5), "\n".join(out5))

    print("\n== decoder unit checks ==")
    blob = encode_i24be_pad([BASELINE, BASELINE + 10, BASELINE - 7,
                             BASELINE + 3])
    dec = qidi_cs_read._decode_all(blob, 4)
    ok &= check("i24be_first round-trips exactly",
                dec['i24be_first'] == [BASELINE, BASELINE + 10, BASELINE - 7,
                                       BASELINE + 3],
                str(dec['i24be_first']))
    ranked = qidi_cs_read._rank_decodings(dec)
    ok &= check("ranking prefers the in-range decoding",
                ranked[0][1] == 'i24be_first',
                str([(n, round(s, 4), st['in_24bit_range'])
                     for s, n, st in ranked]))
    i32be = [s for s in ranked if s[1] == 'i32be']
    ok &= check("32-bit big-endian is rejected as out of 24-bit range",
                bool(i32be) and not i32be[0][2]['in_24bit_range'],
                str(i32be))
    ok &= check("negative 24-bit values decode signed",
                qidi_cs_read._s24(0xFF, 0xFF, 0xFF) == -1)

    print("\n== report file ==")
    path = os.path.join(tmpdir, 'read.json')
    ok &= check("read.json written", os.path.exists(path))
    if os.path.exists(path):
        import json
        with open(path) as f:
            data = json.load(f)
        kinds = {e['kind'] for e in data}
        ok &= check("contains both result kinds", {'read', 'stream'} <= kinds,
                    str(kinds))

    print("\n== an internal error must NOT be allowed to shut the MCU down ==")
    # Klipper turns an unhandled exception in a gcode command into an INTERNAL
    # ERROR and latches every MCU into shutdown - that happened for real on
    # 2026-09-15 and took a FIRMWARE_RESTART to clear. The guard has to turn
    # anything unexpected into a clean command failure instead.

    def boom(*a, **k):
        raise NameError("corr_ms")

    for name, attr in (('QIDI_CS_READ', '_run_READ'),
                       ('QIDI_CS_STREAM', '_run_STREAM')):
        g, mod, r = build(FakeSensor(MockReactor()), tempfile.mkdtemp())
        setattr(mod, attr, boom)
        try:
            g.run(name, SECONDS=1, HZ=10)
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
