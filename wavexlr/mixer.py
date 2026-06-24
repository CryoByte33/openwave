"""Audio mixer — manages pw-loopback subprocesses for the matrix.

A loopback exists for each non-zero cell in the matrix (mic → mix), plus one
that always routes Personal Mix → Wave XLR headphones so the user hears
anything routed there. Volume + mute per cell are pushed onto the loopback's
playback node via wpctl.

State is persisted to ~/.config/openwave/mixes.json so per-cell levels survive
restarts (the loopbacks themselves do not — they're respawned by start()).
"""

import atexit
import ctypes
import json
import logging
import os
import signal
import subprocess
import threading
import time
from threading import Event, Lock

_log = logging.getLogger(__name__)

# Linux-only: make spawned children receive SIGTERM if our process dies.
# Survives SIGKILL on the parent, hard crashes, anything that skips Python
# cleanup paths. Without this, pw-loopback children leak on unclean exit.
_PR_SET_PDEATHSIG = 1
try:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
    _libc.prctl.argtypes = (
        ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
    )
    _libc.prctl.restype = ctypes.c_int
except (OSError, AttributeError):
    _libc = None


def _set_pdeathsig():
    if _libc is not None:
        _libc.prctl(_PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0)

CONFIG_PATH = os.path.expanduser("~/.config/openwave/mixes.json")

MIX_SINKS = {
    "personal": "openwave_personal_mix",
    "chat":     "openwave_chat_mix",
    "record":   "openwave_record_mix",
}
PERSONAL_MIX_SINK = "openwave_personal_mix"
HP_LOOPBACK_KEY = "_personal_to_hp"
HP_LOOPBACK_NODE = "openwave_loop_personal_to_hp"

# Chat and Record are submix buses, each exposed to apps (Discord/OBS) as a
# first-class, app-selectable capture device: a pw-loopback captures the bus
# sink's monitor and presents it as a media.class=Audio/Source node. (A raw
# sink monitor isn't shown in app mic pickers; Audio/Source forwards audio and
# is listable, whereas Audio/Source/Virtual has no output ports / no forwarding
# on PW 1.6.7.) The matrix routes sources into the sink. Personal isn't here —
# you hear it via the headphones, you don't capture it.
# Maps mix_id -> (sink_description, source_node_name, source_description).
MIX_DEVICES = {
    "chat":   ("OpenWave Chat Mix",   "openwave_chat",   "OpenWave Chat"),
    "record": ("OpenWave Record Mix", "openwave_record", "OpenWave Record"),
}


def src_sink_name(source_id):
    """Per-source virtual sink. An app source's streams are *moved* onto this
    sink (PipeWire target.object); its monitor is the source's single stable
    output that the matrix routes into the mixes. This is what makes a cell
    subtractive — the app has no path to the mixes except through here."""
    return f"openwave_src_{source_id}"


# Catch-all source: every app stream not matched by a user-defined source is
# moved here, so everything is routable instead of leaking to the default sink.
SYSTEM_SOURCE = "system"


def _pactl_short(kind):
    try:
        r = subprocess.run(
            ["pactl", "list", "short", kind],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    return [line.split("\t") for line in r.stdout.splitlines() if line.strip()]


def find_wave_xlr_alsa():
    """Return (mic_node_name, hp_node_name); either may be None if unplugged."""
    mic = next(
        (p[1] for p in _pactl_short("sources")
         if len(p) > 1 and p[1].startswith("alsa_input") and "Wave_XLR" in p[1]),
        None,
    )
    hp = next(
        (p[1] for p in _pactl_short("sinks")
         if len(p) > 1 and p[1].startswith("alsa_output") and "Wave_XLR" in p[1]),
        None,
    )
    return mic, hp


def _node_id_by_name(name, retries=20):
    """Look up a PipeWire node's global id by node.name, polling briefly so
    we don't race a just-spawned pw-loopback. Returns None if not found."""
    for _ in range(retries):
        try:
            r = subprocess.run(
                ["pw-cli", "ls", "Node"],
                capture_output=True, text=True, timeout=3,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        current_id = None
        for raw in r.stdout.splitlines():
            line = raw.strip()
            if line.startswith("id "):
                try:
                    current_id = line.split()[1].rstrip(",")
                except (IndexError, ValueError):
                    current_id = None
            elif current_id and line == f'node.name = "{name}"':
                return current_id
        time.sleep(0.05)
    return None


def _wpctl(*args):
    try:
        subprocess.run(
            ["wpctl", *args],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pass


def _ports(direction_flag, node_name):
    """Return the list of `node:port` strings for one direction of a node.

    direction_flag is '-i' (inputs) or '-o' (outputs). Filters pw-link's
    global output to ports whose node.name equals `node_name`.
    """
    try:
        r = subprocess.run(
            ["pw-link", direction_flag],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    prefix = f"{node_name}:"
    return [line.strip() for line in r.stdout.splitlines() if line.strip().startswith(prefix)]


def list_audio_streams():
    """Return [{id, app_name, media_name, node_name}, ...] for active output streams."""
    import json as _json
    try:
        r = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return []
        objects = _json.loads(r.stdout)
    except (FileNotFoundError, subprocess.SubprocessError, _json.JSONDecodeError):
        return []

    out = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        app = props.get("application.name") or props.get("node.name") or "Unknown"
        # Skip our own loopbacks
        node_name = props.get("node.name", "")
        if node_name.startswith("openwave_"):
            continue
        out.append({
            "id": obj["id"],
            "app_name": app,
            "media_name": props.get("media.name", ""),
            "node_name": node_name,
            "binary": props.get("application.process.binary", ""),
        })
    return out


class Mixer:
    """Manages pw-loopback subprocesses for the matrix's mic row."""

    def __init__(self):
        self._lock = Lock()
        self._procs = {}
        self._loop_node_ids = {}   # cell key -> loopback playback node id (cached)
        self._state = self._load_state()
        self._sources = {}
        self._streams = {}
        self._moved = set()          # stream ids we've retargeted (restore on exit)
        self._sinks_created = set()  # source ids whose src-sink is live (no dupes)
        self._started = False        # True after _do_start; gates pre-start reconciles
        self.mic, self.hp = find_wave_xlr_alsa()

        # Background worker: every operation that talks to pw-loopback /
        # pw-cli / wpctl runs here so the GTK main thread never blocks on a
        # subprocess. Pending work is a dict keyed by (kind, …) so successive
        # set_cell calls on the same cell collapse to a single reconcile.
        self._pending = {}
        self._pending_lock = Lock()
        self._wake = Event()
        self._worker_running = True
        self._worker = threading.Thread(
            target=self._worker_loop, name="openwave-mixer", daemon=True,
        )
        self._worker.start()

        # Belt-and-suspenders: even if do_shutdown is skipped, the interpreter
        # almost always runs atexit before the process image goes away.
        atexit.register(self._atexit_cleanup)

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
        capture_node_name = f"{node_name}_cap"
        try:
            proc = subprocess.Popen(
                [
                    "pw-loopback",
                    "--capture-props="
                    f"node.autoconnect=false node.name={capture_node_name} "
                    "audio.channels=2 audio.position=[FL,FR]",
                    "--playback-props="
                    f"target.object={playback_target} node.name={node_name} "
                    "audio.channels=2 audio.position=[FL,FR]",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=_set_pdeathsig,
            )
        except (FileNotFoundError, OSError):
            return
        self._procs[key] = proc
        self._link_capture(capture_source_name, capture_node_name)

    @staticmethod
    def _link_capture(source_node_name, capture_node_name, retries=20):
        """Wire each output port of `source_node_name` to a corresponding
        input port of `capture_node_name`. Mono → stereo duplicates."""
        for _ in range(retries):
            src_ports = _ports("-o", source_node_name)
            dst_ports = _ports("-i", capture_node_name)
            if src_ports and dst_ports:
                break
            time.sleep(0.05)
        else:
            return
        for i, dst in enumerate(dst_ports):
            src = src_ports[i % len(src_ports)]
            try:
                subprocess.run(
                    ["pw-link", src, dst],
                    capture_output=True, text=True, timeout=2,
                )
            except (FileNotFoundError, subprocess.SubprocessError):
                return

    def _set_loop_volume(self, key, node_name, volume, muted):
        """Apply volume/mute to a cell's loopback, caching its node id so we
        don't run a (slow) `pw-cli ls` lookup on every slider change."""
        node_id = self._loop_node_ids.get(key)
        if node_id is None:
            node_id = _node_id_by_name(node_name)
            if node_id is not None:
                self._loop_node_ids[key] = node_id
        if node_id is not None:
            _wpctl("set-volume", node_id, f"{volume:.3f}")
            _wpctl("set-mute", node_id, "1" if muted else "0")

    def _destroy_loopback(self, key):
        self._loop_node_ids.pop(key, None)
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
            try:
                subprocess.run(["pw-metadata", str(stream_id), "target.object", ""],
                               capture_output=True, timeout=1)
            except Exception:
                continue

    # ----- per-source sinks + stream moving -----
    def _app_source_ids(self):
        """App-style sources (route from a src-sink monitor): System + user."""
        return [SYSTEM_SOURCE] + list(self._sources)

    def _ensure_src_sink(self, source_id):
        """Create the source's virtual sink if it isn't live yet. Idempotent —
        the _sinks_created set prevents a registration race from making dupes."""
        if source_id in self._sinks_created:
            return
        name = src_sink_name(source_id)
        if _node_id_by_name(name, retries=3) is not None:
            self._sinks_created.add(source_id)  # adopt a leaked one from before
            return
        nm = ("System" if source_id == SYSTEM_SOURCE
              else self._sources.get(source_id, {}).get("name", source_id))
        desc = f"OpenWave: {nm}"
        try:
            subprocess.run(
                ["pw-cli", "create-node", "adapter",
                 "{ factory.name=support.null-audio-sink "
                 f"node.name={name} node.description=\"{desc}\" "
                 "media.class=Audio/Sink audio.position=[FL FR] object.linger=true }"],
                capture_output=True, text=True, timeout=5,
            )
            self._sinks_created.add(source_id)
        except (FileNotFoundError, subprocess.SubprocessError):
            pass

    def _destroy_src_sink(self, source_id):
        self._sinks_created.discard(source_id)
        nid = _node_id_by_name(src_sink_name(source_id), retries=1)
        if nid is not None:
            subprocess.run(["pw-cli", "destroy", nid], capture_output=True, text=True)

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
            if _node_id_by_name(sink, retries=2) is None:
                subprocess.run(
                    ["pw-cli", "create-node", "adapter",
                     "{ factory.name=support.null-audio-sink "
                     f"node.name={sink} node.description=\"{sink_desc}\" "
                     "media.class=Audio/Sink audio.position=[FL FR] object.linger=true }"],
                    capture_output=True, text=True, timeout=5,
                )
                time.sleep(0.3)  # let the .monitor register before the loopback binds
            key = ("mixsrc", mix_id)
            if key in self._procs:
                return
            proc = subprocess.Popen(
                ["pw-loopback",
                 "--capture-props={ stream.capture.sink=true "
                 f"target.object={sink} node.name=openwave_loop_{mix_id}_src_cap "
                 "node.passive=true audio.position=[FL FR] }",
                 "--playback-props={ media.class=Audio/Source "
                 f"node.name={src_name} node.description=\"{src_desc}\" "
                 "audio.position=[FL FR] }"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=_set_pdeathsig,
            )
            self._procs[key] = proc
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            pass

    def _move_stream(self, stream_id, sink_name):
        try:
            subprocess.run(["pw-metadata", str(stream_id), "target.object", sink_name],
                           capture_output=True, text=True, timeout=3)
            self._moved.add(stream_id)
        except (FileNotFoundError, subprocess.SubprocessError):
            pass

    def _clear_move(self, stream_id):
        try:
            subprocess.run(["pw-metadata", str(stream_id), "target.object", ""],
                           capture_output=True, text=True, timeout=3)
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
        self._moved.discard(stream_id)

    def _reconcile_streams(self):
        """Move each app output stream onto its source's sink — matched apps to
        their own source, everything else to the System catch-all."""
        with self._lock:
            sources = dict(self._sources)
            streams = dict(self._streams)
        name_to_src = {s.get("match_app_name"): sid for sid, s in sources.items()}
        for stream_id, info in streams.items():
            src = name_to_src.get(info.get("app_name"))
            self._move_stream(stream_id, src_sink_name(src if src is not None else SYSTEM_SOURCE))

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
        self._enqueue(
            ("cell", source_id, mix_id),
            lambda sid=source_id, mid=mix_id: self._reconcile_cell(sid, mid),
        )

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
        self._enqueue(
            ("master", source_id),
            lambda sid=source_id: self._reconcile_source(sid),
        )

    def set_sources(self, sources):
        """Update the app-source configuration; reconcile on worker. Before the
        mixer has started, just record them — _do_start does the initial build."""
        with self._lock:
            self._sources = dict(sources)
        if self._started:
            self._enqueue(("set_sources",), self._on_sources_changed)

    def _on_sources_changed(self):
        for source_id in self._app_source_ids():
            self._ensure_src_sink(source_id)
        self._reconcile_streams()
        self._reconcile_all()

    def remove_source(self, source_id):
        """Forget persisted cells now; tear down loopbacks on worker."""
        with self._lock:
            prefix = f"{source_id}."
            for cell_key in [k for k in self._state if k.startswith(prefix)]:
                del self._state[cell_key]
            self._save_state()
            self._sources.pop(source_id, None)
        self._enqueue(
            ("remove", source_id),
            lambda sid=source_id: self._do_remove_source(sid),
        )

    def poll_streams(self):
        """Refresh the active-stream cache; reconcile on worker if anything moved.

        Returns (added, removed) stream-id sets for the caller's bookkeeping."""
        new = {s["id"]: s for s in list_audio_streams()}
        with self._lock:
            added = set(new) - set(self._streams)
            removed = set(self._streams) - set(new)
            self._streams = new
        if added or removed:
            for sid in removed:
                self._moved.discard(sid)
            self._enqueue(("poll",), self._poll_reconcile)
        return added, removed

    def _poll_reconcile(self):
        self._reconcile_streams()
        self._reconcile_all()

    # ----- worker-side implementations -----
    def _do_start(self):
        self._started = True
        # Rebuild from a clean slate: drop anything we already spawned, then
        # sweep loopbacks leaked from a previous process. (Destroying our own
        # first keeps self._procs in sync — the sweep would otherwise leave dead
        # handles that block respawns.)
        for key in list(self._procs.keys()):
            self._destroy_loopback(key)
        self._sweep_stale_loopbacks()
        self._sweep_stale_remaps()
        # Pick up the device in case it became ready after the mixer was built.
        self.mic, self.hp = find_wave_xlr_alsa()
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
            self._streams = {s["id"]: s for s in list_audio_streams()}
        self._reconcile_streams()
        self._reconcile_all()

    def _do_refresh_device(self):
        # PipeWire/ALSA nodes lag the USB connect after a replug; poll briefly.
        mic = hp = None
        for _ in range(10):
            mic, hp = find_wave_xlr_alsa()
            if mic or hp:
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
        for mix_id in MIX_SINKS:
            self._reconcile_cell("mic", mix_id)

    def _do_remove_source(self, source_id):
        with self._lock:
            keys = [
                k for k in self._procs
                if isinstance(k, tuple) and k and k[0] == source_id
            ]
        for k in keys:
            self._destroy_loopback(k)
        # The source is already gone from self._sources, so re-homing the
        # streams moves its app onto the System catch-all (not back to default).
        self._reconcile_streams()
        self._destroy_src_sink(source_id)

    @staticmethod
    def _sweep_stale_loopbacks():
        try:
            subprocess.run(
                ["pkill", "-f", "pw-loopback.*openwave_loop_"],
                capture_output=True, timeout=2,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return
        time.sleep(0.2)  # give the kernel a beat to reap so we don't race

    @staticmethod
    def _sweep_stale_remaps():
        """Unload any module-remap-source left by older builds (we now expose
        chat/record via a pw-loopback Audio/Source); a stale module would
        collide on the same source name."""
        try:
            r = subprocess.run(
                ["pactl", "list", "modules", "short"],
                capture_output=True, text=True, timeout=3,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return
        for line in r.stdout.splitlines():
            if "module-remap-source" not in line:
                continue
            if "openwave_chat" in line or "openwave_record" in line:
                idx = line.split("\t")[0]
                subprocess.run(["pactl", "unload-module", idx], capture_output=True)

    # ----- internal -----
    def _reconcile_all(self):
        for source_id in (["mic"] + self._app_source_ids()):
            for mix_id in MIX_SINKS:
                self._reconcile_cell(source_id, mix_id)

    def _reconcile_source(self, source_id):
        """Re-apply every send for one source (after its master fader moves)."""
        for mix_id in MIX_SINKS:
            self._reconcile_cell(source_id, mix_id)

    def _apply_master(self, source_id, volume, muted):
        """Fold the source's master fader into a cell: effective level is
        cell × master, and the send is muted if either is muted."""
        m = self._state.get(f"{source_id}.master", {"volume": 1.0, "muted": False})
        return volume * m.get("volume", 1.0), (muted or m.get("muted", False))

    def _reconcile_cell(self, source_id, mix_id):
        state = self._state.get(
            f"{source_id}.{mix_id}", {"volume": 0.0, "muted": False}
        )
        if source_id == "mic":
            self._reconcile_mic_cell(mix_id, state["volume"], state["muted"])
        else:
            self._reconcile_app_cell(source_id, mix_id, state["volume"], state["muted"])

    def _reconcile_mic_cell(self, mix_id, volume, muted):
        if not self.mic:
            return
        mix_sink = MIX_SINKS.get(mix_id)
        if not mix_sink:
            return
        volume, muted = self._apply_master("mic", volume, muted)
        key = ("mic", mix_id)
        node_name = f"openwave_loop_mic_to_{mix_id}"
        if volume <= 0.0:
            self._destroy_loopback(key)
            return
        if key not in self._procs:
            self._spawn_loopback(key, self.mic, mix_sink, node_name)
        self._set_loop_volume(key, node_name, volume, muted)

    def _reconcile_app_cell(self, source_id, mix_id, volume, muted):
        # The app's streams are moved onto the source sink (see _reconcile_streams),
        # so we route the source sink's *monitor* into the mix — one stable
        # loopback per cell, regardless of how many streams the app has. This is
        # the only path from the source to the mix, so the cell is subtractive.
        if source_id != SYSTEM_SOURCE and source_id not in self._sources:
            return
        mix_sink = MIX_SINKS.get(mix_id)
        if not mix_sink:
            return
        volume, muted = self._apply_master(source_id, volume, muted)
        key = (source_id, mix_id)
        node_name = f"openwave_loop_{source_id}_to_{mix_id}"
        if volume <= 0.0:
            self._destroy_loopback(key)
            return
        if key not in self._procs:
            self._spawn_loopback(key, src_sink_name(source_id), mix_sink, node_name)
        self._set_loop_volume(key, node_name, volume, muted)
