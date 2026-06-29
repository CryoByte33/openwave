"""MixerController — GTK-free runtime control for the submixer.

Owns the live mixer writes (master + per-cell volume/mute, throttled like the
device sliders), the stream poll, and the meter binding. The window only wires
slider signals to these methods and renders levels through the on_level
callback. This is the mixer-side mirror of DeviceController: same Scheduler seam,
same Throttler, exercised with a FakeMixer + fake scheduler + fake meter — no
GTK, no PipeWire.

Channel lifecycle (add / remove / rename / ungroup) stays in the window: those
are dialog + strip orchestration. They call poll_streams() / refresh_meter() /
stop_meter() here so metering follows the edit.
"""

from .pwnames import src_sink
from .routing import SYSTEM_SOURCE
from .scheduler import Throttler

_POLL_S = 2.0          # stream hotplug poll
_THROTTLE_S = 0.08     # live-slider throttle (matches the device sliders)


class MixerController:
    def __init__(self, mixer, sources, meter, scheduler, on_level):
        self._mixer = mixer
        self._sources = sources
        self._meter = meter
        self._sched = scheduler
        self._on_level = on_level            # callback(source_id, level 0..1)
        self._throttler = Throttler(scheduler, _THROTTLE_S)
        self._meter_targets = {}             # source_id -> stream id being metered
        self._poll_handle = None

    # ----- live mixer writes (the slider path) -----
    def set_master_volume(self, source_id, value):
        self._throttler.push(
            ("master", source_id), value,
            lambda v: self._mixer.set_master(
                source_id, v, self._mixer.get_master(source_id)["muted"]))

    def set_master_mute(self, source_id, muted):
        self._mixer.set_master(
            source_id, self._mixer.get_master(source_id)["volume"], muted)

    def set_cell_volume(self, source_id, mix_id, value):
        self._throttler.push(
            ("cell", source_id, mix_id), value,
            lambda v: self._mixer.set_cell(
                source_id, mix_id, v, self._mixer.get_cell(source_id, mix_id)["muted"]))

    def set_cell_mute(self, source_id, mix_id, muted):
        cur = self._mixer.get_cell(source_id, mix_id)
        self._mixer.set_cell(source_id, mix_id, cur["volume"], muted)

    # ----- stream poll + metering -----
    def start_polling(self):
        self._sched.cancel(self._poll_handle)
        self._poll_handle = self._sched.call_every(_POLL_S, self._poll_tick)

    def _poll_tick(self):
        self.poll_streams()
        return True

    def poll_streams(self):
        """Re-scan PipeWire streams and re-point every source's meter."""
        self._mixer.poll_streams()
        for source_id in list(self._sources.ids()):
            self.refresh_meter(source_id)

    def start_meters(self):
        """Meter the mic, the System catch-all, and every app source."""
        if self._mixer.mic:
            self._meter.start("mic", self._mixer.mic,
                              lambda level: self._on_level("mic", level))
        # System level = its source sink's monitor (aggregate of unmatched apps).
        self._meter.start(SYSTEM_SOURCE, src_sink(SYSTEM_SOURCE),
                          lambda level: self._on_level(SYSTEM_SOURCE, level))
        for source_id in self._sources.ids():
            self.refresh_meter(source_id)

    def refresh_meter(self, source_id):
        """Re-point the meter at the first currently-matching stream, or stop it
        if none match."""
        if self._sources.get(source_id) is None:
            return
        streams = self._mixer.streams()
        candidate = next(
            iter(self._sources.streams_for(source_id, streams.values())), None)
        current = self._meter_targets.get(source_id)
        if candidate is None:
            if current is not None:
                self._meter.stop(source_id)
                self._meter_targets.pop(source_id, None)
                self._on_level(source_id, 0.0)
            return
        if current == candidate["id"]:
            return  # already metering this stream
        self._meter.start(
            source_id, candidate["node_name"],
            lambda level, sid=source_id: self._on_level(sid, level))
        self._meter_targets[source_id] = candidate["id"]

    def stop_meter(self, source_id):
        self._meter.stop(source_id)
        self._meter_targets.pop(source_id, None)

    def app_display_names(self):
        """{match-key app_name: friendly display name} from current streams, only
        where they differ — labels group members in the manage popover."""
        out = {}
        for s in self._mixer.streams().values():
            app, disp = s.get("app_name"), s.get("display_name")
            if app and disp and disp != app:
                out[app] = disp
        return out

    def stop(self):
        self._sched.cancel(self._poll_handle)
        self._poll_handle = None
        self._throttler.cancel_all()
