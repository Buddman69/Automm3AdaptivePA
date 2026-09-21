"""Off-printer tests for qidi_pa_envelope (Stage 2).

Stage 2 is pure maths, so these are mostly arithmetic checks against the
geometry equations - plus the two properties that matter structurally:
v_E is geometry-independent, a_E is not.
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
import qidi_pa_envelope as EN


def build(tmpdir, **cfg):
    printer = MockPrinter(MockReactor())
    g = ScriptGCode()
    printer._objects['gcode'] = g
    v = {'out_dir': tmpdir}
    v.update(cfg)
    mod = EN.load_config(MockConfig(printer, v))
    return g, mod


def main():
    ok = True
    tmp = tempfile.mkdtemp()

    print("\n== the geometry equations ==")
    # For the 0.6 nozzle at 0.3 layer / 0.66 width, R = 0.0743.
    a = EN.line_area(0.30, 0.66)
    r = a / EN.filament_area()
    ok &= check("R = 0.0743 at 0.30 layer / 0.66 width", abs(r - 0.0743) < 5e-5,
                "got %.5f" % r)
    ok &= check("A_fil = 2.405 mm2", abs(EN.filament_area() - 2.405) < 1e-3,
                "%.4f" % EN.filament_area())
    a2 = EN.line_area(0.24, 0.62)
    r2 = a2 / EN.filament_area()
    ok &= check("R = 0.0567 at 0.24 / 0.62", abs(r2 - 0.0567) < 5e-5,
                "got %.5f" % r2)

    print("\n== v_E is geometry-independent, a_E is not ==")
    # The four-corner layout this used to be written against was removed on
    # 2026-09-17; the property it proves belongs to the flow set too, and
    # matters more there because the flow axis is now the only axis.
    e1 = EN.build_flow_set(19.5, 0.24, 0.62)
    e2 = EN.build_flow_set(19.5, 0.30, 0.66)
    v1 = [round(p['v_e_mm_s'], 9) for p in e1['points']]
    v2 = [round(p['v_e_mm_s'], 9) for p in e2['points']]
    ok &= check("v_E identical across geometries (flow cancels it)", v1 == v2,
                "%s vs %s" % (v1[:2], v2[:2]))
    a1 = [p['a_e_mm_s2'] for p in e1['points']]
    a2_ = [p['a_e_mm_s2'] for p in e2['points']]
    diff = 100 * (a2_[0] - a1[0]) / a1[0]
    ok &= check("a_E differs by ~31% between them", abs(diff - 31.0) < 1.0,
                "%.1f%%" % diff)
    ok &= check("the geometry is recorded with the table",
                abs(e1['geometry']['layer_h'] - 0.24) < 1e-9
                and abs(e1['geometry']['R'] - 0.0567) < 5e-5)

    print("\n== weak points are flagged, not hidden ==")
    # ramp/K rises with flow at fixed accel, so the top point is always the
    # hardest to measure - that is what the flag has to catch.
    slow = EN.build_flow_set(19.5, 0.24, 0.62, accel=800.0)
    ok &= check("the highest flow is the worst point",
                max(slow['points'], key=lambda p: p['ramp_over_k'])
                is slow['points'][-1],
                str([round(p['ramp_over_k'], 2) for p in slow['points']]))
    ok &= check("and at a low accel it is flagged weak",
                slow['points'][-1]['weak'],
                str([(p['name'], round(p['ramp_over_k'], 2))
                     for p in slow['points']]))
    fast = EN.build_flow_set(19.5, 0.24, 0.62, accel=5000.0)
    ok &= check("at the real 5000 accel nothing is flagged",
                not any(p['weak'] for p in fast['points']),
                str([(p['name'], round(p['ramp_over_k'], 2))
                     for p in fast['points']]))
    print("\n== flow mode: geometric points, top anchored at max ==")
    # The accel axis was measured and found not to matter (t=1.37, bounded at
    # +/-16%) while flow does (t=5.9), so the measurements it was spending on
    # replicates buy flow resolution instead - at identical cost.
    fs = EN.build_flow_set(19.5, 0.24, 0.62)
    qs = [p['flow_mm3_s'] for p in fs['points']]
    ok &= check("five points by default", len(qs) == 5, str(len(qs)))
    ok &= check("lowest is 15% of max", abs(qs[0] - 0.15 * 19.5) < 1e-9,
                "%.3f" % qs[0])
    ok &= check("highest IS max, not 85% of it", abs(qs[-1] - 19.5) < 1e-9,
                "%.3f" % qs[-1])
    r = [qs[i + 1] / qs[i] for i in range(4)]
    ok &= check("spacing is geometric, so points concentrate at low flow",
                max(r) - min(r) < 1e-9, str([round(x, 4) for x in r]))
    ok &= check("only the top point is anchored",
                [p['anchor'] for p in fs['points']]
                == ['centre'] * 4 + ['top'],
                str([p['anchor'] for p in fs['points']]))
    ok &= check("every point shares one accel - it is no longer an axis",
                len({p['accel_xy'] for p in fs['points']}) == 1,
                str({p['accel_xy'] for p in fs['points']}))
    ok &= check("and frac_of_max is recorded for Stage 4",
                abs(fs['points'][-1]['frac_of_max'] - 1.0) < 1e-9)

    print("\n== the anchored top point never exceeds max ==")
    import qidi_pa_measure as PM
    top = fs['points'][-1]
    lo, hi = PM.wave(top, 0.25)
    ok &= check("v_hi IS the max, exactly",
                abs(hi - 19.5 / EN.filament_area()) < 1e-9, "%.4f" % hi)
    ok &= check("a CENTRED wave there would have overshot by 12.5%",
                top['v_e_mm_s'] * 1.125 > hi,
                "%.4f vs %.4f" % (top['v_e_mm_s'] * 1.125, hi))
    ok &= check("dv is the same either way", abs((hi - lo)
                - 0.25 * top['v_e_mm_s']) < 1e-9, "%.4f" % (hi - lo))
    mid = fs['points'][2]
    mlo, mhi = PM.wave(mid, 0.25)
    ok &= check("a non-anchored point is still centred",
                abs((mlo + mhi) / 2 - mid['v_e_mm_s']) < 1e-9)

    print("\n== command output and report ==")
    g, mod = build(tmp)
    out = g.run('QIDI_PA_ENVELOPE', QMAX=19.5, LAYER=0.24, WIDTH=0.62)
    text = "\n".join(out)
    ok &= check("prints R", 'R = 0.0567' in text, text[:300])
    ok &= check("flow mode is the default, so points are named by flow",
                'mm3/s' in text and '(max)' in text, text)
    ok &= check("states the geometry the table is valid for",
                'valid for 0.24 layer / 0.62 width ONLY' in text, text[-300:])
    path = os.path.join(tmp, 'envelope.json')
    ok &= check("envelope.json written", os.path.exists(path))
    if os.path.exists(path):
        with open(path) as f:
            d = json.load(f)
        pl = d['payload']
        ok &= check("records geometry, range and points",
                    all(x in pl for x in ('geometry', 'range', 'points')),
                    str(list(pl)))

    print("\n== picks up Stage 1's answer when QMAX is omitted ==")
    tmp2 = tempfile.mkdtemp()
    with open(os.path.join(tmp2, 'flow_ramp.json'), 'w') as f:
        json.dump([{'kind': 'flow_ramp',
                    'payload': {'mode': 'search', 'working_max': 20.0}}], f)
    g2, mod2 = build(tmp2)
    out2 = g2.run('QIDI_PA_ENVELOPE', LAYER=0.24, WIDTH=0.62)
    t2 = "\n".join(out2)
    ok &= check("reads working_max from flow_ramp.json",
                '20.00 mm3/s (from the last QIDI_FLOW_SEARCH)' in t2, t2[:200])
    # Flow mode runs 15% to 100% of max, not 15-85% - closing the extrapolation
    # gap that let solid infill run above the top measured point.
    ok &= check("and the points span 15% to 100% of it",
                '3.00-20.00 mm3/s' in t2, t2[:400])
    ok &= check("it states that accel is not an axis and why",
                'NOT an axis' in t2 and 't=1.37' in t2, t2[:500])
    ok &= check("and explains the anchored top point",
                'ANCHORED' in t2 and 'overshoot' in t2, t2[:600])

    print("\n== refuses nonsense ==")
    g3, mod3 = build(tempfile.mkdtemp())
    # Klipper silently ignores parameters a command does not read, so a stale
    # MODE=corners would otherwise run flow mode and look like it worked.
    try:
        g3.run('QIDI_PA_ENVELOPE', QMAX=19.5, MODE='corners')
        ok &= check("a removed MODE fails loudly rather than silently doing "
                    "something else", False, "no error")
    except RuntimeError as e:
        ok &= check("a removed MODE fails loudly rather than silently doing "
                    "something else",
                    'MODE was removed' in str(e), str(e))
    try:
        g3.run('QIDI_PA_ENVELOPE', LAYER=0.24)
        ok &= check("errors clearly with no QMAX and no prior run", False,
                    "no error")
    except RuntimeError as e:
        ok &= check("errors clearly with no QMAX and no prior run",
                    'run QIDI_FLOW_SEARCH first' in str(e), str(e))

    print("\n== an internal error must NOT be allowed to shut the MCU down ==")
    # Klipper turns an unhandled exception in a gcode command into an INTERNAL
    # ERROR and latches every MCU into shutdown - that happened for real on
    # 2026-09-15 and took a FIRMWARE_RESTART to clear. The guard has to turn
    # anything unexpected into a clean command failure instead.

    def boom(*a, **k):
        raise NameError("corr_ms")

    g, mod = build(tempfile.mkdtemp())
    mod._run_ENVELOPE = boom
    try:
        g.run('QIDI_PA_ENVELOPE', MAXFLOW=19.5)
        ok &= check("QIDI_PA_ENVELOPE converts a stray exception", False,
                    "nothing raised")
    except RuntimeError as e:
        ok &= check("QIDI_PA_ENVELOPE converts a stray exception",
                    'internal error' in str(e) and 'NameError' in str(e),
                    str(e))
    except NameError:
        ok &= check("QIDI_PA_ENVELOPE converts a stray exception", False,
                    "NameError escaped - this shuts both MCUs down")

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
