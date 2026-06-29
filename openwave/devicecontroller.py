"""DeviceController — the Wave XLR connection + control state machine.

Owns the device backend, the polled hardware state, the device-specific caps,
and the gain/HP conversions; turns device reads into a DeviceView the window
renders, and user actions into device writes. It is GTK-free: timers and
off-main-thread work go through an injected Scheduler, and updates are pushed to
an on_view callback. So connect/poll/reconnect/throttle is testable with a fake
scheduler (controllable clock) and a fake backend — no main loop, no hardware.
"""

from dataclasses import dataclass

from .device import WaveXLR, WaveXLRMk2, detect_pid, PID_MK2
from .scheduler import Throttler

_POLL_S = 0.1          # 10 Hz hardware-state poll
_RECONNECT_S = 2.0     # hotplug poll
_THROTTLE_S = 0.08     # live-slider throttle


@dataclass(frozen=True)
class DeviceCaps:
    hp_detents: tuple = None          # None = continuous dB slider
    supports_low_impedance: bool = True
    supports_volume_select: bool = True
    supports_crossfade: bool = False
    supports_voice_effects: bool = False


@dataclass(frozen=True)
class DeviceView:
    """Everything the device pane shows; rendered idempotently by the window.
    When connected is False, only the status is meaningful."""
    status: str
    connected: bool
    gain_raw: int = 0
    gain_label: str = "—"
    muted: bool = False
    hp_value: float = 0.0             # detent index if hp_detents else dB
    hp_label: str = "—"
    low_impedance: bool = False
    knob_label: str = "Gain"         # "Headphones" | "Gain"
    crossfade: int = 100             # 0..200, self-monitoring mic/PC blend
    lowcut: bool = False
    expander: bool = False
    voice_tune: bool = False
    voice_tune_strength: int = 50    # 0..100, Weak..Strong
    caps: DeviceCaps = DeviceCaps()
    fw_version: str = "—"
    api_version: str = "—"
    serial: str = "—"


class DeviceController:
    def __init__(self, scheduler, on_view, on_connected=None, backend=None,
                 backend_factory=None, logger=None):
        self._sched = scheduler
        self._on_view = on_view              # callback(DeviceView)
        self._on_connected = on_connected    # fired after a successful (re)connect
        self.xlr = backend or WaveXLR()
        # Picks the backend for whichever revision is attached; injectable so a
        # test can supply a fake without going through detect_pid / real USB.
        self._make_backend = backend_factory or self._default_backend
        self._log = logger
        self._state = None
        self._caps = DeviceCaps()
        self._info = {}
        self._connecting = False
        self._poll_handle = None
        self._reconnect_handle = None
        self._throttler = Throttler(scheduler, _THROTTLE_S)

    @property
    def connected(self):
        return self.xlr.connected

    # ----- connection state machine -----
    def start(self):
        self.connect()

    def connect(self):
        if self._connecting:
            return
        self._connecting = True
        self._emit("Connecting…", connected=False)
        self._sched.run_async(self._do_connect, self._connected, self._failed)

    def _default_backend(self):
        # Keep the current instance if it already matches the attached revision,
        # so a reconnect doesn't needlessly churn the USB handle.
        if detect_pid() == PID_MK2:
            return self.xlr if isinstance(self.xlr, WaveXLRMk2) else WaveXLRMk2()
        return self.xlr if isinstance(self.xlr, WaveXLR) else WaveXLR()

    def _do_connect(self):
        self.xlr.disconnect()
        self.xlr = self._make_backend()
        self.xlr.connect()
        info = {}
        try:
            info = self.xlr.read_device_info()
        except Exception:
            pass
        return {"state": self.xlr.get_all(), "info": info}

    def _connected(self, result):
        self._connecting = False
        self._stop_reconnect()
        self._state = result["state"]
        self._info = result["info"]
        detents = getattr(self.xlr, "hp_detents", None)
        self._caps = DeviceCaps(
            hp_detents=tuple(detents) if detents else None,
            supports_low_impedance=getattr(self.xlr, "supports_low_impedance", True),
            supports_volume_select=getattr(self.xlr, "supports_volume_select", True),
            supports_crossfade=getattr(self.xlr, "supports_crossfade", False),
            supports_voice_effects=getattr(self.xlr, "supports_voice_effects", False),
        )
        self._emit("OpenWave", connected=True)
        if self._on_connected is not None:
            self._on_connected()
        self._start_polling()

    def _failed(self, e):
        self._connecting = False
        if self._log is not None:
            self._log.warning("Connect failed: %r", e)
        self._emit("Disconnected", connected=False)
        self._start_reconnect()

    def _lost(self, _e=None):
        self.xlr.disconnect()
        self._stop_polling()
        self._emit("Disconnected", connected=False)
        self._start_reconnect()

    # ----- polling -----
    def _start_polling(self):
        self._sched.cancel(self._poll_handle)
        self._poll_handle = self._sched.call_every(_POLL_S, self._poll_tick)

    def _stop_polling(self):
        self._sched.cancel(self._poll_handle)
        self._poll_handle = None

    def _poll_tick(self):
        if not self.xlr.connected:
            self._poll_handle = None
            return False
        self._sched.run_async(self.xlr.get_all, self._polled, self._lost)
        return True

    def _polled(self, state):
        if state != self._state:
            self._state = state
            self._emit("OpenWave", connected=True)

    # ----- reconnect (hotplug) -----
    def _start_reconnect(self):
        if self._reconnect_handle is None:
            self._reconnect_handle = self._sched.call_every(_RECONNECT_S, self._reconnect_tick)

    def _reconnect_tick(self):
        if self.xlr.connected:
            self._reconnect_handle = None
            return False
        if not self._connecting and detect_pid() is not None:
            self.connect()
        return True

    def _stop_reconnect(self):
        self._sched.cancel(self._reconnect_handle)
        self._reconnect_handle = None

    # ----- control writes -----
    def set_mute(self, muted):
        if not self.xlr.connected:
            return
        self._sched.run_async(lambda: self.xlr.set_mute(muted), on_error=self._lost)

    def toggle_mute(self):
        if self._state is not None:
            self.set_mute(not self._state["mute"])

    def set_low_impedance(self, enabled):
        if not self.xlr.connected:
            return
        self._sched.run_async(lambda: self.xlr.set_low_impedance(enabled), on_error=self._lost)

    def set_crossfade(self, value):
        """Throttled self-monitoring crossfade write (live slider)."""
        self._throttler.push("crossfade", int(value),
                             lambda v: self._dispatch(lambda: self.xlr.set_crossfade(v)))

    def set_lowcut(self, enabled):
        if not self.xlr.connected:
            return
        self._sched.run_async(lambda: self.xlr.set_lowcut(enabled), on_error=self._lost)

    def set_expander(self, enabled):
        if not self.xlr.connected:
            return
        self._sched.run_async(lambda: self.xlr.set_expander(enabled), on_error=self._lost)

    def set_voice_tune(self, enabled):
        if not self.xlr.connected:
            return
        self._sched.run_async(lambda: self.xlr.set_voice_tune(enabled), on_error=self._lost)

    def set_voice_tune_strength(self, value):
        """Throttled voice-tune strength write (live slider)."""
        self._throttler.push("voice_tune_strength", int(value),
                             lambda v: self._dispatch(lambda: self.xlr.set_voice_tune_strength(v)))

    def set_gain(self, raw):
        """Throttled gain write. Returns the formatted label for an optimistic
        UI update while dragging."""
        self._throttler.push("gain", raw,
                             lambda v: self._dispatch(lambda: self.xlr.set_gain_raw(v)))
        return self.xlr.format_gain(raw)

    def set_hp(self, slider_value):
        """Throttled HP write from the slider value (detent index in detent
        mode, else dB). Returns the formatted dB label for the live UI."""
        if self._caps.hp_detents:
            idx = max(0, min(len(self._caps.hp_detents) - 1, int(round(slider_value))))
            db = self._caps.hp_detents[idx]
        else:
            db = slider_value
        self._throttler.push("hp", db, lambda v: self._dispatch(lambda: self.xlr.set_hp_volume_db(v)))
        return f"{db:.1f} dB"

    def stop(self):
        self._stop_polling()
        self._stop_reconnect()
        self._throttler.cancel_all()
        self.xlr.disconnect()

    # ----- view -----
    def _emit(self, status, connected):
        self._on_view(self._view(status, connected))

    def _view(self, status, connected):
        s = self._state
        if not connected or s is None:
            return DeviceView(status=status, connected=False)
        detents = self._caps.hp_detents
        if detents:
            idx = min(range(len(detents)), key=lambda i: abs(detents[i] - s["hp_volume_db"]))
            hp_value, hp_label = idx, f"{detents[idx]:.1f} dB"
        else:
            hp_value, hp_label = s["hp_volume_db"], f"{s['hp_volume_db']:.1f} dB"
        return DeviceView(
            status=status, connected=True,
            gain_raw=s["gain_raw"], gain_label=self.xlr.format_gain(s["gain_raw"]),
            muted=s["mute"],
            hp_value=hp_value, hp_label=hp_label,
            low_impedance=s["low_impedance"],
            knob_label="Headphones" if s["volume_select"] == "hp" else "Gain",
            crossfade=s.get("crossfade", 100),
            lowcut=s.get("lowcut", False),
            expander=s.get("expander", False),
            voice_tune=s.get("voice_tune", False),
            voice_tune_strength=s.get("voice_tune_strength", 50),
            caps=self._caps,
            fw_version=self._info.get("fw_version", "—"),
            api_version=self._info.get("api_version", "—"),
            serial=self._info.get("serial", "—"),
        )

    def _dispatch(self, fn):
        """Run a throttled device write off the main thread, but only while
        connected; a write that fails mid-drag trips reconnect via _lost."""
        if self.xlr.connected:
            self._sched.run_async(fn, on_error=self._lost)
