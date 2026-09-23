"""Off-printer tests for qidi_update.

The thing this module can do wrong is not "show the wrong menu" - it is INSTALL
A FILE THAT STOPS KLIPPER STARTING. There is no console after that, so no way to
undo it without SSH. So most of these tests drive the validation guards with
files that are deliberately broken, and assert that the live directory is
untouched afterwards.

GitHub is never contacted: fetch_releases and _get are replaced with fixtures.
"""
import json
import os
import re
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mock_klipper import MockReactor, MockPrinter, MockConfig
from test_flow_ramp import ScriptGCode, check
import qidi_update as U

ACTION = re.compile(r'^// action:prompt_([^ ]+)(?: (.+))?')

RELEASES = [
    {'tag_name': 'qidi-max4-v2.1.1', 'prerelease': False},
    {'tag_name': 'qidi-max4-v2.1.0', 'prerelease': False},
    {'tag_name': 'qidi-max4-v1.9.0', 'prerelease': False},
    {'tag_name': 'qidi-max4-v2.2.0', 'prerelease': True},
    {'tag_name': 'qidi-q2-v1.8.5', 'prerelease': False},
    {'tag_name': 'acme-widget-v1.0.0', 'prerelease': False},
    {'tag_name': 'not-a-valid-tag', 'prerelease': False},
    {'tag_name': 'v3.0.0', 'prerelease': False},
]

GOOD = "import os\nVALUE = 1\n"
SYNTAX_BAD = "def broken(:\n    pass\n"
IMPORT_BAD = "raise RuntimeError('boom at import time')\n"


def parse(lines):
    d = {'title': None, 'text': [], 'buttons': [], 'footer': []}
    for line in lines:
        m = ACTION.match(line)
        if not m:
            continue
        verb, payload = m.group(1), m.group(2)
        if verb == 'begin':
            d = {'title': payload, 'text': [], 'buttons': [], 'footer': []}
        elif verb == 'text':
            d['text'].append(payload)
        elif verb in ('button', 'footer_button'):
            key = 'buttons' if verb == 'button' else 'footer'
            d[key].append(tuple((payload or '').split('|')))
    return d


def click(g, command):
    bits = command.split()
    return g.run(bits[0], **dict(p.split('=', 1) for p in bits[1:]))


def find(dlg, needle):
    for b in dlg['buttons'] + dlg['footer']:
        if needle.lower() in b[0].lower():
            return b[1]
    raise AssertionError("no button %r in %s"
                         % (needle, [b[0] for b in dlg['buttons']]))


def make_zip(path, entries):
    """entries: {in-zip relative path -> content}. Every real release's zip is
    the WHOLE repo at that tag, wrapped in one generated top directory - so
    every fixture nests under that, exactly like the real thing."""
    with zipfile.ZipFile(path, 'w') as z:
        for name, body in entries.items():
            z.writestr("Buddman69-Automm3AdaptivePA-abc1234/" + name, body)


def build(extras=None, releases=RELEASES, zip_files=None, model_path="QIDI/Max4",
          raw_zip_files=None):
    """zip_files: {basename -> content}, auto-nested under model_path - the
    convenience form nearly every test below wants, all of them installing
    from a qidi-max4-... tag. raw_zip_files: {full in-zip path -> content},
    for tests that need to place files under a SPECIFIC (possibly more than
    one) brand/model path themselves - namely the cross-model collision test.
    """
    printer = MockPrinter(MockReactor())
    g = ScriptGCode()
    printer._objects['gcode'] = g
    data = tempfile.mkdtemp()
    mod = U.load_config(MockConfig(printer, {'data_dir': data,
                                             'repo': 'Buddman69/x'}))
    mod.extras = extras or tempfile.mkdtemp()

    zf = None
    if zip_files is not None or raw_zip_files is not None:
        entries = dict(raw_zip_files or {})
        for name, body in (zip_files or {}).items():
            entries["%s/%s" % (model_path, name)] = body
        zf = os.path.join(tempfile.mkdtemp(), 'r.zip')
        make_zip(zf, entries)

    def fake_get(url, binary=False):
        if binary:
            with open(zf, 'rb') as f:
                return f.read()
        return releases
    mod._get = fake_get
    return g, mod, data


def main():
    ok = True

    print("\n== tags are parsed, junk is ignored ==")
    g, mod, _ = build()
    rels = mod.fetch_releases()
    ok &= check("only well-formed tags survive", len(rels) == 6,
                str([r['tag'] for r in rels]))
    ok &= check("a tag with no brand-model is dropped",
                not any(r['tag'] == 'v3.0.0' for r in rels))
    # "not-a-valid-tag" parses as brand=not, model=a, version=alid-tag unless
    # the version is required to start with a digit - and it then sorts ABOVE
    # every real release, because a non-numeric chunk outranks a numeric one.
    ok &= check("and junk that LOOKS like brand-model-vX is dropped too",
                not any(r['tag'] == 'not-a-valid-tag' for r in rels),
                str([r['tag'] for r in rels]))
    ok &= check("another brand is kept, not assumed to be ours",
                any(r['brand'] == 'acme' for r in rels))
    ok &= check("versions sort numerically, so 2.1.1 beats 2.1.0",
                rels[0]['version'] == '2.2.0', str(rels[0]))
    ok &= check("1.10 would beat 1.9",
                U.version_key('1.10') > U.version_key('1.9'))

    print("\n== the picker: brand -> model -> release -> confirm ==")
    g, mod, _ = build()
    dlg = parse(g.run('QIDI_UPDATE'))
    # Sorted on the raw tag key, so 'acme' < 'qidi'. A brand with no entry in
    # PRETTY falls back to title case rather than being dropped.
    ok &= check("brand screen lists both brands alphabetically",
                [b[0] for b in dlg['buttons']] == ['Acme', 'QIDI'],
                str([b[0] for b in dlg['buttons']]))
    dlg = parse(click(g, find(dlg, 'QIDI')))
    ok &= check("model screen is alphabetical",
                [b[0] for b in dlg['buttons']] == ['Max4', 'Q2'],
                str([b[0] for b in dlg['buttons']]))
    dlg = parse(click(g, find(dlg, 'Max4')))
    labels = [b[0] for b in dlg['buttons']]
    ok &= check("releases are newest first",
                labels[0].startswith('2.2.0') and labels[-1].startswith('1.9.0'),
                str(labels))
    ok &= check("pre-releases are marked, not hidden",
                'pre-release' in labels[0], str(labels))
    ok &= check("and only this model's releases are offered",
                not any('1.8.5' in x for x in labels), str(labels))
    dlg = parse(click(g, find(dlg, '2.1.1')))
    ok &= check("confirm shows both versions",
                any('Install:' in t and '2.1.1' in t for t in dlg['text'])
                and any('Installed:' in t for t in dlg['text']),
                str(dlg['text']))
    ok &= check("nothing has been downloaded yet", mod.installed() == {},
                str(mod.installed()))

    print("\n== a good release installs ==")
    extras = tempfile.mkdtemp()
    with open(os.path.join(extras, 'qidi_thing.py'), 'w') as f:
        f.write("VALUE = 0\n")
    g, mod, data = build(extras=extras,
                         zip_files={'qidi_thing.py': GOOD,
                                    'qidi_other.py': GOOD,
                                    'README.md': "# not a module\n"})
    g.run('QIDI_UPDATE')
    g.run('_QIDI_UPD_STEP', SET='brand', VAL='qidi')
    g.run('_QIDI_UPD_STEP', SET='model', VAL='max4')
    g.run('_QIDI_UPD_STEP', SET='tag', VAL='qidi-max4-v2.1.1')
    g.run('_QIDI_UPD_STEP', GO=1)
    ok &= check("the module was replaced",
                open(os.path.join(extras, 'qidi_thing.py')).read() == GOOD)
    ok &= check("and a new one added",
                os.path.exists(os.path.join(extras, 'qidi_other.py')))
    ok &= check("non-module files in the zip are ignored",
                not os.path.exists(os.path.join(extras, 'README.md')))
    st = mod.installed()
    ok &= check("the installed version is recorded",
                st.get('tag') == 'qidi-max4-v2.1.1', str(st))
    ok &= check("and a backup was taken", os.path.isdir(st.get('backup', '')),
                str(st.get('backup')))
    ok &= check("the backup holds the PREVIOUS content",
                open(os.path.join(st['backup'], 'qidi_thing.py')).read()
                == "VALUE = 0\n")

    print("\n== rollback puts the old files back ==")
    g.run('QIDI_UPDATE', ROLLBACK=1)
    ok &= check("restored from the backup",
                open(os.path.join(extras, 'qidi_thing.py')).read()
                == "VALUE = 0\n")

    print("\n== A RELEASE THAT WOULD BRICK KLIPPER IS REFUSED ==")
    # This is the whole reason the module exists. A file that does not compile,
    # or that throws the moment it is imported, must never reach klippy/extras -
    # Klipper would refuse to start and there would be no console left to fix it
    # from.
    for label, body in (("will not compile", SYNTAX_BAD),
                        ("compiles but throws on import", IMPORT_BAD)):
        extras = tempfile.mkdtemp()
        live = os.path.join(extras, 'qidi_thing.py')
        with open(live, 'w') as f:
            f.write("VALUE = 0\n")
        g, mod, _ = build(extras=extras, zip_files={'qidi_thing.py': body})
        g.run('QIDI_UPDATE')
        g.run('_QIDI_UPD_STEP', SET='tag', VAL='qidi-max4-v2.1.1')
        try:
            g.run('_QIDI_UPD_STEP', GO=1)
            ok &= check("a file that %s is refused" % label, False,
                        "it installed")
        except RuntimeError as e:
            ok &= check("a file that %s is refused" % label,
                        'NOTHING was changed' in str(e), str(e)[:150])
        ok &= check("  and the live file is untouched",
                    open(live).read() == "VALUE = 0\n", open(live).read()[:40])
        ok &= check("  and nothing was recorded as installed",
                    mod.installed() == {}, str(mod.installed()))

    print("\n== a release with no modules in it is refused ==")
    extras = tempfile.mkdtemp()
    g, mod, _ = build(extras=extras, zip_files={'README.md': "# nothing\n"})
    g.run('QIDI_UPDATE')
    g.run('_QIDI_UPD_STEP', SET='tag', VAL='qidi-max4-v2.1.1')
    try:
        g.run('_QIDI_UPD_STEP', GO=1)
        ok &= check("refuses an empty release", False, "no error")
    except RuntimeError as e:
        ok &= check("refuses an empty release", 'no qidi_*.py' in str(e),
                    str(e)[:120])

    print("\n== a zip cannot write outside the staging directory ==")
    # Only the BASENAME of each entry is ever used as a destination, so a path
    # like ../../../etc/QIDI/Max4/qidi_evil.py still lands as exactly
    # staging/qidi_evil.py - nothing can escape via the file's own path.
    extras = tempfile.mkdtemp()
    g, mod, _ = build(extras=extras, raw_zip_files={
        '../../../etc/QIDI/Max4/qidi_evil.py': GOOD})
    g.run('QIDI_UPDATE')
    g.run('_QIDI_UPD_STEP', SET='tag', VAL='qidi-max4-v2.1.1')
    g.run('_QIDI_UPD_STEP', GO=1)
    ok &= check("it still only ever writes a flat basename",
                os.path.exists(os.path.join(extras, 'qidi_evil.py')))
    ok &= check("and nothing landed outside it",
                sorted(os.listdir(extras)) == ['qidi_evil.py'],
                str(os.listdir(extras)))

    print("\n== THE BUG THIS RESTRUCTURING INTRODUCED, FIXED: cross-model "
          "collision ==")
    # Every model's code now lives in the SAME repo (QIDI/Max4/, QIDI/Q2/),
    # so a release's zipball is a snapshot of the WHOLE repo, not just one
    # model's folder - and the two codebases are deliberately near-identical
    # in filenames. Matching qidi_*.py by basename alone (the ORIGINAL
    # implementation) would have installed whichever model's copy the zip
    # happened to iterate last, onto EITHER printer, regardless of which one
    # was selected. This is the scenario that catches that.
    extras = tempfile.mkdtemp()
    with open(os.path.join(extras, 'qidi_thing.py'), 'w') as f:
        f.write("VALUE = 0\n")
    g, mod, _ = build(extras=extras, raw_zip_files={
        'QIDI/Max4/qidi_thing.py': "VALUE = 'max4'\n",
        'QIDI/Q2/qidi_thing.py': "VALUE = 'q2'\n",
    })
    g.run('QIDI_UPDATE')
    g.run('_QIDI_UPD_STEP', SET='tag', VAL='qidi-max4-v2.1.1')
    g.run('_QIDI_UPD_STEP', GO=1)
    got = open(os.path.join(extras, 'qidi_thing.py')).read()
    ok &= check("selecting Max4 installs the Max4 file",
                got == "VALUE = 'max4'\n", got)
    ok &= check("NOT the Q2 file that happens to share its name",
                got != "VALUE = 'q2'\n", got)

    extras2 = tempfile.mkdtemp()
    g2, mod2, _ = build(extras=extras2, releases=RELEASES, raw_zip_files={
        'QIDI/Max4/qidi_thing.py': "VALUE = 'max4'\n",
        'QIDI/Q2/qidi_thing.py': "VALUE = 'q2'\n",
    })
    g2.run('QIDI_UPDATE')
    g2.run('_QIDI_UPD_STEP', SET='tag', VAL='qidi-q2-v1.8.5')
    g2.run('_QIDI_UPD_STEP', GO=1)
    got2 = open(os.path.join(extras2, 'qidi_thing.py')).read()
    ok &= check("and selecting Q2 installs the Q2 file, not Max4's",
                got2 == "VALUE = 'q2'\n", got2)

    print("\n== THE BUG FOUND LIVE 2026-09-23, FIXED: a brand-new module's "
          "config section is added, not just its .py file ==")
    # qidi_cal_wizard_bed.py installed via QIDI_UPDATE and Klipper restarted,
    # but QIDI_CALIBRATE_BED did not exist - nothing had ever added
    # [qidi_cal_wizard_bed] to printer.cfg, so Klipper never loaded the file
    # it had just been given. This is the scenario that catches that class of
    # bug: a module the printer.cfg fixture has never seen before.
    extras = tempfile.mkdtemp()
    cfg_dir = tempfile.mkdtemp()
    cfg_path = os.path.join(cfg_dir, 'printer.cfg')
    with open(cfg_path, 'w') as f:
        f.write("[qidi_thing]\nsome_option: 1\n\n"
                "#*# <---------------------- SAVE_CONFIG ---------------------->\n"
                "#*# DO NOT EDIT THIS BLOCK OR BELOW\n")
    with open(os.path.join(extras, 'qidi_thing.py'), 'w') as f:
        f.write("VALUE = 0\n")
    g, mod, _ = build(extras=extras,
                      zip_files={'qidi_thing.py': GOOD,
                                 'qidi_brand_new.py': GOOD})
    mod.printer._start_args['config_file'] = cfg_path
    g.run('QIDI_UPDATE')
    g.run('_QIDI_UPD_STEP', SET='brand', VAL='qidi')
    g.run('_QIDI_UPD_STEP', SET='model', VAL='max4')
    g.run('_QIDI_UPD_STEP', SET='tag', VAL='qidi-max4-v2.1.1')
    out = "\n".join(g.run('_QIDI_UPD_STEP', GO=1))
    cfg_after = open(cfg_path).read()
    ok &= check("the new module's section was added",
                bool(re.search(r'(?m)^\[qidi_brand_new\]\s*$', cfg_after)),
                cfg_after)
    ok &= check("the EXISTING module's section was left alone, not duplicated",
                cfg_after.count('[qidi_thing]') == 1, cfg_after)
    ok &= check("the new section landed ABOVE the SAVE_CONFIG marker",
                cfg_after.index('[qidi_brand_new]')
                < cfg_after.index('SAVE_CONFIG'), cfg_after)
    ok &= check("everything below the marker survived untouched",
                cfg_after.endswith("#*# DO NOT EDIT THIS BLOCK OR BELOW\n"),
                cfg_after)
    ok &= check("printer.cfg was backed up first",
                any(f.startswith('printer.cfg.bak-upd-')
                    for f in os.listdir(cfg_dir)),
                str(os.listdir(cfg_dir)))
    ok &= check("and said so in the console output",
                'added missing section' in out and 'qidi_brand_new' in out,
                out)

    print("\n== a module whose section already exists is left alone ==")
    extras = tempfile.mkdtemp()
    cfg_dir = tempfile.mkdtemp()
    cfg_path = os.path.join(cfg_dir, 'printer.cfg')
    with open(cfg_path, 'w') as f:
        f.write("[qidi_thing]\n")
    before = open(cfg_path).read()
    with open(os.path.join(extras, 'qidi_thing.py'), 'w') as f:
        f.write("VALUE = 0\n")
    g, mod, _ = build(extras=extras, zip_files={'qidi_thing.py': GOOD})
    mod.printer._start_args['config_file'] = cfg_path
    g.run('QIDI_UPDATE')
    g.run('_QIDI_UPD_STEP', SET='tag', VAL='qidi-max4-v2.1.1')
    out = "\n".join(g.run('_QIDI_UPD_STEP', GO=1))
    ok &= check("printer.cfg is byte-identical - no section was added or "
                "duplicated", open(cfg_path).read() == before,
                open(cfg_path).read())
    ok &= check("and no backup was made - nothing needed changing",
                os.listdir(cfg_dir) == ['printer.cfg'],
                str(os.listdir(cfg_dir)))
    ok &= check("nothing said about sections in the output",
                'section' not in out.lower(), out)

    print("\n== a missing printer.cfg does not undo an otherwise-good "
          "install ==")
    extras = tempfile.mkdtemp()
    with open(os.path.join(extras, 'qidi_thing.py'), 'w') as f:
        f.write("VALUE = 0\n")
    g, mod, _ = build(extras=extras, zip_files={'qidi_thing.py': GOOD})
    # No config_file in start args at all - the common case in these tests,
    # and also whatever a real host would give if it could not be found.
    g.run('QIDI_UPDATE')
    g.run('_QIDI_UPD_STEP', SET='tag', VAL='qidi-max4-v2.1.1')
    g.run('_QIDI_UPD_STEP', GO=1)
    ok &= check("the module still installed",
                open(os.path.join(extras, 'qidi_thing.py')).read() == GOOD)
    ok &= check("and it is recorded as installed",
                mod.installed().get('tag') == 'qidi-max4-v2.1.1',
                str(mod.installed()))

    print("\n== CHECK reports without changing anything ==")
    extras = tempfile.mkdtemp()
    g, mod, _ = build(extras=extras)
    out = "\n".join(g.run('QIDI_UPDATE', CHECK=1))
    ok &= check("names the newest release", '2.2.0' in out, out)
    ok &= check("and says what is installed", 'installed' in out.lower(), out)
    ok &= check("no dialog was opened",
                not any('prompt_show' in x for x in g.output), str(g.output))
    ok &= check("and extras is untouched", os.listdir(extras) == [],
                str(os.listdir(extras)))

    print("\n== COMMANDS lists what this project registers ==")
    g, mod, _ = build()
    g.gcode_help = {'QIDI_CALIBRATE': 'Ask for the settings.',
                    'QIDI_UPDATE': 'Install an update.',
                    'COMMANDS': 'List commands.',
                    'G28': 'Home the printer.'}
    out = "\n".join(g.run('COMMANDS'))
    ok &= check("lists ours", 'QIDI_CALIBRATE' in out and 'QIDI_UPDATE' in out,
                out)
    ok &= check("with their descriptions", 'Ask for the settings.' in out, out)
    ok &= check("and not Klipper's hundreds", 'G28' not in out, out)

    print("\n== cancel closes and changes nothing ==")
    extras = tempfile.mkdtemp()
    g, mod, _ = build(extras=extras)
    g.run('QIDI_UPDATE')
    out = g.run('_QIDI_UPD_STEP', CANCEL=1)
    ok &= check("dialog closed",
                any('prompt_end' in x for x in out), str(out))
    ok &= check("nothing installed", os.listdir(extras) == [],
                str(os.listdir(extras)))

    print("\n== a stray exception must not shut the MCU down ==")
    g, mod, _ = build()
    mod.fetch_releases = lambda: (_ for _ in ()).throw(NameError("boom"))
    try:
        g.run('QIDI_UPDATE')
        ok &= check("converted to a command error", False, "nothing raised")
    except RuntimeError as e:
        ok &= check("converted to a command error", 'NameError' in str(e),
                    str(e))
    except NameError:
        ok &= check("converted to a command error", False, "NameError escaped")

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
