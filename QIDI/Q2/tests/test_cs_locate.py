"""Off-printer tests for qidi_cs_locate (Stage A).

The thing under test is a walk that must find an object by type while invoking
nothing.  So most of these tests are traps: callables and live descriptors that
raise the moment they are touched.  A passing run means the walk went round them.
"""
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mock_klipper import MockReactor, MockPrinter, MockConfig
import qidi_cs_locate


# --- fake vendor modules ----------------------------------------------------
# qidi_cs_locate does `from . import cs1237`, which only works inside Klipper's
# extras package.  Off-printer we inject stand-ins with the same class names.

class FakeCS1237:
    """Mimics the real class: methods only, no add_client, plus traps."""

    def __init__(self, bulk):
        self.batch_bulk = bulk
        self.gain = 128
        self.sample_rate = 1280
        self.oid = 3

    def read_origin_data(self):
        raise AssertionError("locate invoked read_origin_data")

    def check_cs1237_zero(self):
        raise AssertionError("locate invoked check_cs1237_zero")

    def get_mcu(self):
        raise AssertionError("locate invoked get_mcu")

    @property
    def live_reading(self):
        raise AssertionError("locate read a live descriptor")

    @property
    def set_danger(self):
        raise AssertionError("locate read a set_* attribute")


class FakeCS1237Command:
    def cmd_CS_WEIGHT_BEGIN(self, gcmd):
        raise AssertionError("locate invoked a cmd_ method")


class FakeBatchBulkHelper:
    def __init__(self):
        # Short list of non-scalars - the case that used to vanish from the
        # report instead of being counted.
        self.clients = [{'cb': 1}, {'cb': 2}, {'cb': 3}]

    def add_client(self, cb):
        raise AssertionError("locate invoked add_client")


class FakeBulkDataQueue:
    pass


def fake_modules():
    cs = types.ModuleType('cs1237')
    cs.CS1237 = FakeCS1237
    cs.CS1237Command = FakeCS1237Command
    cs.USE_SPEED = 1280
    bulk = types.ModuleType('bulk_sensor')
    bulk.BatchBulkHelper = FakeBatchBulkHelper
    bulk.BulkDataQueue = FakeBulkDataQueue
    return cs, bulk


# --- fake printer objects ---------------------------------------------------

class FakeWeighEndstopWrapper:
    def __init__(self, chip):
        self.chip = chip
        self.trigger_count = 0


class FakePrinterAirProbe:
    """probe_air as it appears on the real machine: the CS1237 is nested, not
    registered, and reachable only through an attribute chain."""

    def __init__(self, chip):
        self.weigh = FakeWeighEndstopWrapper(chip)
        self.z_offset = -0.05
        self.voltage = 4.95
        self.fil = 0.9


class SlottedProbe:
    """No instance __dict__ - forces the dir() fallback, and every slot reads
    as a member_descriptor, which the walk must refuse rather than read.
    Stands in for a Cython cdef class."""
    __slots__ = ('chip', 'z_offset')

    def __init__(self, chip):
        self.chip = chip
        self.z_offset = -0.05


def build(root_obj, tmpdir):
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    printer.add('probe_air', root_obj)
    printer.add('toolhead', object())
    cfg = MockConfig(printer, {'out_dir': tmpdir})
    mod = qidi_cs_locate.load_config(cfg)
    return printer.lookup_object('gcode'), mod, printer


def check(label, cond, detail=''):
    print(("  PASS  " if cond else "  FAIL  ") + label + (
        ('  <- ' + detail) if detail and not cond else ''))
    return cond


def main():
    ok = True
    tmpdir = tempfile.mkdtemp()

    cs, bulk = fake_modules()
    qidi_cs_locate._cs1237_mod = cs
    qidi_cs_locate._bulk_mod = bulk

    print("\n== finds a nested CS1237 by type ==")
    chip = FakeCS1237(FakeBatchBulkHelper())
    g, mod, printer = build(FakePrinterAirProbe(chip), tmpdir)
    out = g.run('QIDI_CS_LOCATE')
    text = "\n".join(out)
    ok &= check("locates the chip at probe_air.weigh.chip",
                'probe_air.weigh.chip' in text and 'cs1237.CS1237' in text,
                text[-500:])
    ok &= check("locates the bulk helper",
                'bulk_sensor.BatchBulkHelper' in text, text[-500:])
    ok &= check("reports the chip's methods",
                'def  read_origin_data' in text, text[-500:])
    ok &= check("notes add_client is absent from CS1237",
                'def  add_client' not in text.split('batch_bulk')[0])

    print("\n== invokes nothing (traps raise on contact) ==")
    # Every trap above raises AssertionError.  Reaching here means none fired.
    ok &= check("did not call any method", True)
    ok &= check("did not read a live descriptor", True)
    ok &= check("did not read a set_* attribute", True)

    print("\n== refuses live descriptors on a dict-less object ==")
    chip2 = FakeCS1237(FakeBatchBulkHelper())
    g2, mod2, printer2 = build(SlottedProbe(chip2), tmpdir)
    out2 = g2.run('QIDI_CS_LOCATE')
    text2 = "\n".join(out2)
    ok &= check("reports attributes refused", 'attributes refused' in text2,
                text2[-400:])
    ok &= check("names what it declined to read",
                'member_descriptor' in text2, text2[-400:])
    ok &= check("says a null result is still a result",
                'not a failure' in text2 or 'HIT' in text2, text2[-400:])

    print("\n== report file ==")
    path = os.path.join(tmpdir, 'locate.json')
    ok &= check("locate.json written", os.path.exists(path))
    if os.path.exists(path):
        import json
        with open(path) as f:
            data = json.load(f)
        ok &= check("records hits, tree, refused, scalars and surfaces",
                    all(k in data[0]['payload']
                        for k in ('hits', 'tree', 'refused', 'scalars',
                                  'surfaces')),
                    str(list(data[0]['payload'])))
        sc = data[0]['payload']['scalars']
        chip_vals = sc.get('probe_air.weigh.chip', {})
        ok &= check("captures scalar values from __dict__",
                    chip_vals.get('gain') == 128
                    and chip_vals.get('sample_rate') == 1280,
                    str(chip_vals))
        ok &= check("scalar capture did not fire the traps", True)
        bq = sc.get('probe_air.weigh.chip.batch_bulk', {})
        ok &= check("a short list of non-scalars reports its length, "
                    "not nothing",
                    bq.get('clients') == '<list len=3>', str(bq))
        hits = data[0]['payload']['hits']
        ok &= check("hit path is exact",
                    any(h['path'] == 'probe_air.weigh.chip' for h in hits),
                    str(hits))

    print("\n== classification does not touch the instance ==")
    kind, owner = qidi_cs_locate._classify(chip, 'live_reading')
    ok &= check("property classified without reading it", kind == 'property',
                kind)
    kind, owner = qidi_cs_locate._classify(chip, 'read_origin_data')
    ok &= check("method classified as a function",
                qidi_cs_locate._is_method_kind(kind), kind)
    kind, owner = qidi_cs_locate._classify(chip, 'gain')
    ok &= check("instance attr classified without reading it",
                kind == 'instance_attr', kind)

    print("\n== an internal error must NOT be allowed to shut the MCU down ==")
    # Klipper turns an unhandled exception in a gcode command into an INTERNAL
    # ERROR and latches every MCU into shutdown - that happened for real on
    # 2026-09-15 and took a FIRMWARE_RESTART to clear. The guard has to turn
    # anything unexpected into a clean command failure instead.

    def boom(*a, **k):
        raise NameError("corr_ms")

    g, mod, printer = build(FakePrinterAirProbe(FakeCS1237(
        FakeBatchBulkHelper())), tempfile.mkdtemp())
    mod._run_LOCATE = boom
    try:
        g.run('QIDI_CS_LOCATE')
        ok &= check("QIDI_CS_LOCATE converts a stray exception", False,
                    "nothing raised")
    except RuntimeError as e:
        ok &= check("QIDI_CS_LOCATE converts a stray exception",
                    'internal error' in str(e) and 'NameError' in str(e),
                    str(e))
    except NameError:
        ok &= check("QIDI_CS_LOCATE converts a stray exception", False,
                    "NameError escaped - this shuts both MCUs down")

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
