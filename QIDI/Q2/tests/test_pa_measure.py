"""Off-printer tests for qidi_pa_measure (Stage 3).

The novel part is the maths: recovering the melt time constant from a step
response by the integral method. Everything else mirrors Stage 1, which is
proven on the machine. So the fit gets tested against synthetic data at the
real sample rate, the real noise, and with the slow melt drift superimposed -
because if tau comes out biased, Stage 3 returns a plausible wrong number.
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
import qidi_pa_measure as PA

G = PA.COUNTS_PER_GF


class LCG:
    def __init__(self, seed=1):
        self.s = seed

    def u(self):
        self.s = (1103515245 * self.s + 12345) % (1 << 31)
        return self.s / float(1 << 31) - 0.5


def step_response(tau, f0_gf, finf_gf, rate_hz=222.0, span_tau=5.0,
                  noise_gf=0.0, drift_gf_s=0.0, seed=1, jitter_s=0.0):
    """A first-order step in COUNTS, sampled the way the raw path samples."""
    rng = LCG(seed)
    dt = 1.0 / rate_hz
    n = int(span_tau * tau / dt)
    ts, fs = [], []
    t = 0.0
    for i in range(n):
        tt = t + (rng.u() * 2 * jitter_s if jitter_s else 0.0)
        if tt < 0:
            tt = 0.0
        val = finf_gf + (f0_gf - finf_gf) * math.exp(-tt / tau) \
            + drift_gf_s * tt
        if noise_gf:
            val += rng.u() * 2.0 * noise_gf
        ts.append(tt)
        fs.append(val * G)
        t += dt
    return ts, fs


def payload(out_dir):
    """The most recent run. pa_table.json appends, so a comparison run never
    destroys the one before it."""
    with open(os.path.join(out_dir, 'pa_table.json')) as f:
        data = json.load(f)
    assert isinstance(data, list), "pa_table.json must be a list (appended)"
    return data[-1]['payload']


def build(tmpdir, envelope=None, **cfg):
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    g = ScriptGCode()
    printer._objects['gcode'] = g
    if envelope is not None:
        os.makedirs(tmpdir, exist_ok=True)
        with open(os.path.join(tmpdir, 'envelope.json'), 'w') as f:
            json.dump({'payload': envelope}, f)
    v = {'out_dir': tmpdir}
    v.update(cfg)
    mod = PA.load_config(MockConfig(printer, v))
    return g, mod


def main():
    ok = True

    print("\n== cumulative trapezoid ==")
    ts = [0.0, 1.0, 2.0, 3.0]
    ok &= check("integral of a constant is t",
                PA.cumtrapz(ts, [2.0] * 4) == [0.0, 2.0, 4.0, 6.0],
                str(PA.cumtrapz(ts, [2.0] * 4)))
    I = PA.cumtrapz([0.0, 1.0, 2.0], [0.0, 1.0, 2.0])
    ok &= check("integral of t is t^2/2", abs(I[-1] - 2.0) < 1e-9, str(I))
    ok &= check("handles irregular spacing",
                abs(PA.cumtrapz([0.0, 0.1, 0.5], [1.0, 1.0, 1.0])[-1] - 0.5)
                < 1e-9)

    print("\n== the fit recovers a known tau, noise-free ==")
    # Across the whole plausible PA range - an unknown filament could sit
    # anywhere in here, and the short end is where trapezoidal discretisation
    # bites hardest (only ~2.7 samples per tau at 0.010).
    for tau in (0.010, 0.012, 0.024, 0.048, 0.080, 0.100):
        ts, fs = step_response(tau, -500.0, -750.0)
        r = PA.fit_tau(ts, fs)
        err = abs(r['tau'] - tau) / tau if r else 1.0
        ok &= check("tau = %.3f recovered to %.2f%%" % (tau, 100 * err),
                    r is not None and err < 0.01,
                    "got %.5f" % (r['tau'] if r else float('nan')))

    print("\n== and at a lower sample rate, where the bias is worse ==")
    for rate in (150.0, 222.0, 312.0):
        ts, fs = step_response(0.024, -500.0, -750.0, rate_hz=rate)
        r = PA.fit_tau(ts, fs)
        err = abs(r['tau'] - 0.024) / 0.024 if r else 1.0
        ok &= check("%.0f Hz -> %.2f%%" % (rate, 100 * err),
                    r is not None and err < 0.015,
                    "got %.5f" % (r['tau'] if r else float('nan')))

    print("\n== sign-independent: up and down transitions ==")
    # melt pressure is NEGATIVE, and a down-transition runs the other way
    ts, fs = step_response(0.024, -750.0, -500.0)
    r = PA.fit_tau(ts, fs)
    ok &= check("down-transition gives the same tau",
                r is not None and abs(r['tau'] - 0.024) / 0.024 < 0.01,
                "got %.5f" % (r['tau'] if r else float('nan')))
    ts, fs = step_response(0.024, 500.0, 750.0)
    r = PA.fit_tau(ts, fs)
    ok &= check("positive-force convention works too",
                r is not None and abs(r['tau'] - 0.024) / 0.024 < 0.01,
                "got %.5f" % (r['tau'] if r else float('nan')))

    print("\n== at the real sample rate, real noise, real step sizes ==")
    # 0.99 gf noise is the measured raw-path figure. 250 gf is the centre
    # point's step at dv=0.25*v_E; 70 gf is the worst corner's.
    for step_gf, label in ((250.0, 'centre'), (70.0, 'worst corner')):
        errs = []
        for seed in range(1, 25):
            ts, fs = step_response(0.024, -500.0, -500.0 - step_gf,
                                   rate_hz=222.0, noise_gf=0.99, seed=seed,
                                   jitter_s=0.0005)
            r = PA.fit_tau(ts, fs)
            if r:
                errs.append((r['tau'] - 0.024) / 0.024)
        bias = sum(errs) / len(errs)
        sd = math.sqrt(sum((e - bias) ** 2 for e in errs) / len(errs))
        ok &= check("%s: single-transition bias %.1f%%, scatter %.1f%%"
                    % (label, 100 * bias, 100 * sd),
                    abs(bias) < 0.10 and sd < 0.25,
                    "bias %.3f sd %.3f over %d fits" % (bias, sd, len(errs)))
        # aggregating is what Stage 3 actually reports
        med = PA._med([0.024 * (1 + e) for e in errs])
        ok &= check("  aggregated over %d transitions -> %.4f s"
                    % (len(errs), med), abs(med - 0.024) / 0.024 < 0.10,
                    "%.5f" % med)

    print("\n== the slow melt drift must not be read as PA lag ==")
    # melt equilibration is 1-15 s; over a 120 ms window that is near-linear.
    # The t^2 term exists to absorb it.
    for drift in (-200.0, 0.0, 200.0):
        ts, fs = step_response(0.024, -500.0, -750.0, noise_gf=0.99,
                               drift_gf_s=drift, seed=3)
        r = PA.fit_tau(ts, fs)
        ok &= check("drift %+.0f gf/s still gives tau ~0.024" % drift,
                    r is not None and abs(r['tau'] - 0.024) / 0.024 < 0.15,
                    "got %.5f" % (r['tau'] if r else float('nan')))

    print("\n== degenerate input is refused, not guessed at ==")
    ok &= check("too few samples", PA.fit_tau([0.0, 1.0], [1.0, 2.0]) is None)
    ok &= check("a flat trace has no decay",
                PA.fit_tau([i * 0.004 for i in range(30)], [100.0] * 30)
                is None)
    ts, fs = step_response(0.024, -500.0, -750.0)
    ok &= check("a growing exponential is refused",
                PA.fit_tau(ts, [-f for f in fs]) is None
                or PA.fit_tau(ts, [-f for f in fs])['tau'] > 0)

    print("\n== the force abort scales with THIS machine's cell ==")
    # The abort is the only overload protection the printer has - QIDI disables
    # every Klipper extrusion guard - so a cell not reading the configured
    # counts_per_gf gets its ceiling at the wrong FORCE while the console
    # prints the right number. This Q2 was measured at 201 counts/gf across 15
    # points, 8.5 gf to 2063 gf - see qidi_flow_ramp.py for the fit. The
    # X-Max 4 cell measured 182.96, an 11% difference between two machines of
    # the same family, which is why this can never be a shared constant.
    _, m_def = build(tempfile.mkdtemp())
    ok &= check("defaults to the measured 201",
                abs(m_def.counts_per_gf - 201.0) < 1e-9,
                str(m_def.counts_per_gf))
    _, m_lo = build(tempfile.mkdtemp(), counts_per_gf=120.0)
    ok &= check("a configured scale is honoured",
                abs(m_lo.counts_per_gf - 120.0) < 1e-9, str(m_lo.counts_per_gf))
    for mod_, scale in ((m_def, 201.0), (m_lo, 120.0)):
        gf = (PA.ABORT_GF * mod_.counts_per_gf) / scale
        ok &= check("at %.2f counts/gf the ceiling is still %.0f gf"
                    % (scale, PA.ABORT_GF), abs(gf - PA.ABORT_GF) < 1e-6,
                    "%.0f gf" % gf)
    ok &= check("so it can no longer exceed the cell's 2000 gf rating",
                (PA.ABORT_GF * m_lo.counts_per_gf) / 120.0 < 2000.0,
                "%.0f gf" % ((PA.ABORT_GF * m_lo.counts_per_gf) / 120.0))

    print("\n== confidence scoring ==")
    tight = [0.024] * 10
    loose = [0.024 * (1 + 0.4 * ((i % 3) - 1)) for i in range(10)]
    s_tight = PA.confidence(tight, [0.99] * 10, 250.0)
    s_loose = PA.confidence(loose, [0.85] * 10, 70.0)
    ok &= check("a tight high-SNR point scores well", s_tight >= 65,
                str(s_tight))
    ok &= check("a scattered low-SNR point scores lower", s_loose < s_tight,
                "%d vs %d" % (s_loose, s_tight))
    ok &= check("empty scores zero", PA.confidence([], [], 100.0) == 0)
    ok &= check("grades map to the documented bands",
                PA.grade(85) == 'excellent' and PA.grade(70) == 'good'
                and PA.grade(50) == 'fair' and PA.grade(35) == 'weak'
                and PA.grade(10) == 'unusable')

    print("\n== the plan ==")
    env = {'geometry': {'R': 0.0567, 'layer_h': 0.24, 'line_w': 0.62},
           'points': [
               {'name': 'lo flow, lo accel', 'flow_mm3_s': 2.92,
                'accel_xy': 2000.0, 'v_e_mm_s': 1.214, 'a_e_mm_s2': 113.4,
                'weak': False},
               {'name': 'centre', 'flow_mm3_s': 9.75, 'accel_xy': 5000.0,
                'v_e_mm_s': 4.054, 'a_e_mm_s2': 283.6, 'weak': False}]}
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, env)
    out = "\n".join(g.run('QIDI_PA_MEASURE', DRY=1))
    ok &= check("explains why a K search would not have worked",
                'can_pressure_advance' in out, out[:400])
    ok &= check("lists both points", 'centre' in out and 'lo accel' in out, out)
    ok &= check("states the amplitude", 'dv = 25% of v_E' in out, out[:400])
    ok &= check("estimates filament", 'total filament' in out, out)
    ok &= check("DRY moves nothing", g.scripts == [], str(g.scripts))

    print("\n== the move buffer is respected ==")
    g, mod = build(tmp, env)
    out = "\n".join(g.run('QIDI_PA_MEASURE', DRY=1))
    ok &= check("the default block fits the buffer silently",
                'WARNING' not in out,
                "5 cycles x 2 x 200ms = %.1fs vs %.1fs buffer"
                % (2 * 0.2 * 5, PA.BUFFER_TIME_HIGH_S))
    g, mod = build(tmp, env)
    out = "\n".join(g.run('QIDI_PA_MEASURE', DRY=1, CYCLES=15))
    ok &= check("an oversized block is warned about",
                'WARNING' in out and 'move\nbuffer' in out or 'move buffer'
                in out, out[:600])
    ok &= check("and it suggests a CYCLES that fits",
                'Reduce CYCLES to' in out, out[:600])

    print("\n== one point can be selected ==")
    g, mod = build(tmp, env)
    out = "\n".join(g.run('QIDI_PA_MEASURE', DRY=1, POINT='centre'))
    ok &= check("only the centre is planned",
                out.count('centre') >= 1 and 'lo flow, lo accel' not in out,
                out)
    g, mod = build(tmp, env)
    try:
        g.run('QIDI_PA_MEASURE', DRY=1, POINT='nonsense')
        ok &= check("rejects an unknown point name", False, "no error")
    except RuntimeError as e:
        ok &= check("rejects an unknown point name",
                    'no envelope point' in str(e), str(e))

    print("\n== an explicit point needs no envelope ==")
    g, mod = build(tempfile.mkdtemp())
    out = "\n".join(g.run('QIDI_PA_MEASURE', DRY=1, FLOW=9.75, ACCEL=5000))
    ok &= check("works with FLOW and ACCEL", 'explicit' in out, out)
    g, mod = build(tempfile.mkdtemp())
    try:
        g.run('QIDI_PA_MEASURE', DRY=1)
        ok &= check("without either, says to run Stage 2", False, "no error")
    except RuntimeError as e:
        ok &= check("without either, says to run Stage 2",
                    'QIDI_PA_ENVELOPE' in str(e), str(e))

    print("\n== it will not extrude in the wrong place ==")

    class Heater:
        def __init__(self, t):
            self.t = t

        def get_status(self, e):
            return {'temperature': self.t}

    class TH:
        def __init__(self, homed='xyz'):
            self._h = homed
            self.waits = 0
            self.max_accel = 10000.0

        def get_status(self, e):
            return {'homed_axes': self._h, 'max_accel': self.max_accel,
                    'minimum_cruise_ratio': 0.5}

        def get_last_move_time(self):
            return 0.0

        def wait_moves(self):
            self.waits += 1

    class RawCmd:
        def send(self, d, minclock=0, reqclock=0):
            return {'oid': 5, 'data': b'\x00\x00\x00\x00'}

    class S:
        oid = 5
        query_cs1237_end_cmd = RawCmd()

        def read_origin_data(self):
            return -388700.0

        def get_mcu(self):
            class M:
                def estimated_print_time(self, e):
                    return e
            return M()

    def hot(tmpdir, homed='xyz', temp=275.0, park=135.0):
        g, mod = build(tmpdir, env)
        pr = mod.printer

        class Root:
            sensor_helper = S()
        pr.add('probe_air', Root())
        pr.add('toolhead', TH(homed))
        pr.add('extruder', Heater(temp))
        if park is not None:
            class KM:
                def get_status(self, e):
                    return {'park_x': park}
            pr.add('gcode_macro _km_globals', KM())
        return g, mod

    g, mod = hot(tempfile.mkdtemp(), homed='')
    try:
        g.run('QIDI_PA_MEASURE', POINT='centre')
        ok &= check("refuses to run unhomed", False, "no error")
    except RuntimeError as e:
        ok &= check("refuses to run unhomed", 'home' in str(e).lower(), str(e))
    ok &= check("and extruded nothing while unhomed",
                not any('G1 E' in s for s in g.scripts), str(g.scripts))

    g, mod = hot(tempfile.mkdtemp(), temp=40.0)
    try:
        g.run('QIDI_PA_MEASURE', POINT='centre')
        ok &= check("refuses to run cold", False, "no error")
    except RuntimeError as e:
        ok &= check("refuses to run cold", 'heat it' in str(e), str(e))
    ok &= check("and extruded nothing while cold",
                not any('G1 E' in s for s in g.scripts), str(g.scripts))

    g, mod = hot(tempfile.mkdtemp())
    try:
        g.run('QIDI_PA_MEASURE', POINT='centre', BLOCKS=1, CYCLES=2)
    except Exception:
        pass
    joined = "\n".join(g.scripts)
    ok &= check("moves to the purge chute before extruding",
                'MOVE_TO_TRASH' in joined
                and joined.index('MOVE_TO_TRASH')
                < (joined.index('G1 E') if 'G1 E' in joined else 1 << 30),
                joined[:300])
    ok &= check("wipes relative to park_x, not at absolute X 0",
                'G1 X115.00' in joined and 'G1 X85.00' in joined,
                str([l for l in joined.split("\n") if l.startswith('G1 X')][:6]))
    ok &= check("turns filtration on and off again",
                'M106 P3 S254' in joined and 'M106 P3 S0' in joined,
                str([l for l in joined.split("\n") if 'M106' in l]))
    ok &= check("retracts on the way out", 'G1 E-1.0' in joined,
                str([l for l in joined.split("\n") if 'G1 E-' in l]))

    print("\n== K is the up-transitions, not the pool ==")
    # Measured on the machine over seven runs at two leg lengths: up contains
    # the calibrated 0.024, down and the pool both exclude it, and the gap GREW
    # when LEG_MS doubled - so it is physics, not a fit-window artefact.
    def fake_analysis(up, down):
        return {'up': list(up), 'down': list(down), 'all': list(up) + list(down),
                'r2': [0.95] * (len(up) + len(down)), 'r2_up': [0.95] * len(up),
                'steps_gf': [80.0] * (len(up) + len(down)),
                'steps_up': [80.0] * len(up), 'detail': []}

    g, mod = hot(tempfile.mkdtemp())
    mod._analyse = lambda tr: fake_analysis([0.024] * 10, [0.018] * 8)
    g.run('QIDI_PA_MEASURE', POINT='centre', BLOCKS=1, CYCLES=2)
    res = payload(mod.out_dir)['results'][0]
    ok &= check("k_s is the up median, not the pooled 0.0213",
                abs(res['k_s'] - 0.024) < 1e-9, "%.4f" % res['k_s'])
    ok &= check("and it says so", res['k_source'] == 'up', str(res['k_source']))
    ok &= check("n counts the up transitions", res['n'] == 10, str(res['n']))
    ok &= check("down is still recorded",
                abs(res['k_down'] - 0.018) < 1e-9, str(res['k_down']))
    txt = "\n".join(g.output)
    ok &= check("down is labelled diagnostic in the console",
                'diagnostic only' in txt,
                str([l for l in txt.split("\n") if 'down' in l]))

    g, mod = hot(tempfile.mkdtemp())
    mod._analyse = lambda tr: fake_analysis([], [0.018] * 8)
    g.run('QIDI_PA_MEASURE', POINT='centre', BLOCKS=1, CYCLES=2)
    res = payload(mod.out_dir)['results'][0]
    ok &= check("with no up fits it falls back to pooled",
                res['k_source'] == 'pooled', str(res['k_source']))
    ok &= check("and warns the fallback is biased low",
                'NO UP TRANSITIONS' in "\n".join(g.output),
                "\n".join(g.output)[-300:])

    print("\n== the square wave: centred, anchored, and AMP ==")
    c = {'v_e_mm_s': 4.0}
    lo, hi = PA.wave(c, 0.25)
    ok &= check("a plain point is centred on its v_E",
                abs((lo + hi) / 2 - 4.0) < 1e-9 and abs(hi - lo - 1.0) < 1e-9,
                "%.3f..%.3f" % (lo, hi))
    t = {'v_e_mm_s': 8.107, 'anchor': 'top'}
    tlo, thi = PA.wave(t, 0.25)
    ok &= check("an anchored point puts v_hi AT its v_E, never above",
                abs(thi - 8.107) < 1e-9, "%.4f" % thi)
    ok &= check("which a centred wave would have exceeded by 12.5%",
                abs(8.107 * 1.125 - 9.120) < 0.01)
    ok &= check("but dv is identical either way",
                abs((thi - tlo) - 0.25 * 8.107) < 1e-9, "%.4f" % (thi - tlo))
    for a in (0.1, 0.5):
        l2, h2 = PA.wave(c, a)
        ok &= check("AMP=%.2f scales dv" % a, abs(h2 - l2 - a * 4.0) < 1e-9,
                    "%.4f" % (h2 - l2))
        l3, h3 = PA.wave(t, a)
        ok &= check("  and never lifts an anchored v_hi", abs(h3 - 8.107) < 1e-9)

    g, mod = build(tmp, env)
    out = "\n".join(g.run('QIDI_PA_MEASURE', DRY=1, AMP=0.5))
    ok &= check("AMP reaches the plan", 'dv = 50% of v_E' in out, out[:400])
    env_top = dict(env)
    env_top['points'] = env['points'] + [
        {'name': '19.50 mm3/s (max)', 'flow_mm3_s': 19.5, 'accel_xy': 5000.0,
         'v_e_mm_s': 8.107, 'a_e_mm_s2': 283.6, 'anchor': 'top', 'weak': False}]
    g, mod = build(tempfile.mkdtemp(), env_top)
    out = "\n".join(g.run('QIDI_PA_MEASURE', DRY=1))
    ok &= check("the plan marks the anchored point", 'TOP-ANCHORED' in out, out)

    print("\n== BLOCKS range ==")
    # The cap was 10, which rejected the 12-block averaging run this module
    # exists to make possible.
    g, mod = build(tmp, env)
    out = "\n".join(g.run('QIDI_PA_MEASURE', DRY=1, POINT='centre', BLOCKS=12))
    ok &= check("BLOCKS=12 is accepted", 'centre' in out, out[:200])
    ok &= check("and the estimate accounts for the wipes",
                'inter-block wipe' in out, out)
    g, mod = build(tmp, env)
    out = "\n".join(g.run('QIDI_PA_MEASURE', DRY=1, POINT='centre', BLOCKS=50))
    ok &= check("BLOCKS=50 is still accepted", 'centre' in out, out[:200])
    g, mod = build(tmp, env)
    try:
        g.run('QIDI_PA_MEASURE', DRY=1, POINT='centre', BLOCKS=51)
        ok &= check("but 51 is refused", False, "no error")
    except Exception as e:
        ok &= check("but 51 is refused", 'BLOCKS' in str(e) or 'maximum'
                    in str(e), str(e))

    print("\n== volume-based wiping between blocks ==")
    # centre: (3.547+4.560)*0.2*5*2.4053 = 19.5 mm3 per block, so 200 mm3 falls
    # after ~10 blocks. Stage 1 established that threshold over several hot runs.
    g, mod = hot(tempfile.mkdtemp())
    try:
        g.run('QIDI_PA_MEASURE', POINT='centre', BLOCKS=3)
    except Exception:
        pass
    wipes_short = "\n".join(g.scripts).count('SAVE_GCODE_STATE NAME=_qidi_pa_wipe')
    g, mod = hot(tempfile.mkdtemp())
    try:
        g.run('QIDI_PA_MEASURE', POINT='centre', BLOCKS=14)
    except Exception:
        pass
    j = "\n".join(g.scripts)
    wipes_long = j.count('SAVE_GCODE_STATE NAME=_qidi_pa_wipe')
    ok &= check("a short run stays under the threshold and adds no wipe",
                wipes_short == 2, "%d wipes (entry + cleanup)" % wipes_short)
    ok &= check("a long run wipes mid-point", wipes_long > wipes_short,
                "%d vs %d" % (wipes_long, wipes_short))
    ok &= check("and re-primes afterwards so the melt is back up",
                ('G1 E%.3f' % (PA.REPRIME_MM3 / PA.A_FIL_MM2)) in j,
                str([l for l in j.split("\n") if l.startswith('G1 E2')][:3]))
    ok &= check("the mid-point wipe runs at machine accel, not a_E",
                j.count('SET_VELOCITY_LIMIT ACCEL=10000') >= 2,
                str([l for l in j.split("\n") if 'VELOCITY' in l][:6]))

    print("\n== the machine's motion limits are put back ==")
    g, mod = hot(tempfile.mkdtemp())
    try:
        g.run('QIDI_PA_MEASURE', BLOCKS=1, CYCLES=2)
    except Exception:
        pass
    lim = [l for l in "\n".join(g.scripts).split("\n")
           if 'SET_VELOCITY_LIMIT' in l]
    ok &= check("restores the captured 10000, not the a_E it just set",
                lim and lim[-1].startswith('SET_VELOCITY_LIMIT ACCEL=10000'),
                str(lim[-3:]))
    ok &= check("never restores to an envelope a_E",
                not any('ACCEL=113' in l or 'ACCEL=454' in l or 'ACCEL=284' in l
                        for l in lim if 'CRUISE_RATIO=0.500' in l),
                str(lim))
    ok &= check("and puts the cruise ratio back too",
                any('MINIMUM_CRUISE_RATIO=0.500' in l for l in lim), str(lim))

    print("\n== the limits are captured once, before the trash move ==")
    # A structural check, not a behavioural one - the mock toolhead's
    # max_accel does not change as a side effect of a script call, so a test
    # run through it cannot catch a re-capture picking up a stale value.
    # MOVE_TO_TRASH sets its own M204 and never restores it, so capturing
    # AGAIN after that move would silently pick up the macro's acceleration
    # instead of the machine's real configured one - this was a live bug,
    # invisible on a machine where the two numbers happen to match.
    src = open(PA.__file__, encoding='utf-8').read()
    ok &= check("exactly one capture of _motion_limits(toolhead) in the file",
                src.count('self._motion_limits(toolhead)') == 1,
                "%d occurrences" % src.count('self._motion_limits(toolhead)'))
    i_capture = src.find('self._motion_limits(toolhead)')
    i_trash = src.find('"MOVE_TO_TRASH"')
    ok &= check("and it happens before the move to the chute",
                -1 < i_capture < i_trash, "capture=%d trash=%d"
                % (i_capture, i_trash))

    print("\n== wipe speed ==")
    # Doubling these left waste on the nozzle on a real run. The saving was
    # ~14 s across a five-point run, so the proven speed wins.
    ok &= check("fast pass is back to Stage 1's proven 167 mm/s",
                PA.WIPE_FEED_FAST == 10000,
                "%.0f mm/s" % (PA.WIPE_FEED_FAST / 60.0))
    ok &= check("slow pass likewise", PA.WIPE_FEED_SLOW == 6000,
                str(PA.WIPE_FEED_SLOW))
    ok &= check("both stay inside the 400 mm/s machine limit",
                PA.WIPE_FEED_FAST / 60.0 < 400)
    ok &= check("6 wipe passes, raised after waste was left behind",
                PA.WIPE_PASSES == 6, str(PA.WIPE_PASSES))
    g, mod = hot(tempfile.mkdtemp())
    try:
        g.run('QIDI_PA_MEASURE', POINT='centre', BLOCKS=1, CYCLES=2)
    except Exception:
        pass
    jj = "\n".join(g.scripts)
    first = jj.split('SAVE_GCODE_STATE NAME=_qidi_pa_wipe')[1].split(
        'RESTORE_GCODE_STATE')[0]
    # 6 long strokes x 2 moves + 4 short strokes x 2 moves + the park move.
    want = 2 * PA.WIPE_PASSES + 2 * PA.WIPE_SHORT_PASSES + 1
    ok &= check("which is %d X moves per wipe (%d long, %d short, plus park)"
                % (want, PA.WIPE_PASSES, PA.WIPE_SHORT_PASSES),
                first.count('G1 X') == want, str(first.count('G1 X')))
    # The short strokes must actually be shorter: every X in the wipe should sit
    # at or below the far end, and the short ones strictly inside it.
    #
    # On the X-Max 4 build, WIPE_LO_OFFSET is 0.0, so the wipe's low point and
    # the final park-return point are the SAME X - only 3 distinct stops. On
    # this Q2 build, WIPE_LO_OFFSET is 10.0 (matching QIDI's own
    # CLEAR_NOZZLE_PLR, which does not wipe all the way back to park), so park
    # and lo are two different points - 4 distinct stops: park < lo < short_hi
    # < hi.
    xs = sorted({float(t.split()[0]) for t in first.split('G1 X')[1:]})
    ok &= check("park, lo, short_hi and hi are four distinct, ordered stops",
                len(xs) == 4 and xs[0] < xs[1] < xs[2] < xs[3], str(xs))
    g, mod = hot(tempfile.mkdtemp())
    try:
        g.run('QIDI_PA_MEASURE', POINT='centre', BLOCKS=1, CYCLES=2)
    except Exception:
        pass
    j = "\n".join(g.scripts)
    ok &= check("the wipe uses the proven feedrates",
                'F10000' in j and 'F6000' in j,
                str([l for l in j.split("\n") if l.startswith('G1 X')][:4]))

    print("\n== cleanup still runs when the point raises ==")
    g, mod = hot(tempfile.mkdtemp())
    mod._measure_point = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("force abort"))
    try:
        g.run('QIDI_PA_MEASURE', POINT='centre')
    except Exception:
        pass
    joined = "\n".join(g.scripts)
    ok &= check("filtration is still switched off", 'M106 P3 S0' in joined,
                str([l for l in joined.split("\n") if 'M106' in l]))
    ok &= check("and the nozzle is still retracted and wiped",
                'G1 E-1.0' in joined and 'G1 X' in joined, joined[-200:])

    print("\n== a stray exception must not shut the MCU down ==")
    g, mod = build(tempfile.mkdtemp(), env)
    mod._run = lambda gc: (_ for _ in ()).throw(NameError("corr_ms"))
    try:
        g.run('QIDI_PA_MEASURE', DRY=1)
        ok &= check("converted to a command error", False, "nothing raised")
    except RuntimeError as e:
        ok &= check("converted to a command error",
                    'internal error' in str(e) and 'NameError' in str(e),
                    str(e))
    except NameError:
        ok &= check("converted to a command error", False, "NameError escaped")

    print("\n== transition windowing ==")
    # 4 cycles = 8 legs; transition 0 is dropped because its leg starts at rest
    rows = [(0.5 + i * 0.004, -500.0 * G) for i in range(400)]
    trs = mod._transitions(rows, 0.5, 0.5 + 8 * 0.2, 4, 0.2)
    ok &= check("skips the first transition of a block",
                all(t['k'] >= 1 for t in trs), str([t['k'] for t in trs]))
    ok &= check("alternates up and down",
                [t['up'] for t in trs[:4]] == [True, False, True, False],
                str([t['up'] for t in trs[:4]]))
    ok &= check("no transitions from an empty capture",
                mod._transitions([], 0.0, 1.0, 4, 0.2) == [])

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
