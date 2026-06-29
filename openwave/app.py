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
from .mixercontroller import MixerController
from .scheduler import GLibScheduler
from .meter import MeterMonitor
from .mixer import Mixer, SYSTEM_SOURCE
from .mixmatrix import MixMatrix
from .sourcedialog import AddSourceDialog, PickAppsDialog
from . import setup, service
from .sources import Source, SourceSet

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")


class OpenWaveWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, title="OpenWave", default_width=1100, default_height=620)
        self.set_size_request(900, 520)
        # Set during render() so the device widgets' setters don't fire their
        # handlers back at the controller (feedback guard).
        self._updating_ui = False
        self.controller = None      # built after the UI; guards early handlers
        self.mixerctl = None        # built after the UI; guards early handlers
        self._sources = SourceSet.load()

        self._build_ui()
        self._update_service_status()
        self.mixer = Mixer()
        self.mixer.set_sources(self._sources)
        self.mixer.start()
        self.meter = MeterMonitor()
        scheduler = GLibScheduler()
        # The mixer controller owns the live mixer writes, the stream poll, and
        # the meter binding (GTK-free); the window just wires signals to it.
        self.mixerctl = MixerController(
            self.mixer, self._sources, self.meter, scheduler, self._set_source_level)
        self._restore_strips()
        self.mixerctl.start_meters()
        self.mixerctl.start_polling()
        # The device controller owns the device connection + the device-pane
        # state, and rebuilds the mic/HP loopbacks on each (re)connect.
        self.controller = DeviceController(
            scheduler, on_view=self._render,
            on_connected=self.mixer.refresh_device,
            logger=logging.getLogger("openwave.app"),
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
            subtitle="To voice apps as “OpenWave Chat”",
            icon_name="system-users-symbolic",
        )
        self.matrix.add_mix(
            "record", title="Record Mix",
            subtitle="To OBS as “OpenWave Record”",
            icon_name="media-record-symbolic",
        )

        self.mic_source = self.matrix.add_source(
            "mic", name="Wave XLR",
            icon_name="audio-input-microphone-symbolic",
            has_level=True,
        )
        self._wire_strip(self.mic_source, "mic")

        # System catch-all: every app not added as its own source is routed here.
        self.system_source = self.matrix.add_source(
            SYSTEM_SOURCE, name="System",
            icon_name="computer-symbolic",
            has_level=True,
        )
        self._wire_strip(self.system_source, SYSTEM_SOURCE)

        # User-defined channels (persisted) — each binds one app or a group.
        for source in self._sources:
            strip = self.matrix.add_source(
                source.id,
                name=source.name,
                icon_name=source.icon_name,
                has_level=True,
                removable=True,
                members=source.members,
                is_group=source.is_group,
            )
            self._wire_strip(strip, source.id, removable=True)

        self.matrix.connect("add-source-clicked", self._on_add_source_clicked)
        self.matrix.connect("remove-source-clicked", self._on_remove_source_clicked)
        self.matrix.connect("sources-reordered", self._on_sources_reordered)

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
        """Microphone / Headphones / Device groups, then the Audio Service status
        pinned at the bottom (highlighted when the service isn't running)."""
        # --- Mic controls ---
        mic_group = Adw.PreferencesGroup(title="Microphone")
        parent.append(mic_group)

        mute_row = Adw.SwitchRow(title="Mute", subtitle="Toggle microphone mute")
        mute_row.connect("notify::active", self._on_mute_changed)
        self.mute_row = mute_row
        mic_group.add(mute_row)

        # Slider lives in the row so it's clearly the Gain control; value at the edge.
        gain_row = Adw.ActionRow(title="Gain")
        self.gain_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL, draw_value=False,
            adjustment=Gtk.Adjustment(lower=0x0000, upper=0x5000, step_increment=0x40, page_increment=0x200),
        )
        self.gain_scale.set_size_request(150, -1)
        self.gain_scale.connect("value-changed", self._on_gain_changed)
        gain_row.add_suffix(self.gain_scale)
        self.gain_label = Gtk.Label(label="—", width_chars=7, xalign=1)
        self.gain_label.add_css_class("monospace")
        gain_row.add_suffix(self.gain_label)
        mic_group.add(gain_row)

        knob_row = Adw.ActionRow(title="Knob Controls", subtitle="What the physical knob adjusts")
        self.knob_label = Gtk.Label(label="Gain")
        self.knob_label.add_css_class("dim-label")
        knob_row.add_suffix(self.knob_label)
        self.knob_row = knob_row
        mic_group.add(knob_row)

        # --- Microphone effects (hardware DSP on the mic signal) ---
        self.effects_group = Adw.PreferencesGroup(title="Microphone Effects")
        parent.append(self.effects_group)

        self.lowcut_row = Adw.SwitchRow(
            title="Low-cut Filter", subtitle="Roll off low-frequency rumble")
        self.lowcut_row.connect("notify::active", self._on_lowcut_changed)
        self.effects_group.add(self.lowcut_row)

        self.expander_row = Adw.SwitchRow(
            title="Expander", subtitle="Gate quiet background noise between words")
        self.expander_row.connect("notify::active", self._on_expander_changed)
        self.effects_group.add(self.expander_row)

        self.voicetune_row = Adw.SwitchRow(
            title="Voice Tune", subtitle="Pitch-correct your voice")
        self.voicetune_row.connect("notify::active", self._on_voicetune_changed)
        self.effects_group.add(self.voicetune_row)

        self.strength_row = Adw.ActionRow(title="Voice Tune Strength")
        self.strength_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, draw_value=False,
            adjustment=Gtk.Adjustment(lower=0, upper=100, step_increment=1, page_increment=10))
        self.strength_scale.add_mark(0, Gtk.PositionType.BOTTOM, "Weak")
        self.strength_scale.add_mark(100, Gtk.PositionType.BOTTOM, "Strong")
        self.strength_scale.set_size_request(180, -1)
        self.strength_scale.connect("value-changed", self._on_strength_changed)
        self.strength_row.add_suffix(self.strength_scale)
        self.effects_group.add(self.strength_row)

        # --- Headphone controls ---
        hp_group = Adw.PreferencesGroup(title="Headphones")
        parent.append(hp_group)

        hp_vol_row = Adw.ActionRow(title="Volume")
        self.hp_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL, draw_value=False,
            adjustment=Gtk.Adjustment(lower=-60.0, upper=0.0, step_increment=0.5, page_increment=2.0),
        )
        self.hp_scale.set_size_request(150, -1)
        self.hp_scale.connect("value-changed", self._on_hp_changed)
        hp_vol_row.add_suffix(self.hp_scale)
        self.hp_label = Gtk.Label(label="—", width_chars=5, xalign=1)
        self.hp_label.add_css_class("monospace")
        hp_vol_row.add_suffix(self.hp_label)
        hp_group.add(hp_vol_row)

        # Self-monitoring: the device's headphone blend between your own mic
        # (zero-latency hardware monitor) and PC audio.
        self.crossfade_row = Adw.ActionRow(
            title="Self-monitoring", subtitle="Headphone blend of your mic vs PC audio")
        self.crossfade_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, draw_value=False,
            adjustment=Gtk.Adjustment(lower=0, upper=200, step_increment=2, page_increment=20))
        self.crossfade_scale.add_mark(0, Gtk.PositionType.BOTTOM, "PC")
        self.crossfade_scale.add_mark(100, Gtk.PositionType.BOTTOM, "Equal")
        self.crossfade_scale.add_mark(200, Gtk.PositionType.BOTTOM, "Mic")
        self.crossfade_scale.set_size_request(200, -1)
        self.crossfade_scale.connect("value-changed", self._on_crossfade_changed)
        self.crossfade_row.add_suffix(self.crossfade_scale)
        hp_group.add(self.crossfade_row)

        # "High Power Mode" is Elgato's name for low-impedance mode; show both.
        lowz_row = Adw.SwitchRow(
            title="High Power Mode", subtitle="Boosts output for low-impedance headphones")
        lowz_row.connect("notify::active", self._on_lowz_changed)
        self.lowz_row = lowz_row
        hp_group.add(lowz_row)

        # --- Device info (folded behind an ⓘ so it doesn't crowd the controls) ---
        info_group = Adw.PreferencesGroup(title="Device")
        parent.append(info_group)

        info_row = Adw.ActionRow(title="Wave XLR", subtitle="Firmware, API, serial")
        info_btn = Gtk.MenuButton(
            icon_name="dialog-information-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text="Device details")
        info_btn.add_css_class("flat")
        info_row.add_suffix(info_btn)
        info_group.add(info_row)

        pop = Gtk.Popover()
        info_btn.set_popover(pop)
        grid = Gtk.Grid(
            row_spacing=8, column_spacing=16,
            margin_start=14, margin_end=14, margin_top=14, margin_bottom=14,
        )
        pop.set_child(grid)
        self.fw_label = Gtk.Label(label="—", xalign=1)
        self.api_label = Gtk.Label(label="—", xalign=1)
        self.serial_label = Gtk.Label(label="—", xalign=1)
        for r, (title, lbl) in enumerate((
                ("Firmware", self.fw_label),
                ("API Version", self.api_label),
                ("Serial", self.serial_label))):
            key = Gtk.Label(label=title, xalign=0)
            key.add_css_class("dim-label")
            lbl.add_css_class("monospace")
            grid.attach(key, 0, r, 1, 1)
            grid.attach(lbl, 1, r, 1, 1)

        # --- Audio service status — pinned at the bottom, highlighted when down ---
        status_group = Adw.PreferencesGroup(title="Audio Service")
        parent.append(status_group)

        self.audio_status_row = Adw.ActionRow(
            title="Capture Fix",
            subtitle="Keeps mic capture active to prevent the race condition")
        self.audio_status_icon = Gtk.Image(icon_name="emblem-ok-symbolic")
        self.audio_status_icon.add_css_class("dim-label")
        self.audio_status_row.add_suffix(self.audio_status_icon)

        self.uninstall_btn = Gtk.Button(
            icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text="Uninstall capture fix")
        self.uninstall_btn.add_css_class("flat")
        self.uninstall_btn.connect("clicked", self._on_uninstall_clicked)
        self.audio_status_row.add_suffix(self.uninstall_btn)
        status_group.add(self.audio_status_row)

    def _update_service_status(self):
        """Reflect whether the audio service is running; highlight the row if not."""
        if service.is_running():
            self.audio_status_icon.set_from_icon_name("emblem-ok-symbolic")
            self.audio_status_icon.remove_css_class("dim-label")
            self.audio_status_row.set_subtitle("Audio service running")
            self.audio_status_row.remove_css_class("openwave-service-down")
            self.uninstall_btn.set_visible(True)
        else:
            self.audio_status_icon.set_from_icon_name("dialog-warning-symbolic")
            self.audio_status_icon.remove_css_class("dim-label")
            self.audio_status_row.set_subtitle("Audio service not running")
            self.audio_status_row.add_css_class("openwave-service-down")
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
            "Boosts output for low-impedance headphones" if caps.supports_low_impedance
            else "Not supported on this device"
        )
        self.knob_row.set_visible(caps.supports_volume_select)
        # Crossfade + voice effects are MK.2-only (MK.1 not yet mapped).
        self.crossfade_row.set_visible(caps.supports_crossfade)
        self.effects_group.set_visible(caps.supports_voice_effects)
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
        self.hp_label.set_label(self._hp_percent(view.hp_value))
        self.lowz_row.set_active(view.low_impedance)
        self.crossfade_scale.set_value(view.crossfade)
        self.lowcut_row.set_active(view.lowcut)
        self.expander_row.set_active(view.expander)
        self.voicetune_row.set_active(view.voice_tune)
        self.strength_scale.set_value(view.voice_tune_strength)
        # Strength only matters while Voice Tune is on.
        self.strength_row.set_sensitive(view.voice_tune)
        self.knob_label.set_label(view.knob_label)
        self.fw_label.set_label(view.fw_version)
        self.api_label.set_label(view.api_version)
        self.serial_label.set_label(view.serial)
        self._updating_ui = False

    def _hp_percent(self, value):
        """Headphone volume as 0–100% of the slider's range — friendlier than raw
        dB, where 100% = 0 dB reads as 'zero' to non-audio folks. Works for both
        the continuous MK1 dB range and the MK2 detent index."""
        adj = self.hp_scale.get_adjustment()
        lo, hi = adj.get_lower(), adj.get_upper()
        if hi <= lo:
            return "0%"
        return f"{round((value - lo) / (hi - lo) * 100)}%"

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
            self.controller.set_hp(scale.get_value())   # still drives the device in dB
            self.hp_label.set_label(self._hp_percent(scale.get_value()))

    def _on_lowz_changed(self, row, _pspec):
        if self._live():
            self.controller.set_low_impedance(row.get_active())

    def _on_crossfade_changed(self, scale):
        if self._live():
            self.controller.set_crossfade(scale.get_value())

    def _on_lowcut_changed(self, row, _pspec):
        if self._live():
            self.controller.set_lowcut(row.get_active())

    def _on_expander_changed(self, row, _pspec):
        if self._live():
            self.controller.set_expander(row.get_active())

    def _on_voicetune_changed(self, row, _pspec):
        if self._live():
            self.controller.set_voice_tune(row.get_active())
            self.strength_row.set_sensitive(row.get_active())

    def _on_strength_changed(self, scale):
        if self._live():
            self.controller.set_voice_tune_strength(scale.get_value())

    def _wire_strip(self, strip, source_id, removable=False):
        """Wire a channel strip to the mixer: the master trim/mute (GoXLR channel
        fader — scales all sends), each mix's cell fader/mute, and, for a user
        channel, the group member edits. Strip setters are signal-blocked, so
        restoring persisted state never echoes back here."""
        strip.member_name_resolver = self.mixerctl.app_display_names
        strip.connect("master-volume-changed", self._on_master_volume_changed, source_id)
        strip.connect("master-mute-toggled", self._on_master_mute_toggled, source_id)
        strip.connect("cell-volume-changed", self._on_cell_volume_changed, source_id)
        strip.connect("cell-mute-toggled", self._on_cell_mute_toggled, source_id)
        if removable:
            strip.connect("add-member-clicked", self._on_add_member_clicked, source_id)
            strip.connect("remove-member-clicked", self._on_remove_member_clicked, source_id)
            strip.connect("rename-requested", self._on_rename_requested, source_id)
            strip.connect("ungroup-clicked", self._on_ungroup_clicked, source_id)

    def _on_master_volume_changed(self, _strip, value, source_id):
        self.mixerctl.set_master_volume(source_id, value)

    def _on_master_mute_toggled(self, _strip, muted, source_id):
        self.mixerctl.set_master_mute(source_id, muted)

    def _on_cell_volume_changed(self, _strip, mix_id, value, source_id):
        self.mixerctl.set_cell_volume(source_id, mix_id, value)

    def _on_cell_mute_toggled(self, _strip, mix_id, muted, source_id):
        self.mixerctl.set_cell_mute(source_id, mix_id, muted)

    # ----- group membership -----
    def _on_add_member_clicked(self, _strip, source_id):
        dialog = PickAppsDialog(exclude_apps=self._sources.bound_apps())
        dialog.connect("apps-picked", self._on_member_apps_picked, source_id)
        dialog.present(self)

    def _on_member_apps_picked(self, _dialog, apps, source_id):
        for app in apps:
            self._sources.add_member(source_id, app)
        self._sources.save()
        self._refresh_strip_members(source_id)
        self.mixer.set_sources(self._sources)
        self.mixerctl.poll_streams()

    def _on_remove_member_clicked(self, _strip, app, source_id):
        if self._sources.get(source_id) is None:
            return
        self._sources.remove_member(source_id, app)
        remaining = self._sources.get(source_id)
        if remaining is None or not remaining.members:
            self._remove_source(source_id)   # last app gone — drop the channel
            return
        self._sources.save()
        self._refresh_strip_members(source_id)
        self.mixer.set_sources(self._sources)
        self.mixerctl.poll_streams()

    def _on_rename_requested(self, _strip, name, source_id):
        name = name.strip()
        if not name:
            return
        self._sources.rename(source_id, name)
        self._sources.save()
        strip = self.matrix.source(source_id)
        if strip is not None:
            strip.set_name(name)

    def _on_ungroup_clicked(self, _strip, source_id):
        group = self._sources.get(source_id)
        if group is None or not group.is_group:
            return
        # Copy the group's master + cells onto each new single-app channel so
        # audio doesn't jump, then swap the group strip for the new ones.
        master = self.mixer.get_master(source_id)
        cells = {m: self.mixer.get_cell(source_id, m)
                 for m in ("personal", "chat", "record")}
        new_sources = self._sources.ungroup(source_id)
        self._sources.save()
        self.mixerctl.stop_meter(source_id)
        self.matrix.remove_source(source_id)
        self.mixer.remove_source(source_id)
        for ns in new_sources:
            self.mixer.set_master(ns.id, master["volume"], master["muted"])
            for mix_id, c in cells.items():
                self.mixer.set_cell(ns.id, mix_id, c["volume"], c["muted"])
            strip = self.matrix.add_source(
                ns.id, name=ns.name, icon_name=ns.icon_name,
                has_level=True, removable=True,
                members=ns.members, is_group=ns.is_group,
            )
            self._wire_strip(strip, ns.id, removable=True)
            self._restore_strip(ns.id)
        self.mixer.set_sources(self._sources)
        self.mixerctl.poll_streams()

    def _refresh_strip_members(self, source_id):
        source = self._sources.get(source_id)
        strip = self.matrix.source(source_id)
        if source is not None and strip is not None:
            strip.set_members(source.members, source.is_group)

    def _remove_source(self, source_id):
        """Tear a user channel down everywhere: meter, strip, persisted sources,
        and the mixer's sinks/loopbacks (its apps re-home to System)."""
        self.mixerctl.stop_meter(source_id)
        self.matrix.remove_source(source_id)
        self._sources.discard(source_id)
        self._sources.save()
        self.mixer.remove_source(source_id)

    def _on_sources_reordered(self, _matrix):
        """Persist a drag-reorder. Order is purely cosmetic (routing is per-id, not
        positional), so this just re-saves the new order — no mixer change."""
        order = [sid for sid in self.matrix.source_order()
                 if self._sources.get(sid) is not None]
        self._sources.reorder(order)
        self._sources.save()

    def _restore_strips(self):
        """Push persisted master + cell levels onto every strip. Strip setters
        are signal-blocked, so this doesn't echo back to the mixer. Masters
        default to 1.0, so this must run or a fader would sit at 0 while the
        mixer treats it as full."""
        # First-run: make the System catch-all audible in Personal by default.
        if f"{SYSTEM_SOURCE}.personal" not in self.mixer.cells():
            self.mixer.set_cell(SYSTEM_SOURCE, "personal", 1.0, False)
        for source_id in ["mic", SYSTEM_SOURCE] + list(self._sources.ids()):
            self._restore_strip(source_id)

    def _restore_strip(self, source_id):
        strip = self.matrix.source(source_id)
        if strip is None:
            return
        master = self.mixer.get_master(source_id)
        strip.set_master(master["volume"], master["muted"])
        for mix_id in ("personal", "chat", "record"):
            c = self.mixer.get_cell(source_id, mix_id)
            strip.set_cell(mix_id, c["volume"], c["muted"])

    def _set_source_level(self, source_id, level):
        cell = self.matrix.source(source_id)
        if cell is not None:
            cell.set_level(level)

    def _on_add_source_clicked(self, _matrix):
        dialog = AddSourceDialog(exclude_apps=self._sources.bound_apps())
        dialog.connect("source-confirmed", self._on_source_confirmed)
        dialog.present(self)

    def _on_source_confirmed(self, _dialog, name, members, icon_name):
        members = [m for m in members if m not in self._sources.bound_apps()]
        if not members:
            return  # all already bound — the picker hides these, but guard anyway
        source = Source.new(name=name, members=members, icon_name=icon_name)
        self._sources.add(source)
        self._sources.save()
        strip = self.matrix.add_source(
            source.id,
            name=source.name,
            icon_name=source.icon_name,
            has_level=True,
            removable=True,
            members=source.members,
            is_group=source.is_group,
        )
        self._wire_strip(strip, source.id, removable=True)
        # New channels are *moved* onto a private sink, so they're silent until
        # routed — default them audible in the Personal mix.
        self.mixer.set_cell(source.id, "personal", 1.0, False)
        self._restore_strip(source.id)
        self.mixer.set_sources(self._sources)
        self.mixerctl.poll_streams()

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
        self._remove_source(source_id)


class OpenWaveApp(Adw.Application):
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
            self._window = OpenWaveWindow(application=self)
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
        win = OpenWaveWindow(application=self)
        self._window = win
        win.present()

    def do_shutdown(self):
        if self._window and self._window.controller is not None:
            self._window.controller.stop()
        Adw.Application.do_shutdown(self)


def main():
    app = OpenWaveApp()
    app.run(sys.argv)
