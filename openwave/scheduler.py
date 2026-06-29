"""Scheduler — the timing/threading seam the DeviceController runs on.

The controller never touches GLib or threads directly: it asks a Scheduler to
run blocking USB work off the main thread and to fire repeating timers, and to
marshal results back. GLibScheduler is the production adapter; a fake
(synchronous, controllable-clock) one lets the connect/poll/reconnect logic be
exercised without a GTK main loop or a real device.

Interface (duck-typed):
    run_async(fn, on_done=None, on_error=None)
        Run fn() off the main thread; deliver on_done(result) or on_error(exc)
        back on the main thread.
    call_every(interval_s, fn) -> handle
        Call fn() every interval_s seconds; fn returns True to keep going.
    cancel(handle)
        Stop a timer started by call_every.
"""

import threading

from gi.repository import GLib


class GLibScheduler:
    """Production scheduler: GLib timeouts + worker threads marshalled via idle_add."""

    def run_async(self, fn, on_done=None, on_error=None):
        def _worker():
            try:
                result = fn()
                if on_done is not None:
                    GLib.idle_add(on_done, result)
            except Exception as e:
                if on_error is not None:
                    GLib.idle_add(on_error, e)
        threading.Thread(target=_worker, daemon=True).start()

    def call_every(self, interval_s, fn):
        return GLib.timeout_add(int(interval_s * 1000), fn)

    def cancel(self, handle):
        if handle is not None:
            GLib.source_remove(handle)


class Throttler:
    """Paces rapid live updates (mixer + device sliders) so a drag doesn't flood
    the device or the audio graph: the first value fires immediately (leading),
    then at most once per interval while values keep arriving (periodic), then a
    final trailing value once they stop. Keyed by name so independent sliders
    pace independently.

    The Throttler owns only the timing; the caller's `setter` owns dispatch —
    inline for the mixer (which queues to its own worker), or off-thread with a
    connected-guard for the device. Runs on an injected Scheduler, so the pacing
    is testable with a controllable-clock fake (no GLib, no main loop)."""

    def __init__(self, scheduler, interval_s):
        self._sched = scheduler
        self._interval = interval_s
        self._pending = {}   # name -> latest value awaiting send
        self._setter = {}    # name -> callable(value)
        self._handle = {}    # name -> timer handle (None = idle)

    def push(self, name, value, setter):
        """Record the latest value for `name` and send it, paced."""
        self._pending[name] = value
        self._setter[name] = setter
        if self._handle.get(name) is None:
            self._flush(name)            # leading edge
            self._handle[name] = self._sched.call_every(
                self._interval, lambda n=name: self._tick(n))

    def _tick(self, name):
        if name in self._pending:        # value changed since last flush
            self._flush(name)
            return True                  # keep the timer alive
        self._handle[name] = None        # idle — stop ticking
        return False

    def _flush(self, name):
        if name not in self._pending:
            return
        self._setter[name](self._pending.pop(name))

    def cancel_all(self):
        for handle in self._handle.values():
            self._sched.cancel(handle)
        self._handle.clear()
