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
