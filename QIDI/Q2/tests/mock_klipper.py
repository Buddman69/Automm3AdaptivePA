"""Minimal Klipper mocks + a fake QIDI-style sensor, for testing the
discovery extra off-printer.  Simulated clock, so update rates are exact."""
import math


class MockReactor:
    NOW = 0.
    NEVER = float('inf')

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def pause(self, waketime):
        self.t = max(self.t, waketime)
        return self.t


class MockGCode:
    def __init__(self):
        self.commands = {}
        self.output = []

    def register_command(self, name, func, desc=None):
        self.commands[name] = func

    def run(self, name, **params):
        self.output = []
        cmd = MockGCode.Cmd(self, params)
        self.commands[name](cmd)
        return self.output

    class Cmd:
        def __init__(self, gcode, params):
            self._g = gcode
            self._p = params

        def respond_info(self, msg):
            self._g.output.append(msg)

        def get(self, key, default='__REQ__'):
            if key in self._p:
                return str(self._p[key])
            if default == '__REQ__':
                raise ValueError("missing " + key)
            return default

        def get_int(self, key, default=None, minval=None, maxval=None):
            return int(self._p.get(key, default))

        def get_float(self, key, default=None, minval=None, maxval=None,
                      above=None):
            return float(self._p.get(key, default))

        def error(self, msg):
            return RuntimeError(msg)


class MockPrinter:
    def __init__(self, reactor):
        self._reactor = reactor
        self._objects = {'gcode': MockGCode()}

    def get_reactor(self):
        return self._reactor

    def add(self, name, obj):
        self._objects[name] = obj

    def lookup_object(self, name, default='__RAISE__'):
        if name in self._objects:
            return self._objects[name]
        if default == '__RAISE__':
            raise KeyError(name)
        return default

    def lookup_objects(self, module=None):
        return sorted(self._objects.items())


class MockConfig:
    def __init__(self, printer, values=None):
        self._printer = printer
        self._v = values or {}

    def get_printer(self):
        return self._printer

    def get(self, key, default=None):
        return self._v.get(key, default)

    def getint(self, key, default=None, minval=None, maxval=None):
        # Klipper returns the default OBJECT when the option is absent, so a
        # None default comes back as None - it does not coerce. Coercing turned
        # an optional setting into a TypeError the moment one was added.
        v = self._v.get(key, default)
        return None if v is None else int(v)

    def getfloat(self, key, default=None, minval=None, maxval=None,
                 above=None, below=None):
        v = self._v.get(key, default)
        return None if v is None else float(v)

    def getboolean(self, key, default=None):
        return bool(self._v.get(key, default))


# --- fake QIDI sensor -------------------------------------------------------

class FakeSensorHelper:
    """Stands in for QIDI's sensor_helper: one live reading that updates at a
    fixed rate off the simulated clock, plus decoys that never move."""

    def __init__(self, reactor, rate_hz, baseline=8_400_000.):
        self._reactor = reactor
        self._rate = rate_hz
        self._baseline = baseline
        self.press = 0.          # external hand-press offset, for WATCH tests
        # static decoys of the kind a real driver carries
        self.gain = 128
        self.channel_id = 0
        self.sample_bits = 24
        self.name = 'c_sensor'
        self.enabled = True
        self.calibration = [1.0, 0.0, 2.5]

    @property
    def read_origin_data(self):
        tick = int(self._reactor.monotonic() * self._rate)
        noise = math.sin(tick * 2.399) * 120. + (tick % 7) * 3.
        return self._baseline + noise + self.press

    def read_raw(self):          # callable - must never be invoked
        raise AssertionError("discovery invoked a callable")

    # must be skipped by SKIP_PREFIXES even though it is a property
    @property
    def set_danger(self):
        raise AssertionError("discovery read a set_* attribute")


class FakeProbeAir:
    def __init__(self, reactor, rate_hz):
        self.sensor_helper = FakeSensorHelper(reactor, rate_hz)
        self.z_offset = -0.05
        self.speed = 5
        self.samples = 2
        self.voltage = 4.95
        self.delta_v = 0.08
