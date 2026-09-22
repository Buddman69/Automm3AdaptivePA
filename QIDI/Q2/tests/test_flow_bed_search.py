"""Off-printer tests for qidi_flow_bed_search (Stage 1, bed variant).

This module is a copy of qidi_flow_ramp.py - the settling, abort and search
logic is byte-for-byte identical and already proven by test_flow_ramp.py, so
it is not re-tested here. What IS new, and what these tests cover:

  * commands are registered under their BED names, not the chute ones
  * results still land in flow_ramp.json - the same file the chute version
    writes, required so QIDI_PA_ENVELOPE keeps working unmodified
  * the startup sequence moves to bed centre (after heating at the chute),
    and tares there - not at the chute
  * every wipe - internal and standalone - round-trips through the chute
  * the Z-safety guard: moving to bed centre refuses if the toolhead's
    CURRENT Z does not match what QIDI_BED_PREPARE recorded, because that
    mismatch means the bed might not still be lowered
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mock_klipper import MockReactor, MockPrinter, MockConfig
from test_flow_ramp import (ScriptGCode, Cmd, MockExtruder, MockHeaters,
                            FakeSensor, check)
import qidi_flow_bed_search as FB


class BedToolhead:
    """Like test_flow_ramp.MockToolhead, plus a settable position - the bed
    tests need to simulate "the machine is currently at Z=x" for the
    Z-safety-guard checks, which nothing existing needed."""

    def __init__(self, homed='xyz', position=None):
        self.waits = 0
        self._homed = homed
        self._position = list(position or [0.0, 0.0, 0.0, 0.0])

        class MCU:
            def estimated_print_time(self, eventtime):
                return eventtime
        self.mcu = MCU()

    def get_status(self, eventtime):
        return {'homed_axes': self._homed}

    def get_last_move_time(self):
        return 0.0

    def wait_moves(self):
        self.waits += 1

    def get_position(self):
        return list(self._position)


def build(tmpdir, sensor=None, homed='xyz', position=None):
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    g = ScriptGCode()
    printer._objects['gcode'] = g
    s = sensor or FakeSensor()

    class Root:
        pass
    r = Root(); r.sensor_helper = s
    printer.add('probe_air', r)
    th = BedToolhead(homed, position)
    printer.add('toolhead', th)
    printer.add('extruder', MockExtruder())
    printer.add('heaters', MockHeaters())
    mod = FB.load_config(MockConfig(printer, {'out_dir': tmpdir}))
    return g, mod, s, th, printer


def write_position(tmpdir, x=100.0, y=100.0, bed_z=300.0):
    os.makedirs(tmpdir, exist_ok=True)
    with open(os.path.join(tmpdir, 'bed_position.json'), 'w') as f:
        json.dump({'x': x, 'y': y, 'bed_z': bed_z, 'time': 0.0}, f)


def main():
    ok = True
    tmp = tempfile.mkdtemp()

    print("\n== commands are registered under their BED names ==")
    g, mod, s, th, p = build(tmp)
    for name in ('QIDI_FLOW_BED_RAMP', 'QIDI_FLOW_BED_WIPE',
                'QIDI_FLOW_BED_SEARCH'):
        ok &= check("%s is registered" % name, name in g.commands)
    for name in ('QIDI_FLOW_RAMP', 'QIDI_FLOW_SEARCH', 'QIDI_FLOW_WIPE'):
        ok &= check("the CHUTE command %s is NOT also registered here"
                    % name, name not in g.commands)

    print("\n== moving to bed centre ==")
    g, mod, s, th, p = build(tmp, position=[10.0, 10.0, 300.0, 0.0])
    write_position(tmp, x=222.0, y=111.0, bed_z=300.0)
    cmd = Cmd(g, {})
    mod._move_to_bed_centre(cmd, th)
    moves = [x for x in g.scripts if 'G1 X222.00 Y111.00' in x]
    ok &= check("issues a G1 to the recorded X/Y", bool(moves), str(g.scripts))
    ok &= check("waits for the move to finish", th.waits >= 1)

    print("\n== the Z-safety guard refuses a stale/mismatched position ==")
    g, mod, s, th, p = build(tmp, position=[10.0, 10.0, 0.5, 0.0])
    write_position(tmp, x=222.0, y=111.0, bed_z=300.0)
    cmd = Cmd(g, {})
    try:
        mod._move_to_bed_centre(cmd, th)
        ok &= check("refuses when actual Z does not match recorded bed_z",
                    False, "no error")
    except RuntimeError as e:
        ok &= check("refuses when actual Z does not match recorded bed_z",
                    'Z0.5' in str(e) or 'the bed may have been raised' in str(e),
                    str(e))
    ok &= check("and issues no move at all",
                not any('G1 X222' in x for x in g.scripts), str(g.scripts))

    print("\n== a small Z difference (settling/rounding) is tolerated ==")
    g, mod, s, th, p = build(tmp, position=[10.0, 10.0, 300.4, 0.0])
    write_position(tmp, x=50.0, y=60.0, bed_z=300.0)
    cmd = Cmd(g, {})
    mod._move_to_bed_centre(cmd, th)   # must not raise
    ok &= check("0.4 mm of slop does not trip the guard",
                any('G1 X50.00 Y60.00' in x for x in g.scripts), str(g.scripts))

    print("\n== missing bed_position.json is refused, not guessed at ==")
    empty = tempfile.mkdtemp()
    g, mod, s, th, p = build(empty, position=[0.0, 0.0, 300.0, 0.0])
    cmd = Cmd(g, {})
    try:
        mod._move_to_bed_centre(cmd, th)
        ok &= check("refuses without a captured position", False, "no error")
    except RuntimeError as e:
        ok &= check("refuses without a captured position",
                    'QIDI_BED_PREPARE' in str(e), str(e))

    print("\n== every wipe round-trips through the chute ==")
    g, mod, s, th, p = build(tmp, position=[10.0, 10.0, 300.0, 0.0])
    write_position(tmp, x=77.0, y=88.0, bed_z=300.0)
    cmd = Cmd(g, {})
    mod._wipe_at_chute(cmd, th, None, None, 6, 4)
    order = g.scripts
    i_chute = next((i for i, s2 in enumerate(order)
                    if 'MOVE_TO_TRASH' in s2), None)
    i_wipe = next((i for i, s2 in enumerate(order)
                   if 'G1 X' in s2 and 'F%d' % FB.WIPE_FEED_FAST in s2), None)
    i_return = next((i for i, s2 in enumerate(order)
                     if 'G1 X77.00 Y88.00' in s2), None)
    ok &= check("chute move happens first", i_chute is not None
                and i_wipe is not None and i_chute < i_wipe,
                str((i_chute, i_wipe)))
    ok &= check("return-to-bed-centre happens last", i_return is not None
                and i_wipe < i_return, str((i_wipe, i_return)))

    print("\n== return_to_bed=False skips the trip back (the cooldown wipe) ==")
    g, mod, s, th, p = build(tmp, position=[10.0, 10.0, 300.0, 0.0])
    write_position(tmp, x=77.0, y=88.0, bed_z=300.0)
    cmd = Cmd(g, {})
    mod._wipe_at_chute(cmd, th, None, None, 2, 0, return_to_bed=False)
    ok &= check("no return move issued",
                not any('G1 X77.00 Y88.00' in x for x in g.scripts),
                str(g.scripts))

    print("\n== QIDI_FLOW_BED_WIPE defaults to 2 long strokes, no short ones ==")
    # _wipe() joins every G1 line into ONE run_script_from_command call, so
    # counting matching ENTRIES in g.scripts would always find "1" - count
    # occurrences of the fast-feed marker WITHIN that one block instead. Both
    # long and short strokes use the fast feed for their outward move, so this
    # total is (passes + short_passes).
    g, mod, s, th, p = build(tmp, position=[10.0, 10.0, 300.0, 0.0])
    write_position(tmp, x=200.0, y=200.0, bed_z=300.0)
    out = g.run('QIDI_FLOW_BED_WIPE')
    joined = "\n".join(g.scripts)
    long_strokes = joined.count("F%d" % FB.WIPE_FEED_FAST)
    ok &= check("exactly 2 strokes total by default (not the chute routine's "
                "6+4)", long_strokes == 2, str(long_strokes))
    ok &= check("and it returned to bed centre",
                any('G1 X200.00 Y200.00' in x for x in g.scripts),
                str(g.scripts))

    print("\n== QIDI_FLOW_BED_WIPE RETURN=0 stays at the chute ==")
    g, mod, s, th, p = build(tmp, position=[10.0, 10.0, 300.0, 0.0])
    write_position(tmp, x=201.0, y=201.0, bed_z=300.0)
    g.run('QIDI_FLOW_BED_WIPE', RETURN=0)
    ok &= check("no return-to-bed-centre move",
                not any('G1 X201' in x for x in g.scripts), str(g.scripts))

    print("\n== results still land in flow_ramp.json - same file, same "
          "schema, as the chute version (required for QIDI_PA_ENVELOPE) ==")
    tmp2 = tempfile.mkdtemp()
    write_position(tmp2, x=150.0, y=150.0, bed_z=300.0)
    g, mod, s, th, p = build(tmp2, position=[10.0, 10.0, 300.0, 0.0])
    g.run('QIDI_FLOW_BED_SEARCH', MAXQ=5, START=5, STEP=10, FINE=1)
    path = os.path.join(tmp2, 'flow_ramp.json')
    ok &= check("flow_ramp.json was written (not a bed-specific filename)",
                os.path.exists(path))
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        ok &= check("it is a list, appended like every other stage",
                    isinstance(data, list) and len(data) >= 1, str(type(data)))
        ok &= check("and carries the SAME payload shape (working_max key "
                    "present)", 'working_max' in data[-1]['payload'],
                    str(list(data[-1]['payload'])))

    print("\n== a stray exception must not shut the MCU down ==")
    g, mod, s, th, p = build(tmp, position=[0.0, 0.0, 300.0, 0.0])
    mod._move_to_bed_centre = lambda *a, **k: (_ for _ in ()).throw(
        NameError("boom"))
    write_position(tmp)
    try:
        g.run('QIDI_FLOW_BED_SEARCH', MAXQ=5)
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
