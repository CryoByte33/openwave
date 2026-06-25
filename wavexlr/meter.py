"""Per-source level metering.

For each source we want a level bar for, spawn a low-rate `pw-cat --record`
in mono s16 at 8 kHz (16 KB/s — cheap), read in a daemon thread, compute the
peak of each chunk, and marshal the value onto the GTK main thread via
GLib.idle_add. Independent of the loopback subprocess plumbing in mixer.py
to keep concerns separate.
"""

import os
import struct
import subprocess
import threading

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from .pipewire import SubprocessPipeWire


class MeterMonitor:
    SAMPLE_RATE = 8000
    CHUNK_BYTES = 256  # ~16 ms of s16 mono @ 8 kHz → ~60 Hz updates

    def __init__(self, pw=None):
        self._pw = pw or SubprocessPipeWire()
        self._procs = {}        # source_id -> Popen
        self._threads = {}      # source_id -> Thread
        self._stop_flags = {}   # source_id -> threading.Event
        self._cbs = {}          # source_id -> callable(float)

    def start(self, source_id, source_node_name, callback):
        """Begin streaming peak values for `source_id`. Replaces any existing
        meter for that id. `callback(level: float)` is invoked on the main
        thread at the chunk rate."""
        if source_id in self._procs:
            self.stop(source_id)
        proc = self._pw.spawn_capture(source_node_name, rate=self.SAMPLE_RATE)
        if proc is None:
            return

        stop_flag = threading.Event()
        thread = threading.Thread(
            target=self._reader,
            args=(source_id, proc, stop_flag),
            daemon=True,
        )
        self._procs[source_id] = proc
        self._threads[source_id] = thread
        self._stop_flags[source_id] = stop_flag
        self._cbs[source_id] = callback
        thread.start()

    def stop(self, source_id):
        flag = self._stop_flags.pop(source_id, None)
        if flag is not None:
            flag.set()
        proc = self._procs.pop(source_id, None)
        self._threads.pop(source_id, None)
        self._cbs.pop(source_id, None)
        if proc is None:
            return
        try:
            proc.terminate()
        except (OSError, ProcessLookupError):
            return
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
                pass

    def stop_all(self):
        for sid in list(self._procs.keys()):
            self.stop(sid)

    def _reader(self, source_id, proc, stop_flag):
        """Background thread: read s16 chunks, compute peak, marshal to UI."""
        try:
            while not stop_flag.is_set():
                data = proc.stdout.read(self.CHUNK_BYTES)
                if not data or len(data) < 2:
                    break
                n = len(data) // 2
                samples = struct.unpack(f"<{n}h", data[: n * 2])
                peak = max(abs(s) for s in samples) / 32768.0
                GLib.idle_add(self._dispatch, source_id, peak)
        except (OSError, ValueError):
            pass
        # Final zero so the UI doesn't get stuck on the last value when the
        # subprocess dies (mic unplugged, app closed, etc.)
        GLib.idle_add(self._dispatch, source_id, 0.0)

    def _dispatch(self, source_id, peak):
        cb = self._cbs.get(source_id)
        if cb is not None:
            cb(peak)
        return False  # one-shot idle handler
