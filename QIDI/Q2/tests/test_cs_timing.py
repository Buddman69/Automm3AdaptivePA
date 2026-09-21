"""Off-printer tests for qidi_cs_timing.

The module's whole job is to discriminate between three explanations for the
~13 ms per read, so the tests drive a simulated clock at each explanation's cost
and check it reaches the right verdict - plus the two free extras, the DRDY grid
test and the filter-location autocorrelation.
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
import qidi_cs_timing as T


class LCG:
    """Reproducible noise without numpy."""

    def __init__(self, seed=12345):
        self.s = seed

    def unit(self):
        self.s = (1103515245 * self.s + 12345) % (1 << 31)
        return self.s / float(1 << 31)

    def centred(self):
        return self.unit() - 0.5


class TimedReactor(MockReactor):
    """MockReactor whose clock is also what the module times against."""

    def __init__(self, pause_cost_s=0.0):
        MockReactor.__init__(self)
        self.pause_cost_s = pause_cost_s

    def pause(self, waketime):
        self.t = max(self.t, waketime) + self.pause_cost_s
        return self.t


class TimedSensor:
    """read_origin_data() that costs a configurable amount of simulated time."""

    def __init__(self, reactor, cost_ms, jitter_ms=0.0, mode='white',
                 seed=7, ar=0.9, noise=106.0, drift_per_call=0.0):
        self._r = reactor
        self.cost_ms = cost_ms
        self.jitter_ms = jitter_ms
        self.mode = mode
        self.rng = LCG(seed)
        self.ar = ar
        self.noise = noise
        self.drift = drift_per_call
        self._x = 0.0
        self._i = 0
        self.calls = 0

    def read_origin_data(self):
        self.calls += 1
        cost = self.cost_ms + self.jitter_ms * self.rng.centred() * 2.0
        self._r.t += max(cost, 0.0) / 1000.0
        e = self.rng.centred() * 2.0 * self.noise
        if self.mode == 'ar1':
            self._x = self.ar * self._x + e * math.sqrt(1 - self.ar ** 2)
        else:
            self._x = e
        self._i += 1
        return -391600.0 + self._x + self.drift * self._i


class TimedProbe:
    def __init__(self, sensor):
        self.sensor_helper = sensor


def build(tmpdir, cost_ms, **kw):
    reactor = TimedReactor(pause_cost_s=kw.pop('pause_cost_s', 0.0))
    printer = MockPrinter(reactor)
    g = ScriptGCode()
    printer._objects['gcode'] = g
    sensor = TimedSensor(reactor, cost_ms, **kw)
    printer._objects['probe_air'] = TimedProbe(sensor)
    mod = T.load_config(MockConfig(printer, {'out_dir': tmpdir}))
    mod.clock = lambda: reactor.t
    return g, mod, sensor, reactor


def main():
    ok = True

    print("\n== the arithmetic the verdict rests on ==")
    ok &= check("conversion period is 0.78125 ms",
                abs(T.SAMPLE_PERIOD_MS - 0.78125) < 1e-9,
                "%.6f" % T.SAMPLE_PERIOD_MS)
    ok &= check("a 12-conversion burst is 9.375 ms",
                abs(12 * T.SAMPLE_PERIOD_MS - 9.375) < 1e-9,
                "%.4f" % (12 * T.SAMPLE_PERIOD_MS))
    ok &= check("9.375 ms sits inside the burst band",
                T.BURST_LO_MS < 9.375 < T.BURST_HI_MS)
    ok &= check("so does the measured 13-conversion burst, 10.156 ms",
                T.BURST_LO_MS < 13 * T.SAMPLE_PERIOD_MS < T.BURST_HI_MS,
                "%.4f" % (13 * T.SAMPLE_PERIOD_MS))
    ok &= check("the bands do not overlap",
                T.LATENCY_BOUND_MS < T.BURST_LO_MS)

    print("\n== burst fit reads the floor, not the median ==")
    # The machine measured a 10.315 ms floor. That must resolve to 13, not 12
    # or 14 - the distinction is the whole conclusion.
    k, rt = T._burst_fit(10.315)
    ok &= check("10.315 ms floor is 13 conversions", k == 13, str(k))
    ok &= check("and leaves 0.16 ms of round trip", abs(rt - 0.159) < 0.01,
                "%.4f" % rt)
    ok &= check("a 12-conversion floor still reads as 12",
                T._burst_fit(12 * T.SAMPLE_PERIOD_MS + 0.1)[0] == 12)
    ok &= check("a 14-conversion floor still reads as 14",
                T._burst_fit(14 * T.SAMPLE_PERIOD_MS + 0.1)[0] == 14)
    ok &= check("round trip is never negative for a real floor", rt > 0)

    print("\n== grid significance scales with n ==")
    # A fixed threshold on R would call the same concentration locked at large
    # n and unlocked at small n. Rayleigh must not.
    p_big, u_big = T._rayleigh(0.3575, 512)
    p_small, u_small = T._rayleigh(0.3575, 10)
    ok &= check("R=0.3575 at n=512 is overwhelming", p_big < 1e-20,
                "%.3g" % p_big)
    ok &= check("the same R at n=10 is not", p_small > 0.1, "%.3g" % p_small)
    ok &= check("expected-by-chance R falls with n", u_big < u_small,
                "%.3f vs %.3f" % (u_big, u_small))
    ok &= check("too few samples returns no verdict",
                T._rayleigh(0.5, 4) == (None, None))

    print("\n== percentiles ==")
    st = T._stats([1.0, 2.0, 3.0, 4.0, 5.0])
    ok &= check("median of 1..5 is 3", abs(st['median'] - 3.0) < 1e-9,
                str(st['median']))
    ok &= check("min and max", st['min'] == 1.0 and st['max'] == 5.0)
    ok &= check("single value does not divide by zero",
                T._stats([2.5])['median'] == 2.5)
    ok &= check("empty returns None", T._stats([]) is None)

    print("\n== verdict: MCU burst ==")
    g, mod, sensor, _ = build(tempfile.mkdtemp(), 9.375, jitter_ms=0.15)
    out = "\n".join(g.run('QIDI_CS_TIMING', N=200))
    ok &= check("calls the sensor", sensor.calls >= 200, str(sensor.calls))
    ok &= check("reaches MCU BURST", 'MCU BURST' in out,
                out[-400:])
    ok &= check("reports ~12 conversion periods",
                'median = 12.0' in out or 'median = 11.9' in out
                or 'median = 12.1' in out,
                str([l for l in out.split("\n")
                     if 'conversion periods' in l]))
    ok &= check("does not also claim latency-bound",
                'LATENCY-BOUND' not in out)

    print("\n== verdict: latency-bound ==")
    tmp_lat = tempfile.mkdtemp()
    g, mod, sensor, _ = build(tmp_lat, 1.8, jitter_ms=0.4)
    out = "\n".join(g.run('QIDI_CS_TIMING', N=200))
    ok &= check("reaches LATENCY-BOUND", 'LATENCY-BOUND' in out, out[-400:])
    with open(os.path.join(tmp_lat, 'timing.json')) as f:
        lat = json.load(f)[-1]['payload']
    ok &= check("median lands at the simulated 1.8 ms",
                abs(lat['bare']['stats']['median'] - 1.8) < 0.3,
                "%.3f" % lat['bare']['stats']['median'])
    ok &= check("sustained rate is well over 400 Hz",
                lat['bare']['rate_hz'] > 400.0,
                "%.1f" % lat['bare']['rate_hz'])

    print("\n== verdict: neither band ==")
    g, mod, sensor, _ = build(tempfile.mkdtemp(), 20.0, jitter_ms=2.0)
    out = "\n".join(g.run('QIDI_CS_TIMING', N=100))
    ok &= check("reaches NEITHER BAND", 'NEITHER BAND' in out, out[-400:])
    ok &= check("refuses to draw a conclusion",
                'before drawing a conclusion' in out)

    print("\n== DRDY grid concentration ==")
    on_grid = [k % 13 * T.SAMPLE_PERIOD_MS + 0.002 for k in range(200)]
    r_on = T._grid_concentration(on_grid)
    ok &= check("durations on the grid give R near 1", r_on > 0.99,
                "%.4f" % r_on)
    rng = LCG(99)
    smeared = [5.0 + rng.unit() * 6.0 for _ in range(400)]
    r_off = T._grid_concentration(smeared)
    ok &= check("durations smeared over many periods give low R", r_off < 0.3,
                "%.4f" % r_off)
    ok &= check("too few samples returns None",
                T._grid_concentration([1.0, 2.0]) is None)

    print("\n== where the 0.9 filter lives ==")
    g, mod, sensor, _ = build(tempfile.mkdtemp(), 9.375, mode='ar1', ar=0.9)
    out = "\n".join(g.run('QIDI_CS_TIMING', N=300))
    ok &= check("an AR(1) series is called out as correlated",
                'carries ACROSS calls' in out,
                str([l for l in out.split("\n")
                     if 'autocorrelation' in l]))
    ok &= check("and warns averaging gains less than sqrt(n)",
                'less than sqrt(n)' in out)

    g, mod, sensor, _ = build(tempfile.mkdtemp(), 9.375, mode='white')
    out = "\n".join(g.run('QIDI_CS_TIMING', N=300))
    ok &= check("white noise is called out as independent",
                'consecutive reads are independent' in out,
                str([l for l in out.split("\n")
                     if 'autocorrelation' in l]))

    print("\n== drift does not fake correlation ==")
    # ~1.6 gf/min at 105 Hz is tiny per call, but check the detrend anyway by
    # using a drift far larger than reality.
    g, mod, sensor, _ = build(tempfile.mkdtemp(), 9.375, mode='white',
                              drift_per_call=8.0)
    out = "\n".join(g.run('QIDI_CS_TIMING', N=300))
    ok &= check("a strong linear ramp is detrended away, not read as the filter",
                'consecutive reads are independent' in out,
                str([l for l in out.split("\n")
                     if 'autocorrelation' in l]))
    ramp = [100.0 * i for i in range(50)]
    ok &= check("a pure ramp detrends to ~zero",
                max(abs(x) for x in T._detrend(ramp)) < 1e-6,
                "%.3g" % max(abs(x) for x in T._detrend(ramp)))

    print("\n== pacing overhead is attributed to the loop ==")
    tmp = tempfile.mkdtemp()
    g, mod, sensor, _ = build(tmp, 9.375, pause_cost_s=0.0035)
    out = "\n".join(g.run('QIDI_CS_TIMING', N=200))
    ok &= check("names the pacing cost", 'was our own loop' in out, out[-500:])
    ok &= check("reports reactor.pause on its own",
                'reactor.pause alone' in out)

    print("\n== report ==")
    path = os.path.join(tmp, 'timing.json')
    ok &= check("timing.json written", os.path.exists(path))
    if os.path.exists(path):
        with open(path) as f:
            d = json.load(f)
        pl = d[-1]['payload']
        ok &= check("records bare, paced and the verdict",
                    all(k in pl for k in ('bare', 'paced', 'verdict')),
                    str(list(pl)))
        ok &= check("keeps the raw durations for re-analysis",
                    len(pl['bare']['durations_ms']) > 50,
                    str(len(pl['bare']['durations_ms'])))
        ok &= check("bounds what it stores", len(pl['bare']['values']) <= 512)
        ok &= check("appends rather than overwriting", isinstance(d, list))

    print("\n== refuses what it cannot time ==")
    reactor = TimedReactor()
    printer = MockPrinter(reactor)
    g4 = ScriptGCode()
    printer._objects['gcode'] = g4

    class NoRead:
        sensor_helper = object()
    printer._objects['probe_air'] = NoRead()
    m4 = T.load_config(MockConfig(printer, {'out_dir': tempfile.mkdtemp()}))
    try:
        g4.run('QIDI_CS_TIMING')
        ok &= check("errors when read_origin_data is missing", False, "no error")
    except RuntimeError as e:
        ok &= check("errors when read_origin_data is missing",
                    'no read_origin_data' in str(e), str(e))

    class AttrRead:
        class S:
            read_origin_data = 1234.0
        sensor_helper = S()
    printer._objects['probe_air'] = AttrRead()
    m5 = T.load_config(MockConfig(printer, {'out_dir': tempfile.mkdtemp()}))
    try:
        g4.run('QIDI_CS_TIMING')
        ok &= check("errors when it is an attribute, not a method", False,
                    "no error")
    except RuntimeError as e:
        ok &= check("errors when it is an attribute, not a method",
                    'not callable' in str(e), str(e))

    print("\n== an internal error must NOT be allowed to shut the MCU down ==")
    # Klipper turns an unhandled exception in a gcode command into an INTERNAL
    # ERROR and latches every MCU into shutdown - that happened for real on
    # 2026-09-15 and took a FIRMWARE_RESTART to clear. The guard has to turn
    # anything unexpected into a clean command failure instead.

    def boom(*a, **k):
        raise NameError("corr_ms")

    g, mod, sensor, reactor = build(tempfile.mkdtemp(), 8.0)
    mod._run_TIMING = boom
    try:
        g.run('QIDI_CS_TIMING', N=20)
        ok &= check("QIDI_CS_TIMING converts a stray exception", False,
                    "nothing raised")
    except RuntimeError as e:
        ok &= check("QIDI_CS_TIMING converts a stray exception",
                    'internal error' in str(e) and 'NameError' in str(e),
                    str(e))
    except NameError:
        ok &= check("QIDI_CS_TIMING converts a stray exception", False,
                    "NameError escaped - this shuts both MCUs down")

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
