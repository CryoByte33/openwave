"""Audio mixer — the GoXLR/Wave-Link-style submix engine.

Each app's output stream is *moved* (PipeWire target.object) onto a per-source
null sink, so the source has one stable output; per-cell pw-loopback
subprocesses route those monitors into the Personal/Chat/Record mix buses. That
makes a matrix cell subtractive — lowering or muting it removes the source from
the mix. The mic is captured from the Wave XLR directly. Personal feeds the
headphones; Chat/Record are exposed to apps as selectable capture devices. Each
source also has a master fader (effective send = cell × master).

Every operation that talks to pw-loopback / pw-cli / wpctl runs on a single
background worker so the GTK main thread never blocks. Per-cell and master
levels persist to ~/.config/openwave/mixes.json; the loopbacks themselves do
not — they're (re)spawned by start() and refresh_device().
"""

import atexit
import json
import logging
import os
import subprocess
import threading
import time
from threading import Event, Lock

from .pipewire import SubprocessPipeWire
from .pwnames import (
    CAPTURE_SOURCE_NAMES, HP_LOOPBACK_NODE, MIX_DEVICES, MIX_SINKS,
    PERSONAL_MIX_SINK, WAVE_XLR_MATCH, cap_node, capture_src_cap, cell_loop,
    src_sink,
)
from .routing import SYSTEM_SOURCE, plan
from .sources import SourceSet


def _is_cell_key(key):
    """Cell-loopback keys are (source_id, mix_id). The HP loopback's key is a
    str and the capture devices use ('mixsrc', mix_id), so neither matches."""
    return isinstance(key, tuple) and len(key) == 2 and key[0] != "mixsrc"

_log = logging.getLogger(__name__)

CONFIG_PATH = os.path.expanduser("~/.config/openwave/mixes.json")

# Key into self._procs for the always-on Personal→headphones loopback (not a
# node name — see pwnames.HP_LOOPBACK_NODE for that).
HP_LOOPBACK_KEY = "_personal_to_hp"


class Mixer:
    """Owns the submix routing: per-source sinks + stream moves, the per-cell
    and mic→mix loopbacks, the Personal→HP loopback, the Chat/Record capture
    devices, and per-source master faders. Public methods return immediately;
    the pw-loopback/pw-cli/wpctl work runs on one background worker thread."""

    def __init__(self, pw=None, start_worker=True):
        # The PipeWire adapter is the only thing that touches the audio graph;
        # injectable so a FakePipeWire can drive the routing logic in tests.
        self._pw = pw or SubprocessPipeWire()
        self._lock = Lock()
        self._procs = {}
        self._loop_node_ids = {}   # cell key -> loopback playback node id (cached)
        self._cell_state = {}      # cell key -> (volume, muted) last applied
        self._move_targets = {}    # stream id -> sink last applied (move diff)
        self._state = self._load_state()
        self._sources = SourceSet()
        self._streams = {}
        self._moved = set()          # stream ids we've retargeted (restore on exit)
        self._sinks_created = set()  # source ids whose src-sink is live (no dupes)
        self._started = False        # True after _do_start; gates pre-start reconciles
        self.mic, self.hp = self._find_alsa()

        # Background worker: every operation that talks to pw-loopback /
        # pw-cli / wpctl runs here so the GTK main thread never blocks on a
        # subprocess. Pending work is a dict keyed by (kind, …) so successive
        # set_cell calls on the same cell collapse to a single reconcile.
        self._pending = {}
        self._pending_lock = Lock()
        self._wake = Event()
        self._worker_running = True
        self._worker = None
        # start_worker=False lets a test construct the mixer and drive _reconcile
        # directly — no background thread, no atexit cleanup.
        if start_worker:
            self._worker = threading.Thread(
                target=self._worker_loop, name="openwave-mixer", daemon=True,
            )
            self._worker.start()
            # Belt-and-suspenders: even if do_shutdown is skipped, the interpreter
            # almost always runs atexit before the process image goes away.
            atexit.register(self._atexit_cleanup)

    def _find_alsa(self):
        """Return (mic_node_name, hp_node_name) for the Wave XLR; either may be
        None if unplugged."""
        mic = next(
            (p[1] for p in self._pw.short_list("sources")
             if len(p) > 1 and p[1].startswith("alsa_input") and WAVE_XLR_MATCH in p[1]),
            None,
        )
        hp = next(
            (p[1] for p in self._pw.short_list("sinks")
             if len(p) > 1 and p[1].startswith("alsa_output") and WAVE_XLR_MATCH in p[1]),
            None,
        )
        return mic, hp

    # ----- worker thread -----
    def _enqueue(self, key, task):
        """Coalesce a task by key. Latest task for the same key wins."""
        with self._pending_lock:
            self._pending[key] = task
            self._wake.set()

    def _worker_loop(self):
        while self._worker_running:
            self._wake.wait(timeout=1.0)
            while True:
                with self._pending_lock:
                    if not self._pending:
                        self._wake.clear()
                        break
                    key = next(iter(self._pending))
                    task = self._pending.pop(key)
                try:
                    task()
                except Exception:
                    _log.exception("mixer task failed: %s", key)
            if not self._worker_running:
                return

    # ----- persistence -----
    def _load_state(self):
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._state, f, indent=2)
        os.replace(tmp, CONFIG_PATH)

    def get_cell(self, source_id, mix_id):
        return self._state.get(
            f"{source_id}.{mix_id}", {"volume": 0.0, "muted": False}
        )

    def cells(self):
        return dict(self._state)

    def streams(self):
        """Snapshot of currently-known PipeWire output streams (id → info)."""
        with self._lock:
            return dict(self._streams)

    # ----- subprocess lifecycle -----
    def _spawn_loopback(self, key, capture_source_name, playback_target, node_name):
        """Spawn a pw-loopback and *manually* link the capture side to
        `capture_source_name`'s output ports. We disable autoconnect on capture
        because the session manager will otherwise hijack the loopback by
        wiring the default source (the Wave XLR mic) into it whenever
        target.object can't be resolved to a Source node — which is exactly
        the case for null-sink monitors. The link is set up after a brief
        wait so the node has time to register.
        """
        if key in self._procs:
            return
        capture_node_name = cap_node(node_name)
        proc = self._pw.spawn_loopback(
            f"node.autoconnect=false node.name={capture_node_name} "
            "audio.channels=2 audio.position=[FL,FR]",
            f"target.object={playback_target} node.name={node_name} "
            "audio.channels=2 audio.position=[FL,FR]",
        )
        if proc is None:
            return
        self._procs[key] = proc
        self._link_capture(capture_source_name, capture_node_name)

    def _link_capture(self, source_node_name, capture_node_name, retries=20):
        """Wire each output port of `source_node_name` to a corresponding
        input port of `capture_node_name`. Mono → stereo duplicates."""
        for _ in range(retries):
            src_ports = self._pw.ports("-o", source_node_name)
            dst_ports = self._pw.ports("-i", capture_node_name)
            if src_ports and dst_ports:
                break
            time.sleep(0.05)
        else:
            return
        for i, dst in enumerate(dst_ports):
            self._pw.link(src_ports[i % len(src_ports)], dst)

    def _set_loop_volume(self, key, node_name, volume, muted):
        """Apply volume/mute to a cell's loopback, caching its node id so we
        don't run a (slow) `pw-cli ls` lookup on every slider change."""
        node_id = self._loop_node_ids.get(key)
        if node_id is None:
            node_id = self._pw.node_id(node_name)
            if node_id is not None:
                self._loop_node_ids[key] = node_id
        if node_id is not None:
            self._pw.set_node_volume(node_id, volume, muted)

    def _destroy_loopback(self, key):
        self._loop_node_ids.pop(key, None)
        self._cell_state.pop(key, None)
        proc = self._procs.pop(key, None)
        if proc is None:
            return
        try:
            proc.terminate()
        except (OSError, ProcessLookupError):
            return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
                pass

    def _atexit_cleanup(self):
        """Fast best-effort tear-down on interpreter exit. No locking, no waits."""
        for proc in list(self._procs.values()):
            try:
                proc.terminate()
            except (OSError, ProcessLookupError):
                continue
        self._procs.clear()
        # Return any moved app streams to their default output so audio isn't
        # left stranded on a (soon-to-vanish) source sink.
        for stream_id in list(self._moved):
            self._pw.clear_stream(stream_id)

    # ----- per-source sinks + stream moving -----
    def _app_source_ids(self):
        """App-style sources (route from a src-sink monitor): System + user."""
        return [SYSTEM_SOURCE] + self._sources.ids()

    def _ensure_src_sink(self, source_id):
        """Create the source's virtual sink if it isn't live yet. Idempotent —
        the _sinks_created set prevents a registration race from making dupes."""
        if source_id in self._sinks_created:
            return
        name = src_sink(source_id)
        if self._pw.node_id(name, retries=3) is not None:
            self._sinks_created.add(source_id)  # adopt a leaked one from before
            return
        source = self._sources.get(source_id)
        nm = ("System" if source_id == SYSTEM_SOURCE
              else source.name if source is not None else source_id)
        if self._pw.create_null_sink(name, f"OpenWave: {nm}"):
            self._sinks_created.add(source_id)

    def _destroy_src_sink(self, source_id):
        self._sinks_created.discard(source_id)
        nid = self._pw.node_id(src_sink(source_id), retries=1)
        if nid is not None:
            self._pw.destroy_node(nid)

    def _ensure_mix_device(self, mix_id):
        """Ensure a chat/record submix bus exists as a null sink, plus a
        loopback that exposes its monitor as a first-class, app-selectable
        capture device (Discord/OBS list it like any mic; a bare monitor isn't
        listed). The matrix routes sources into the sink; the loopback captures
        the sink monitor and presents an Audio/Source — which forwards audio and
        takes a spaced node.description, unlike Audio/Source/Virtual."""
        spec = MIX_DEVICES.get(mix_id)
        if spec is None:
            return
        sink_desc, src_name, src_desc = spec
        sink = MIX_SINKS[mix_id]
        try:
            if self._pw.node_id(sink, retries=2) is None:
                self._pw.create_null_sink(sink, sink_desc)
                time.sleep(0.3)  # let the .monitor register before the loopback binds
            key = ("mixsrc", mix_id)
            if key in self._procs:
                return
            proc = self._pw.spawn_loopback(
                "{ stream.capture.sink=true "
                f"target.object={sink} node.name={capture_src_cap(mix_id)} "
                "node.passive=true audio.position=[FL FR] }",
                "{ media.class=Audio/Source "
                f"node.name={src_name} node.description=\"{src_desc}\" "
                "audio.position=[FL FR] }",
            )
            if proc is not None:
                self._procs[key] = proc
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            pass

    def _move_stream(self, stream_id, sink_name):
        if self._pw.move_stream(stream_id, sink_name):
            self._moved.add(stream_id)

    def _clear_move(self, stream_id):
        self._pw.clear_stream(stream_id)
        self._moved.discard(stream_id)

    # Below this, the slider snaps to 0 — sub-1% values keep the loopback alive
    # at imperceptible-but-not-silent volume and confuse "I put it back to 0".
    _ZERO_THRESHOLD = 0.01

    # ----- public API (returns immediately; subprocess work runs on worker) -----
    def start(self):
        """Spawn always-on Personal→HP loopback, snapshot streams, restore cells."""
        self._enqueue(("start",), self._do_start)

    def refresh_device(self):
        """Re-detect the Wave XLR ALSA nodes and rebuild the mic/HP loopbacks.
        Call after a (re)connect/hotplug: node names can change and the old
        loopbacks break when the device vanishes. App sources are untouched —
        their sinks and moves don't depend on the device."""
        self._enqueue(("refresh_device",), self._do_refresh_device)

    def stop(self):
        """Stop the worker and tear down every loopback. Brief block expected."""
        self._worker_running = False
        self._wake.set()
        try:
            if self._worker is not None:
                self._worker.join(timeout=3)
        except RuntimeError:
            pass
        with self._lock:
            for key in list(self._procs.keys()):
                self._destroy_loopback(key)
            sources = self._app_source_ids()
        # Return moved app streams to default, then remove the source sinks.
        for stream_id in list(self._moved):
            self._clear_move(stream_id)
        for source_id in sources:
            self._destroy_src_sink(source_id)

    def set_cell(self, source_id, mix_id, volume, muted):
        """Persist state synchronously; reconcile the cell on the worker."""
        volume = max(0.0, min(1.0, float(volume)))
        if volume < self._ZERO_THRESHOLD:
            volume = 0.0
        with self._lock:
            self._state[f"{source_id}.{mix_id}"] = {
                "volume": volume, "muted": bool(muted),
            }
            self._save_state()
        self._enqueue(("apply",), self._apply_plan)

    def get_master(self, source_id):
        """Per-source master level (GoXLR channel fader). Scales every send."""
        return self._state.get(f"{source_id}.master", {"volume": 1.0, "muted": False})

    def set_master(self, source_id, volume, muted):
        """Persist the source's master level; re-reconcile all its sends so each
        cell's effective level becomes cell × master."""
        volume = max(0.0, min(1.0, float(volume)))
        if volume < self._ZERO_THRESHOLD:
            volume = 0.0
        with self._lock:
            self._state[f"{source_id}.master"] = {
                "volume": volume, "muted": bool(muted),
            }
            self._save_state()
        self._enqueue(("apply",), self._apply_plan)

    def set_sources(self, sources):
        """Update the app-source configuration; reconcile on worker. Before the
        mixer has started, just record them — _do_start does the initial build."""
        with self._lock:
            self._sources = SourceSet(sources)
        if self._started:
            self._enqueue(("set_sources",), self._on_sources_changed)

    def _on_sources_changed(self):
        for source_id in self._app_source_ids():
            self._ensure_src_sink(source_id)
        self._apply_plan()

    def remove_source(self, source_id):
        """Forget persisted cells now; tear down loopbacks on worker."""
        with self._lock:
            prefix = f"{source_id}."
            for cell_key in [k for k in self._state if k.startswith(prefix)]:
                del self._state[cell_key]
            self._save_state()
            self._sources.discard(source_id)
        self._enqueue(
            ("remove", source_id),
            lambda sid=source_id: self._do_remove_source(sid),
        )

    def poll_streams(self):
        """Refresh the active-stream cache; reconcile on worker if anything moved.

        Returns (added, removed) stream-id sets for the caller's bookkeeping."""
        new = {s["id"]: s for s in self._pw.output_streams()}
        with self._lock:
            added = set(new) - set(self._streams)
            removed = set(self._streams) - set(new)
            self._streams = new
        if added or removed:
            for sid in removed:
                self._moved.discard(sid)
            self._enqueue(("apply",), self._apply_plan)
        return added, removed

    # ----- worker-side implementations -----
    def _do_start(self):
        self._started = True
        # Rebuild from a clean slate: drop anything we already spawned, then
        # sweep loopbacks leaked from a previous process. (Destroying our own
        # first keeps self._procs in sync — the sweep would otherwise leave dead
        # handles that block respawns.)
        for key in list(self._procs.keys()):
            self._destroy_loopback(key)
        self._move_targets.clear()   # re-assert every move from a clean slate
        self._pw.sweep_loopbacks()
        self._pw.unload_remap_modules(CAPTURE_SOURCE_NAMES)
        # Pick up the device in case it became ready after the mixer was built.
        self.mic, self.hp = self._find_alsa()
        for source_id in self._app_source_ids():
            self._ensure_src_sink(source_id)
        # Ensure the chat/record submix sinks exist before reconciling so the
        # cell loopbacks have a sink to route into.
        for mix_id in MIX_DEVICES:
            self._ensure_mix_device(mix_id)
        if self.hp:
            self._spawn_loopback(
                HP_LOOPBACK_KEY, PERSONAL_MIX_SINK, self.hp, HP_LOOPBACK_NODE,
            )
        with self._lock:
            self._streams = {s["id"]: s for s in self._pw.output_streams()}
        self._apply_plan()

    def _do_refresh_device(self):
        # PipeWire/ALSA nodes lag the USB connect after a replug; poll up to ~3s.
        # Wait for the mic AND the headphones — they're the same device, so
        # acting on whichever registers first would drop the other's loopback.
        mic = hp = None
        for _ in range(10):
            mic, hp = self._find_alsa()
            if mic and hp:
                break
            time.sleep(0.3)
        self.mic, self.hp = mic, hp
        # The HP + mic loopbacks bind to ALSA nodes that change across a replug,
        # so tear them down and respawn against the current device.
        self._destroy_loopback(HP_LOOPBACK_KEY)
        for mix_id in MIX_SINKS:
            self._destroy_loopback(("mic", mix_id))
        if self.hp:
            self._spawn_loopback(
                HP_LOOPBACK_KEY, PERSONAL_MIX_SINK, self.hp, HP_LOOPBACK_NODE,
            )
        self._apply_plan()   # respawn the mic sends against the new device

    def _do_remove_source(self, source_id):
        # The source is already gone from self._sources, so the plan no longer
        # includes its cell loopbacks (dropped here) and re-homes its app's
        # streams onto the System catch-all.
        self._apply_plan()
        self._destroy_src_sink(source_id)

    # ----- internal: declarative apply -----
    def _apply_plan(self):
        """Snapshot the current state, compute the desired routing
        (routing.plan), and reconcile the live graph to it. The single reconcile
        path — every source/cell/master/stream change funnels here."""
        with self._lock:
            sources = SourceSet(self._sources)
            streams = dict(self._streams)
            state = dict(self._state)
        self._reconcile(plan(sources, state, self.mic, streams))

    def _reconcile(self, p):
        """Diff a RoutingPlan against the live graph and apply only the deltas:
        move app streams onto their source's sink (whose monitor each cell
        loopback carries into a mix — what makes a cell subtractive), then
        add/drop/re-volume the cell loopbacks. Talks to the graph only through
        self._pw, so a FakePipeWire can verify exactly which calls a plan
        produces — no threads, no subprocesses."""
        # Stream moves — only where the target sink changed.
        for stream_id, sink in p.moves.items():
            if self._move_targets.get(stream_id) != sink:
                self._move_stream(stream_id, sink)
                self._move_targets[stream_id] = sink
        for stream_id in [s for s in self._move_targets if s not in p.moves]:
            self._move_targets.pop(stream_id, None)   # stream is gone

        # Cell loopbacks — destroy the ones no longer wanted, spawn the new
        # ones, re-volume only those whose effective level actually changed.
        desired = {s.key: s for s in p.sends}
        for key in [k for k in self._procs if _is_cell_key(k) and k not in desired]:
            self._destroy_loopback(key)
        for key, send in desired.items():
            node_name = cell_loop(send.source_id, send.mix_id)
            if key not in self._procs:
                self._spawn_loopback(key, send.capture, send.target, node_name)
            if self._cell_state.get(key) != (send.volume, send.muted):
                self._set_loop_volume(key, node_name, send.volume, send.muted)
                self._cell_state[key] = (send.volume, send.muted)
