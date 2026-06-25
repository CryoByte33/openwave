"""PipeWire adapter — the one place OpenWave shells out to the audio graph.

``SubprocessPipeWire`` owns every ``pw-*`` / ``wpctl`` / ``pactl`` / ``pkill``
invocation, its retry/poll, and its text parsing; callers cross this interface
instead of building command lines. The adapter is name-agnostic — node names
come from :mod:`wavexlr.pwnames`. The methods are instance methods on purpose:
a ``FakePipeWire`` backed by an in-memory graph can satisfy the same interface
for tests (not built yet — see the architecture review).
"""

import ctypes
import json
import signal
import subprocess
import time

from .pwnames import LOOPBACK_SWEEP, is_ours

# Linux-only: make spawned children receive SIGTERM if our process dies. Survives
# SIGKILL on the parent, hard crashes, anything that skips Python cleanup paths.
# Without this, pw-loopback children leak on an unclean exit.
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


def output_streams():
    """[{id, app_name, media_name, node_name, binary}, ...] for active app
    output streams. Our own openwave_* nodes are excluded. Module-level so the
    add-source dialog can enumerate apps without holding an adapter."""
    try:
        r = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        objects = json.loads(r.stdout)
    except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError):
        return []

    out = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        node_name = props.get("node.name", "")
        if is_ours(node_name):
            continue
        out.append({
            "id": obj["id"],
            "app_name": props.get("application.name") or node_name or "Unknown",
            "media_name": props.get("media.name", ""),
            "node_name": node_name,
            "binary": props.get("application.process.binary", ""),
        })
    return out


class SubprocessPipeWire:
    """Talks to the live PipeWire graph via the pw-*/wpctl/pactl CLIs."""

    # ----- queries -----
    def short_list(self, kind):
        """`pactl list short <kind>` split into columns, or [] on failure."""
        try:
            r = subprocess.run(
                ["pactl", "list", "short", kind],
                capture_output=True, text=True, timeout=3,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return []
        return [line.split("\t") for line in r.stdout.splitlines() if line.strip()]

    def node_id(self, name, retries=20):
        """Global id of the node named `name`, polling briefly so we don't race
        a just-spawned pw-loopback. None if not found."""
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

    def ports(self, direction_flag, node_name):
        """`node:port` strings for one direction of a node. direction_flag is
        '-i' (inputs) or '-o' (outputs)."""
        try:
            r = subprocess.run(
                ["pw-link", direction_flag],
                capture_output=True, text=True, timeout=3,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return []
        prefix = f"{node_name}:"
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith(prefix)]

    def output_streams(self):
        return output_streams()

    # ----- mutations -----
    def create_null_sink(self, name, description):
        """Create a lingering virtual Audio/Sink. Best-effort; returns success."""
        try:
            subprocess.run(
                ["pw-cli", "create-node", "adapter",
                 "{ factory.name=support.null-audio-sink "
                 f"node.name={name} node.description=\"{description}\" "
                 "media.class=Audio/Sink audio.position=[FL FR] object.linger=true }"],
                capture_output=True, text=True, timeout=5,
            )
            return True
        except (FileNotFoundError, subprocess.SubprocessError):
            return False

    def destroy_node(self, node_id):
        try:
            subprocess.run(["pw-cli", "destroy", node_id], capture_output=True, text=True)
        except (FileNotFoundError, subprocess.SubprocessError):
            pass

    def set_node_volume(self, node_id, volume, muted):
        """Apply volume (0..1) and mute to a node via wpctl."""
        for args in (("set-volume", node_id, f"{volume:.3f}"),
                     ("set-mute", node_id, "1" if muted else "0")):
            try:
                subprocess.run(["wpctl", *args], capture_output=True, text=True, timeout=3)
            except (FileNotFoundError, subprocess.SubprocessError):
                pass

    def link(self, src_port, dst_port):
        try:
            subprocess.run(["pw-link", src_port, dst_port],
                           capture_output=True, text=True, timeout=2)
        except (FileNotFoundError, subprocess.SubprocessError):
            pass

    def move_stream(self, stream_id, sink_name):
        """Retarget a stream onto `sink_name` (PipeWire target.object)."""
        try:
            subprocess.run(["pw-metadata", str(stream_id), "target.object", sink_name],
                           capture_output=True, text=True, timeout=3)
            return True
        except (FileNotFoundError, subprocess.SubprocessError):
            return False

    def clear_stream(self, stream_id):
        """Return a stream to its default output."""
        try:
            subprocess.run(["pw-metadata", str(stream_id), "target.object", ""],
                           capture_output=True, text=True, timeout=3)
        except (FileNotFoundError, subprocess.SubprocessError):
            pass

    def unload_remap_modules(self, names):
        """Unload any module-remap-source whose args mention one of `names`
        (cleans up devices left by older OpenWave builds)."""
        try:
            r = subprocess.run(["pactl", "list", "modules", "short"],
                               capture_output=True, text=True, timeout=3)
        except (FileNotFoundError, subprocess.SubprocessError):
            return
        for line in r.stdout.splitlines():
            if "module-remap-source" not in line:
                continue
            if any(nm in line for nm in names):
                subprocess.run(["pactl", "unload-module", line.split("\t")[0]],
                               capture_output=True)

    def sweep_loopbacks(self):
        """Kill any of our loopbacks leaked by a previous process."""
        try:
            subprocess.run(["pkill", "-f", LOOPBACK_SWEEP], capture_output=True, timeout=2)
        except (FileNotFoundError, subprocess.SubprocessError):
            return
        time.sleep(0.2)  # give the kernel a beat to reap so we don't race

    # ----- process lifecycle -----
    def spawn_loopback(self, capture_props, playback_props):
        """Spawn a pw-loopback with the given prop strings; returns the Popen
        (the caller tracks and terminates it) or None if pw-loopback is absent."""
        try:
            return subprocess.Popen(
                ["pw-loopback",
                 f"--capture-props={capture_props}",
                 f"--playback-props={playback_props}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=_set_pdeathsig,
            )
        except (FileNotFoundError, OSError):
            return None
