"""Off-printer tests for qidi_cs_clock.

The property that matters most is the refusal: this module must never register
over another subsystem's response handler, because doing that to ClockSync
would break timing for the whole printer.
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
import qidi_cs_clock as K

FREQ = 72000000.0


class FakeSerial:
    def __init__(self, handlers=None):
        self.handlers = dict(handlers or {})


class FakeUptimeCmd:
    """Answers at a settable fraction of the round trip."""

    def __init__(self, reactor, rtt_s=0.0030, frac=0.5, jitter=0.0):
        self.reactor = reactor
        self.rtt_s = rtt_s
        self.frac = frac
        self.jitter = jitter
        self.i = 0
        self.mcu_at = None

    def send(self, data=None, minclock=0, reqclock=0):
        self.i += 1
        j = ((self.i * 7919) % 101 - 50) / 50.0 * self.jitter
        f = min(max(self.frac + j, 0.0), 1.0)
        self.mcu_at = self.reactor.t + f * self.rtt_s
        self.reactor.t += self.rtt_s
        # No 32-bit truncation here: reconstructing a wrapped counter is
        # Klipper's clock32_to_clock64 job, which the module delegates to. What
        # these tests check is the module's own arithmetic on top of it.
        clk = int(self.mcu_at * FREQ)
        return {'high': clk >> 32, 'clock': clk & 0xffffffff}


class FakeBurst:
    def __init__(self, reactor, rtt_s):
        self.reactor = reactor
        self.rtt_s = rtt_s

    def send(self, data, minclock=0, reqclock=0):
        self.reactor.t += self.rtt_s
        return {'oid': 5, 'data': b'\x00\x00\x00\x00'}


class FakeMCU:
    def __init__(self, reactor, handlers, uptime_cmd):
        self.reactor = reactor
        self._serial = FakeSerial(handlers)
        self._uptime = uptime_cmd

    def lookup_query_command(self, msg, resp, oid=None, cq=None,
                             is_async=False):
        return self._uptime

    # A perfect clock mapping, so any offset the test sees is the one the
    # fake command injected rather than a modelling artefact. Stands in for
    # Klipper's real reconstruction, which is tested upstream.
    def clock32_to_clock64(self, c32):
        base = int(self.reactor.t * FREQ)
        high = base - (base & 0xffffffff)
        c = high + c32
        # pick the nearest wrap, as Klipper's own version does
        for cand in (c - (1 << 32), c, c + (1 << 32)):
            if abs(cand - base) <= (1 << 31):
                return cand
        return c

    def clock_to_print_time(self, c):
        return c / FREQ

    def estimated_print_time(self, eventtime):
        return eventtime


class FakeSensor:
    def __init__(self, mcu, burst=None):
        self.oid = 5
        self._mcu = mcu
        if burst is not None:
            self.query_cs1237_end_cmd = burst

    def get_mcu(self):
        return self._mcu


class FakeProbe:
    def __init__(self, sensor):
        self.sensor_helper = sensor


def build(tmpdir, frac=0.5, rtt_s=0.0030, jitter=0.0, handlers=None,
          burst_rtt=0.0030):
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    g = ScriptGCode()
    printer._objects['gcode'] = g
    up = FakeUptimeCmd(reactor, rtt_s, frac, jitter)
    if handlers is None:
        handlers = {('clock', None): lambda p: None}
    mcu = FakeMCU(reactor, handlers, up)
    burst = FakeBurst(reactor, burst_rtt) if burst_rtt else None
    printer._objects['probe_air'] = FakeProbe(FakeSensor(mcu, burst))
    mod = K.load_config(MockConfig(printer, {'out_dir': tmpdir}))
    return g, mod


def main():
    ok = True

    print("\n== it must NOT clobber an existing handler ==")
    g, mod = build(tempfile.mkdtemp(),
                   handlers={('clock', None): lambda p: None,
                             ('uptime', None): lambda p: None})
    try:
        g.run('QIDI_CS_CLOCK', N=20)
        ok &= check("refuses when the uptime slot is taken", False, "no error")
    except RuntimeError as e:
        ok &= check("refuses when the uptime slot is taken",
                    'already handles' in str(e) and 'Refusing' in str(e),
                    str(e))

    g, mod = build(tempfile.mkdtemp(),
                   handlers={('clock', None): lambda p: None})
    out = "\n".join(g.run('QIDI_CS_CLOCK', N=40))
    ok &= check("runs when the slot is free", 'round trip' in out, out[:200])
    ok &= check("does not warn about the clock slot when ClockSync holds it",
                'nothing holds the' not in out, out[:300])

    g, mod = build(tempfile.mkdtemp(), handlers={})
    out = "\n".join(g.run('QIDI_CS_CLOCK', N=40))
    ok &= check("but does flag a missing ClockSync handler as odd",
                "nothing holds the 'clock' slot" in out, out[:300])

    print("\n== it recovers an asymmetric round trip ==")
    for frac, label in ((0.5, 'symmetric'), (0.25, 'early'), (0.8, 'late')):
        tmp = tempfile.mkdtemp()
        g, mod = build(tmp, frac=frac, rtt_s=0.0030)
        g.run('QIDI_CS_CLOCK', N=120)
        with open(os.path.join(tmp, 'clock.json')) as f:
            p = json.load(f)[-1]['payload']
        got_f = p['frac']['median']
        got_off = p['offset_ms']['median']
        want_off = (frac - 0.5) * 3.0
        ok &= check("f recovered for the %s case (%.2f)" % (label, frac),
                    abs(got_f - frac) < 0.02, "got %.4f" % got_f)
        ok &= check("  and the offset is %+.2f ms" % want_off,
                    abs(got_off - want_off) < 0.05, "got %+.4f" % got_off)

    print("\n== a symmetric result is reported as reassurance, not a fix ==")
    g, mod = build(tempfile.mkdtemp(), frac=0.5)
    out = "\n".join(g.run('QIDI_CS_CLOCK', N=120))
    ok &= check("reports the systematic as measured and removed",
                'measured and removed' in out, out[-500:])
    ok &= check("names both candidate estimators",
                'proportional (t0 + f*RTT)' in out and 'fixed (t0 + c)' in out,
                out[-600:])

    g, mod = build(tempfile.mkdtemp(), frac=0.2)
    out = "\n".join(g.run('QIDI_CS_CLOCK', N=120))
    ok &= check("subtracts the buffer staleness term",
                '- 0.391 ms' in out, out[-500:])
    ok &= check("reports the random budget as a percentage of K",
                'of a 24 ms K' in out, out[-500:])

    print("\n== jitter is separated from the systematic offset ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, frac=0.5, jitter=0.30)
    g.run('QIDI_CS_CLOCK', N=200)
    with open(os.path.join(tmp, 'clock.json')) as f:
        p = json.load(f)[-1]['payload']
    ok &= check("median offset stays near zero despite jitter",
                abs(p['offset_ms']['median']) < 0.15,
                "%+.4f" % p['offset_ms']['median'])
    ok &= check("but the scatter is reported as large",
                p['offset_ms']['mad'] > 0.2, "%.4f" % p['offset_ms']['mad'])

    print("\n== the transferability check ==")
    g, mod = build(tempfile.mkdtemp(), rtt_s=0.0030, burst_rtt=0.0030)
    out = "\n".join(g.run('QIDI_CS_CLOCK', N=60))
    ok &= check("comparable round trips -> f should transfer",
                'f should transfer' in out,
                str([l for l in out.split("\n") if 'transfer' in l]))

    g, mod = build(tempfile.mkdtemp(), rtt_s=0.0030, burst_rtt=0.0100)
    out = "\n".join(g.run('QIDI_CS_CLOCK', N=60))
    ok &= check("very different round trips -> warns f may not transfer",
                'may NOT transfer' in out,
                str([l for l in out.split("\n") if 'transfer' in l]))

    print("\n== an internal error must NOT be allowed to shut the MCU down ==")
    # Klipper turns an unhandled exception in a gcode command into an internal
    # error and shuts down every MCU. This happened for real on 2026-09-15.
    g, mod = build(tempfile.mkdtemp())

    def boom(gcmd):
        raise NameError("corr_ms")
    mod._run = boom
    try:
        g.run('QIDI_CS_CLOCK', N=20)
        ok &= check("converts a stray exception into a command error", False,
                    "nothing raised")
    except RuntimeError as e:
        ok &= check("converts a stray exception into a command error",
                    'internal error' in str(e) and 'NameError' in str(e),
                    str(e))
    except NameError as e:
        ok &= check("converts a stray exception into a command error", False,
                    "NameError escaped: %s" % (e,))

    print("\n== report ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, frac=0.35)
    g.run('QIDI_CS_CLOCK', N=80)
    path = os.path.join(tmp, 'clock.json')
    ok &= check("clock.json written", os.path.exists(path))
    with open(path) as f:
        p = json.load(f)[-1]['payload']
    ok &= check("records frac, offset, rtt and the correction",
                all(k in p for k in ('frac', 'offset_ms', 'rtt_ms',
                                     'correction_ms')), str(list(p)))
    ok &= check("keeps raw fractions for re-analysis",
                len(p['fracs']) >= 80, str(len(p['fracs'])))

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
