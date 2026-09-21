"""Off-printer tests for qidi_cs_validate.

Two properties carry real risk and are tested hardest: the accelerometer must
always be stopped, and a stray exception must never become a Klipper internal
error (which shuts down every MCU - it happened for real on 2026-09-15).
"""
import json
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mock_klipper import MockReactor, MockPrinter, MockConfig
from test_flow_ramp import ScriptGCode, check
import qidi_cs_validate as V


def enc(v):
    u = v & 0xffffff
    return bytes([u & 0xff, (u >> 8) & 0xff, (u >> 16) & 0xff, 0x00])


class Rig:
    """A simulated load cell both paths read, so agreement is testable."""

    def __init__(self, reactor, scale=1.0, offset=0.0, torn_every=0,
                 noise=0.0, ramp_gf_s=0.0):
        self.r = reactor
        self.scale = scale          # raw = scale*true + offset
        self.offset = offset
        self.torn_every = torn_every
        self.noise = noise
        self.ramp = ramp_gf_s * V.COUNTS_PER_GF
        self.base = -388700.0
        self.i = 0
        self.seed = 12345

    def _n(self):
        if not self.noise:
            return 0.0
        self.seed = (1103515245 * self.seed + 12345) % (1 << 31)
        return (self.seed / float(1 << 31) - 0.5) * 2 * self.noise

    def true(self):
        # A triangle sweep, so a press over a real range is simulated.
        t = self.r.t % 20.0
        f = t / 10.0 if t < 10.0 else (20.0 - t) / 10.0
        return self.base - self.ramp * f

    def wrapper(self):
        self.r.t += 0.0109
        return self.true() + self._n()

    def raw(self):
        self.r.t += 0.0030
        self.i += 1
        if self.torn_every and self.i % self.torn_every == 0:
            return -33
        return self.scale * (self.true() - self.base) + self.base \
            + self.offset + self._n()


class RawCmd:
    def __init__(self, rig):
        self.rig = rig

    def send(self, data, minclock=0, reqclock=0):
        return {'oid': 5, 'data': enc(int(round(self.rig.raw())))}


class Sensor:
    def __init__(self, rig):
        self.oid = 5
        self.rig = rig
        self.query_cs1237_end_cmd = RawCmd(rig)

    def read_origin_data(self):
        return self.rig.wrapper()


class Probe:
    def __init__(self, sensor):
        self.sensor_helper = sensor


class GCodeWithScript(ScriptGCode):
    def __init__(self, fail_on=None):
        ScriptGCode.__init__(self)
        self.fail_on = fail_on

    def run_script_from_command(self, script):
        self.scripts.append(script)
        if self.fail_on and self.fail_on in script:
            raise RuntimeError("accel refused")


def build(tmpdir, fail_on=None, **kw):
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    g = GCodeWithScript(fail_on)
    printer._objects['gcode'] = g
    rig = Rig(reactor, **kw)
    printer._objects['probe_air'] = Probe(Sensor(rig))
    mod = V.load_config(MockConfig(printer, {'out_dir': tmpdir}))
    return g, mod, rig


def payload(tmpdir):
    with open(os.path.join(tmpdir, 'validate.json')) as f:
        return json.load(f)[-1]['payload']


def main():
    ok = True

    print("\n== the torn-read gate ==")
    ok &= check("gate is 50000 counts = 273 gf",
                abs(V.TORN_GATE_COUNTS / V.COUNTS_PER_GF - 273.3) < 1.0,
                "%.1f gf" % (V.TORN_GATE_COUNTS / V.COUNTS_PER_GF))
    ok &= check("which is ~91000 gf/s across a 3 ms gap - no hand reaches it",
                V.TORN_GATE_COUNTS / V.COUNTS_PER_GF / 0.003 > 50000)
    ok &= check("decodes the observed torn value -33",
                V._decode24le(enc(-33)) == -33)
    ok &= check("decodes a real baseline",
                V._decode24le(enc(-388751)) == -388751)

    print("\n== PAIR: identical paths agree ==")
    tmp = tempfile.mkdtemp()
    g, mod, rig = build(tmp, ramp_gf_s=800.0)
    out = "\n".join(g.run('QIDI_CS_PAIR', SECS=8))
    p = payload(tmp)
    ok &= check("finds the same scale", 'SAME SCALE' in out, out[-400:])
    ok &= check("slope is 1.000", abs(p['regression']['slope'] - 1.0) < 0.005,
                "%.6f" % p['regression']['slope'])
    ok &= check("reports the same tare", 'Same tare' in out, out[-300:])
    ok &= check("swept a usable range", p['spread_gf'] > V.MIN_SPREAD_GF,
                "%.0f gf" % p['spread_gf'])

    print("\n== PAIR: a scale error is caught ==")
    tmp = tempfile.mkdtemp()
    g, mod, rig = build(tmp, scale=1.05, ramp_gf_s=800.0)
    out = "\n".join(g.run('QIDI_CS_PAIR', SECS=8))
    ok &= check("calls out a 5% scale difference", 'SCALES DIFFER' in out,
                out[-400:])
    ok &= check("and refuses to let it hold an abort",
                'before it can hold any abort' in out, out[-300:])

    print("\n== PAIR: a tare offset is caught ==")
    tmp = tempfile.mkdtemp()
    g, mod, rig = build(tmp, offset=400.0, ramp_gf_s=800.0)
    out = "\n".join(g.run('QIDI_CS_PAIR', SECS=8))
    ok &= check("reports a tare offset (400 counts = 2.2 gf)",
                'TARE OFFSET' in out, out[-400:])
    ok &= check("says a fresh tare is needed per path",
                'not\nshared' in out or 'not shared' in out
                or 'fresh\ntare' in out or 'fresh tare' in out, out[-300:])

    print("\n== PAIR: too small a sweep is called inconclusive ==")
    tmp = tempfile.mkdtemp()
    g, mod, rig = build(tmp, ramp_gf_s=2.0)
    out = "\n".join(g.run('QIDI_CS_PAIR', SECS=8))
    ok &= check("refuses to judge the scale on a tiny range",
                'INCONCLUSIVE ON SCALE' in out, out[-400:])
    ok &= check("and does not claim the scales match",
                'SAME SCALE' not in out, out[-400:])

    print("\n== PAIR: torn reads are rejected, not averaged in ==")
    tmp = tempfile.mkdtemp()
    g, mod, rig = build(tmp, torn_every=5, ramp_gf_s=800.0)
    out = "\n".join(g.run('QIDI_CS_PAIR', SECS=10))
    p = payload(tmp)
    ok &= check("counts the torn reads", p['torn'] > 0, str(p['torn']))
    ok &= check("and still recovers slope 1.000 despite them",
                abs(p['regression']['slope'] - 1.0) < 0.005,
                "%.6f" % p['regression']['slope'])
    ok &= check("residual stays tiny", p['regression']['rms_resid']
                / V.COUNTS_PER_GF < 1.0,
                "%.3f gf" % (p['regression']['rms_resid'] / V.COUNTS_PER_GF))

    print("\n== LOAD: the accelerometer is always stopped ==")
    tmp = tempfile.mkdtemp()
    g, mod, rig = build(tmp)
    g.run('QIDI_CS_LOAD', SECS=1)
    starts = [s for s in g.scripts if 'ACCELEROMETER_MEASURE' in s]
    ok &= check("started and stopped exactly once each", len(starts) == 2,
                str(g.scripts))

    tmp = tempfile.mkdtemp()
    g, mod, rig = build(tmp)
    orig = mod._raw
    state = {'n': 0}

    def flaky(raw, oid):
        # Fail only once the accelerometer is actually streaming - a failure
        # during the idle burst has nothing to stop, so it would not test the
        # finally: block at all.
        if g.scripts:
            state['n'] += 1
            if state['n'] > 5:
                raise RuntimeError("link died mid-burst")
        return orig(raw, oid)
    mod._raw = flaky
    try:
        g.run('QIDI_CS_LOAD', SECS=2)
    except Exception:
        pass
    starts = [s for s in g.scripts if 'ACCELEROMETER_MEASURE' in s]
    ok &= check("stopped even when the burst raised", len(starts) == 2,
                str(g.scripts))

    print("\n== LOAD: a refusing accelerometer degrades gracefully ==")
    tmp = tempfile.mkdtemp()
    g, mod, rig = build(tmp, fail_on='ACCELEROMETER_MEASURE')
    out = "\n".join(g.run('QIDI_CS_LOAD', SECS=1))
    ok &= check("reports idle figures only", 'idle figures only' in out,
                out[-300:])
    ok &= check("never claims a loaded result", 'delta:' not in out, out[-300:])
    p = payload(tmp)
    ok &= check("records loaded as absent", p['loaded'] is None, str(p))

    print("\n== LOAD: verdicts ==")
    tmp = tempfile.mkdtemp()
    g, mod, rig = build(tmp)
    out = "\n".join(g.run('QIDI_CS_LOAD', SECS=2))
    ok &= check("a clean run clears the raw path", 'clear to use it' in out,
                out[-400:])

    tmp = tempfile.mkdtemp()
    g, mod, rig = build(tmp, torn_every=2)
    out = "\n".join(g.run('QIDI_CS_LOAD', SECS=2))
    ok &= check("heavy tearing is called out as degradation",
                'degrades badly' in out, out[-400:])
    ok &= check("and recommends the wrapper instead",
                'read_origin_data' in out, out[-400:])

    print("\n== a stray exception must not become an internal error ==")
    for cmd in ('QIDI_CS_PAIR', 'QIDI_CS_LOAD'):
        g, mod, rig = build(tempfile.mkdtemp())
        if cmd == 'QIDI_CS_PAIR':
            mod._run_pair = lambda gc: (_ for _ in ()).throw(
                NameError("corr_ms"))
        else:
            mod._run_load = lambda gc: (_ for _ in ()).throw(
                NameError("corr_ms"))
        try:
            g.run(cmd, SECS=1)
            ok &= check("%s converts it" % cmd, False, "nothing raised")
        except RuntimeError as e:
            ok &= check("%s converts it" % cmd,
                        'internal error' in str(e) and 'NameError' in str(e),
                        str(e))
        except NameError:
            ok &= check("%s converts it" % cmd, False, "NameError escaped")

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
