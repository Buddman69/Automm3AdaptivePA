"""Off-printer tests for qidi_cs_batch.

The module's whole question is whether query_cs1237_read returns ONE sample or
a batch of twelve, so most of what is worth testing is the decoder: given a
blob off the wire, does it pick the right layout and refuse an implausible one?

Plus the guard - an unhandled exception in a gcode command is an INTERNAL ERROR
to Klipper and latches every MCU into shutdown.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mock_klipper import MockReactor, MockPrinter, MockConfig
from test_flow_ramp import ScriptGCode, check
import qidi_cs_batch as B


def block(values):
    """Encode counts the way the wire carries them: 24-bit LE plus a pad."""
    out = bytearray()
    for v in values:
        u = v & 0xffffff
        out += bytes([u & 0xff, (u >> 8) & 0xff, (u >> 16) & 0xff, 0x00])
    return bytes(out)


class FakeCmd:
    """Stands in for query_cs1237_end_cmd. `payload` is a callable taking
    (oid, reg, len) and returning the reply dict, or an exception to raise."""

    def __init__(self, payload):
        self.payload = payload
        self.sent = []

    def send(self, args):
        self.sent.append(tuple(args))
        r = self.payload(*args)
        if isinstance(r, Exception):
            raise r
        return r


class FakeSensor:
    def __init__(self, cmd, reading=-391648.0):
        self.oid = 5
        self.query_cs1237_end_cmd = cmd
        self._reading = reading

    def read_origin_data(self):
        return self._reading


class FakeProbe:
    def __init__(self, sensor):
        self.sensor_helper = sensor


def build(tmpdir, payload=None, sensor=True):
    printer = MockPrinter(MockReactor())
    g = ScriptGCode()
    printer._objects['gcode'] = g
    cmd = FakeCmd(payload or (lambda oid, reg, ln: {'data': b''}))
    if sensor:
        printer._objects['probe_air'] = FakeProbe(FakeSensor(cmd))
    mod = B.load_config(MockConfig(printer, {'out_dir': tmpdir}))
    return g, mod, cmd


def main():
    ok = True

    print("\n== decoder unit checks ==")
    # 0x2006fa00 little-endian 24-bit is the value the live probe decoded.
    ok &= check("24-bit LE round-trips through the encoder",
                B._decode(block([-391648]))['i24le_first'][0] == -391648,
                str(B._decode(block([-391648]))['i24le_first']))
    ok &= check("signed 24-bit wraps at 2^23",
                B._s24(0x80, 0x00, 0x00) == -(1 << 23),
                str(B._s24(0x80, 0x00, 0x00)))
    ok &= check("0x7fffff is the positive rail",
                B._s24(0x7f, 0xff, 0xff) == (1 << 23) - 1)

    print("\n== plausibility rejects what cannot be a sample batch ==")
    ok &= check("a single value is not a batch",
                B._plausible([12345]) is None)
    ok &= check("out of 24-bit range is rejected",
                B._plausible([0, 1 << 24]) is None)
    q = B._plausible([-391648, -391700, -391610, -391655])
    ok &= check("a quiet consecutive run scores a small median step",
                q is not None and q['median_step'] < 200, str(q))
    ok &= check("and brackets the reference reading",
                q.get('brackets_ref') is None, "ref not supplied")
    q2 = B._plausible([-391648, -391700, -391610, -391655], ref=-391650.)
    ok &= check("brackets_ref is true when the reference sits in the span",
                q2.get('brackets_ref') is True, str(q2))
    q3 = B._plausible([-391648, -391700, -391610, -391655], ref=500000.)
    ok &= check("and false when it does not",
                q3.get('brackets_ref') is False, str(q3))

    print("\n== a 12-sample reply is reported as the batched read ==")
    # Step by more than a byte. A smaller step leaves i24be_last (which
    # discards the low byte entirely) reading constant, so it wins the
    # smallest-median-step tie-break on an artefact of the fixture rather
    # than on the data. Positive values keep i32le and i24le_first in
    # agreement, so the decoded samples are the same either way - on a
    # negative reading those two layouts genuinely disagree and a single
    # batch cannot tell them apart.
    vals = [1200000 + i * 300 for i in range(12)]
    g, mod, cmd = build(tempfile.mkdtemp(),
                        lambda oid, reg, ln: {'data': block(vals)})
    out = "\n".join(g.run('QIDI_CS_BATCH', REG=0, LEN=48))
    ok &= check("sent exactly the requested (reg, len)",
                cmd.sent == [(5, 0, 48)], str(cmd.sent))
    ok &= check("decoded 48 bytes", '48 bytes' in out, out[-400:])
    ok &= check("found all twelve samples", 'n=12' in out, out[-400:])
    ok &= check("picked a little-endian layout, not a byte-swapped one",
                'i32le' in out or 'i24le_first' in out, out[-400:])
    ok &= check("called it the batched read",
                'this is the batched read' in out, out[-400:])

    print("\n== a one-sample reply is not ==")
    g, mod, cmd = build(tempfile.mkdtemp(),
                        lambda oid, reg, ln: {'data': block([-391648])})
    out = "\n".join(g.run('QIDI_CS_BATCH', REG=0, LEN=4))
    ok &= check("falls back to the integrating design",
                'nothing returned a plausible multi-sample' in out,
                out[-400:])

    print("\n== a command that errors does not stop the sweep ==")
    def flaky(oid, reg, ln):
        if ln == 4:
            return RuntimeError("invalid read_len")
        return {'data': block([-391648 + i * 30 for i in range(ln // 4)])}
    g, mod, cmd = build(tempfile.mkdtemp(), flaky)
    out = "\n".join(g.run('QIDI_CS_BATCH'))
    ok &= check("swept every candidate despite the failures",
                len(cmd.sent) == len(B.CANDIDATE_REGS) * len(B.CANDIDATE_LENS),
                str(len(cmd.sent)))
    ok &= check("reported the errors", 'error: invalid read_len' in out,
                out[:400])

    print("\n== refuses a sensor it cannot drive ==")
    g, mod, cmd = build(tempfile.mkdtemp(), sensor=False)
    try:
        g.run('QIDI_CS_BATCH')
        ok &= check("errors with no probe_air", False, "nothing raised")
    except RuntimeError as e:
        ok &= check("errors with no probe_air", 'no object probe_air' in str(e),
                    str(e))

    print("\n== report ==")
    tmp = tempfile.mkdtemp()
    g, mod, cmd = build(tmp, lambda oid, reg, ln: {'data': block(vals)})
    g.run('QIDI_CS_BATCH', REG=0, LEN=48)
    path = os.path.join(tmp, 'batch.json')
    ok &= check("batch.json written", os.path.exists(path))
    with open(path) as f:
        p = json.load(f)[-1]['payload']
    ok &= check("records oid, the reference and every attempt",
                all(k in p for k in ('oid', 'ref', 'attempts')), str(list(p)))
    ok &= check("keeps the decoded samples for re-analysis",
                p['attempts'][0]['samples'] == vals,
                str(p['attempts'][0].get('samples'))[:120])

    print("\n== an internal error must NOT be allowed to shut the MCU down ==")
    # Klipper turns an unhandled exception in a gcode command into an INTERNAL
    # ERROR and latches every MCU into shutdown - that happened for real on
    # 2026-09-15 and took a FIRMWARE_RESTART to clear. The guard has to turn
    # anything unexpected into a clean command failure instead.

    def boom(*a, **k):
        raise NameError("corr_ms")

    g, mod, cmd = build(tempfile.mkdtemp())
    mod._run_BATCH = boom
    try:
        g.run('QIDI_CS_BATCH')
        ok &= check("QIDI_CS_BATCH converts a stray exception", False,
                    "nothing raised")
    except RuntimeError as e:
        ok &= check("QIDI_CS_BATCH converts a stray exception",
                    'internal error' in str(e) and 'NameError' in str(e),
                    str(e))
    except NameError:
        ok &= check("QIDI_CS_BATCH converts a stray exception", False,
                    "NameError escaped - this shuts both MCUs down")

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
