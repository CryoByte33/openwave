"""Routing plan — the pure, declarative core of the submix engine.

Given the current state (the sources, the persisted cell/master levels, the mic
node, and the live app streams), :func:`plan` computes the *desired* routing as
data: the cell sends that should exist with their effective volume/mute, and
where each app stream should be moved. There is no I/O here — the mixer's
executor diffs this plan against what is live and applies only the deltas. That
keeps the routing rules unit-testable without spawning a single process.
"""

from dataclasses import dataclass

from .pwnames import MIX_SINKS, src_sink

# The catch-all source: every app stream not matched by a user-defined source is
# routed here, so everything stays routable instead of leaking to the default.
SYSTEM_SOURCE = "system"
# The mic isn't an app source — it's captured from the Wave XLR directly.
MIC = "mic"

_DEFAULT_CELL = {"volume": 0.0, "muted": False}
_DEFAULT_MASTER = {"volume": 1.0, "muted": False}


@dataclass(frozen=True)
class Send:
    """One cell loopback that should exist: carry `capture`'s audio into
    `target` (a mix-bus sink) at the given effective volume/mute."""
    source_id: str
    mix_id: str
    capture: str
    target: str
    volume: float
    muted: bool

    @property
    def key(self):
        return (self.source_id, self.mix_id)


@dataclass(frozen=True)
class RoutingPlan:
    sends: tuple        # the cell loopbacks that should exist (tuple[Send])
    moves: dict         # stream_id -> the sink it should be moved onto


def effective_send(state, source_id, mix_id):
    """A cell's effective level: cell × master, muted if either is muted."""
    c = state.get(f"{source_id}.{mix_id}", _DEFAULT_CELL)
    m = state.get(f"{source_id}.master", _DEFAULT_MASTER)
    return c["volume"] * m.get("volume", 1.0), c["muted"] or m.get("muted", False)


def plan(sources, state, mic, streams):
    """Compute the desired routing.

    `sources` is a SourceSet, `state` the persisted cell/master dict, `mic` the
    Wave XLR node name (or None when unplugged), `streams` a {id: info} map of
    live app output streams. Returns a RoutingPlan; performs no I/O.
    """
    sends = []
    for source_id in [MIC, SYSTEM_SOURCE] + sources.ids():
        if source_id == MIC:
            if not mic:
                continue            # no device → no mic sends
            capture = mic
        else:
            capture = src_sink(source_id)   # the source's private sink monitor
        for mix_id in MIX_SINKS:
            volume, muted = effective_send(state, source_id, mix_id)
            if volume > 0.0:        # a zero send has no loopback at all
                sends.append(Send(source_id, mix_id, capture,
                                  MIX_SINKS[mix_id], volume, muted))
    moves = {
        stream_id: src_sink(sources.source_for(info) or SYSTEM_SOURCE)
        for stream_id, info in streams.items()
    }
    return RoutingPlan(tuple(sends), moves)
