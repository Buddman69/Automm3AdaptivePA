"""Off-printer tests for qidi_auto_cal_bed.

Mirrors test_auto_cal.py's approach (order, abort behaviour, the results
block) but for the bed chain, plus what is genuinely different from the
chute orchestrator:

  * QIDI_BED_PREPARE: G28 is ALWAYS run (never skipped, unlike the chute
    routine's home-if-needed), captures the toolhead's post-home position,
    moves to the chute, lowers the bed, and writes bed_position.json
  * the chain calls the _BED command names throughout
  * the cooldown wipe does not return to bed centre and does not raise the
    bed back up
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
import qidi_auto_cal_bed as ACB

ROWS = ["0.0349,2.92,500", "0.0286,4.70,500", "0.0236,7.55,500",
        "0.0204,12.14,500", "0.0203,19.50,500",
        "0.0349,2.92,10000", "0.0286,4.70,10000", "0.0236,7.55,10000",
        "0.0204,12.14,10000", "0.0203,19.50,10000"]


class Heater:
    def __init__(self, t=275.0):
        self.t = t

    def get_status(self, e):
        return {'temperature': self.t}


class BedToolhead:
    """G28 always runs here, so the mock's homed state does not gate
    anything the way the chute orchestrator's does - it exists only so
    get_position() has something to return post-home."""

    def __init__(self, position=None):
        self._position = list(position or [0.0, 0.0, 0.0, 0.0])
        self.waits = 0

    def get_status(self, e):
        return {'homed_axes': 'xyz'}

    def get_position(self):
        return list(self._position)

    def wait_moves(self):
        self.waits += 1


class Driver(ScriptGCode):
    """Records scripts and writes the JSON each stage would have written -
    same pattern as test_auto_cal.py's Driver."""

    def __init__(self, tmpdir, fail_on=None, no_qmax=False, no_table=False,
                post_home_position=(50.0, 60.0, 0.0, 0.0)):
        ScriptGCode.__init__(self)
        self.dir = tmpdir
        self.fail_on = fail_on
        self.no_qmax = no_qmax
        self.no_table = no_table
        self.post_home_position = post_home_position

    def run_script_from_command(self, script):
        self.scripts.append(script)
        if self.fail_on and self.fail_on in script:
            raise RuntimeError("stage aborted: %s" % self.fail_on)
        if script == 'G28':
            # G28's effect that QIDI_BED_PREPARE relies on: the toolhead ends
            # up at bed centre. Simulate that by moving the mock toolhead.
            th = self.printer._objects.get('toolhead')
            if th is not None:
                th._position[0] = self.post_home_position[0]
                th._position[1] = self.post_home_position[1]
        if script.startswith('G1 Z'):
            th = self.printer._objects.get('toolhead')
            if th is not None:
                z = float(script.split('Z')[1].split()[0])
                th._position[2] = z
        if script.startswith('QIDI_FLOW_BED_SEARCH'):
            p = {} if self.no_qmax else {'working_max': 19.5}
            self._w('flow_ramp.json', [{'payload': p}])
        elif script.startswith('QIDI_PA_TABLE'):
            if self.no_table:
                self._w('orca_pa.json', {'payload': {}})
            else:
                self._w('orca_pa.json', {'payload': {
                    'rows': ROWS, 'fallback_pressure_advance': 0.0236,
                    'geometry': {'layer_h': 0.24, 'line_w': 0.62}}})

    def _w(self, name, obj):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, name), 'w') as f:
            json.dump(obj, f)


def build(tmpdir, **kw):
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    g = Driver(tmpdir, **kw)
    printer._objects['gcode'] = g
    g.printer = printer
    printer.add('extruder', Heater())
    printer.add('toolhead', BedToolhead())
    mod = ACB.load_config(MockConfig(printer, {'out_dir': tmpdir}))
    return g, mod


def main():
    ok = True

    print("\n== DRY plans the chain and moves nothing ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp)
    out = "\n".join(g.run('QIDI_AUTO_CALIBRATE_BED', DRY=1))
    ok &= check("nothing executed", g.scripts == [], str(g.scripts))
    for step in ('QIDI_BED_PREPARE', 'QIDI_FLOW_BED_SEARCH',
                'QIDI_PA_ENVELOPE', 'QIDI_PA_BED_MEASURE', 'QIDI_PA_TABLE',
                'TEMPERATURE_WAIT'):
        ok &= check("plan mentions %s" % step, step in out, out[:600])
    ok &= check("plan states homing is mandatory here",
                'mandatory' in out.lower() or 'always' in out.lower(),
                out[:400])

    print("\n== QIDI_BED_PREPARE always homes, captures position, moves to "
          "the chute, then lowers the bed - in that order ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, post_home_position=(77.0, 88.0, 0.0, 0.0))
    g.run('QIDI_BED_PREPARE')
    order = [s.split()[0] if not s.startswith('G1') else s.split()[0]
             for s in g.scripts]
    ok &= check("G28 first", g.scripts[0] == 'G28', str(g.scripts[:4]))
    i_chute = next(i for i, s in enumerate(g.scripts)
                   if 'MOVE_TO_TRASH' in s)
    i_bed_z = next(i for i, s in enumerate(g.scripts) if s.startswith('G1 Z'))
    ok &= check("chute move happens BEFORE the bed goes down (toolhead "
                "clear first, per Budd's explicit reordering)",
                i_chute < i_bed_z, str(g.scripts))
    ok &= check("bed goes to Z200 by default",
                any('G1 Z200.00' in s for s in g.scripts), str(g.scripts))
    pos_path = os.path.join(tmp, 'bed_position.json')
    ok &= check("bed_position.json was written", os.path.exists(pos_path))
    if os.path.exists(pos_path):
        with open(pos_path) as f:
            d = json.load(f)
        ok &= check("with the position G28 actually left the toolhead at "
                    "(77, 88) - not a hardcoded guess",
                    d['x'] == 77.0 and d['y'] == 88.0, str(d))
        ok &= check("and the Z it was actually lowered to",
                    d['bed_z'] == 200.0, str(d))

    print("\n== BED_Z and BED_FEED are overridable ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp)
    g.run('QIDI_BED_PREPARE', BED_Z=250, BED_FEED=800)
    ok &= check("uses the overridden Z and feed",
                any('G1 Z250.00 F800' in s for s in g.scripts), str(g.scripts))

    print("\n== the full chain runs in the right order ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp)
    g.run('QIDI_AUTO_CALIBRATE_BED')
    order = [s.split()[0] for s in g.scripts]
    want = ['QIDI_BED_PREPARE', 'QIDI_FLOW_BED_SEARCH', 'QIDI_FLOW_BED_WIPE',
            'QIDI_PA_ENVELOPE', 'QIDI_PA_BED_MEASURE', 'QIDI_FLOW_BED_WIPE',
            'QIDI_PA_TABLE', 'M104', 'TEMPERATURE_WAIT', 'QIDI_FLOW_BED_WIPE',
            'M106']
    ok &= check("prepare -> flow -> wipe -> envelope -> PA -> wipe -> table "
                "-> cooldown -> wipe -> filter off",
                order == want, str(order))
    ok &= check("the measured max flow is passed to Stage 2",
                any('QIDI_PA_ENVELOPE QMAX=19.500' in s for s in g.scripts),
                str([s for s in g.scripts if 'ENVELOPE' in s]))
    ok &= check("the cooldown wipe does NOT ask to return to bed centre "
                "(RETURN=0) - the routine is ending",
                any('QIDI_FLOW_BED_WIPE RETURN=0' in s for s in g.scripts),
                str([s for s in g.scripts if 'WIPE' in s]))
    # QIDI_BED_PREPARE is recorded as one opaque call here (the mock does not
    # unroll a sub-command's own internals, matching how every other stage
    # call in this chain is recorded) - its own G1 Z move is proven separately
    # above, by invoking it directly. What THIS test can and does verify is
    # that the orchestrator's OWN script list - which matched `want` exactly,
    # above - contains no G1 Z of its own anywhere in the chain.
    ok &= check("the orchestrator itself issues no bed-raising move",
                not any(s.startswith('G1 Z') for s in g.scripts), str(order))

    print("\n== the results block is byte-for-byte what gets pasted ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp)
    out = g.run('QIDI_AUTO_CALIBRATE_BED')
    txt = "\n".join(out)
    ok &= check("banner names this as the BED routine",
                "Buddman69's Auto Test Results (BED ROUTINE)" in txt,
                txt[-900:])
    ok &= check("max mm3 header and value",
                'max mm3 =' in txt and '19.5mm3' in txt, txt[-900:])
    ok &= check("PA centre header and value",
                'PA Centre Value =' in txt and 'K=0.0236' in txt, txt[-900:])
    ok &= check("all ten rows present, in order",
                all(r in txt for r in ROWS)
                and txt.index(ROWS[0]) < txt.index(ROWS[-1]), txt[-900:])
    ok &= check("support links are present, same as the chute routine",
                'reddit.com/user/Sport_Subject' in txt
                and 'amazon.com.au/hz/wishlist' in txt, txt[-400:])

    print("\n== a stage abort still leaves the machine cold and wiped ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, fail_on='QIDI_PA_BED_MEASURE')
    try:
        g.run('QIDI_AUTO_CALIBRATE_BED')
        ok &= check("the abort propagates", False, "no error")
    except RuntimeError as e:
        ok &= check("the abort propagates", 'aborted' in str(e), str(e))
    ok &= check("Stage 4 never ran on a half-result",
                not any(s.startswith('QIDI_PA_TABLE') for s in g.scripts),
                str(g.scripts))
    ok &= check("but the machine was still cooled and wiped",
                any(s.startswith('M104 S0') for s in g.scripts)
                and any('QIDI_FLOW_BED_WIPE' in s for s in g.scripts[-3:]),
                str(g.scripts[-4:]))

    print("\n== a failed prepare aborts before anything heats ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, fail_on='QIDI_BED_PREPARE')
    try:
        g.run('QIDI_AUTO_CALIBRATE_BED')
        ok &= check("the prepare failure propagates", False, "no error")
    except RuntimeError as e:
        ok &= check("the prepare failure propagates", 'aborted' in str(e),
                    str(e))
    ok &= check("nothing was heated",
                not any('QIDI_FLOW_BED_SEARCH' in s for s in g.scripts),
                str(g.scripts))

    print("\n== SKIP_FLOW reuses the previous result ==")
    tmp = tempfile.mkdtemp()
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, 'flow_ramp.json'), 'w') as f:
        json.dump([{'payload': {'working_max': 17.0}}], f)
    g, mod = build(tmp)
    g.run('QIDI_AUTO_CALIBRATE_BED', SKIP_FLOW=1)
    ok &= check("no flow search was run",
                not any('QIDI_FLOW_BED_SEARCH' in s for s in g.scripts),
                str(g.scripts))
    ok &= check("and the stored max is used",
                any('QMAX=17.000' in s for s in g.scripts),
                str([s for s in g.scripts if 'ENVELOPE' in s]))
    ok &= check("QIDI_BED_PREPARE still ran - the position must still be "
                "captured fresh even when the flow search is skipped",
                any(s.startswith('QIDI_BED_PREPARE') for s in g.scripts),
                str(g.scripts))

    print("\n== a stray exception must not shut the MCU down ==")
    g, mod = build(tempfile.mkdtemp())
    mod._run = lambda gc: (_ for _ in ()).throw(NameError("boom"))
    try:
        g.run('QIDI_AUTO_CALIBRATE_BED')
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
