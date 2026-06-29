"""Unit tests for the mixer's reconcile step: given a RoutingPlan, does it issue
the right PipeWire calls and apply only the deltas? Driven through a FakePipeWire
with start_worker=False, so there's no background thread and no real audio graph.

Run: python3 tests/test_mixer_reconcile.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openwave.mixer as mixer_mod
from openwave.mixer import Mixer
from openwave.routing import RoutingPlan, Send

# Never touch the user's real mixes.json while constructing test mixers.
mixer_mod.CONFIG_PATH = "/tmp/openwave-test-nonexistent-mixes.json"


class FakeProc:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


class FakePipeWire:
    """Records every graph call so a reconcile can be asserted exactly."""

    def __init__(self):
        self.moves = []
        self.cleared = []
        self.spawns = []
        self.links = []
        self.volumes = []
        self._nid = 100

    # used during Mixer.__init__ (_find_alsa) — no Wave XLR in the fake graph
    def short_list(self, kind):
        return []

    def move_stream(self, sid, sink):
        self.moves.append((sid, sink))
        return True

    def clear_stream(self, sid):
        self.cleared.append(sid)

    def spawn_loopback(self, cap_props, play_props):
        p = FakeProc()
        self.spawns.append((cap_props, play_props, p))
        return p

    def ports(self, direction, node):
        return [f"{node}:port"]

    def link(self, src, dst):
        self.links.append((src, dst))

    def node_id(self, name, retries=0):
        self._nid += 1
        return self._nid

    def set_node_volume(self, nid, vol, muted):
        self.volumes.append((nid, vol, muted))


def _mixer():
    fake = FakePipeWire()
    return fake, Mixer(pw=fake, start_worker=False)


def _send(src="g1", mix="personal", vol=0.5, muted=False):
    return Send(src, mix, f"cap_{src}", f"sink_{mix}", vol, muted)


def test_reconcile_spawns_and_volumes_a_send():
    fake, m = _mixer()
    m._reconcile(RoutingPlan(sends=(_send(),), moves={}))
    assert len(fake.spawns) == 1
    assert len(fake.volumes) == 1 and fake.volumes[-1][1:] == (0.5, False)
    assert ("g1", "personal") in m._procs


def test_reconcile_is_idempotent():
    fake, m = _mixer()
    plan = RoutingPlan(sends=(_send(),), moves={})
    m._reconcile(plan)
    m._reconcile(plan)                       # same plan again
    assert len(fake.spawns) == 1             # not respawned
    assert len(fake.volumes) == 1            # not re-volumed


def test_reconcile_revolumes_without_respawn():
    fake, m = _mixer()
    m._reconcile(RoutingPlan(sends=(_send(vol=0.5),), moves={}))
    m._reconcile(RoutingPlan(sends=(_send(vol=0.8),), moves={}))
    assert len(fake.spawns) == 1             # same loopback
    assert len(fake.volumes) == 2 and fake.volumes[-1][1:] == (0.8, False)


def test_reconcile_destroys_a_dropped_send():
    fake, m = _mixer()
    m._reconcile(RoutingPlan(sends=(_send(),), moves={}))
    proc = m._procs[("g1", "personal")]
    m._reconcile(RoutingPlan(sends=(), moves={}))
    assert proc.terminated
    assert ("g1", "personal") not in m._procs


def test_reconcile_moves_streams_only_on_change():
    fake, m = _mixer()
    m._reconcile(RoutingPlan(sends=(), moves={1: "sink_a"}))
    m._reconcile(RoutingPlan(sends=(), moves={1: "sink_a"}))   # unchanged
    assert fake.moves == [(1, "sink_a")]
    m._reconcile(RoutingPlan(sends=(), moves={1: "sink_b"}))   # retargeted
    assert fake.moves[-1] == (1, "sink_b")


def test_reconcile_forgets_vanished_move():
    fake, m = _mixer()
    m._reconcile(RoutingPlan(sends=(), moves={1: "sink_a"}))
    m._reconcile(RoutingPlan(sends=(), moves={}))              # stream gone
    assert 1 not in m._move_targets


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
