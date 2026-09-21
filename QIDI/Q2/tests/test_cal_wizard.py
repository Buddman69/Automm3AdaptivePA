"""Off-printer tests for qidi_cal_wizard.

The wizard's whole job is to be clickable in a browser, and the failure modes
are invisible from the printer side: a malformed action line renders nothing, a
button whose command has a typo is simply dead, and a dialog that launches on a
half-answered state runs a calibration with defaults the user never saw.

So these tests parse the emitted "// action:prompt_*" lines the way Fluidd's
own parser does, and drive the wizard by executing the commands its buttons
carry - a real click-through, not a call into internals.
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mock_klipper import MockReactor, MockPrinter, MockConfig
from test_flow_ramp import ScriptGCode, check
import qidi_cal_wizard as W

# Fluidd's own regex, copied from the compiled bundle.
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
    """Render the emitted lines the way Fluidd does: title, text items, body
    buttons, footer buttons, and whether the dialog was told to open."""
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
            # A verb with no payload arrives as None - Fluidd would render it
            # as an empty row, so the module must never emit one by accident.
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
    """Do what the browser does: send the button's gcode back."""
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

    print("\n== the dialog is well-formed for Fluidd's parser ==")
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE'))
    ok &= check("it has a title", bool(dlg['title']), str(dlg['title']))
    ok &= check("it is explicitly opened", dlg['open'], str(dlg))
    ok &= check("it has explanatory text", len(dlg['text']) >= 1,
                str(dlg['text']))
    ok &= check("no text line was emitted without a payload",
                all(t is not None for t in dlg['text']), str(dlg['text']))
    ok &= check("every button is label|command|colour",
                all(len(b) == 3 for b in dlg['buttons'] + dlg['footer']),
                str(dlg['buttons']))
    ok &= check("no label or command contains the | separator",
                all('|' not in part for b in dlg['buttons'] + dlg['footer']
                    for part in b), str(dlg['buttons']))
    ok &= check("only verbs Fluidd implements are used",
                all(ACTION.match(l).group(1)
                    in ('begin', 'text', 'button', 'footer_button', 'show',
                        'end')
                    for l in g.output if ACTION.match(l)),
                str([l for l in g.output if ACTION.match(l)][:4]))
    acts = [ACTION.match(l).group(1) for l in g.output if ACTION.match(l)]
    ok &= check("a stale dialog is cleared before the new one is built",
                acts[0] == 'end' and acts[1] == 'begin', str(acts[:3]))
    ok &= check("and it is shown last, once fully built",
                acts[-1] == 'show', str(acts[-3:]))

    print("\n== every button calls a command that exists ==")
    # A typo here is invisible from the printer: the button just does nothing.
    seen = set()
    todo = ['QIDI_CALIBRATE']
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE'))
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
    g.run('QIDI_CALIBRATE')
    dlg = parse(g.output)
    for b in dlg['buttons']:
        if 'GO=1' not in b[1]:
            click(g, b[1])
    ok &= check("no gcode was executed while answering questions",
                g.scripts == [], str(g.scripts))

    print("\n== a full click-through runs the right command ==")
    g, mod = build(target=265.0)
    dlg = parse(g.run('QIDI_CALIBRATE'))
    ok &= check("screen 1 starts from the temperature already set in the UI",
                any('265' in t for t in dlg['text']), str(dlg['text']))
    ok &= check("and offers coarse, medium and fine steps both ways",
                [b[0] for b in dlg['buttons']]
                == ['temp   -20 C', 'temp   -5 C', 'temp   -1 C',
                    'temp   +1 C', 'temp   +5 C', 'temp   +20 C'],
                str([b[0] for b in dlg['buttons']]))
    dlg = parse(click(g, find(dlg, 'Use this')))
    ok &= check("screen 2 is the nozzle", 'Nozzle' in dlg['title'],
                str(dlg['title']))
    dlg = parse(click(g, find(dlg, '0.6 mm')))
    ok &= check("screen 3 is the geometry", 'Geometry' in dlg['title'],
                str(dlg['title']))
    ok &= check("0.6 nozzle defaults to 0.24 layer / 0.62 width",
                any('0.24' in t and '0.62' in t for t in dlg['text']),
                str(dlg['text']))
    dlg = parse(click(g, find(dlg, 'Use these')))
    dlg = parse(click(g, find(dlg, '12 blocks')))
    dlg = parse(click(g, find(dlg, '5 points')))
    ok &= check("the last screen reviews every answer",
                any('265' in t for t in dlg['text'])
                and any('12 blocks' in t for t in dlg['text']),
                str(dlg['text']))
    ok &= check("and warns that it heats and extrudes",
                any('HEATS' in t for t in dlg['text']), str(dlg['text']))
    click(g, find(dlg, 'START'))
    ok &= check("exactly one command was run", len(g.scripts) == 1,
                str(g.scripts))
    ok &= check("with every answer passed through",
                g.scripts[0] == 'QIDI_AUTO_CALIBRATE TEMP=265 BLOCKS=12 '
                                'POINTS=5 LAYER=0.240 WIDTH=0.620',
                str(g.scripts))
    ok &= check("and the dialog was closed first",
                '// action:prompt_end' in g.output, str(g.output[:2]))

    print("\n== the dry run goes through the same path ==")
    g, mod = build()
    # NOZZLE seeds the geometry's starting values but not the answer, so that
    # screen is still shown - the user has to see what it will be measured at.
    dlg = parse(g.run('QIDI_CALIBRATE', TEMP=275, NOZZLE=0.6, BLOCKS=12,
                      POINTS=5))
    ok &= check("a seeded nozzle still shows the geometry screen",
                'Geometry' in dlg['title'], str(dlg['title']))
    dlg = parse(click(g, find(dlg, 'Use these')))
    click(g, find(dlg, 'Dry run'))
    ok &= check("DRY=1 is appended", g.scripts[0].endswith(' DRY=1'),
                str(g.scripts))

    print("\n== DRY=1 on the command line ==")
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE', DRY=1, TEMP=275, NOZZLE=0.6,
                      LAYER=0.24, WIDTH=0.62, BLOCKS=12, POINTS=5))
    ok &= check("the review screen says it is a dry run",
                'Dry run' in dlg['title'], str(dlg['title']))
    ok &= check("and does not threaten to heat anything",
                not any('HEATS' in t for t in dlg['text']), str(dlg['text']))
    ok &= check("the command shown includes DRY=1",
                any(t.endswith('DRY=1') for t in dlg['text']), str(dlg['text']))
    ok &= check("the primary button is the dry run",
                dlg['buttons'][0][1] == '_QIDI_CAL_STEP GO=1 DRY=1',
                str(dlg['buttons'][0]))
    # A preference, not a lock - the real run stays reachable and labelled.
    ok &= check("but the real run is still available, clearly marked",
                any(b[1] == '_QIDI_CAL_STEP GO=1'
                    and 'real' in b[0].lower() for b in dlg['buttons']),
                str([b[0] for b in dlg['buttons']]))
    click(g, dlg['buttons'][0][1])
    ok &= check("and pressing it appends DRY=1",
                len(g.scripts) == 1 and g.scripts[0].endswith(' DRY=1'),
                str(g.scripts))

    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE', TEMP=275, NOZZLE=0.6, LAYER=0.24,
                      WIDTH=0.62, BLOCKS=12, POINTS=5))
    ok &= check("without DRY the real run is primary",
                dlg['buttons'][0][1] == '_QIDI_CAL_STEP GO=1',
                str(dlg['buttons'][0]))
    ok &= check("and the dry run is still offered",
                any(b[1].endswith('DRY=1') for b in dlg['buttons']),
                str([b[0] for b in dlg['buttons']]))

    print("\n== pre-seeding skips the question it answers ==")
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE', TEMP=250))
    ok &= check("giving TEMP skips straight to the nozzle",
                'Nozzle' in dlg['title'], str(dlg['title']))
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE', LAYER=0.18, WIDTH=0.65))
    ok &= check("giving LAYER and WIDTH skips the geometry screen",
                'Geometry' not in dlg['title'], str(dlg['title']))
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE', TEMP=250, NOZZLE=0.4, LAYER=0.18,
                      WIDTH=0.65, BLOCKS=20, POINTS=7))
    ok &= check("seeding everything goes straight to the review",
                'Ready' in dlg['title'], str(dlg['title']))
    click(g, find(dlg, 'START'))
    ok &= check("and a typed geometry survives verbatim",
                'LAYER=0.180 WIDTH=0.650' in g.scripts[0], str(g.scripts))

    print("\n== the temperature steppers ==")
    g, mod = build(target=265.0)
    dlg = parse(g.run('QIDI_CALIBRATE'))
    dlg = parse(click(g, find(dlg, 'temp   +20')))
    ok &= check("coarse up: 265 -> 285", any('285 C' in t for t in dlg['text']),
                str(dlg['text']))
    dlg = parse(click(g, find(dlg, 'temp   -5')))
    dlg = parse(click(g, find(dlg, 'temp   -1')))
    ok &= check("then -5 and -1 land exactly on 279",
                any('279 C' in t for t in dlg['text']), str(dlg['text']))
    ok &= check("it repaints the TEMPERATURE screen, not the geometry one",
                'Temperature' in dlg['title'], str(dlg['title']))

    # A cold printer has no target to start from, which is exactly when a
    # calibration gets set up - so there must be a fallback.
    g, mod = build(target=0.0)
    dlg = parse(g.run('QIDI_CALIBRATE'))
    ok &= check("a cold printer falls back to the configured default",
                any('275 C' in t for t in dlg['text']), str(dlg['text']))
    ok &= check("and does not claim to have started from a target",
                not any('already set' in t for t in dlg['text']),
                str(dlg['text']))

    print("\n== temperature is clamped to what the chain will accept ==")
    # qidi_auto_cal takes TEMP above=150 below=350, both exclusive. The wizard
    # must never produce a value that command would reject.
    g, mod = build(target=265.0)
    dlg = parse(g.run('QIDI_CALIBRATE'))
    for _ in range(20):
        dlg = parse(click(g, find(dlg, 'temp   +20')))
    ok &= check("cannot be pushed to or past 350",
                mod.state['temp'] <= 345.0, str(mod.state['temp']))
    for _ in range(20):
        dlg = parse(click(g, find(dlg, 'temp   -20')))
    ok &= check("nor down to or past 150",
                mod.state['temp'] >= 155.0, str(mod.state['temp']))

    print("\n== the geometry steppers ==")
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE', TEMP=275, NOZZLE=0.6))
    ok &= check("coarse and fine, both directions, for both values",
                len(dlg['buttons']) == 8, str([b[0] for b in dlg['buttons']]))
    dlg = parse(click(g, find(dlg, 'layer   -0.04')))
    ok &= check("coarse down: 0.24 -> 0.20",
                any('0.20 mm layer' in t for t in dlg['text']),
                str(dlg['text']))
    dlg = parse(click(g, find(dlg, 'layer   -0.01')))
    dlg = parse(click(g, find(dlg, 'layer   -0.01')))
    ok &= check("fine steps land exactly, with no float drift",
                any('0.18 mm layer' in t for t in dlg['text']),
                str(dlg['text']))
    dlg = parse(click(g, find(dlg, 'width   +0.04')))
    ok &= check("width steps independently of layer",
                any('0.18 mm layer' in t and '0.66' in t for t in dlg['text']),
                str(dlg['text']))
    click(g, find(dlg, 'Use these'))

    print("\n== the steppers are clamped to something printable ==")
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE', TEMP=275, NOZZLE=0.6))
    for _ in range(40):
        dlg = parse(click(g, find(dlg, 'layer   -0.04')))
    ok &= check("layer cannot be driven to zero or negative",
                mod.state['layer'] >= 0.04, str(mod.state))
    for _ in range(40):
        dlg = parse(click(g, find(dlg, 'layer   +0.04')))
    ok &= check("nor above 75% of the nozzle",
                mod.state['layer'] <= 0.45 + 1e-9, str(mod.state))
    for _ in range(40):
        dlg = parse(click(g, find(dlg, 'width   -0.04')))
    ok &= check("width cannot fall below the nozzle bore",
                mod.state['width'] >= 0.48 - 1e-9, str(mod.state))

    print("\n== every nozzle has the agreed default geometry ==")
    for noz, layer, width in ((0.2, 0.10, 0.22), (0.4, 0.20, 0.42),
                              (0.6, 0.24, 0.62), (0.8, 0.40, 0.82)):
        g, mod = build()
        dlg = parse(g.run('QIDI_CALIBRATE', TEMP=275, NOZZLE=noz))
        ok &= check("%.1f -> %.2f layer / %.2f width" % (noz, layer, width),
                    abs(mod.state['layer'] - layer) < 1e-9
                    and abs(mod.state['width'] - width) < 1e-9,
                    str(mod.state))

    print("\n== Cancel runs nothing ==")
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE'))
    out = click(g, find(dlg, 'Cancel'))
    ok &= check("no gcode executed", g.scripts == [], str(g.scripts))
    ok &= check("the dialog is closed",
                '// action:prompt_end' in out and not parse(out)['open'],
                str(out))

    print("\n== a half-answered START cannot launch ==")
    # Nothing in the UI produces this, but a stale button in an open tab or a
    # hand-typed command could - and launching here would silently calibrate at
    # values the user never saw.
    g, mod = build()
    g.run('QIDI_CALIBRATE', TEMP=275)
    dlg = parse(g.run('_QIDI_CAL_STEP', GO=1))
    ok &= check("it repaints a question instead of launching",
                'Ready' not in dlg['title'], str(dlg['title']))
    ok &= check("and runs nothing", g.scripts == [], str(g.scripts))
    # Having a VALUE is not enough - the nozzle, layer and width always hold
    # one. Only being shown the screen, or answering on the command line,
    # satisfies it.
    ok &= check("even though every value already has a default",
                all(mod.state.get(k) is not None
                    for k in ('temp', 'nozzle', 'layer', 'width')),
                str(mod.state))
    # Unlike the geometry, blocks and points have no default anywhere - seeing
    # their screen is not enough, they have to be chosen.
    for screen in ('nozzle', 'geom', 'blocks', 'points'):
        g.run('_QIDI_CAL_STEP', GOTO=screen)
    g.run('_QIDI_CAL_STEP', GO=1)
    ok &= check("visiting the precision screens without choosing is not enough",
                g.scripts == [], str(g.scripts))
    g.run('_QIDI_CAL_STEP', SET='blocks', VAL=12)
    g.run('_QIDI_CAL_STEP', SET='points', VAL=5)
    g.run('_QIDI_CAL_STEP', GO=1)
    ok &= check("but once every question is answered, START works",
                len(g.scripts) == 1
                and g.scripts[0].startswith('QIDI_AUTO_CALIBRATE'),
                str(g.scripts))

    print("\n== Back goes back ==")
    g, mod = build()
    dlg = parse(g.run('QIDI_CALIBRATE', TEMP=275, NOZZLE=0.6, LAYER=0.24,
                      WIDTH=0.62, BLOCKS=12, POINTS=5))
    ok &= check("review has a Back button", bool(find(dlg, 'Back')))
    dlg = parse(click(g, find(dlg, 'Back')))
    ok &= check("which lands on the previous screen",
                'Flow points' in dlg['title'], str(dlg['title']))

    print("\n== bad input is refused, not guessed at ==")
    g, mod = build()
    g.run('QIDI_CALIBRATE')
    for params, why in (({'SET': 'nozzle'}, 'SET without VAL'),
                        ({'SET': 'wibble', 'VAL': 1}, 'an unknown key'),
                        ({'ADJ': 'wibble', 'BY': 0.01}, 'an unknown adjust')):
        try:
            g.run('_QIDI_CAL_STEP', **params)
            ok &= check("refuses %s" % why, False, "no error")
        except RuntimeError as e:
            ok &= check("refuses %s" % why, 'wizard:' in str(e), str(e))

    print("\n== a stray exception must not shut the MCU down ==")
    g, mod = build()
    mod._show = lambda name: (_ for _ in ()).throw(NameError("boom"))
    try:
        g.run('QIDI_CALIBRATE')
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
