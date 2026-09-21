"""Off-printer tests for qidi_cs_bulk.

The safety-critical properties are that LISTEN mode sends nothing at all, and
that START mode always stops the sampler - including when collection raises.
Those get tested first and hardest.
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
import qidi_cs_bulk as B

CLOCK = 72000000.0


class FakeCmd:
    def __init__(self, fail_on_stop=False):
        self.sent = []
        self.fail_on_stop = fail_on_stop

    def send(self, data, minclock=0, reqclock=0):
        self.sent.append(list(data))
        if self.fail_on_stop and data[1] == 0:
            raise RuntimeError("serial went away")


class FakeMCU:
    def __init__(self):
        self.handlers = {}

    def register_response(self, cb, name, oid=None):
        self.handlers[(name, oid)] = cb

    def seconds_to_clock(self, s):
        return int(s * CLOCK)

    def deliver(self, name, oid, params):
        cb = self.handlers.get((name, oid))
        if cb is None:
            raise AssertionError("nothing registered for %s oid=%s"
                                 % (name, oid))
        cb(params)


class FakeSensor:
    def __init__(self, mcu, cmd, value=-391600.0):
        self.oid = 5
        self._mcu = mcu
        self.query_cs1237_cmd = cmd
        self._value = value

    def get_mcu(self):
        return self._mcu

    def read_origin_data(self):
        return self._value


class FakeProbe:
    def __init__(self, sensor):
        self.sensor_helper = sensor


class FakePrintStats:
    def __init__(self, state='standby'):
        self.state = state

    def get_status(self, eventtime):
        return {'state': self.state}


def build(tmpdir, printing=None, fail_on_stop=False):
    reactor = MockReactor()
    printer = MockPrinter(reactor)
    g = ScriptGCode()
    printer._objects['gcode'] = g
    mcu = FakeMCU()
    cmd = FakeCmd(fail_on_stop=fail_on_stop)
    sensor = FakeSensor(mcu, cmd)
    printer._objects['probe_air'] = FakeProbe(sensor)
    if printing is not None:
        printer._objects['print_stats'] = FakePrintStats(printing)
    mod = B.load_config(MockConfig(printer, {'out_dir': tmpdir}))
    return g, mod, mcu, cmd, sensor


def block(values):
    """Encode counts as the wire format: 24-bit LE plus a 0x00 pad."""
    out = bytearray()
    for v in values:
        u = v & 0xffffff
        out += bytes([u & 0xff, (u >> 8) & 0xff, (u >> 16) & 0xff, 0x00])
    return bytes(out)


def main():
    ok = True

    print("\n== the wire format, as established by QIDI_CS_BATCH ==")
    # 2006fa00 decoded to -391648 in the batch probe. Round-trip it.
    ok &= check("0x2006fa00 decodes to -391648",
                B.decode_block(bytes([0x20, 0x06, 0xfa, 0x00])) == [-391648],
                str(B.decode_block(bytes([0x20, 0x06, 0xfa, 0x00]))))
    vals = [-391648, -391531, 0, 1, -1, 8388607, -8388608]
    ok &= check("round-trips through the encoder",
                B.decode_block(block(vals)) == vals,
                str(B.decode_block(block(vals))))
    ok &= check("13 samples is a 52-byte block",
                len(block([0] * 13)) == 52, str(len(block([0] * 13))))
    ok &= check("a trailing partial sample is ignored",
                B.decode_block(block([5]) + b'\x01\x02') == [5])

    print("\n== LISTEN mode sends NOTHING ==")
    tmp = tempfile.mkdtemp()
    g, mod, mcu, cmd, sensor = build(tmp)
    out = "\n".join(g.run('QIDI_CS_BULK', SECS=2))
    ok &= check("not a single command was sent", cmd.sent == [], str(cmd.sent))
    ok &= check("says so explicitly", 'sending nothing' in out, out[:200])
    ok &= check("registers for sensor_bulk_data on the sensor's oid",
                ('sensor_bulk_data', 5) in mcu.handlers,
                str(sorted(mcu.handlers)))
    ok &= check("and for sensor_bulk_status",
                ('sensor_bulk_status', 5) in mcu.handlers)
    ok &= check("never registers a global oid=None handler that could displace "
                "lis2dw or adxl345",
                not any(k[1] is None for k in mcu.handlers),
                str(sorted(mcu.handlers)))
    ok &= check("reports silence as expected", 'silent, as expected' in out,
                out[-300:])

    print("\n== LISTEN mode notices if something IS already streaming ==")
    g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp())

    class Once:
        done = False
    real_pause = mod.reactor.pause

    def pause_and_deliver(waketime):
        if not Once.done:
            Once.done = True
            mcu.deliver('sensor_bulk_data', 5,
                        {'oid': 5, 'sequence': 1, 'data': block([-391600] * 13)})
        return real_pause(waketime)
    mod.reactor.pause = pause_and_deliver
    out = "\n".join(g.run('QIDI_CS_BULK', SECS=1))
    ok &= check("calls it out as unexpected", 'UNEXPECTED' in out, out[-300:])
    ok &= check("still sent nothing", cmd.sent == [], str(cmd.sent))

    print("\n== START mode: starts, then ALWAYS stops ==")
    tmp2 = tempfile.mkdtemp()
    g, mod, mcu, cmd, sensor = build(tmp2)
    out = "\n".join(g.run('QIDI_CS_BULK', HZ=100, SECS=2))
    ok &= check("exactly two commands sent", len(cmd.sent) == 2, str(cmd.sent))
    ok &= check("first starts at 100 Hz = 720000 ticks",
                cmd.sent[0] == [5, 720000], str(cmd.sent[0]))
    ok &= check("last stops with rest_ticks=0", cmd.sent[-1] == [5, 0],
                str(cmd.sent[-1]))
    ok &= check("1280 Hz would be 56250 ticks",
                int(CLOCK / 1280) == 56250, str(int(CLOCK / 1280)))

    print("\n== the stop survives an exception mid-collection ==")
    g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp())

    def boom(waketime):
        raise RuntimeError("reactor exploded")
    mod.reactor.pause = boom
    try:
        g.run('QIDI_CS_BULK', HZ=100, SECS=2)
    except Exception:
        pass
    ok &= check("the sampler was still stopped", [5, 0] in cmd.sent,
                str(cmd.sent))

    print("\n== if the stop itself fails, say so loudly ==")
    g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp(), fail_on_stop=True)
    out = "\n".join(g.run('QIDI_CS_BULK', HZ=100, SECS=1))
    ok &= check("tells the user to FIRMWARE_RESTART",
                'FAILED TO STOP' in out and 'FIRMWARE_RESTART' in out,
                out[-300:])

    print("\n== START mode with data arriving ==")
    tmp3 = tempfile.mkdtemp()
    g, mod, mcu, cmd, sensor = build(tmp3)
    state = {'seq': 0, 'n': 0}
    real_pause2 = mod.reactor.pause

    def pause_stream(waketime):
        if state['n'] < 10:
            state['n'] += 1
            state['seq'] += 1
            mcu.deliver('sensor_bulk_data', 5,
                        {'oid': 5, 'sequence': state['seq'],
                         'data': block([-391600 + state['seq']] * 13)})
        return real_pause2(waketime)
    mod.reactor.pause = pause_stream
    out = "\n".join(g.run('QIDI_CS_BULK', HZ=100, SECS=2))
    ok &= check("declares that it streams", 'IT STREAMS' in out, out[-400:])
    ok &= check("counts 130 samples from 10 messages of 13",
                '130 samples' in out, str([l for l in out.split("\n")
                                           if 'samples' in l]))
    ok &= check("reports the payload size", '52-52 bytes' in out,
                str([l for l in out.split("\n") if 'payload' in l]))
    ok &= check("finds no sequence gaps", '0 discontinuities' in out,
                str([l for l in out.split("\n") if 'sequence' in l]))
    ok &= check("still stopped the sampler afterwards", cmd.sent[-1] == [5, 0],
                str(cmd.sent))
    ok &= check("confirms the polled path survived",
                'polled path still works' in out, out[-400:])

    print("\n== sequence gaps are reported, not hidden ==")
    g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp())
    st = {'n': 0}
    rp = mod.reactor.pause
    seqs = [1, 2, 7, 8]

    def pause_gappy(waketime):
        if st['n'] < len(seqs):
            s = seqs[st['n']]
            st['n'] += 1
            mcu.deliver('sensor_bulk_data', 5,
                        {'oid': 5, 'sequence': s, 'data': block([1, 2, 3])})
        return rp(waketime)
    mod.reactor.pause = pause_gappy
    out = "\n".join(g.run('QIDI_CS_BULK', HZ=100, SECS=2))
    ok &= check("one discontinuity found", '1 discontinuity' in out,
                str([l for l in out.split("\n") if 'sequence' in l]))

    print("\n== refuses unsafe conditions ==")
    for state_name in ('printing', 'paused'):
        g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp(),
                                         printing=state_name)
        try:
            g.run('QIDI_CS_BULK', HZ=100, SECS=1)
            ok &= check("refuses while %s" % state_name, False, "no error")
        except RuntimeError as e:
            ok &= check("refuses while %s" % state_name,
                        'refusing' in str(e), str(e))
        ok &= check("and sent nothing while %s" % state_name, cmd.sent == [],
                    str(cmd.sent))

    g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp(), printing='standby')
    out = "\n".join(g.run('QIDI_CS_BULK', HZ=100, SECS=1))
    ok &= check("but runs when idle", len(cmd.sent) == 2, str(cmd.sent))

    for bad in (0, -5, 2000):
        g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp())
        try:
            g.run('QIDI_CS_BULK', HZ=bad, SECS=1)
            ok &= check("rejects HZ=%s" % bad, False, "no error")
        except RuntimeError as e:
            ok &= check("rejects HZ=%s" % bad, 'HZ must be' in str(e), str(e))
        ok &= check("and sent nothing for HZ=%s" % bad, cmd.sent == [],
                    str(cmd.sent))

    print("\n== ZERO mode: the zero-read reply ==")

    class ZeroCmd:
        def __init__(self, payload):
            self.payload = payload
            self.sent = []

        def send(self, data, minclock=0, reqclock=0):
            self.sent.append(list(data))
            return {'oid': 5, 'data': self.payload}

    # one sample per call - the disappointing outcome
    g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp())
    sensor.query_cs1237_zero_read_cmd = ZeroCmd(block([-391600]))
    out = "\n".join(g.run('QIDI_CS_BULK', ZERO=3))
    ok &= check("calls the read-only sibling, not the tare",
                sensor.query_cs1237_zero_read_cmd.sent == [[5], [5], [5]],
                str(sensor.query_cs1237_zero_read_cmd.sent))
    ok &= check("never touches query_cs1237_cmd in ZERO mode", cmd.sent == [],
                str(cmd.sent))
    ok &= check("reports one sample per call",
                'one sample per call here too' in out, out[-300:])

    # 13 samples per call - the prize
    g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp())
    thirteen = [-391600 + i for i in range(13)]
    sensor.query_cs1237_zero_read_cmd = ZeroCmd(block(thirteen))
    out = "\n".join(g.run('QIDI_CS_BULK', ZERO=2))
    ok &= check("recognises a 13-sample block",
                'THIS RETURNS A BLOCK - 13 samples' in out, out[-400:])
    ok &= check("says Stage 3 should be redesigned around it",
                'redesigned around it' in out)
    ok &= check("decodes the block correctly",
                '52 bytes = 13 sample(s)' in out,
                str([l for l in out.split("\n") if 'bytes' in l]))

    g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp())
    sensor.query_cs1237_zero_read_cmd = ZeroCmd(block([1]))
    for bad in (0, 99):
        try:
            g.run('QIDI_CS_BULK', ZERO=bad)
            ok &= check("rejects ZERO=%d" % bad, False, "no error")
        except RuntimeError as e:
            ok &= check("rejects ZERO=%d" % bad, 'ZERO must be' in str(e),
                        str(e))

    g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp(), printing='printing')
    sensor.query_cs1237_zero_read_cmd = ZeroCmd(block([1]))
    try:
        g.run('QIDI_CS_BULK', ZERO=2)
        ok &= check("ZERO mode refuses while printing", False, "no error")
    except RuntimeError as e:
        ok &= check("ZERO mode refuses while printing", 'refusing' in str(e),
                    str(e))
    ok &= check("and sent nothing", sensor.query_cs1237_zero_read_cmd.sent == [],
                str(sensor.query_cs1237_zero_read_cmd.sent))

    print("\n== FAST mode: fresh samples or a stale cache? ==")

    class SeqCmd:
        """Returns a new value each call, or the same one, on demand."""

        def __init__(self, fresh, cost_s=0.0032, reactor=None):
            self.fresh = fresh
            self.i = 0
            self.cost_s = cost_s
            self.reactor = reactor
            self.sent = []

        def send(self, data, minclock=0, reqclock=0):
            self.sent.append(list(data))
            if self.reactor is not None:
                self.reactor.t += self.cost_s
            v = -391600 + (self.i if self.fresh else 0)
            self.i += 1
            return {'oid': 5, 'data': block([v])}

    g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp())
    sensor.query_cs1237_zero_read_cmd = SeqCmd(True, reactor=mod.reactor)
    out = "\n".join(g.run('QIDI_CS_BULK', FAST=200))
    ok &= check("a changing series reads as FRESH AND FAST",
                'FRESH AND FAST' in out, out[-400:])
    ok &= check("quotes the multiple over the polled path",
                'the polled rate' in out and 'x the polled rate' in out,
                str([l for l in out.split("\n") if 'polled rate' in l]))
    ok &= check("never used query_cs1237_cmd", cmd.sent == [], str(cmd.sent))

    g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp())
    sensor.query_cs1237_zero_read_cmd = SeqCmd(False, reactor=mod.reactor)
    out = "\n".join(g.run('QIDI_CS_BULK', FAST=200))
    ok &= check("a constant series reads as STALE CACHE",
                'STALE CACHE' in out, out[-400:])
    ok &= check("and says it is useless alone", 'Useless without' in out)

    # A fresh series that is nonetheless slow must not be sold as a win.
    g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp())
    sensor.query_cs1237_zero_read_cmd = SeqCmd(True, cost_s=0.020,
                                               reactor=mod.reactor)
    out = "\n".join(g.run('QIDI_CS_BULK', FAST=50))
    ok &= check("fresh but only 50 Hz is not called a win",
                'FRESH AND FAST' not in out, out[-300:])

    for bad in (4, 5000):
        g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp())
        sensor.query_cs1237_zero_read_cmd = SeqCmd(True, reactor=mod.reactor)
        try:
            g.run('QIDI_CS_BULK', FAST=bad)
            ok &= check("rejects FAST=%d" % bad, False, "no error")
        except RuntimeError as e:
            ok &= check("rejects FAST=%d" % bad, 'FAST must be' in str(e),
                        str(e))

    print("\n== report ==")
    path = os.path.join(tmp3, 'bulk.json')
    ok &= check("bulk.json written", os.path.exists(path))
    if os.path.exists(path):
        with open(path) as f:
            d = json.load(f)
        pl = d[-1]['payload']
        ok &= check("records mode, rest_ticks and the summary",
                    pl['mode'] == 'start' and pl['rest_ticks'] == 720000
                    and pl['summary']['samples'] == 130, str(pl)[:300])
        ok &= check("keeps sequences for re-analysis",
                    len(pl['summary']['sequences']) == 10,
                    str(pl['summary']['sequences']))

    print("\n== an internal error must NOT be allowed to shut the MCU down ==")
    # Klipper turns an unhandled exception in a gcode command into an INTERNAL
    # ERROR and latches every MCU into shutdown - that happened for real on
    # 2026-09-15 and took a FIRMWARE_RESTART to clear. The guard has to turn
    # anything unexpected into a clean command failure instead.

    def boom(*a, **k):
        raise NameError("corr_ms")

    g, mod, mcu, cmd, sensor = build(tempfile.mkdtemp())
    mod._run_BULK = boom
    try:
        g.run('QIDI_CS_BULK', SECS=1)
        ok &= check("QIDI_CS_BULK converts a stray exception", False,
                    "nothing raised")
    except RuntimeError as e:
        ok &= check("QIDI_CS_BULK converts a stray exception",
                    'internal error' in str(e) and 'NameError' in str(e),
                    str(e))
    except NameError:
        ok &= check("QIDI_CS_BULK converts a stray exception", False,
                    "NameError escaped - this shuts both MCUs down")

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
