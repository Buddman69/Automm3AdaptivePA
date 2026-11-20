"""Off-printer test: every option in qidi_calibration_q2.cfg is one its module
actually reads.

Klipper refuses to start on an option that no section consumed
(configfile.py check_unused_options):

    Option 'park_x' is not valid in section 'qidi_flow_ramp'

So an option that is only documentation must be a comment. This loads each
[qidi_*] section's module with a config object that records which options it
asks for, then compares that with what the .cfg sets.
"""
import configparser
import importlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mock_klipper import MockReactor, MockPrinter, MockConfig
from test_flow_ramp import ScriptGCode, check

CFG = os.path.join(os.path.dirname(HERE), 'qidi_calibration_q2.cfg')


class RecordingConfig(MockConfig):
    def __init__(self, printer, values):
        MockConfig.__init__(self, printer, values)
        self.asked = set()

    def get(self, key, default=None):
        self.asked.add(key)
        return MockConfig.get(self, key, default)

    def getint(self, key, default=None, **kw):
        self.asked.add(key)
        return MockConfig.getint(self, key, default, **kw)

    def getfloat(self, key, default=None, **kw):
        self.asked.add(key)
        return MockConfig.getfloat(self, key, default, **kw)

    def getboolean(self, key, default=None):
        self.asked.add(key)
        return MockConfig.getboolean(self, key, default)


def main():
    ok = True
    cp = configparser.RawConfigParser(inline_comment_prefixes=('#', ';'))
    cp.read(CFG)
    print("\n== every option in qidi_calibration_q2.cfg is read by its module ==")
    for section in cp.sections():
        values = dict(cp.items(section))
        printer = MockPrinter(MockReactor())
        printer._objects['gcode'] = ScriptGCode()
        values.setdefault('out_dir', tempfile.mkdtemp())
        rc = RecordingConfig(printer, values)
        try:
            mod = importlib.import_module(section)
            mod.load_config(rc)
        except Exception as e:
            ok &= check("[%s] loads" % section, False, repr(e))
            continue
        unused = sorted(set(cp.options(section)) - rc.asked)
        ok &= check("[%s] %d option(s), none unused" % (
                        section, len(cp.options(section))),
                    not unused, "Klipper would reject: %s" % ", ".join(unused))
    print("\nALL PASS" if ok else "\nSOME TESTS FAILED")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
