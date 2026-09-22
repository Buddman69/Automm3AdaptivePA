"""Off-printer tests for qidi_pa_bed_measure (Stage 3, bed variant).

Same principle as test_flow_bed_search.py: the tau-fitting maths is an
unmodified copy of qidi_pa_measure.py and already proven by test_pa_measure.py,
so it is not re-tested here. What IS new:

  * QIDI_PA_BED_MEASURE is registered, not QIDI_PA_MEASURE
  * results still land in pa_table.json - required for QIDI_PA_TABLE
  * it moves straight to bed centre at its own start - no chute detour,
    unlike Stage 1, because heating does not care about position
  * every wipe (per-block round-trip, and the final cleanup) goes via the
    chute and back
  * the same Z-safety guard as Stage 1's bed variant
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mock_klipper import MockReactor, MockPrinter, MockConfig
from test_flow_ramp import ScriptGCode, Cmd, check
from test_flow_bed_search import BedToolhead, write_position
import qidi_pa_bed_measure as PB

G = PB.COUNTS_PER_GF


class HotExtruder:
    def __init__(self, t=275.0):
        self.t = t

    def get_status(self, e):
        return {'temperature': self.t}


class FakeSensorHelper:
    """query_cs1237_end_cmd.send(...) must look like a CmdQueryWrapper: an
    object with .send() returning {'data': <3-byte counts>}. Constant force,
    since the tau-fitting path is not what these tests exercise."""

    def __init__(self, tare_counts=-500.0 * G):
        self.oid = 7
        self._tare = tare_counts

        class Query:
            def send(inner_self, params):
                u = int(-500.0 * G) & 0xFFFFFF
                return {'data': bytes([u & 0xFF, (u >> 8) & 0xFF,
                                       (u >> 16) & 0xFF])}
        self.query_cs1237_end_cmd = Query()

        class MCU:
            def estimated_print_time(self, host):
                return host
        self._mcu = MCU()

    def get_mcu(self):
        return self._mcu

    def read_origin_data(self):
        return -500.0 * G


def build(tmpdir, envelope=None, position=None, homed='xyz', **cfg):
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    g = ScriptGCode()
    printer._objects['gcode'] = g
    printer.add('extruder', HotExtruder())
    th = BedToolhead(homed, position)
    printer.add('toolhead', th)

    class Root:
        pass
    r = Root(); r.sensor_helper = FakeSensorHelper()
    printer.add('probe_air', r)

    if envelope is not None:
        os.makedirs(tmpdir, exist_ok=True)
        with open(os.path.join(tmpdir, 'envelope.json'), 'w') as f:
            json.dump({'payload': envelope}, f)
    v = {'out_dir': tmpdir}
    v.update(cfg)
    mod = PB.load_config(MockConfig(printer, v))
    return g, mod, th


def main():
    ok = True
    tmp = tempfile.mkdtemp()

    print("\n== QIDI_PA_BED_MEASURE is registered, not QIDI_PA_MEASURE ==")
    g, mod, th = build(tmp)
    ok &= check("QIDI_PA_BED_MEASURE registered", 'QIDI_PA_BED_MEASURE'
                in g.commands)
    ok &= check("QIDI_PA_MEASURE (chute) is NOT registered here",
                'QIDI_PA_MEASURE' not in g.commands)

    print("\n== moving to bed centre, and the Z-safety guard ==")
    g, mod, th = build(tmp, position=[5.0, 5.0, 300.0, 0.0])
    write_position(tmp, x=90.0, y=95.0, bed_z=300.0)
    cmd = Cmd(g, {})
    mod._move_to_bed_centre(cmd, th)
    ok &= check("issues a G1 to the recorded X/Y",
                any('G1 X90.00 Y95.00' in x for x in g.scripts), str(g.scripts))

    g2, mod2, th2 = build(tmp, position=[5.0, 5.0, 12.0, 0.0])
    write_position(tmp, x=90.0, y=95.0, bed_z=300.0)
    cmd2 = Cmd(g2, {})
    try:
        mod2._move_to_bed_centre(cmd2, th2)
        ok &= check("refuses when the bed is not actually lowered",
                    False, "no error")
    except RuntimeError as e:
        ok &= check("refuses when the bed is not actually lowered",
                    'QIDI_BED_PREPARE' in str(e), str(e))

    print("\n== wipes round-trip via the chute ==")
    g, mod, th = build(tmp, position=[5.0, 5.0, 300.0, 0.0])
    write_position(tmp, x=60.0, y=61.0, bed_z=300.0)
    cmd = Cmd(g, {})
    mod._wipe_at_chute(cmd, th)
    i_chute = next((i for i, s in enumerate(g.scripts)
                    if 'OPTIMIZED_MOVE_TO_TRASH' in s), None)
    i_return = next((i for i, s in enumerate(g.scripts)
                     if 'G1 X60.00 Y61.00' in s), None)
    ok &= check("chute move issued", i_chute is not None, str(g.scripts))
    ok &= check("and it returns to bed centre afterwards",
                i_return is not None and i_chute < i_return,
                str((i_chute, i_return)))

    g2, mod2, th2 = build(tmp, position=[5.0, 5.0, 300.0, 0.0])
    write_position(tmp, x=60.0, y=61.0, bed_z=300.0)
    cmd2 = Cmd(g2, {})
    mod2._wipe_at_chute(cmd2, th2, return_to_bed=False)
    ok &= check("return_to_bed=False skips the trip back (cleanup wipe)",
                not any('G1 X60.00 Y61.00' in x for x in g2.scripts),
                str(g2.scripts))

    print("\n== a full run: heats, goes straight to bed centre (no chute "
          "detour), measures, cleans up ==")
    tmp2 = tempfile.mkdtemp()
    write_position(tmp2, x=123.0, y=124.0, bed_z=300.0)
    env = {'geometry': {'R': 0.0567, 'layer_h': 0.24, 'line_w': 0.62},
           'points': [{'name': 'centre', 'flow_mm3_s': 7.55,
                       'accel_xy': 5000.0, 'v_e_mm_s': 3.14,
                       'a_e_mm_s2': 283.6}]}
    g, mod, th = build(tmp2, envelope=env, position=[0.0, 0.0, 300.0, 0.0])
    out = g.run('QIDI_PA_BED_MEASURE', TEMP=275, BLOCKS=1, CYCLES=2)
    ok &= check("heats via M109 (position-independent)",
                any(x.startswith('M109') for x in g.scripts), str(g.scripts))
    ok &= check("moves straight to bed centre - no OPTIMIZED_MOVE_TO_TRASH "
                "before the first measurement point",
                'G1 X123.00 Y124.00' in "\n".join(g.scripts), str(g.scripts))
    i_first_bedmove = next(i for i, s in enumerate(g.scripts)
                           if 'G1 X123.00 Y124.00' in s)
    i_any_chute = next((i for i, s in enumerate(g.scripts)
                        if 'OPTIMIZED_MOVE_TO_TRASH' in s), None)
    ok &= check("...specifically BEFORE any chute visit happens",
                i_any_chute is None or i_first_bedmove < i_any_chute,
                str((i_first_bedmove, i_any_chute)))

    print("\n== results still land in pa_table.json - same file, same "
          "schema, as the chute version (required for QIDI_PA_TABLE) ==")
    path = os.path.join(tmp2, 'pa_table.json')
    ok &= check("pa_table.json was written (not a bed-specific filename)",
                os.path.exists(path))
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        ok &= check("it is a list, appended like every other stage",
                    isinstance(data, list) and len(data) >= 1, str(type(data)))
        ok &= check("and carries the SAME payload shape (results key present)",
                    'results' in data[-1]['payload'],
                    str(list(data[-1]['payload'])))

    print("\n== a stray exception must not shut the MCU down ==")
    tmp3 = tempfile.mkdtemp()
    write_position(tmp3, x=1.0, y=1.0, bed_z=300.0)
    g, mod, th = build(tmp3, envelope=env, position=[0.0, 0.0, 300.0, 0.0])
    mod._move_to_bed_centre = lambda *a, **k: (_ for _ in ()).throw(
        NameError("boom"))
    try:
        g.run('QIDI_PA_BED_MEASURE', TEMP=275, BLOCKS=1, CYCLES=2)
        ok &= check("converted to a command error", False, "nothing raised")
    except RuntimeError as e:
        ok &= check("converted to a command error",
                    'internal error' in str(e) and 'NameError' in str(e),
                    str(e))
    except NameError:
        ok &= check("converted to a command error", False, "NameError escaped")

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
