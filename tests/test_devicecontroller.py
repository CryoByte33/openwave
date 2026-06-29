"""Unit tests for DeviceController's connect / poll / reconnect / write logic,
driven through a FakeDevice and a controllable-clock FakeScheduler. No GTK main
loop, no USB hardware — the seam the controller was built around, finally with a
second adapter.

Run: python3 tests/test_devicecontroller.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openwave.devicecontroller as dcmod
from openwave.devicecontroller import DeviceController


class FakeScheduler:
    """Synchronous run_async + timers that fire only on tick()."""

    def __init__(self):
        self.timers = {}
        self._next = 0

    def run_async(self, fn, on_done=None, on_error=None):
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001
            if on_error:
                on_error(e)
            return
        if on_done:
            on_done(result)

    def call_every(self, interval_s, fn):
        h = self._next
        self._next += 1
        self.timers[h] = fn
        return h

    def cancel(self, handle):
        self.timers.pop(handle, None)

    def tick(self):
        for h, fn in list(self.timers.items()):
            if fn() is False:
                self.timers.pop(h, None)


class FakeDevice:
    """An in-memory Wave XLR backend satisfying device.DeviceBackend."""

    supports_low_impedance = True
    supports_volume_select = False
    supports_crossfade = True
    supports_voice_effects = True
    hp_detents = None

    def __init__(self, connect_error=None):
        self._connected = False
        self._connect_error = connect_error   # raise once on connect, then clear
        self.get_all_error = False            # flip True to simulate a lost device
        self.calls = []
        self.state = {
            "gain_raw": 1000, "mute": False, "hp_volume_db": -20.0,
            "volume_select": "gain", "low_impedance": False,
            "crossfade": 100, "lowcut": False, "expander": False,
            "voice_tune": False, "voice_tune_strength": 50,
        }

    @property
    def connected(self):
        return self._connected

    def connect(self):
        if self._connect_error is not None:
            err, self._connect_error = self._connect_error, None
            raise err
        self._connected = True

    def disconnect(self):
        self._connected = False

    def read_device_info(self):
        return {"fw_version": "9.9", "api_version": "1.0", "serial": "FAKE123"}

    def get_all(self):
        if self.get_all_error:
            raise RuntimeError("device lost")
        return dict(self.state)

    def format_gain(self, raw):
        return f"{raw} u"

    def set_mute(self, m): self.state["mute"] = m; self.calls.append(("mute", m))
    def set_gain_raw(self, v): self.state["gain_raw"] = v; self.calls.append(("gain", v))
    def set_hp_volume_db(self, db): self.state["hp_volume_db"] = db; self.calls.append(("hp", db))
    def set_low_impedance(self, e): self.state["low_impedance"] = e; self.calls.append(("lowz", e))
    def set_crossfade(self, v): self.state["crossfade"] = v; self.calls.append(("crossfade", v))
    def set_lowcut(self, e): self.state["lowcut"] = e; self.calls.append(("lowcut", e))
    def set_expander(self, e): self.state["expander"] = e; self.calls.append(("expander", e))
    def set_voice_tune(self, e): self.state["voice_tune"] = e; self.calls.append(("vt", e))
    def set_voice_tune_strength(self, v): self.state["voice_tune_strength"] = v; self.calls.append(("vts", v))


def _make(fake):
    sched = FakeScheduler()
    views = []
    dc = DeviceController(sched, views.append, backend=fake, backend_factory=lambda: fake)
    return sched, views, dc


def test_connect_emits_connected_view():
    fake = FakeDevice()
    _, views, dc = _make(fake)
    dc.connect()
    assert dc.connected
    v = views[-1]
    assert v.connected and v.status == "OpenWave"
    assert v.muted is False and v.serial == "FAKE123" and v.crossfade == 100


def test_caps_come_from_backend():
    fake = FakeDevice()
    _, views, dc = _make(fake)
    dc.connect()
    caps = views[-1].caps
    assert caps.supports_crossfade and caps.supports_voice_effects
    assert caps.supports_volume_select is False


def test_poll_emits_only_on_change():
    fake = FakeDevice()
    sched, views, dc = _make(fake)
    dc.connect()
    n = len(views)
    sched.tick()                       # poll, nothing changed
    assert len(views) == n
    fake.state["mute"] = True          # hardware button pressed
    sched.tick()
    assert views[-1].muted is True


def test_set_mute_writes_through():
    fake = FakeDevice()
    _, _, dc = _make(fake)
    dc.connect()
    dc.set_mute(True)
    assert ("mute", True) in fake.calls and fake.state["mute"] is True


def test_throttled_gain_fires_leading_edge():
    fake = FakeDevice()
    _, _, dc = _make(fake)
    dc.connect()
    label = dc.set_gain(2048)
    assert ("gain", 2048) in fake.calls
    assert label == fake.format_gain(2048)


def test_write_while_disconnected_is_dropped():
    fake = FakeDevice()
    _, _, dc = _make(fake)          # never connected
    dc.set_mute(True)
    assert fake.calls == []


def test_lost_during_poll_disconnects():
    fake = FakeDevice()
    sched, views, dc = _make(fake)
    dc.connect()
    fake.get_all_error = True
    sched.tick()                       # poll raises -> _lost
    assert not dc.connected
    assert views[-1].status == "Disconnected"


def test_reconnect_retries_when_device_present():
    fake = FakeDevice(connect_error=RuntimeError("not ready"))
    sched, views, dc = _make(fake)
    orig = dcmod.detect_pid
    dcmod.detect_pid = lambda: dcmod.PID_MK2   # pretend the device is attached
    try:
        dc.connect()                   # first attempt raises -> reconnect armed
        assert not dc.connected and views[-1].status == "Disconnected"
        sched.tick()                   # reconnect tick -> connect succeeds
        assert dc.connected
    finally:
        dcmod.detect_pid = orig


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
