"""OpenWave — GTK4 + Adwaita control application for the Elgato Wave XLR."""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Adw, GLib, GObject, Gio, Gdk
import logging
import os
import signal
import sys

from .devicecontroller import DeviceController
from .scheduler import GLibScheduler
from .meter import MeterMonitor
from .mixer import Mixer, SYSTEM_SOURCE
from .pwnames import src_sink
from .mixmatrix import MixMatrix
from .sourcedialog import AddSourceDialog
from . import setup, service
from .sources import Source, SourceSet

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")


class WaveXLRWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, title="OpenWave", default_width=1100, default_height=620)
        self.set_size_request(900, 520)
        # Set during render() so the device widgets' setters don't fire their
        # handlers back at the controller (feedback guard).
        self._updating_ui = False
        self.controller = None      # built after the UI; guards early handlers
        self._stream_poll_id = None
        # Live-update throttle for the matrix cell + master sliders (the device
        # gain/HP sliders throttle inside the controller instead).
        self._thr_pending = {}   # name -> latest value awaiting send
        self._thr_tid = {}       # name -> GLib timeout id (None = idle)
        self._thr_send = {}      # name -> setter callable
        self._sources = SourceSet.load()

        self._build_ui()
        self._update_service_status()
        self.mixer = Mixer()
        self.mixer.set_sources(self._sources)
        self.mixer.start()
        self.meter = MeterMonitor()
        self._meter_targets = {}
        self._wire_matrix_cells()
        self._start_meters()
        self._start_stream_poll()
        # The controller owns the device connection + the device-pane state, and
        # rebuilds the mic/HP loopbacks on each (re)connect.
        self.controller = DeviceController(
            GLibScheduler(), on_view=self._render,
            on_connected=self.mixer.refresh_device,
            logger=logging.getLogger("wavexlr.app"),
        )
        self.controller.start()

    def _build_ui(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(box)

        # Header bar
        header = Adw.HeaderBar()
        self.status_label = Gtk.Label(label="Disconnected")
        self.status_label.add_css_class("dim-label")
        header.set_title_widget(self.status_label)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reconnect")
        refresh_btn.connect("clicked", lambda _: self.controller and self.controller.connect())
        header.pack_end(refresh_btn)

        # Sidebar toggle (placed at the end so it sits next to the close button)
        self.sidebar_toggle = Gtk.ToggleButton(
            icon_name="sidebar-show-symbolic",
            tooltip_text="Toggle device panel",
            active=True,
        )
        header.pack_end(self.sidebar_toggle)
        box.append(header)

        # --- Split view: matrix (content) | device controls (sidebar) ---------
        self.split = Adw.OverlaySplitView(
            sidebar_position=Gtk.PackType.END,
            min_sidebar_width=320,
            max_sidebar_width=420,
            sidebar_width_fraction=0.30,
            vexpand=True,
        )
        box.append(self.split)

        self.sidebar_toggle.bind_property(
            "active", self.split, "show-sidebar",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )

        # Auto-collapse the sidebar into an overlay on narrow windows.
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        # --- Content: mix matrix ---------------------------------------------
        self.matrix = MixMatrix()
        self.split.set_content(self.matrix)

        self.matrix.add_mix(
            "personal", title="Personal Mix",
            subtitle="What you hear",
            icon_name="audio-headphones-symbolic",
        )
        self.matrix.add_mix(
            "chat", title="Chat Mix",
            subtitle="To voice apps (v0.3.0)",
            icon_name="system-users-symbolic",
        )
        self.matrix.add_mix(
            "record", title="Record Mix",
            subtitle="To OBS / recording (v0.3.0)",
            icon_name="media-record-symbolic",
        )

        self.mic_source = self.matrix.add_source(
            "mic", name="Wave XLR",
            icon_name="audio-input-microphone-symbolic",
            has_level=True,
        )
        self._wire_source_master(self.mic_source, "mic")

        # System catch-all: every app not added as its own source is routed here.
        self.system_source = self.matrix.add_source(
            SYSTEM_SOURCE, name="System",
            icon_name="computer-symbolic",
            has_level=True,
        )
        self._wire_source_master(self.system_source, SYSTEM_SOURCE)

        # User-defined app sources (persisted)
        for source in self._sources:
            cell = self.matrix.add_source(
                source.id,
                name=source.name,
                icon_name=source.icon_name,
                has_level=True,
                removable=True,
            )
            self._wire_source_master(cell, source.id)

        self.matrix.connect("add-source-clicked", self._on_add_source_clicked)
        self.matrix.connect("remove-source-clicked", self._on_remove_source_clicked)

        # --- Sidebar: device controls -----------------------------------------
        sidebar_scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        sidebar_clamp = Adw.Clamp(
            maximum_size=380,
            margin_start=12, margin_end=12, margin_top=12, margin_bottom=12,
        )
        sidebar_scroll.set_child(sidebar_clamp)

        sidebar_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        sidebar_clamp.set_child(sidebar_content)
        self._build_device_pane(sidebar_content)

        self.split.set_sidebar(sidebar_scroll)

    def _build_device_pane(self, parent):
        """Populate the right-hand column with Audio / Mic / HP / Device Info groups."""
        # --- Audio fix status ---
        status_group = Adw.PreferencesGroup(title="Audio")
        parent.append(status_group)

        self.audio_status_row = Adw.ActionRow(
            title="Capture Fix",
            subtitle="Keeps mic capture active to prevent the race condition"
        )
        self.audio_status_icon = Gtk.Image(icon_name="emblem-ok-symbolic")
        self.audio_status_icon.add_css_class("dim-label")
        self.audio_status_row.add_suffix(self.audio_status_icon)

        self.uninstall_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Uninstall capture fix")
        self.uninstall_btn.add_css_class("flat")
        self.uninstall_btn.connect("clicked", self._on_uninstall_clicked)
        self.audio_status_row.add_suffix(self.uninstall_btn)

        status_group.add(self.audio_status_row)

        # --- Mic controls ---
        mic_group = Adw.PreferencesGroup(title="Microphone")
        parent.append(mic_group)

        mute_row = Adw.SwitchRow(title="Mute", subtitle="Toggle microphone mute")
        mute_row.connect("notify::active", self._on_mute_changed)
        self.mute_row = mute_row
        mic_group.add(mute_row)

        gain_row = Adw.ActionRow(title="Gain")
        self.gain_label = Gtk.Label(label="—", width_chars=8, xalign=1)
        self.gain_label.add_css_class("monospace")
        gain_row.add_suffix(self.gain_label)
        mic_group.add(gain_row)

        self.gain_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            hexpand=True,
            draw_value=False,
            adjustment=Gtk.Adjustment(lower=0x0000, upper=0x5000, step_increment=0x40, page_increment=0x200),
        )
        self.gain_scale.set_margin_start(12)
        self.gain_scale.set_margin_end(12)
        self.gain_scale.connect("value-changed", self._on_gain_changed)
        parent.append(self.gain_scale)

        knob_row = Adw.ActionRow(title="Knob Controls", subtitle="What the physical knob adjusts")
        self.knob_label = Gtk.Label(label="Gain")
        self.knob_label.add_css_class("dim-label")
        knob_row.add_suffix(self.knob_label)
        self.knob_row = knob_row
        mic_group.add(knob_row)

        # --- Headphone controls ---
        hp_group = Adw.PreferencesGroup(title="Headphones")
        parent.append(hp_group)

        hp_vol_row = Adw.ActionRow(title="Volume")
        self.hp_label = Gtk.Label(label="0.0 dB", width_chars=10, xalign=1)
        self.hp_label.add_css_class("monospace")
        hp_vol_row.add_suffix(self.hp_label)
        hp_group.add(hp_vol_row)

        self.hp_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            hexpand=True,
            draw_value=False,
            adjustment=Gtk.Adjustment(lower=-60.0, upper=0.0, step_increment=0.5, page_increment=2.0),
        )
        self.hp_scale.set_margin_start(12)
        self.hp_scale.set_margin_end(12)
        self.hp_scale.connect("value-changed", self._on_hp_changed)
        parent.append(self.hp_scale)

        lowz_row = Adw.SwitchRow(title="Low Impedance", subtitle="For low impedance headphones")
        lowz_row.connect("notify::active", self._on_lowz_changed)
        self.lowz_row = lowz_row
        hp_group.add(lowz_row)

        # --- Device info ---
        info_group = Adw.PreferencesGroup(title="Device Info")
        parent.append(info_group)

        self.fw_row = Adw.ActionRow(title="Firmware")
        self.fw_label = Gtk.Label(label="—")
        self.fw_label.add_css_class("dim-label")
        self.fw_row.add_suffix(self.fw_label)
        info_group.add(self.fw_row)

        self.api_row = Adw.ActionRow(title="API Version")
        self.api_label = Gtk.Label(label="—")
        self.api_label.add_css_class("dim-label")
        self.api_row.add_suffix(self.api_label)
        info_group.add(self.api_row)

        self.serial_row = Adw.ActionRow(title="Serial")
        self.serial_label = Gtk.Label(label="—")
        self.serial_label.add_css_class("dim-label")
        self.serial_row.add_suffix(self.serial_label)
        info_group.add(self.serial_row)

    def _update_service_status(self):
        """Check if the audio service is running."""
        active = service.is_running()

        if active:
            self.audio_status_icon.set_from_icon_name("emblem-ok-symbolic")
            self.audio_status_icon.remove_css_class("dim-label")
            self.audio_status_row.set_subtitle("Audio service running")
            self.uninstall_btn.set_visible(True)
        else:
            self.audio_status_icon.set_from_icon_name("dialog-warning-symbolic")
            self.audio_status_row.set_subtitle("Audio service not running")
            self.uninstall_btn.set_visible(False)

    def _on_uninstall_clicked(self, btn):
        dialog = Adw.AlertDialog(
            heading="Uninstall Capture Fix?",
            body="This will remove the audio service and USB permissions.\n\nYou can reinstall them by restarting OpenWave.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("uninstall", "Uninstall")
        dialog.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.choose(self, None, self._on_uninstall_response)

    def _on_uninstall_response(self, dialog, result):
        response = dialog.choose_finish(result)
        if response != "uninstall":
            return
        success, message = setup.run_uninstall()
        self._update_service_status()
        if not success:
            err = Adw.AlertDialog(heading="Uninstall Failed", body=message)
            err.add_response("ok", "OK")
            err.choose(self, None, lambda d, r: d.choose_finish(r))

    def _render(self, view):
        """Render a DeviceView onto the device pane. _updating_ui guards the
        widget setters so they don't fire their handlers back at the controller."""
        self.status_label.set_label(view.status)
        if not view.connected:
            self.status_label.add_css_class("dim-label")
            return
        self.status_label.remove_css_class("dim-label")
        self._updating_ui = True
        caps = view.caps
        self.lowz_row.set_sensitive(caps.supports_low_impedance)
        self.lowz_row.set_subtitle(
            "For low impedance headphones" if caps.supports_low_impedance
            else "Not yet supported on Wave XLR MK.2"
        )
        self.knob_row.set_visible(caps.supports_volume_select)
        adj = self.hp_scale.get_adjustment()
        if caps.hp_detents:
            adj.set_lower(0)
            adj.set_upper(len(caps.hp_detents) - 1)
            adj.set_step_increment(1)
            adj.set_page_increment(4)
        else:
            adj.set_lower(-60.0)
            adj.set_upper(0.0)
            adj.set_step_increment(0.5)
            adj.set_page_increment(2.0)
        self.mute_row.set_active(view.muted)
        self.gain_scale.set_value(view.gain_raw)
        self.gain_label.set_label(view.gain_label)
        self.hp_scale.set_value(view.hp_value)
        self.hp_label.set_label(view.hp_label)
        self.lowz_row.set_active(view.low_impedance)
        self.knob_label.set_label(view.knob_label)
        self.fw_label.set_label(view.fw_version)
        self.api_label.set_label(view.api_version)
        self.serial_label.set_label(view.serial)
        self._updating_ui = False

    def _live(self):
        """True when a user control change should be forwarded to the device."""
        return not self._updating_ui and self.controller is not None \
            and self.controller.connected

    def _on_mute_changed(self, row, _pspec):
        if self._live():
            self.controller.set_mute(row.get_active())

    def _on_gain_changed(self, scale):
        if self._live():
            self.gain_label.set_label(self.controller.set_gain(int(scale.get_value())))

    def _on_hp_changed(self, scale):
        if self._live():
            self.hp_label.set_label(self.controller.set_hp(scale.get_value()))

    def _on_lowz_changed(self, row, _pspec):
        if self._live():
            self.controller.set_low_impedance(row.get_active())

    def _wire_source_master(self, cell, source_id):
        """Bind a source row's master fader/mute to the mixer's per-source master
        (GoXLR channel fader: scales all that source's sends). For the mic this
        is a software level — hardware gain lives only in the sidebar."""
        cell.connect("volume-changed", self._on_source_master_volume_changed, source_id)
        cell.connect("mute-toggled", self._on_source_master_mute_toggled, source_id)

    def _on_source_master_volume_changed(self, _source, value, source_id):
        if self._updating_ui:
            return
        self._throttle(
            ("master", source_id), value,
            lambda v, sid=source_id: self.mixer.set_master(
                sid, v, self.mixer.get_master(sid)["muted"]),
        )

    def _on_source_master_mute_toggled(self, _source, muted, source_id):
        if self._updating_ui:
            return
        self.mixer.set_master(source_id, self.mixer.get_master(source_id)["volume"], muted)

    def _wire_matrix_cells(self):
        """Bind each per-cell slider/mute to the mixer + restore persisted levels."""
        # First-run: make the System catch-all audible in Personal by default.
        if f"{SYSTEM_SOURCE}.personal" not in self.mixer.cells():
            self.mixer.set_cell(SYSTEM_SOURCE, "personal", 1.0, False)
        source_ids = ["mic", SYSTEM_SOURCE] + list(self._sources.ids())
        for source_id in source_ids:
            self._restore_source_master(source_id)
            for mix_id in ("personal", "chat", "record"):
                self._wire_cell(source_id, mix_id)

    def _restore_source_master(self, source_id):
        """Set a source row's master fader/mute to the persisted level. Masters
        default to 1.0, so this must run or the slider would sit at 0 while the
        mixer treats it as full."""
        cell = self.matrix.source(source_id)
        if cell is None:
            return
        master = self.mixer.get_master(source_id)
        cell.set_volume(master["volume"])
        cell.set_muted(master["muted"])

    def _wire_cell(self, source_id, mix_id):
        cell = self.matrix.cell(source_id, mix_id)
        if cell is None:
            return
        state = self.mixer.get_cell(source_id, mix_id)
        cell.set_volume(state["volume"])
        cell.set_muted(state["muted"])
        cell.connect("volume-changed", self._on_cell_volume_changed, source_id, mix_id)
        cell.connect("mute-toggled", self._on_cell_mute_toggled, source_id, mix_id)

    def _start_stream_poll(self):
        """Poll for new/vanished PipeWire output streams every 2 s."""
        if self._stream_poll_id:
            GLib.source_remove(self._stream_poll_id)
        self._stream_poll_id = GLib.timeout_add_seconds(2, self._stream_poll_tick)

    def _stream_poll_tick(self):
        self.mixer.poll_streams()
        for source_id in list(self._sources.ids()):
            self._refresh_app_meter(source_id)
        return True

    def _start_meters(self):
        """Begin metering the mic, the System catch-all, and app sources."""
        if self.mixer.mic:
            self.meter.start(
                "mic", self.mixer.mic,
                lambda level: self._set_source_level("mic", level),
            )
        # System level = its source sink's monitor (aggregate of unmatched apps).
        self.meter.start(
            SYSTEM_SOURCE, src_sink(SYSTEM_SOURCE),
            lambda level: self._set_source_level(SYSTEM_SOURCE, level),
        )
        for source_id in self._sources.ids():
            self._refresh_app_meter(source_id)

    def _refresh_app_meter(self, source_id):
        """Re-point the meter at the first currently-matching stream, or stop it
        if none match. Called on stream-poll changes and source add."""
        if self._sources.get(source_id) is None:
            return
        streams = self.mixer.streams()
        candidate = next(
            iter(self._sources.streams_for(source_id, streams.values())), None,
        )
        current = self._meter_targets.get(source_id)
        if candidate is None:
            if current is not None:
                self.meter.stop(source_id)
                self._meter_targets.pop(source_id, None)
                self._set_source_level(source_id, 0.0)
            return
        if current == candidate["id"]:
            return  # already metering this stream
        self.meter.start(
            source_id, candidate["node_name"],
            lambda level, sid=source_id: self._set_source_level(sid, level),
        )
        self._meter_targets[source_id] = candidate["id"]

    def _set_source_level(self, source_id, level):
        cell = self.matrix.source(source_id)
        if cell is not None:
            cell.set_level(level)

    def _on_add_source_clicked(self, _matrix):
        dialog = AddSourceDialog(exclude_apps=self._sources.bound_apps())
        dialog.connect("source-confirmed", self._on_source_confirmed)
        dialog.present(self)

    def _on_source_confirmed(self, _dialog, name, match_app_name, icon_name):
        if match_app_name in self._sources.bound_apps():
            return  # already bound — the picker hides these, but guard anyway
        source = Source.new(name=name, match_app_name=match_app_name, icon_name=icon_name)
        self._sources.add(source)
        self._sources.save()
        cell = self.matrix.add_source(
            source.id,
            name=source.name,
            icon_name=source.icon_name,
            has_level=True,
            removable=True,
        )
        self._wire_source_master(cell, source.id)
        self._restore_source_master(source.id)
        # New sources are now *moved* onto a private sink, so they're silent
        # until routed — default them audible in the Personal mix.
        self.mixer.set_cell(source.id, "personal", 1.0, False)
        self._wire_cell(source.id, "personal")
        self._wire_cell(source.id, "chat")
        self._wire_cell(source.id, "record")
        self.mixer.set_sources(self._sources)
        self.mixer.poll_streams()
        self._refresh_app_meter(source.id)

    def _on_remove_source_clicked(self, _matrix, source_id):
        source = self._sources.get(source_id)
        name = source.name if source is not None else "this source"
        dialog = Adw.AlertDialog(
            heading="Remove source?",
            body=f"This deletes “{name}” and its mix levels. The bound application "
                 f"itself is not affected.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.choose(self, None, lambda d, r: self._on_remove_response(d, r, source_id))

    def _on_remove_response(self, dialog, result, source_id):
        if dialog.choose_finish(result) != "remove":
            return
        self.meter.stop(source_id)
        self._meter_targets.pop(source_id, None)
        self.matrix.remove_source(source_id)
        self._sources.discard(source_id)
        self._sources.save()
        self.mixer.remove_source(source_id)

    def _on_cell_volume_changed(self, _cell, value, source_id, mix_id):
        # Live throttle (same as the device sliders): leading + ~80ms while
        # dragging + trailing, so the mix updates in real time.
        self._throttle(
            ("cell", source_id, mix_id), value,
            lambda v, s=source_id, m=mix_id: self.mixer.set_cell(
                s, m, v, self.mixer.get_cell(s, m)["muted"]),
        )

    def _on_cell_mute_toggled(self, _cell, muted, source_id, mix_id):
        cur = self.mixer.get_cell(source_id, mix_id)
        self.mixer.set_cell(source_id, mix_id, cur["volume"], muted)

    # Live-update throttle for the matrix sliders: send on the leading edge,
    # then at most every _THROTTLE_MS while the value keeps changing, plus a
    # trailing send — so dragging a cell/master updates the mix in real time
    # without flooding the mixer.
    _THROTTLE_MS = 80

    def _throttle(self, name, value, send_fn):
        self._thr_pending[name] = value
        self._thr_send[name] = send_fn
        if self._thr_tid.get(name) is None:
            self._thr_flush(name)
            self._thr_tid[name] = GLib.timeout_add(self._THROTTLE_MS, self._thr_tick, name)

    def _thr_tick(self, name):
        if name in self._thr_pending:
            self._thr_flush(name)
            return True  # keep ticking while values are still arriving
        self._thr_tid[name] = None
        return False

    def _thr_flush(self, name):
        if name not in self._thr_pending:
            return
        value = self._thr_pending.pop(name)
        self._thr_send[name](value)


class WaveXLRApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="com.github.openwave",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self._window = None
        self._start_hidden = False
        self._tray = None
        self.add_main_option(
            "hide", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            "Start hidden in system tray", None,
        )

    def do_command_line(self, command_line):
        options = command_line.get_options_dict()
        if options.contains("hide"):
            self._start_hidden = True
        self.activate()
        return 0

    def do_startup(self):
        Adw.Application.do_startup(self)
        # Quit gracefully on SIGTERM/SIGINT (e.g. `pkill`) so do_shutdown runs
        # mixer.stop() and apps are returned to their default output instead of
        # being stranded on a now-orphaned source sink.
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_quit_signal)

    def _on_quit_signal(self):
        self._quit_app()
        return GLib.SOURCE_REMOVE

    def do_activate(self):
        if not self._window:
            self._load_css()
            if setup.needs_setup():
                self._show_setup_dialog()
                return
            self._window = WaveXLRWindow(application=self)
            # Hide-to-tray on close instead of quitting
            self._window.connect("close-request", self._on_close_request)
            self._setup_tray()
            if self._start_hidden:
                self._start_hidden = False  # only first launch
                return
        self._window.present()

    def _load_css(self):
        """Load OpenWave's stylesheet — alongside the .py files, or under share/."""
        candidates = (
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "style.css"),
            "/usr/local/share/openwave/style.css",
            "/usr/share/openwave/style.css",
        )
        css_path = next((p for p in candidates if os.path.exists(p)), None)
        if css_path is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(css_path)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def do_shutdown(self):
        """Tear down loopback + meter subprocesses before the process exits."""
        if self._window is not None:
            if hasattr(self._window, "meter"):
                self._window.meter.stop_all()
            if hasattr(self._window, "mixer"):
                self._window.mixer.stop()
        Adw.Application.do_shutdown(self)

    def _on_close_request(self, window):
        if self._tray:
            window.set_visible(False)
            return True  # prevent destroy, keep running in tray
        return False  # normal close → quit

    def _setup_tray(self):
        from .tray import TrayIcon
        self._tray = TrayIcon(
            on_activate=self._toggle_window,
            on_mute=self._toggle_mute,
            on_quit=self._quit_app,
        )
        self._tray.register()
        # Keep app alive when window is hidden
        self.hold()

    def _toggle_mute(self):
        if self._window and self._window.controller is not None:
            self._window.controller.toggle_mute()

    def _quit_app(self):
        self.release()
        self.quit()

    def _toggle_window(self):
        if self._window:
            if self._window.get_visible():
                self._window.set_visible(False)
            else:
                self._window.present()

    def _show_setup_dialog(self):
        dialog = Adw.AlertDialog(
            heading="First-Time Setup",
            body="OpenWave needs to configure USB permissions and install the audio service.\n\nYou may be prompted for your password.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("setup", "Set Up")
        dialog.set_response_appearance("setup", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("setup")

        tmp_win = Adw.ApplicationWindow(application=self)
        tmp_win.present()

        dialog.choose(tmp_win, None, self._on_setup_response, tmp_win)

    def _on_setup_response(self, dialog, result, tmp_win):
        response = dialog.choose_finish(result)
        tmp_win.close()

        if response != "setup":
            self.quit()
            return

        success, message = setup.run_setup()
        if success:
            replug_dialog = Adw.AlertDialog(
                heading="Setup Complete",
                body=f"{message}.\n\nPlease replug your Wave XLR, then click Continue.",
            )
            replug_dialog.add_response("continue", "Continue")
            replug_dialog.set_default_response("continue")

            tmp_win2 = Adw.ApplicationWindow(application=self)
            tmp_win2.present()
            replug_dialog.choose(tmp_win2, None, self._on_replug_done, tmp_win2)
        else:
            err_dialog = Adw.AlertDialog(
                heading="Setup Failed",
                body=message,
            )
            err_dialog.add_response("ok", "OK")
            err_win = Adw.ApplicationWindow(application=self)
            err_win.present()
            err_dialog.choose(err_win, None, lambda d, r, w: (w.close(), self.quit()), err_win)

    def _on_replug_done(self, dialog, result, tmp_win):
        dialog.choose_finish(result)
        tmp_win.close()
        win = WaveXLRWindow(application=self)
        self._window = win
        win.present()

    def do_shutdown(self):
        if self._window and self._window.controller is not None:
            self._window.controller.stop()
        Adw.Application.do_shutdown(self)


def main():
    app = WaveXLRApp()
    app.run(sys.argv)
