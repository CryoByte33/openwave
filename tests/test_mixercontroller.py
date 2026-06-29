"""Unit tests for MixerController: the live mixer writes (throttled), the stream
poll, and the meter binding — driven through a FakeMixer + FakeMeter +
controllable-clock FakeScheduler and a real SourceSet. No GTK, no PipeWire.

Run: python3 tests/test_mixercontroller.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openwave.mixercontroller import MixerController
from openwave.sources import Source, SourceSet


class FakeScheduler:
    def __init__(self):
        self.timers = {}
        self._next = 0

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


class FakeMixer:
    def __init__(self, streams=None):
        self.mic = "mic_node"
        self._streams = streams or {}
        self.master = {}
        self.cell = {}
        self.master_calls = []
        self.cell_calls = []
        self.polled = 0

    def get_master(self, sid):
        return self.master.get(sid, {"volume": 1.0, "muted": False})

    def set_master(self, sid, vol, muted):
        self.master[sid] = {"volume": vol, "muted": muted}
        self.master_calls.append((sid, vol, muted))

    def get_cell(self, sid, mix):
        return self.cell.get((sid, mix), {"volume": 1.0, "muted": False})

    def set_cell(self, sid, mix, vol, muted):
        self.cell[(sid, mix)] = {"volume": vol, "muted": muted}
        self.cell_calls.append((sid, mix, vol, muted))

    def poll_streams(self):
        self.polled += 1

    def streams(self):
        return self._streams


class FakeMeter:
    def __init__(self):
        self.started = []
        self.stopped = []

    def start(self, sid, node, cb):
        self.started.append((sid, node))

    def stop(self, sid):
        self.stopped.append(sid)


def _make(mixer=None, sources=None):
    mixer = mixer or FakeMixer()
    meter = FakeMeter()
    sched = FakeScheduler()
    levels = []
    ss = sources if sources is not None else SourceSet([])
    mc = MixerController(mixer, ss, meter, sched,
                         lambda sid, lvl: levels.append((sid, lvl)))
    return mixer, meter, sched, ss, levels, mc


def test_master_volume_throttled_leading_edge_preserves_mute():
    mixer, _, _, _, _, mc = _make()
    mixer.master["g1"] = {"volume": 0.3, "muted": True}
    mc.set_master_volume("g1", 0.8)              # leading edge fires now
    assert mixer.master_calls[-1] == ("g1", 0.8, True)


def test_master_mute_preserves_volume():
    mixer, _, _, _, _, mc = _make()
    mixer.master["g1"] = {"volume": 0.4, "muted": False}
    mc.set_master_mute("g1", True)
    assert mixer.master_calls[-1] == ("g1", 0.4, True)


def test_cell_volume_throttled_preserves_mute():
    mixer, _, _, _, _, mc = _make()
    mixer.cell[("g1", "chat")] = {"volume": 0.2, "muted": True}
    mc.set_cell_volume("g1", "chat", 0.6)
    assert mixer.cell_calls[-1] == ("g1", "chat", 0.6, True)


def test_cell_mute_preserves_volume():
    mixer, _, _, _, _, mc = _make()
    mixer.cell[("g1", "personal")] = {"volume": 0.5, "muted": False}
    mc.set_cell_mute("g1", "personal", True)
    assert mixer.cell_calls[-1] == ("g1", "personal", 0.5, True)


def test_refresh_meter_starts_on_match():
    g = Source.new(name="Games", members=["Minecraft"])
    streams = {1: {"id": 1, "app_name": "Minecraft", "node_name": "node_mc"}}
    mixer, meter, _, _, _, mc = _make(FakeMixer(streams), SourceSet([g]))
    mc.refresh_meter(g.id)
    assert (g.id, "node_mc") in meter.started


def test_refresh_meter_stops_when_stream_gone():
    g = Source.new(name="Games", members=["Minecraft"])
    streams = {1: {"id": 1, "app_name": "Minecraft", "node_name": "node_mc"}}
    mixer, meter, _, _, levels, mc = _make(FakeMixer(streams), SourceSet([g]))
    mc.refresh_meter(g.id)                        # bound
    mixer._streams = {}                           # app closed
    mc.refresh_meter(g.id)
    assert g.id in meter.stopped and (g.id, 0.0) in levels


def test_poll_streams_polls_and_rebinds_meters():
    g = Source.new(name="Games", members=["Minecraft"])
    streams = {1: {"id": 1, "app_name": "Minecraft", "node_name": "node_mc"}}
    mixer, meter, _, _, _, mc = _make(FakeMixer(streams), SourceSet([g]))
    mc.poll_streams()
    assert mixer.polled == 1
    assert any(sid == g.id for sid, _ in meter.started)


def test_app_display_names_only_where_differ():
    streams = {
        1: {"id": 1, "app_name": "ALSA plug-in [java]", "display_name": "RuneLite"},
        2: {"id": 2, "app_name": "Firefox", "display_name": "Firefox"},
    }
    _, _, _, _, _, mc = _make(FakeMixer(streams))
    assert mc.app_display_names() == {"ALSA plug-in [java]": "RuneLite"}


def test_start_polling_schedules_a_tick_that_polls():
    g = Source.new(name="Games", members=["Minecraft"])
    streams = {1: {"id": 1, "app_name": "Minecraft", "node_name": "node_mc"}}
    mixer, _, sched, _, _, mc = _make(FakeMixer(streams), SourceSet([g]))
    mc.start_polling()
    sched.tick()
    assert mixer.polled == 1


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
