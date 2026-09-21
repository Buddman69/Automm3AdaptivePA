"""Off-printer tests for QIDI_FLOW_SEARCH - the adaptive coarse/bisect/fine
search: coarse stride, bisect, then a fine step.

The thing being tested is the SEARCH, not the measurement primitive (that has
its own suite in test_flow_ramp.py). So the sensor here is a simple oracle: any
flow at or below a true limit equilibrates, anything above it climbs forever.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mock_klipper import MockReactor, MockPrinter, MockConfig
from test_flow_ramp import (ScriptGCode, MockToolhead, MockExtruder,
                            MockHeaters, FakeSensor, check)
import qidi_flow_ramp as FR


class OracleSensor(FakeSensor):
    """Equilibrates at or below `limit`; climbs without bound above it.

    `flaky` makes one specific flow fail its first trial and pass afterwards,
    which is the marginal-transition case the adjacent-steps rule exists for.
    """

    def __init__(self, limit, flaky=None):
        FakeSensor.__init__(self)
        self.limit = limit
        self.q = 0.0
        self.t = 0
        self.flaky = flaky
        self.flaky_seen = 0

    def read_origin_data(self):
        self.reads += 1
        over = self.q > self.limit + 1e-9
        if self.flaky is not None and abs(self.q - self.flaky) < 1e-9:
            over = (self.flaky_seen == 0)
        if over:
            self.t += 1
            gf = 300.0 + self.t * 12.0          # never settles
        else:
            gf = 25.0 * self.q ** 0.5           # steady
        return int(self.tare + gf * FR.COUNTS_PER_GF)


def build(tmpdir, sensor):
    printer = MockPrinter(MockReactor())
    g = ScriptGCode()
    printer._objects['gcode'] = g

    class Root:
        pass
    r = Root(); r.sensor_helper = sensor
    printer.add('probe_air', r)
    printer.add('toolhead', MockToolhead('xyz'))
    printer.add('extruder', MockExtruder())
    printer.add('heaters', MockHeaters())
    mod = FR.load_config(MockConfig(printer, {'out_dir': tmpdir}))
    return g, mod, printer


def run_search(tmpdir, sensor, **kw):
    g, mod, p = build(tmpdir, sensor)
    orig = FR.QidiFlowRamp._run_step

    def patched(self, gcmd, read, toolhead, idx, q, hz, tare, abort_counts,
                var_abort, **kwargs):
        sensor.q = q
        sensor.t = 0
        r = orig(self, gcmd, read, toolhead, idx, q, hz, tare, abort_counts,
                 var_abort, **kwargs)
        if sensor.flaky is not None and abs(q - sensor.flaky) < 1e-9:
            sensor.flaky_seen += 1
        sensor.q = 0.0
        sensor.t = 0
        return r
    FR.QidiFlowRamp._run_step = patched
    try:
        params = dict(HZ=30, MAX_E=9000)
        params.update(kw)
        out = g.run('QIDI_FLOW_SEARCH', **params)
    finally:
        FR.QidiFlowRamp._run_step = orig
    return out, g, p


def limit_from(out):
    for line in out:
        if 'highest flow that equilibrated' in line:
            return float(line.split(':')[-1].replace('mm3/s', '').strip())
    return None


def working_from(out):
    for line in out:
        if 'WORKING MAX' in line:
            return float(line.split('=')[1].split('mm3')[0])
    return None


def tested(out):
    qs = []
    for line in out:
        if '  mm3/s ->' in line.replace('mm3/s ->', '  mm3/s ->'):
            pass
    for line in out:
        if 'mm3/s -> ' in line:
            try:
                qs.append(float(line.split(':')[1].split('mm3')[0]))
            except Exception:
                pass
    return qs


def main():
    ok = True
    tmp = tempfile.mkdtemp()

    print("\n== finds a limit of 22 and reports working max ==")
    out, g, p = run_search(tmp, OracleSensor(22.0))
    text = "\n".join(out)
    lim, wm = limit_from(out), working_from(out)
    ok &= check("limit found is 22", lim == 22.0, "got %s" % lim)
    ok &= check("working max = limit x margin, rounded down",
                wm is not None and abs(wm - FR._working_max(22.0)) < 1e-9,
                "got %s, expected %s" % (wm, FR._working_max(22.0)))
    ok &= check("and never rounds up into the margin",
                wm <= 22.0 * FR.WORKING_MARGIN + 1e-9,
                "%s > %s" % (wm, 22.0 * FR.WORKING_MARGIN))
    qs = tested(out)
    ok &= check("coarse stopped at the first confirmed failure",
                25.0 in qs and 35.0 not in qs, str(sorted(set(qs))))
    ok &= check("bisected to 20", 20.0 in qs, str(sorted(set(qs))))
    ok &= check("fine-stepped 21,22,23", all(x in qs for x in (21., 22., 23.)),
                str(sorted(set(qs))))
    ok &= check("fewer than 20 measurements", len(qs) < 20, str(len(qs)))
    ok &= check("hotend turned off at the end",
                p.lookup_object('heaters').calls[-1][0] == 0.,
                str(p.lookup_object('heaters').calls))

    print("\n== a limit below the bisect: steps up from the lower bracket ==")
    out, g, p = run_search(tmp, OracleSensor(17.0))
    lim = limit_from(out)
    qs = tested(out)
    ok &= check("limit found is 17", lim == 17.0, "got %s" % lim)
    ok &= check("bisect at 20 failed and it fell back to 15",
                20.0 in qs and 16.0 in qs, str(sorted(set(qs))))

    print("\n== a low-flow filament still calibrates ==")
    out, g, p = run_search(tmp, OracleSensor(8.0))
    lim = limit_from(out)
    ok &= check("limit found is 8", lim == 8.0, "got %s" % lim)
    ok &= check("bracket came from 5 pass / 15 fail",
                5.0 in tested(out) and 15.0 in tested(out),
                str(sorted(set(tested(out)))))

    print("\n== nothing fails: says the limit is above the range ==")
    out, g, p = run_search(tmp, OracleSensor(999.0), MAXQ=35)
    text = "\n".join(out)
    ok &= check("reports limit above the tested range",
                'ABOVE the tested range' in text, text[-400:])
    ok &= check("and says that means the measurement is wrong",
                'measurement is wrong' in text, text[-400:])

    print("\n== the floor itself fails: says the limit is below it ==")
    out, g, p = run_search(tmp, OracleSensor(2.0))
    text = "\n".join(out)
    ok &= check("reports limit below the coarse floor",
                'BELOW the coarse floor' in text, text[-400:])
    ok &= check("suggests a lower START", 'lower START' in text, text[-400:])

    print("\n== marginal transition: two fails across adjacent steps ==")
    # True limit 23, but 23 fails its first trial and passes the second, and 24
    # fails genuinely. Two failures across adjacent steps (23 and 24) means 23
    # is treated as the failure point, so the reported limit is 22 - one below
    # the true 23. Conservative, which is the right way to be wrong.
    out, g, p = run_search(tmp, OracleSensor(23.0, flaky=23.0))
    text = "\n".join(out)
    lim = limit_from(out)
    ok &= check("takes the conservative value, one below the true limit",
                lim == 22.0, "got %s (true limit 23)" % lim)
    ok &= check("and flags the transition as marginal",
                'marginal' in text.lower(), text[-500:])
    # And with no flakiness the same limit is found exactly, not conservatively.
    out2, _g, _p = run_search(tmp, OracleSensor(23.0))
    ok &= check("a clean transition reports the true limit exactly",
                limit_from(out2) == 23.0, "got %s" % limit_from(out2))
    ok &= check("and does not flag it marginal",
                'marginal' not in "\n".join(out2).lower())

    print("\n== unit: the cap scales with fill time, not against it ==")
    printer = MockPrinter(MockReactor())
    printer._objects['gcode'] = ScriptGCode()
    mod = FR.load_config(MockConfig(printer, {'out_dir': tmp}))
    c5, c25, c45 = (mod._search_cap(5.), mod._search_cap(25.),
                    mod._search_cap(45.))
    ok &= check("low flow gets the longest cap", c5 > c25 > c45,
                "%.1f %.1f %.1f" % (c5, c25, c45))
    ok &= check("5 mm3/s allows time to fill 100 mm3", c5 >= 30.,
                "%.1f s" % c5)
    ok &= check("high flow is not padded unnecessarily", c45 < 20.,
                "%.1f s" % c45)

    print("\n== unit: the minimum hold clears the fill, not just the cap ==")
    # A flat 3.5 s minimum let 20 mm3/s settle at 3.6 s and read 790 gf where
    # its repeat held 8.6 s and read 1116 - the fill at 20 mm3/s takes 5.0 s,
    # so the stability test passed on a flat stretch of the rising curve.
    for q in (5., 15., 20., 25., 45.):
        mn, cap = mod._search_min(q), mod._search_cap(q)
        fill = FR.SEARCH_FILL_MM3 / q
        ok &= check("Q=%.0f: min %.1fs clears the %.1fs fill" % (q, mn, fill),
                    mn > fill, "min %.1f <= fill %.1f" % (mn, fill))
        ok &= check("Q=%.0f: min %.1fs still under the %.1fs cap" % (q, mn, cap),
                    mn < cap, "min %.1f >= cap %.1f" % (mn, cap))
    ok &= check("20 mm3/s would now hold at least 8.5s, not 3.5s",
                abs(mod._search_min(20.) - 8.5) < 1e-9,
                "%.1f s" % mod._search_min(20.))

    print("\n== unit: working max rounds DOWN to 0.5 ==")
    for lim, want in ((24.0, 20.0), (23.0, 19.5), (22.0, 18.5), (8.0, 6.5)):
        got = FR._working_max(lim)
        ok &= check("limit %.0f -> %.2f -> %.1f" % (lim, lim * FR.WORKING_MARGIN,
                                                    want),
                    abs(got - want) < 1e-9, "got %s" % got)
    ok &= check("never exceeds limit x margin",
                all(FR._working_max(L) <= L * FR.WORKING_MARGIN + 1e-9
                    for L in (5, 8, 13, 17, 22, 24, 31, 47)))

    print("\n== unit: wiping is volume-based, not count-based ==")
    ok &= check("threshold is a volume", FR.SEARCH_WIPE_VOLUME_MM3 > 0)
    ok &= check("one 21 mm3/s measurement alone can trigger a wipe",
                21.0 * 19.8 > FR.SEARCH_WIPE_VOLUME_MM3,
                "416 mm3 vs %.0f" % FR.SEARCH_WIPE_VOLUME_MM3)
    ok &= check("several 5 mm3/s measurements do not",
                5.0 * 7.4 * 3 < FR.SEARCH_WIPE_VOLUME_MM3,
                "111 mm3 vs %.0f" % FR.SEARCH_WIPE_VOLUME_MM3)
    ok &= check("the 1014 mm3 that caused the blob would have wiped twice",
                1014.0 / FR.SEARCH_WIPE_VOLUME_MM3 >= 2.0)

    print("\n== unit: adjacent-steps failure rule ==")
    st = {'fails': {23.0: 1, 24.0: 1}}
    ok &= check("two adjacent single failures -> lower one",
                FR.QidiFlowRamp._confirmed_fail(mod, st, 24.0, 1.0) == 23.0)
    st = {'fails': {23.0: 2}}
    ok &= check("one flow failing twice -> that flow",
                FR.QidiFlowRamp._confirmed_fail(mod, st, 23.0, 1.0) == 23.0)
    st = {'fails': {23.0: 1}}
    ok &= check("a single isolated failure is not confirmed",
                FR.QidiFlowRamp._confirmed_fail(mod, st, 23.0, 1.0) is None)

    print("\n== an internal error must NOT be allowed to shut the MCU down ==")
    # Klipper turns an unhandled exception in a gcode command into an INTERNAL
    # ERROR and latches every MCU into shutdown - that happened for real on
    # 2026-09-15 and took a FIRMWARE_RESTART to clear. The guard has to turn
    # anything unexpected into a clean command failure instead.
    # QIDI_FLOW_SEARCH has its own try/finally cleanup, same as the ramp.

    def boom(*a, **k):
        raise NameError("corr_ms")

    g, mod, p = build(tempfile.mkdtemp(), OracleSensor(20.0))
    mod._test_flow = boom
    try:
        g.run('QIDI_FLOW_SEARCH', HZ=30, MAX_E=9000)
        ok &= check("QIDI_FLOW_SEARCH converts a stray exception", False,
                    "nothing raised")
    except RuntimeError as e:
        ok &= check("QIDI_FLOW_SEARCH converts a stray exception",
                    'internal error' in str(e) and 'NameError' in str(e),
                    str(e))
    except NameError:
        ok &= check("QIDI_FLOW_SEARCH converts a stray exception", False,
                    "NameError escaped - this shuts both MCUs down")
    ok &= check("retract in the finally still ran",
                any('E-2.000' in x for x in g.scripts), str(g.scripts[-4:]))
    ok &= check("hotend was still turned off",
                p.lookup_object('heaters').calls[-1][0] == 0.,
                str(p.lookup_object('heaters').calls))

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
