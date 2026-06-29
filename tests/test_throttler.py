"""Unit tests for the live-slider Throttler (no GLib, no main loop).

Run: python3 tests/test_throttler.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openwave.scheduler import Throttler


class FakeScheduler:
    """Controllable-clock scheduler: timers fire only when tick() is called."""

    def __init__(self):
        self._timers = {}
        self._next = 0

    def call_every(self, interval_s, fn):
        h = self._next
        self._next += 1
        self._timers[h] = fn
        return h

    def cancel(self, handle):
        self._timers.pop(handle, None)

    def tick(self):
        for h, fn in list(self._timers.items()):
            if fn() is False:
                self._timers.pop(h, None)

    def active(self):
        return len(self._timers)


def test_leading_edge_fires_immediately():
    sched = FakeScheduler()
    sent = []
    Throttler(sched, 0.08).push("a", 1, sent.append)
    assert sent == [1]              # fired without any tick
    assert sched.active() == 1      # timer armed for the trailing edge


def test_coalesces_until_tick():
    sched = FakeScheduler()
    t = Throttler(sched, 0.08)
    sent = []
    t.push("a", 1, sent.append)     # leading -> 1
    t.push("a", 2, sent.append)     # coalesced
    t.push("a", 3, sent.append)     # coalesced
    assert sent == [1]
    sched.tick()                    # periodic -> latest only
    assert sent == [1, 3]


def test_trailing_then_idle_stops_and_rearms():
    sched = FakeScheduler()
    t = Throttler(sched, 0.08)
    sent = []
    t.push("a", 1, sent.append)     # leading -> 1
    t.push("a", 2, sent.append)
    sched.tick()                    # trailing -> 2
    assert sent == [1, 2]
    sched.tick()                    # nothing pending -> timer stops
    assert sent == [1, 2]
    assert sched.active() == 0
    t.push("a", 9, sent.append)     # a fresh drag re-fires the leading edge
    assert sent == [1, 2, 9]
    assert sched.active() == 1


def test_independent_keys():
    sched = FakeScheduler()
    t = Throttler(sched, 0.08)
    a, b = [], []
    t.push("gain", 1, a.append)
    t.push("hp", 10, b.append)
    assert a == [1] and b == [10]
    assert sched.active() == 2      # each key paces on its own timer


def test_cancel_all():
    sched = FakeScheduler()
    t = Throttler(sched, 0.08)
    t.push("a", 1, lambda v: None)
    t.push("b", 2, lambda v: None)
    assert sched.active() == 2
    t.cancel_all()
    assert sched.active() == 0


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
