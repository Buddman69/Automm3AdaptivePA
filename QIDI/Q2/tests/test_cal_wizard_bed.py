"""Off-printer tests for qidi_cal_wizard_bed.

Mirrors test_cal_wizard.py's coverage (the dialog mechanics are a byte-
identical copy), plus one check that is new here: that this wizard and the
chute wizard can be loaded into the same gcode dispatcher without their
command names colliding, since both are meant to be installed at once.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mock_klipper import MockReactor, MockPrinter, MockConfig
from test_flow_ramp import ScriptGCode, check
import qidi_cal_wizard as W_CHUTE
import qidi_cal_wizard_bed as W

ACTION = re.compile(r'^// action:prompt_([^ ]+)(?: (.+))?')


class Extruder:
    def __init__(self, target=275.0):
        self.target = target

    def get_status(self, e):
        return {'temperature': self.target, 'target': self.target}


def build(target=275.0, nozzle=0.6):
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    g = ScriptGCode()
    printer._objects['gcode'] = g
    printer.add('extruder', Extruder(target))
    mod = W.load_config(MockConfig(printer, {'nozzle_diameter': nozzle}))
    return g, mod


def parse(lines):
    d = {'title': None, 'text': [], 'buttons': [], 'footer': [], 'open': False}
    for line in lines:
        m = ACTION.match(line)
        if not m:
            continue
        verb, payload = m.group(1), m.group(2)
        if verb == 'begin':
            d = {'title': payload, 'text': [], 'buttons': [], 'footer': [],
                 'open': False}
        elif verb == 'text':
            d['text'].append(payload if payload is not None else None)
        elif verb in ('button', 'footer_button'):
            key = 'buttons' if verb == 'button' else 'footer'
            d[key].append(tuple((payload or '').split('|')))
        elif verb == 'show':
            d['open'] = True
        elif verb == 'end':
            d['open'] = False
    return d


def click(g, command):
    bits = command.split()
    params = dict(p.split('=', 1) for p in bits[1:])
    return g.run(bits[0], **params)


def find(dlg, needle):
    for b in dlg['buttons'] + dlg['footer']:
        if needle.lower() in b[0].lower():
            return b[1]
    raise AssertionError("no button matching %r in %s"
                         % (needle, [b[0] for b in dlg['buttons']
                                     + dlg['footer']]))


def main():
    ok = True

    print("\n== registers its own command names, no collision with the "
          "chute wizard ==")
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    g = ScriptGCode()
    printer._objects['gcode'] = g
    printer.add('extruder', Extruder())
    W_CHUTE.load_config(MockConfig(printer, {'nozzle_diameter': 0.6}))
    before = dict(g.commands)
    W.load_config(MockConfig(printer, {'nozzle_diameter': 0.6}))
    ok &= check("QIDI_CALIBRATE (chute) still points at the chute handler",
                g.commands['QIDI_CALIBRATE'] is before['QIDI_CALIBRATE'],
                "overwritten")
    ok &= check("QIDI_CALIBRATE_BED is a distinct command",
                'QIDI_CALIBRATE_BED' in g.commands
                and g.commands['QIDI_CALIBRATE_BED']
                is not g.commands['QIDI_CALIBRATE'],
                str(list(g.commands)))
    ok &= check("_QIDI_CAL_BED_STEP is distinct from _QIDI_CAL_STEP",
                g.commands['_QIDI_CAL_BED_STEP']
                is not g.commands['_QIDI_CAL_STEP'],
                "collision")

    print("\n== the dialog is well-formed for Fluidd's parser ==")
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE_BED'))
    ok &= check("it has a title", bool(dlg['title']), str(dlg['title']))
    ok &= check("it is explicitly opened", dlg['open'], str(dlg))
    ok &= check("every button is label|command|colour",
                all(len(b) == 3 for b in dlg['buttons'] + dlg['footer']),
                str(dlg['buttons']))
    ok &= check("only verbs Fluidd implements are used",
                all(ACTION.match(l).group(1)
                    in ('begin', 'text', 'button', 'footer_button', 'show',
                        'end')
                    for l in g.output if ACTION.match(l)),
                str([l for l in g.output if ACTION.match(l)][:4]))

    print("\n== every button calls a command that exists ==")
    seen = set()
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE_BED'))
    frontier = [b[1] for b in dlg['buttons'] + dlg['footer']]
    dead = []
    while frontier:
        cmd = frontier.pop()
        if cmd in seen:
            continue
        seen.add(cmd)
        name = cmd.split()[0]
        if name not in g.commands:
            dead.append(cmd)
            continue
        if 'GO=1' in cmd or 'CANCEL=1' in cmd:
            continue
        sub = parse(click(g, cmd))
        frontier += [b[1] for b in sub['buttons'] + sub['footer']]
    ok &= check("no dead buttons anywhere in the wizard", not dead, str(dead))
    ok &= check("and the wizard was actually walked", len(seen) > 15,
                "%d commands reached" % len(seen))

    print("\n== nothing runs until START ==")
    g, mod = build()
    g.run('QIDI_CALIBRATE_BED')
    dlg = parse(g.output)
    for b in dlg['buttons']:
        if 'GO=1' not in b[1]:
            click(g, b[1])
    ok &= check("no gcode was executed while answering questions",
                g.scripts == [], str(g.scripts))

    print("\n== a full click-through runs QIDI_AUTO_CALIBRATE_BED, not the "
          "chute command ==")
    g, mod = build(target=265.0)
    dlg = parse(g.run('QIDI_CALIBRATE_BED'))
    dlg = parse(click(g, find(dlg, 'Use this')))
    dlg = parse(click(g, find(dlg, '0.6 mm')))
    dlg = parse(click(g, find(dlg, 'Use these')))
    dlg = parse(click(g, find(dlg, '12 blocks')))
    dlg = parse(click(g, find(dlg, '5 points')))
    ok &= check("the review screen warns about the BED, not the chute",
                any('BED CENTRE' in t for t in dlg['text'])
                and not any('purge chute' in t and 'HEATS' not in t
                            for t in dlg['text']),
                str(dlg['text']))
    click(g, find(dlg, 'START'))
    ok &= check("exactly one command was run", len(g.scripts) == 1,
                str(g.scripts))
    ok &= check("and it is QIDI_AUTO_CALIBRATE_BED with every answer",
                g.scripts[0] == 'QIDI_AUTO_CALIBRATE_BED TEMP=265 BLOCKS=12 '
                                'POINTS=5 LAYER=0.240 WIDTH=0.620',
                str(g.scripts))

    print("\n== the dry run goes through the same path ==")
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE_BED', TEMP=275, NOZZLE=0.6, BLOCKS=12,
                      POINTS=5))
    dlg = parse(click(g, find(dlg, 'Use these')))
    click(g, find(dlg, 'Dry run'))
    ok &= check("DRY=1 is appended", g.scripts[0].endswith(' DRY=1'),
                str(g.scripts))
    ok &= check("to QIDI_AUTO_CALIBRATE_BED specifically",
                g.scripts[0].startswith('QIDI_AUTO_CALIBRATE_BED'),
                str(g.scripts))

    print("\n== pre-seeding skips the question it answers ==")
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE_BED', TEMP=250))
    ok &= check("giving TEMP skips straight to the nozzle",
                'Nozzle' in dlg['title'], str(dlg['title']))
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE_BED', TEMP=250, NOZZLE=0.4, LAYER=0.18,
                      WIDTH=0.65, BLOCKS=20, POINTS=7))
    ok &= check("seeding everything goes straight to the review",
                'Ready' in dlg['title'], str(dlg['title']))
    click(g, find(dlg, 'START'))
    ok &= check("and a typed geometry survives verbatim",
                'LAYER=0.180 WIDTH=0.650' in g.scripts[0], str(g.scripts))

    print("\n== temperature is clamped to what the bed chain will accept ==")
    g, mod = build(target=265.0)
    dlg = parse(g.run('QIDI_CALIBRATE_BED'))
    for _ in range(20):
        dlg = parse(click(g, find(dlg, 'temp   +20')))
    ok &= check("cannot be pushed to or past 350",
                mod.state['temp'] <= 345.0, str(mod.state['temp']))

    print("\n== Cancel runs nothing ==")
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE_BED'))
    out = click(g, find(dlg, 'Cancel'))
    ok &= check("no gcode executed", g.scripts == [], str(g.scripts))
    ok &= check("the dialog is closed",
                '// action:prompt_end' in out and not parse(out)['open'],
                str(out))

    print("\n== a half-answered START cannot launch ==")
    g, mod = build()
    g.run('QIDI_CALIBRATE_BED', TEMP=275)
    dlg = parse(g.run('_QIDI_CAL_BED_STEP', GO=1))
    ok &= check("it repaints a question instead of launching",
                'Ready' not in dlg['title'], str(dlg['title']))
    ok &= check("and runs nothing", g.scripts == [], str(g.scripts))

    print("\n== bad input is refused, not guessed at ==")
    g, mod = build()
    g.run('QIDI_CALIBRATE_BED')
    for params, why in (({'SET': 'nozzle'}, 'SET without VAL'),
                        ({'SET': 'wibble', 'VAL': 1}, 'an unknown key'),
                        ({'ADJ': 'wibble', 'BY': 0.01}, 'an unknown adjust')):
        try:
            g.run('_QIDI_CAL_BED_STEP', **params)
            ok &= check("refuses %s" % why, False, "no error")
        except RuntimeError as e:
            ok &= check("refuses %s" % why, 'wizard:' in str(e), str(e))

    print("\n== a stray exception must not shut the MCU down ==")
    g, mod = build()
    mod._show = lambda name: (_ for _ in ()).throw(NameError("boom"))
    try:
        g.run('QIDI_CALIBRATE_BED')
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
