"""Off-printer tests for qidi_flow_ramp (Stage 1).

This is the first module that heats and extrudes, and on this machine our aborts
are the only protection that exists - no firmware extrusion guard, no ADC
ceiling, nothing mechanical. So most of these tests are about the abort and
retract paths rather than the arithmetic.
"""
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mock_klipper import MockReactor, MockPrinter, MockConfig
import qidi_flow_ramp as FR


class MockHeater:
    def __init__(self, temp=275.0):
        self.t = temp

    def get_temp(self, eventtime):
        return (self.t, self.t)


class MockExtruder:
    def __init__(self):
        self.heater = MockHeater()

    def get_heater(self):
        return self.heater


class MockHeaters:
    def __init__(self):
        self.calls = []

    def set_temperature(self, heater, target, wait=False):
        self.calls.append((target, wait))
        heater.t = target


class MockMCU:
    def estimated_print_time(self, eventtime):
        return eventtime


class MockToolhead:
    def __init__(self, homed='xyz'):
        self.mcu = MockMCU()
        self.waits = 0
        self._homed = homed

    def get_status(self, eventtime):
        return {'homed_axes': self._homed}

    def get_last_move_time(self):
        return 0.0          # always "empty" so chunks are issued freely

    def wait_moves(self):
        self.waits += 1


class ScriptGCode:
    """MockGCode plus run_script_from_command, recording every script."""

    def __init__(self):
        self.commands = {}
        self.output = []
        self.scripts = []
        self.raw = []

    def register_command(self, name, func, desc=None):
        self.commands[name] = func

    def run_script_from_command(self, script):
        self.scripts.append(script)

    def respond_raw(self, msg):
        # Klipper's own respond_raw. The wizard writes Fluidd's
        # "// action:prompt_*" lines through it, so they are recorded
        # separately from respond_info output.
        self.raw.append(msg)
        self.output.append(msg)

    def run(self, name, **params):
        self.output = []
        self.commands[name](Cmd(self, params))
        return self.output


class Cmd:
    def __init__(self, g, p):
        self._g, self._p = g, p

    def respond_info(self, m):
        self._g.output.append(m)

    def get(self, k, default='__REQ__'):
        # Klipper returns the default object unchanged when the parameter is
        # absent - it does NOT stringify it. Stringifying turned a None default
        # into the string 'None', which reads as a supplied value.
        v = self._p.get(k, default)
        return None if v is None else str(v)

    def get_int(self, k, default=None, minval=None, maxval=None):
        v = self._p.get(k, default)
        if v is None:
            return None
        v = int(v)
        # Klipper enforces these; the mock did not, so a module could cap a
        # parameter and the tests would never notice the cap was wrong.
        if minval is not None and v < minval:
            raise RuntimeError("%s must have minimum of %s" % (k, minval))
        if maxval is not None and v > maxval:
            raise RuntimeError("%s must have maximum of %s" % (k, maxval))
        return v

    def get_float(self, k, default=None, minval=None, maxval=None, above=None,
                  below=None):
        # Klipper returns the default unchanged when it is None.
        v = self._p.get(k, default)
        return None if v is None else float(v)

    def error(self, m):
        return RuntimeError(m)


class FakeSensor:
    """Force follows a power law in the commanded flow, plus noise-free offset.
    `force_override` lets a test drive the reading directly."""

    def __init__(self, tare=-380000.0):
        self.tare = tare
        self.gf = 0.0
        self.force_override = None
        self.raise_after = None
        self.reads = 0

    def read_origin_data(self):
        self.reads += 1
        if self.raise_after is not None and self.reads > self.raise_after:
            raise RuntimeError("simulated sensor failure")
        gf = self.force_override if self.force_override is not None else self.gf
        return int(self.tare + gf * FR.COUNTS_PER_GF)


def build(tmpdir, sensor=None, homed='xyz'):
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    g = ScriptGCode()
    printer._objects['gcode'] = g
    s = sensor or FakeSensor()

    class Root:
        pass
    r = Root(); r.sensor_helper = s
    printer.add('probe_air', r)
    th = MockToolhead(homed)
    printer.add('toolhead', th)
    printer.add('extruder', MockExtruder())
    printer.add('heaters', MockHeaters())
    mod = FR.load_config(MockConfig(printer, {'out_dir': tmpdir}))
    return g, mod, s, th, printer


def check(label, cond, detail=''):
    print(("  PASS  " if cond else "  FAIL  ") + label + (
        ('  <- ' + detail) if detail and not cond else ''))
    return cond


def main():
    ok = True
    tmp = tempfile.mkdtemp()

    print("\n== log spacing and material budget ==")
    g, mod, s, th, p = build(tmp)
    out = g.run('QIDI_FLOW_RAMP', STEPS=8, QMIN=2.0, QMAX=9.98, DRY=1)
    text = "\n".join(out)
    ok &= check("DRY run executes nothing", g.scripts == [], str(g.scripts))
    ok &= check("first step is QMIN", 'Q=  2.00' in text, text)
    ok &= check("last step is QMAX", 'Q=  9.98' in text, text)
    ratio = (9.98 / 2.0) ** (1.0 / 7)
    mid = 2.0 * ratio ** 4
    ok &= check("spacing is geometric, not linear",
                ("Q=%6.2f" % mid) in text, "expected mid step %.2f" % mid)
    ok &= check("reports both likely and worst-case filament use",
                'likely ~' in text and 'worst case' in text
                and ' g)' in text, text[:400])

    print("\n== refuses to exceed the extrusion cap ==")
    g, mod, s, th, p = build(tmp)
    try:
        g.run('QIDI_FLOW_RAMP', STEPS=12, QMIN=2.0, QMAX=25.0, MAX_E=50.0, DRY=1)
        ok &= check("rejects a ramp over MAX_E", False, "no error raised")
    except RuntimeError as e:
        ok &= check("rejects a ramp over MAX_E", 'over the' in str(e), str(e))

    print("\n== refuses to run unhomed ==")
    g, mod, s, th, p = build(tmp, homed='')
    try:
        g.run('QIDI_FLOW_RAMP', STEPS=4, QMIN=2.0, QMAX=6.0)
        ok &= check("requires homing", False, "no error raised")
    except RuntimeError as e:
        ok &= check("requires homing", 'home' in str(e).lower(), str(e))

    print("\n== a clean ramp: power law recovered, retract issued ==")
    A, N = 30.0, 0.75

    class PowerSensor(FakeSensor):
        def read_origin_data(self):
            self.reads += 1
            return int(self.tare + self.gf * FR.COUNTS_PER_GF)
    s = PowerSensor()
    g, mod, s, th, p = build(tmp, sensor=s)
    orig = FR.QidiFlowRamp._run_step

    def patched(self, gcmd, read, toolhead, idx, q, hz, tare, abort_counts,
                var_abort, **kw):
        s.gf = A * q ** N          # steady force for this flow
        r = orig(self, gcmd, read, toolhead, idx, q, hz, tare, abort_counts,
                 var_abort, **kw)
        # Melt pressure decays once extrusion stops. Without this the closing
        # tare would read the last step's force and the drift correction would
        # subtract a baseline shift that never happened.
        s.gf = 0.0
        return r
    FR.QidiFlowRamp._run_step = patched
    out = g.run('QIDI_FLOW_RAMP', STEPS=8, QMIN=2.0, QMAX=9.98, HZ=30)
    text = "\n".join(out)
    ok &= check("moved to the purge chute first",
                any('OPTIMIZED_MOVE_TO_TRASH' in x for x in g.scripts),
                str(g.scripts[:3]))
    ok &= check("heated before taring",
                p.lookup_object('heaters').calls[0][0] == 275.0,
                str(p.lookup_object('heaters').calls))
    ok &= check("recovered the exponent",
                mod and abs(_fit_n(out) - N) < 0.02,
                "got %.3f want %.3f" % (_fit_n(out), N))
    ok &= check("extrapolated to 20 and 25 mm3/s",
                '20 mm3/s' in text.replace('  ', ' ') or
                'extrapolated' in text, text[-400:])
    ok &= check("retracted at the end",
                any('E-2.000' in x for x in g.scripts), str(g.scripts[-2:]))
    FR.QidiFlowRamp._run_step = orig

    print("\n== force abort stops the ramp and still retracts ==")

    class RisingSensor(FakeSensor):
        """Quiet through the tare, then jumps past the limit once the ramp
        starts. Setting the force before the tare would be absorbed by it -
        the module measures differentially, which is the point."""
        jump_after = 95          # 60 tare + 30 drift-check samples

        def read_origin_data(self):
            self.reads += 1
            gf = (FR.ABORT_GF + 100.0) if self.reads > self.jump_after else 0.0
            return int(self.tare + gf * FR.COUNTS_PER_GF)
    g, mod, s, th, p = build(tmp, sensor=RisingSensor())
    out = g.run('QIDI_FLOW_RAMP', STEPS=8, QMIN=2.0, QMAX=9.98, HZ=30, PRIME=0)
    text = "\n".join(out)
    ok &= check("aborted on force",
                'did not equilibrate' in text
                and ('over the %.0f gf' % FR.ABORT_GF) in text,
                text[-600:])
    ok &= check("skipped the flow and kept going",
                'skipping this flow and continuing' in text, text[-500:])
    ok &= check("retracted after abort",
                any('E-2.000' in x for x in g.scripts), str(g.scripts[-2:]))

    print("\n== the prime is abort-protected too ==")
    g, mod, s, th, p = build(tmp, sensor=RisingSensor())
    raised = None
    try:
        g.run('QIDI_FLOW_RAMP', STEPS=8, QMIN=2.0, QMAX=9.98, HZ=30, PRIME=1)
    except RuntimeError as e:
        raised = str(e)
    ok &= check("aborts during prime", bool(raised)
                and 'priming' in raised, str(raised))
    ok &= check("never reached step 1",
                not any('flow_ramp: step 1 ' in x for x in g.output),
                "\n".join(g.output)[-200:])
    ok &= check("still retracted, wiped and cooled after a prime abort",
                any('E-2.000' in x for x in g.scripts)
                and any('X180.00' in x for x in g.scripts)
                and p.lookup_object('heaters').calls[-1][0] == 0.,
                str(p.lookup_object('heaters').calls))

    print("\n== finish: wipe on the silicone wiper, hotend off ==")
    g, mod, s, th, p = build(tmp)
    g.run('QIDI_FLOW_RAMP', STEPS=3, QMIN=2.0, QMAX=4.0, HZ=30, PRIME=0)
    wipes = [x for x in g.scripts if 'X180.00' in x]
    ok &= check("sweeps the full wiper travel and returns to park_x",
                bool(wipes) and wipes[0].count('X180.00') == FR.WIPE_PASSES
                and wipes[0].rstrip().endswith('X135.00 F6000'),
                (wipes[0] if wipes else 'no wipe issued'))
    ok &= check("default travel is wider than QIDI's +8..+27",
                FR.WIPE_HI_OFFSET - FR.WIPE_LO_OFFSET > 19.0,
                "%.1f mm" % (FR.WIPE_HI_OFFSET - FR.WIPE_LO_OFFSET,))
    ok &= check("hotend set to 0 at the end",
                p.lookup_object('heaters').calls[-1][0] == 0.,
                str(p.lookup_object('heaters').calls))
    g, mod, s, th, p = build(tmp)
    g.run('QIDI_FLOW_RAMP', STEPS=3, QMIN=2.0, QMAX=4.0, HZ=30, PRIME=0,
          WIPE=0, COOLDOWN=0)
    ok &= check("WIPE=0 COOLDOWN=0 suppress both",
                not any('X180.00' in x for x in g.scripts)
                and p.lookup_object('heaters').calls[-1][0] != 0.,
                str(p.lookup_object('heaters').calls))

    print("\n== wipe travel is tunable, and QIDI_FLOW_WIPE runs it alone ==")
    g, mod, s, th, p = build(tmp)
    g.run('QIDI_FLOW_RAMP', STEPS=3, QMIN=2.0, QMAX=4.0, HZ=30, PRIME=0,
          WIPE_LO=120, WIPE_HI=200, WIPE_PASSES=2)
    w = [x for x in g.scripts if 'X200.00' in x]
    # X200 is the far end, reached only by the LONG strokes. X120 is the near
    # end, returned to by long AND short strokes, so it counts passes + short.
    ok &= check("LO/HI/PASSES override the defaults",
                bool(w) and w[0].count('X200.00') == 2
                and w[0].count('X120.00') == 2 + FR.WIPE_SHORT_PASSES,
                (w[0] if w else 'none'))
    ok &= check("and the short strokes land inside the overridden travel",
                bool(w) and ('X%.2f' % (120 + FR.WIPE_SHORT_FRAC * 80)) in w[0],
                (w[0] if w else 'none'))
    g, mod, s, th, p = build(tmp)
    g.run('QIDI_FLOW_WIPE', LO=140, HI=190, PASSES=3)
    w = [x for x in g.scripts if 'X190.00' in x]
    ok &= check("QIDI_FLOW_WIPE wipes without running a ramp",
                bool(w) and w[0].count('X190.00') == 3
                and not any('E-' in x for x in g.scripts),
                str(g.scripts))
    ok &= check("and moves to the chute first",
                any('OPTIMIZED_MOVE_TO_TRASH' in x for x in g.scripts),
                str(g.scripts[:2]))

    print("\n== sweep plan: up then down, with repeats ==")
    g, mod, s, th, p = build(tmp)
    out = g.run('QIDI_FLOW_RAMP', STEPS=8, QMIN=2.0, QMAX=9.98, DRY=1)
    text = "\n".join(out)
    ok &= check("up-only by default: 8 measurements for 8 flows",
                '8 measurements' in text and 'up x1' in text, text[:300])
    t_ud = "\n".join(g.run('QIDI_FLOW_RAMP', STEPS=8, QMIN=2.0, QMAX=9.98,
                           DRY=1, UPDOWN=1))
    ok &= check("UPDOWN=1 restores the descending leg",
                'down1' in t_ud and '16 measurements' in t_ud, t_ud[:300])
    ok &= check("a prime step is planned", 'prime' in text.lower(), text[:300])
    g, mod, s, th, p = build(tmp)
    t2 = "\n".join(g.run('QIDI_FLOW_RAMP', STEPS=8, QMIN=2.0, QMAX=9.98,
                          DRY=1, UPDOWN=0, REPEATS=2))
    ok &= check("UPDOWN=0 REPEATS=2 gives 16 up-only",
                '16 measurements' in t2 and 'down' not in t2, t2[:300])

    print("\n== variance abort only fires when armed ==")
    for arm, expect in ((0.0, False), (0.5, True)):
        class NoisySensor(FakeSensor):
            """Sawtooth well above the armed threshold - 400 counts is 2.2 gf,
            the signature a slipping drive gear would produce."""
            def read_origin_data(self):
                self.reads += 1
                if self.reads <= 95:        # quiet through tare + drift check
                    return int(self.tare)
                return int(self.tare + 400.0 * (1 if self.reads % 2 else -1))
        g, mod, s2, th, p = build(tmp, sensor=NoisySensor())
        out = g.run('QIDI_FLOW_RAMP', STEPS=3, QMIN=2.0, QMAX=4.0, HZ=30,
                    VARIANCE_ABORT=arm)
        fired = 'variance' in "\n".join(out)
        ok &= check("VARIANCE_ABORT=%.1f -> abort %s" % (arm, expect),
                    fired == expect, "\n".join(out)[-300:])

    print("\n== a sensor failure mid-ramp still cleans up ==")
    s = FakeSensor(); s.raise_after = 120
    g, mod, s, th, p = build(tmp, sensor=s)
    try:
        g.run('QIDI_FLOW_RAMP', STEPS=8, QMIN=2.0, QMAX=9.98, HZ=30)
    except RuntimeError:
        pass                     # a read failure is a legitimate abort
    ok &= check("retract issued despite the failure",
                any('E-2.000' in x for x in g.scripts), str(g.scripts[-2:]))
    ok &= check("wiped despite the failure",
                any('X180.00' in x for x in g.scripts), str(g.scripts[-2:]))
    ok &= check("hotend still turned off despite the failure",
                p.lookup_object('heaters').calls[-1][0] == 0.,
                str(p.lookup_object('heaters').calls))

    print("\n== phase 2: refines the bracket and reports a working max ==")
    # A melt that equilibrates below Q_LIMIT and climbs without bound above it -
    # the physical picture the equilibrium criterion is built on.
    Q_LIMIT = 15.0

    class LimitSensor(FakeSensor):
        def __init__(self):
            FakeSensor.__init__(self)
            self.q = 0.0
            self.t = 0

        def read_origin_data(self):
            self.reads += 1
            if self.q > Q_LIMIT:
                self.t += 1
                gf = 300.0 + self.t * 8.0      # never settles
            else:
                gf = 30.0 * self.q ** 0.5      # steady
            return int(self.tare + gf * FR.COUNTS_PER_GF)

    ls = LimitSensor()
    g, mod, s2, th, p = build(tmp, sensor=ls)
    orig2 = FR.QidiFlowRamp._run_step

    def patched2(self, gcmd, read, toolhead, idx, q, hz, tare, abort_counts,
                 var_abort, **kw):
        ls.q = q; ls.t = 0
        r = orig2(self, gcmd, read, toolhead, idx, q, hz, tare, abort_counts,
                  var_abort, **kw)
        ls.q = 0.0; ls.t = 0
        return r
    FR.QidiFlowRamp._run_step = patched2
    out = g.run('QIDI_FLOW_RAMP', STEPS=6, QMIN=2.0, QMAX=25.0, HZ=30,
                UPDOWN=0, MAX_ABORTS=5, REFINE=4, REFINE_REPEATS=1,
                MAX_E=5000, VARIANCE_ABORT=0)
    text = "\n".join(out)
    FR.QidiFlowRamp._run_step = orig2
    ok &= check("did not stop at the first unreachable flow",
                text.count('did not equilibrate') >= 2, text[-800:])
    ok &= check("refined between the bracket",
                'refining between' in text and 'refup1' in text, text[-900:])
    ok &= check("reported a working max", 'WORKING MAX' in text, text[-500:])
    wm = None
    for line in out:
        if 'WORKING MAX' in line:
            wm = float(line.split('=')[1].split('mm3')[0])
    ok &= check("working max is below the true limit",
                wm is not None and 0.5 * Q_LIMIT < wm < Q_LIMIT,
                "got %s, true limit %.1f" % (wm, Q_LIMIT))

    print("\n== MAX_ABORTS stops a run that keeps failing ==")
    ls2 = LimitSensor()
    g, mod, s3, th, p = build(tmp, sensor=ls2)

    def patched3(self, gcmd, read, toolhead, idx, q, hz, tare, abort_counts,
                 var_abort, **kw):
        ls2.q = 99.0 if idx > 1 else q      # everything past step 1 fails
        ls2.t = 0
        r = orig2(self, gcmd, read, toolhead, idx, q, hz, tare, abort_counts,
                  var_abort, **kw)
        ls2.q = 0.0; ls2.t = 0
        return r
    FR.QidiFlowRamp._run_step = patched3
    out = g.run('QIDI_FLOW_RAMP', STEPS=6, QMIN=2.0, QMAX=25.0, HZ=30,
                UPDOWN=0, MAX_ABORTS=2, REFINE=0, MAX_E=5000,
                VARIANCE_ABORT=0)
    FR.QidiFlowRamp._run_step = orig2
    t3 = "\n".join(out)
    ok &= check("stops once MAX_ABORTS distinct flows have failed",
                'distinct flows failed to equilibrate' in t3, t3[-500:])
    ok &= check("and names which flows they were",
                '3 distinct flows' in t3, t3[-500:])
    ok &= check("still wiped and cooled after giving up",
                any('X180.00' in x for x in g.scripts)
                and p.lookup_object('heaters').calls[-1][0] == 0.,
                str(p.lookup_object('heaters').calls))

    print("\n== unit: a flow that fails on ANY leg is not equilibrated ==")
    # The bug that produced a working max of 22.5 from the 48-point sweep:
    # 25 mm3/s aborted on both up-legs but looked settled on both down-legs,
    # and "any measurement settled" put it in the good set.
    res = [
        {'flow': 19.87, 'settled': True, 'abort': None, 'leg': 'up1'},
        {'flow': 19.87, 'settled': True, 'abort': None, 'leg': 'down1'},
        {'flow': 25.00, 'settled': False, 'abort': 'force 1879 gf', 'leg': 'up1'},
        {'flow': 25.00, 'settled': True, 'abort': None, 'leg': 'down1'},
    ]
    eq, bad = FR._classify_flows(res)
    ok &= check("25.00 is excluded despite settling on the down-leg",
                eq == [19.87] and bad == [25.0], "eq=%s bad=%s" % (eq, bad))
    ok &= check("so the bracket is 19.87..25.00, not 25.00 and nothing above",
                max(eq) == 19.87 and min(bad) == 25.0)
    ok &= check("prime measurements are ignored",
                FR._classify_flows(res + [{'flow': 10.0, 'settled': False,
                                           'abort': None, 'leg': 'prime'}])[1]
                == [25.0])

    print("\n== every leg is wiped and primed ==")
    g, mod, s, th, p = build(tmp)
    FR.QidiFlowRamp._run_step = patched
    out = g.run('QIDI_FLOW_RAMP', STEPS=4, QMIN=2.0, QMAX=6.0, HZ=30,
                REPEATS=1, UPDOWN=1, REFINE=0)
    FR.QidiFlowRamp._run_step = orig
    text = "\n".join(out)
    ok &= check("both legs announce a wipe and prime",
                text.count('wipe and prime') == 2, str(text.count('wipe and prime')))
    ok &= check("a wipe is issued per leg, not just at the end",
                len([x for x in g.scripts if 'X180.00' in x]) >= 3,
                str(len([x for x in g.scripts if 'X180.00' in x])))
    ok &= check("each leg primes before measuring",
                text.count('prime Q=') >= 2 or text.count('prime') >= 2, text[:400])

    print("\n== report file ==")
    path = os.path.join(tmp, 'flow_ramp.json')
    ok &= check("flow_ramp.json written", os.path.exists(path))
    if os.path.exists(path):
        import json
        with open(path) as f:
            data = json.load(f)
        pl = data[0]['payload']
        ok &= check("records tare, steps and traces",
                    all(k in pl for k in ('tare', 'steps', 'aborted', 'fit')),
                    str(list(pl)))
        ok &= check("each step carries a raw trace",
                    bool(pl['steps']) and 'trace' in pl['steps'][0])

    print("\n== unit: log-log fit ==")
    qs = [2.0, 4.0, 8.0, 16.0]
    fs = [5.0 * q ** 0.6 for q in qs]
    f = FR._loglog_fit(qs, fs)
    ok &= check("recovers a and n exactly",
                abs(f['n'] - 0.6) < 1e-9 and abs(f['a'] - 5.0) < 1e-9,
                str(f))

    print("\n== an internal error must NOT be allowed to shut the MCU down ==")
    # Klipper turns an unhandled exception in a gcode command into an INTERNAL
    # ERROR and latches every MCU into shutdown - that happened for real on
    # 2026-09-15 and took a FIRMWARE_RESTART to clear. The guard has to turn
    # anything unexpected into a clean command failure instead.
    # This module heats and extrudes, so the guard must ALSO leave the
    # retract/wipe/cooldown in cmd_RAMP's finally intact. The failure is
    # injected inside the ramp so that finally has to unwind through it.

    def boom(*a, **k):
        raise NameError("corr_ms")

    g, mod, s, th, p = build(tempfile.mkdtemp())
    mod._run_step = boom
    try:
        g.run('QIDI_FLOW_RAMP', STEPS=3, QMIN=2.0, QMAX=6.0, HZ=30, PRIME=0)
        ok &= check("QIDI_FLOW_RAMP converts a stray exception", False,
                    "nothing raised")
    except RuntimeError as e:
        ok &= check("QIDI_FLOW_RAMP converts a stray exception",
                    'internal error' in str(e) and 'NameError' in str(e),
                    str(e))
    except NameError:
        ok &= check("QIDI_FLOW_RAMP converts a stray exception", False,
                    "NameError escaped - this shuts both MCUs down")
    # ...and the cleanup in the finally must still have run. Only _finish
    # retracts 2.000 mm (the inter-leg wipe retracts 1.000), so this is
    # specifically evidence that the finally unwound.
    ok &= check("retract in the finally still ran",
                any('E-2.000' in x for x in g.scripts), str(g.scripts[-4:]))
    ok &= check("wipe in the finally still ran",
                any('X180.00' in x for x in g.scripts), str(g.scripts[-4:]))
    ok &= check("hotend was still turned off",
                p.lookup_object('heaters').calls[-1][0] == 0.,
                str(p.lookup_object('heaters').calls))
    ok &= check("and the user was told cleanup happened",
                any('retracted 2.00 mm' in x for x in g.output)
                and any('hotend off' in x for x in g.output),
                "\n".join(g.output)[-300:])

    g, mod, s, th, p = build(tempfile.mkdtemp())
    mod._run_WIPE = boom
    try:
        g.run('QIDI_FLOW_WIPE')
        ok &= check("QIDI_FLOW_WIPE converts a stray exception", False,
                    "nothing raised")
    except RuntimeError as e:
        ok &= check("QIDI_FLOW_WIPE converts a stray exception",
                    'internal error' in str(e) and 'NameError' in str(e),
                    str(e))
    except NameError:
        ok &= check("QIDI_FLOW_WIPE converts a stray exception", False,
                    "NameError escaped - this shuts both MCUs down")

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


def _fit_n(out):
    for line in out:
        if 'power law' in line:
            try:
                return float(line.split('Q^')[1].split()[0])
            except Exception:
                return -99.
    return -99.


if __name__ == '__main__':
    sys.exit(main())
