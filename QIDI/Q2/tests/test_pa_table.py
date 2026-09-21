"""Off-printer tests for qidi_pa_table (Stage 4).

Pure maths, no printer. The things that carry risk are the Orca column order
(wrong order silently produces a plausible, useless table) and the run-merge
rule (averaging runs measured differently would be wrong).
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
import qidi_pa_table as T

# The real curve, three AMP=0.5 runs on ASA-CF
CURVE = [(2.92, [0.0349, 0.0334, 0.0365]),
         (4.70, [0.0278, 0.0287, 0.0294]),
         (7.55, [0.0236, 0.0241, 0.0233]),
         (12.14, [0.0207, 0.0192, 0.0214]),
         (19.50, [0.0216, 0.0201, 0.0193])]


def entry(idx, conf=72, leg=0.2, cycles=5, blocks=12, amp=0.5, flows=None):
    res = []
    for q, ks in (flows or CURVE):
        res.append({'point': {'name': '%.2f mm3/s' % q, 'flow_mm3_s': q,
                              'v_xy_mm_s': q / 0.1364, 'accel_xy': 5000.0},
                    'k_s': ks[idx % len(ks)], 'k_mad': 0.008, 'n': 55,
                    'confidence': conf})
    return {'kind': 'pa_table', 'time': 1000.0 + idx,
            'payload': {'results': res, 'leg_s': leg, 'cycles': cycles,
                        'blocks': blocks, 'amplitude_frac': amp,
                        'geometry': {'layer_h': 0.24, 'line_w': 0.62,
                                     'R': 0.0567}}}


def build(tmpdir, entries):
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    g = ScriptGCode()
    printer._objects['gcode'] = g
    os.makedirs(tmpdir, exist_ok=True)
    with open(os.path.join(tmpdir, 'pa_table.json'), 'w') as f:
        json.dump(entries, f)
    mod = T.load_config(MockConfig(printer, {'out_dir': tmpdir}))
    return g, mod


def out_payload(tmpdir):
    with open(os.path.join(tmpdir, 'orca_pa.json')) as f:
        return json.load(f)['payload']


def main():
    ok = True

    print("\n== the Orca column order is PA, flow, accel ==")
    # Read off the stock FILL3D PLA Turbo profile, which has a populated model:
    #   0.03,23.43,5000  ->  PA 0.03, flow 23.43 mm3/s, accel 5000
    # The zeroed default could never have revealed the order.
    pts = [{'k_s': 0.0349, 'flow_mm3_s': 2.92},
           {'k_s': 0.0203, 'flow_mm3_s': 19.50}]
    m = T.orca_model(pts, 500.0, 10000.0)
    rows = m.split("\n")
    ok &= check("four rows: two flows x two accels", len(rows) == 4, str(rows))
    ok &= check("first column is PA", rows[0].startswith('0.0349'), rows[0])
    ok &= check("second is flow", rows[0].split(',')[1] == '2.92', rows[0])
    ok &= check("third is accel", rows[0].split(',')[2] == '500', rows[0])
    ok &= check("the same K appears at both accelerations",
                rows[0].split(',')[0] == rows[2].split(',')[0]
                and rows[2].endswith('10000'), str(rows))
    ok &= check("matches the shape of a real Orca model",
                all(len(r.split(',')) == 3 for r in rows), str(rows))

    print("\n== averaging runs that share a configuration ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, [entry(i) for i in range(3)])
    out = "\n".join(g.run('QIDI_PA_TABLE', RUNS=3))
    p = out_payload(tmp)
    ok &= check("all three averaged", p['runs_averaged'] == 3,
                str(p['runs_averaged']))
    ok &= check("one row per flow, five flows", len(p['points']) == 5,
                str(len(p['points'])))
    got = [round(x['k_s'], 4) for x in p['points']]
    want = round(sum(CURVE[0][1]) / 3, 4)
    ok &= check("the lowest flow is the mean of its three runs",
                abs(got[0] - want) < 1e-9, "%.4f vs %.4f" % (got[0], want))
    ok &= check("and a standard error is reported",
                all(x['se'] > 0 for x in p['points']),
                str([round(x['se'], 5) for x in p['points']]))
    ok &= check("K falls with flow, as measured",
                got == sorted(got, reverse=True), str(got))

    print("\n== runs measured DIFFERENTLY are never merged ==")
    tmp = tempfile.mkdtemp()
    mixed = [entry(0, amp=0.25), entry(1, leg=0.4, cycles=2),
             entry(2, amp=0.5), entry(0, amp=0.5)]
    g, mod = build(tmp, mixed)
    out = "\n".join(g.run('QIDI_PA_TABLE', RUNS=4))
    p = out_payload(tmp)
    ok &= check("only the matching runs were used", p['runs_averaged'] == 2,
                str(p['runs_averaged']))
    ok &= check("and it says the others were measured differently",
                'measured\ndifferently' in out or 'measured differently' in out,
                out[:400])

    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, [entry(0), entry(1, flows=[(2.92, [0.03]),
                                                   (9.99, [0.02])])])
    g.run('QIDI_PA_TABLE', RUNS=2)
    ok &= check("a different set of flows also blocks merging",
                out_payload(tmp)['runs_averaged'] == 1,
                str(out_payload(tmp)['runs_averaged']))

    print("\n== low-confidence points are dropped, not trusted ==")
    tmp = tempfile.mkdtemp()
    e = entry(0)
    e['payload']['results'][0]['confidence'] = 20
    g, mod = build(tmp, [e])
    out = "\n".join(g.run('QIDI_PA_TABLE'))
    p = out_payload(tmp)
    ok &= check("the weak point is marked DROPPED", 'DROPPED' in out, out)
    ok &= check("and excluded from the model",
                '2.92' not in p['model'], p['model'])
    ok &= check("but still listed for inspection", len(p['points']) == 5,
                str(len(p['points'])))
    tmp_lo = tempfile.mkdtemp()
    e_lo = entry(0)
    e_lo['payload']['results'][0]['confidence'] = 20
    g_lo, _ = build(tmp_lo, [e_lo])
    g_lo.run('QIDI_PA_TABLE', MIN_CONF=10)
    ok &= check("lowering MIN_CONF lets it back in",
                '2.92' in out_payload(tmp_lo)['model'],
                out_payload(tmp_lo)['model'][:80])

    tmp = tempfile.mkdtemp()
    e2 = entry(0)
    for r in e2['payload']['results'][1:]:
        r['confidence'] = 10
    g, mod = build(tmp, [e2])
    try:
        g.run('QIDI_PA_TABLE')
        ok &= check("refuses to emit a table from one point", False, "no error")
    except RuntimeError as ex:
        ok &= check("refuses to emit a table from one point",
                    'not enough for a table' in str(ex), str(ex))

    print("\n== what the user actually pastes ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, [entry(i) for i in range(3)])
    out = "\n".join(g.run('QIDI_PA_TABLE', RUNS=3))
    ok &= check("names the Orca setting",
                'adaptive_pressure_advance_model' in out, out[-600:])
    ok &= check("tells them to enable adaptive PA",
                'adaptive_pressure_advance = 1' in out, out[-600:])
    ok &= check("gives a fallback single value",
                'pressure_advance = 0.0' in out, out[-600:])
    ok &= check("quantifies what that single value costs",
                'would be' in out and 'wrong at the lowest flow' in out, out)
    ok &= check("states the geometry it is valid for",
                'VALID ONLY for 0.24 layer / 0.62 width' in out, out)
    ok &= check("ten rows for five flows x two accels",
                len(out_payload(tmp)['rows']) == 10,
                str(len(out_payload(tmp)['rows'])))

    print("\n== refuses nonsense ==")
    g, mod = build(tempfile.mkdtemp(), [entry(0)])
    try:
        g.run('QIDI_PA_TABLE', ACCEL_LO=8000, ACCEL_HI=2000)
        ok &= check("rejects inverted accel bounds", False, "no error")
    except RuntimeError as ex:
        ok &= check("rejects inverted accel bounds",
                    'ACCEL_HI must exceed' in str(ex), str(ex))

    empty = tempfile.mkdtemp()
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    g3 = ScriptGCode()
    printer._objects['gcode'] = g3
    T.load_config(MockConfig(printer, {'out_dir': empty}))
    try:
        g3.run('QIDI_PA_TABLE')
        ok &= check("says to run Stage 3 first", False, "no error")
    except RuntimeError as ex:
        ok &= check("says to run Stage 3 first",
                    'QIDI_PA_MEASURE first' in str(ex), str(ex))

    print("\n== a stray exception must not shut the MCU down ==")
    g, mod = build(tempfile.mkdtemp(), [entry(0)])
    mod._run = lambda gc: (_ for _ in ()).throw(NameError("boom"))
    try:
        g.run('QIDI_PA_TABLE')
        ok &= check("converted to a command error", False, "nothing raised")
    except RuntimeError as ex:
        ok &= check("converted to a command error",
                    'internal error' in str(ex) and 'NameError' in str(ex),
                    str(ex))
    except NameError:
        ok &= check("converted to a command error", False, "NameError escaped")

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
