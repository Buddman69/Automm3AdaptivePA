"""Off-printer tests for qidi_auto_cal.

The orchestrator owns no measurement logic - it drives the four stages and
formats their output. So the tests are about ORDER, ABORT BEHAVIOUR, and the
exact text of the results block, which the user pastes into Orca verbatim.
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
import qidi_auto_cal as AC

ROWS = ["0.0349,2.92,500", "0.0286,4.70,500", "0.0236,7.55,500",
        "0.0204,12.14,500", "0.0203,19.50,500",
        "0.0349,2.92,10000", "0.0286,4.70,10000", "0.0236,7.55,10000",
        "0.0204,12.14,10000", "0.0203,19.50,10000"]


class Heater:
    def __init__(self, t=275.0):
        self.t = t

    def get_status(self, e):
        return {'temperature': self.t}


class Toolhead:
    """G28 is the load-cell check, so whether it runs depends on this."""

    def __init__(self, homed='xyz'):
        self._homed = homed

    def get_status(self, e):
        return {'homed_axes': self._homed}


class Driver(ScriptGCode):
    """Records scripts and writes the JSON each stage would have written."""

    def __init__(self, tmpdir, fail_on=None, no_qmax=False, no_table=False):
        ScriptGCode.__init__(self)
        self.dir = tmpdir
        self.fail_on = fail_on
        self.no_qmax = no_qmax
        self.no_table = no_table

    def run_script_from_command(self, script):
        self.scripts.append(script)
        if self.fail_on and self.fail_on in script:
            raise RuntimeError("stage aborted: %s" % self.fail_on)
        if script.startswith('QIDI_FLOW_SEARCH'):
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


def build(tmpdir, homed='xyz', **kw):
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    g = Driver(tmpdir, **kw)
    printer._objects['gcode'] = g
    printer.add('extruder', Heater())
    printer.add('toolhead', Toolhead(homed))
    mod = AC.load_config(MockConfig(printer, {'out_dir': tmpdir}))
    return g, mod


def main():
    ok = True

    print("\n== DRY plans the chain and moves nothing ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp)
    out = "\n".join(g.run('QIDI_AUTO_CALIBRATE', DRY=1))
    ok &= check("nothing executed", g.scripts == [], str(g.scripts))
    for step in ('G28', 'QIDI_FLOW_SEARCH', 'QIDI_PA_ENVELOPE',
                 'QIDI_PA_MEASURE', 'QIDI_PA_TABLE', 'TEMPERATURE_WAIT'):
        ok &= check("plan mentions %s" % step, step in out, out[:400])

    print("\n== G28 is the load-cell check ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, homed='')
    g.run('QIDI_AUTO_CALIBRATE')
    ok &= check("an unhomed machine is homed first",
                g.scripts and g.scripts[0] == 'G28', str(g.scripts[:3]))
    ok &= check("and it happens before anything heats",
                g.scripts.index('G28')
                < min(i for i, s in enumerate(g.scripts)
                      if s.startswith('QIDI_FLOW_SEARCH')), str(g.scripts[:3]))

    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, homed='xyz')
    g.run('QIDI_AUTO_CALIBRATE')
    ok &= check("an already-homed machine is not re-homed - that home already "
                "proved the probe",
                'G28' not in g.scripts, str(g.scripts[:3]))

    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, homed='xyz')
    g.run('QIDI_AUTO_CALIBRATE', HOME=1)
    ok &= check("HOME=1 forces it anyway", g.scripts[0] == 'G28',
                str(g.scripts[:3]))

    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, homed='')
    g.run('QIDI_AUTO_CALIBRATE', HOME=0)
    ok &= check("HOME=0 skips it even unhomed", 'G28' not in g.scripts,
                str(g.scripts[:3]))

    print("\n== a failed home aborts before the cooldown wipe is armed ==")
    # The wipe refuses on an unhomed machine, so a homing failure must stop the
    # chain outright rather than adding a wipe error on top of it.
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, homed='', fail_on='G28')
    try:
        g.run('QIDI_AUTO_CALIBRATE')
        ok &= check("the homing failure propagates", False, "no error")
    except RuntimeError as e:
        ok &= check("the homing failure propagates", 'aborted' in str(e),
                    str(e))
    ok &= check("and no wipe was attempted on an unhomed machine",
                not any('WIPE' in s for s in g.scripts), str(g.scripts))
    ok &= check("and nothing was heated", not any('QIDI_FLOW_SEARCH' in s
                                                  for s in g.scripts),
                str(g.scripts))

    print("\n== the stages run in the right order ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp)
    g.run('QIDI_AUTO_CALIBRATE')
    order = [s.split()[0] for s in g.scripts]
    want = ['QIDI_FLOW_SEARCH', 'QIDI_FLOW_WIPE', 'QIDI_PA_ENVELOPE',
            'QIDI_PA_MEASURE', 'QIDI_FLOW_WIPE', 'QIDI_PA_TABLE',
            'M104', 'TEMPERATURE_WAIT', 'QIDI_FLOW_WIPE', 'M106']
    ok &= check("flow -> wipe -> envelope -> PA -> wipe -> table -> cooldown",
                order == want, str(order))
    ok &= check("the measured max flow is passed to Stage 2",
                any('QIDI_PA_ENVELOPE QMAX=19.500' in s for s in g.scripts),
                str([s for s in g.scripts if 'ENVELOPE' in s]))

    print("\n== the cooldown wipe ==")
    ok &= check("heater is switched off before waiting",
                order.index('M104') < order.index('TEMPERATURE_WAIT'))
    ok &= check("waits for 170 C by default",
                any('TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=170' in s
                    for s in g.scripts),
                str([s for s in g.scripts if 'TEMPERATURE' in s]))
    ok &= check("and wipes AFTER the wait, not before",
                order.index('TEMPERATURE_WAIT')
                < len(order) - 1 - order[::-1].index('QIDI_FLOW_WIPE'),
                str(order))
    # The filter is the last thing off: the nozzle is still outgassing until it
    # has cooled, so it stays running through the wait and the wipe.
    ok &= check("the air filter is the very last thing switched off",
                order[-1] == 'M106', str(order))

    print("\n== the results block is byte-for-byte what gets pasted ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp)
    out = g.run('QIDI_AUTO_CALIBRATE')
    txt = "\n".join(out)
    ok &= check("banner", "Buddman69's Auto Test Results" in txt, txt[-900:])
    ok &= check("paste instruction",
                'Copy and paste the following into Orca' in txt)
    ok &= check("max mm3 header and value",
                'max mm3 =' in txt and '19.5mm3' in txt, txt[-900:])
    ok &= check("PA centre header and value",
                'PA Centre Value =' in txt and 'K=0.0236' in txt, txt[-900:])
    ok &= check("adaptive header", 'Adaptive PA Values =' in txt)
    ok &= check("all ten rows present, in order",
                all(r in txt for r in ROWS)
                and txt.index(ROWS[0]) < txt.index(ROWS[-1]), txt[-900:])
    ok &= check("footer",
                'project probably maybe on github' in txt, txt[-300:])
    ok &= check("the links appear BELOW the results, not above them",
                'reddit.com/user/Sport_Subject' in txt
                and txt.index(ROWS[-1]) < txt.index('reddit.com'), txt[-400:])
    ok &= check("and the wishlist comes with them",
                'amazon.com.au/hz/wishlist' in txt
                and 'anything helps' in txt, txt[-400:])
    ok &= check("the wishlist URL is not broken across lines",
                '2ZFL750GMOMU1?ref_=wl_share' in txt, txt[-300:])
    with open(os.path.join(tmp, 'auto_cal.json')) as f:
        p = json.load(f)[-1]['payload']
    ok &= check("the block is saved verbatim for later",
                p['block'].count('\n') > 10 and ROWS[0] in p['block'])
    ok &= check("and the numbers are recorded separately",
                p['qmax_mm3_s'] == 19.5 and p['centre_k'] == 0.0236, str(p)[:200])

    print("\n== the chain must not cool the nozzle out from under Stage 3 ==")
    # Stage 1 shuts the hotend and air filter down at the end of ITS run,
    # which is right standalone and wrong in a chain. On 2026-09-17 it left
    # Stage 3 extruding into a cooling nozzle: the LOWEST flow point, 2.92
    # mm3/s, hit the 1650 gf abort at 1662 gf - more force cold than 23 mm3/s
    # made hot. The chain owns the shutdown, and it happens last.
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp)
    g.run('QIDI_AUTO_CALIBRATE')
    search = [s for s in g.scripts if s.startswith('QIDI_FLOW_SEARCH')][0]
    ok &= check("Stage 1 is told not to cool down", "COOLDOWN=0" in search,
                search)
    measure = [s for s in g.scripts if s.startswith('QIDI_PA_MEASURE')][0]
    ok &= check("and Stage 3 sets its own temperature rather than assuming",
                "TEMP=" in measure, measure)
    order = [s.split()[0] for s in g.scripts]
    ok &= check("the heater goes off only after Stage 3 has finished",
                order.index('M104') > order.index('QIDI_PA_MEASURE'),
                str(order))
    ok &= check("and the air filter is switched off at the very end",
                any(s.startswith('M106 P3 S0') for s in g.scripts)
                and order.index('M106') > order.index('QIDI_PA_MEASURE'),
                str(order))

    print("\n== a stage abort stops the chain ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, fail_on='QIDI_PA_MEASURE')
    try:
        g.run('QIDI_AUTO_CALIBRATE')
        ok &= check("the abort propagates", False, "no error")
    except RuntimeError as e:
        ok &= check("the abort propagates", 'aborted' in str(e), str(e))
    ok &= check("Stage 4 never ran on a half-result",
                not any(s.startswith('QIDI_PA_TABLE') for s in g.scripts),
                str(g.scripts))
    ok &= check("but the machine was still cooled, wiped and the filter shut",
                any(s.startswith('M104 S0') for s in g.scripts)
                and g.scripts[-2] == 'QIDI_FLOW_WIPE'
                and g.scripts[-1].startswith('M106'), str(g.scripts[-3:]))

    print("\n== a flow search that finds nothing refuses to continue ==")
    tmp = tempfile.mkdtemp()
    g, mod = build(tmp, no_qmax=True)
    try:
        g.run('QIDI_AUTO_CALIBRATE')
        ok &= check("refuses without a working max", False, "no error")
    except RuntimeError as e:
        ok &= check("refuses without a working max",
                    'no working_max' in str(e), str(e))
    ok &= check("and did not attempt PA measurement",
                not any('QIDI_PA_MEASURE' in s for s in g.scripts),
                str(g.scripts))

    print("\n== SKIP_FLOW reuses the previous result ==")
    tmp = tempfile.mkdtemp()
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, 'flow_ramp.json'), 'w') as f:
        json.dump([{'payload': {'working_max': 17.0}}], f)
    g, mod = build(tmp)
    g.run('QIDI_AUTO_CALIBRATE', SKIP_FLOW=1)
    ok &= check("no flow search was run",
                not any('QIDI_FLOW_SEARCH' in s for s in g.scripts),
                str(g.scripts))
    ok &= check("and the stored max is used",
                any('QMAX=17.000' in s for s in g.scripts),
                str([s for s in g.scripts if 'ENVELOPE' in s]))

    print("\n== a stray exception must not shut the MCU down ==")
    g, mod = build(tempfile.mkdtemp())
    mod._run = lambda gc: (_ for _ in ()).throw(NameError("boom"))
    try:
        g.run('QIDI_AUTO_CALIBRATE')
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
