"""Mix matrix widget — a Wave-Link-style mixer of vertical channel strips.

Across the top is a mix switcher (Personal / Chat / Record). Below it sits one
:class:`ChannelStrip` per source — the Wave XLR mic, the System catch-all, and
each user channel. A channel may bind one app or several; a channel with two or
more member apps is a **group** and shows a "N apps" pill that opens a member
manager. Every member follows the channel's one set of levels.

Each strip's tall fader is that channel's *send into the currently selected mix*
(switching the mix re-points every fader to that mix's stored level, cached in
the strip so the switch is instant); a smaller master trim scales all three of
the channel's sends (effective send = cell × master, GoXLR-style). The strips
emit per-mix cell and per-channel master volume/mute signals, plus member-edit
signals for groups; the window wires those to the mixer.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GObject, Gdk  # noqa: E402

MIX_IDS = ("personal", "chat", "record")


class MixMatrix(Gtk.Box):
    """The mixer surface: a mix switcher over a scrollable row of channel strips."""

    __gsignals__ = {
        "add-source-clicked": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "remove-source-clicked": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "sources-reordered": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("openwave-matrix")

        self._mixes = {}          # mix_id -> (title, subtitle, icon_name)
        self._mix_order = []
        self._tab_btns = {}       # mix_id -> Gtk.ToggleButton
        self._active_mix = None
        self._strips = {}         # source_id -> ChannelStrip
        self._source_ids = []
        self._syncing_tabs = False

        # --- mix switcher -----------------------------------------------------
        switchbar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
            margin_start=16, margin_end=16, margin_top=14, margin_bottom=6,
        )
        self.append(switchbar)

        self._tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._tabs.add_css_class("openwave-mix-tabs")
        switchbar.append(self._tabs)

        self._context_lbl = Gtk.Label(xalign=0, valign=Gtk.Align.CENTER)
        self._context_lbl.add_css_class("dim-label")
        switchbar.append(self._context_lbl)

        # --- strip row --------------------------------------------------------
        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.append(scroll)

        self._strip_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
            margin_start=16, margin_end=16, margin_top=10, margin_bottom=16,
            valign=Gtk.Align.FILL,
        )
        scroll.set_child(self._strip_row)

        # The "+ Add" affordance lives at the end of the strip row.
        self._add_btn = Gtk.Button(valign=Gtk.Align.FILL)
        self._add_btn.add_css_class("openwave-add-strip")
        add_inner = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6,
            valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
        )
        add_inner.append(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_inner.append(Gtk.Label(label="Add\nsource", justify=Gtk.Justification.CENTER))
        self._add_btn.set_child(add_inner)
        self._add_btn.connect("clicked", lambda _: self.emit("add-source-clicked"))
        self._strip_row.append(self._add_btn)

    # ----- mixes -----
    def add_mix(self, mix_id, *, title, subtitle, icon_name):
        self._mixes[mix_id] = (title, subtitle, icon_name)
        self._mix_order.append(mix_id)

        btn = Gtk.ToggleButton()
        btn.add_css_class(f"openwave-tab-{mix_id}")
        content = Adw.ButtonContent(icon_name=icon_name, label=title)
        btn.set_child(content)
        btn.connect("toggled", self._on_tab_toggled, mix_id)
        self._tabs.append(btn)
        self._tab_btns[mix_id] = btn

        if self._active_mix is None:
            self._select_mix(mix_id)

    def _on_tab_toggled(self, btn, mix_id):
        if self._syncing_tabs:
            return
        if btn.get_active():
            self._select_mix(mix_id)
        elif mix_id == self._active_mix:
            # Re-pressing the active tab can't deselect it — keep it pinned.
            self._syncing_tabs = True
            btn.set_active(True)
            self._syncing_tabs = False

    def _select_mix(self, mix_id):
        self._active_mix = mix_id
        self._syncing_tabs = True
        for mid, b in self._tab_btns.items():
            b.set_active(mid == mix_id)
        self._syncing_tabs = False
        # Drive the per-mix accent bars: the active-mix class on the strip row
        # recolours every strip's accent bar via CSS.
        for mid in self._mix_order:
            self._strip_row.remove_css_class(f"openwave-mix-{mid}")
        self._strip_row.add_css_class(f"openwave-mix-{mix_id}")
        _, subtitle, _ = self._mixes[mix_id]
        self._context_lbl.set_label(subtitle)
        for strip in self._strips.values():
            strip.show_mix(mix_id)

    # ----- sources -----
    def add_source(self, source_id, *, name, icon_name, has_level=False,
                   removable=False, members=(), is_group=False):
        strip = ChannelStrip(
            name=name, icon_name=icon_name, has_level=has_level,
            removable=removable, members=tuple(members), is_group=is_group,
        )
        if removable:
            strip.connect(
                "remove-clicked",
                lambda _s, sid=source_id: self.emit("remove-source-clicked", sid),
            )
            self._wire_dnd(strip, source_id)
        # Insert before the trailing "+ Add" button.
        after = self._strips[self._source_ids[-1]] if self._source_ids else None
        self._strip_row.insert_child_after(strip, after)
        self._strips[source_id] = strip
        self._source_ids.append(source_id)
        if self._active_mix is not None:
            strip.show_mix(self._active_mix)
        return strip

    def remove_source(self, source_id):
        strip = self._strips.pop(source_id, None)
        if strip is None:
            return
        self._strip_row.remove(strip)
        self._source_ids.remove(source_id)

    def source(self, source_id):
        return self._strips.get(source_id)

    def source_order(self):
        """Channel ids in their current left-to-right order (mic/System first)."""
        return list(self._source_ids)

    # ----- drag-and-drop reordering (user channels only) -----
    def _wire_dnd(self, strip, source_id):
        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect("prepare", self._drag_prepare, source_id)
        drag.connect("drag-begin", self._drag_begin, strip)
        handle = strip.drag_handle
        handle.add_controller(drag)
        handle.set_cursor(Gdk.Cursor.new_from_name("grab", None))
        handle.set_tooltip_text("Drag to reorder")

        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        drop.connect("enter", self._on_drop_enter, strip)
        drop.connect("leave", self._on_drop_leave, strip)
        drop.connect("drop", self._on_drop, source_id)
        strip.add_controller(drop)

    def _drag_prepare(self, _src, _x, _y, source_id):
        return Gdk.ContentProvider.new_for_value(
            GObject.Value(GObject.TYPE_STRING, source_id))

    def _drag_begin(self, src, _drag, strip):
        src.set_icon(Gtk.WidgetPaintable.new(strip), strip.get_width() // 2, 20)

    def _on_drop_enter(self, _drop, _x, _y, strip):
        strip.add_css_class("openwave-drop-into")
        return Gdk.DragAction.MOVE

    def _on_drop_leave(self, _drop, strip):
        strip.remove_css_class("openwave-drop-into")

    def _on_drop(self, _drop, dragged_id, x, _y, target_id):
        target = self._strips.get(target_id)
        if target is not None:
            target.remove_css_class("openwave-drop-into")
        if not dragged_id or dragged_id == target_id or target is None:
            return False
        self._move_strip(dragged_id, target_id, after=x > target.get_width() / 2)
        return True

    def _move_strip(self, dragged_id, target_id, after):
        strip = self._strips.get(dragged_id)
        target = self._strips.get(target_id)
        if strip is None or target is None:
            return
        self._strip_row.remove(strip)
        # After removal, target's previous sibling is the right anchor for "before".
        anchor = target if after else target.get_prev_sibling()
        self._strip_row.insert_child_after(strip, anchor)
        self._source_ids.remove(dragged_id)
        idx = self._source_ids.index(target_id)
        self._source_ids.insert(idx + (1 if after else 0), dragged_id)
        self.emit("sources-reordered")


class ChannelStrip(Gtk.Box):
    """One vertical mixer channel: a tall send fader (into the selected mix) +
    meter, a master trim that scales all sends, mutes, and — for a group — a
    member-manager pill. Caches each mix's cell level so switching mixes is
    instant. Emits cell/master volume + mute signals and member-edit signals;
    setters are signal-blocked so restoring state doesn't echo back."""

    __gsignals__ = {
        "cell-volume-changed": (GObject.SignalFlags.RUN_FIRST, None, (str, float)),
        "cell-mute-toggled": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
        "master-volume-changed": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "master-mute-toggled": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "remove-clicked": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "add-member-clicked": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "remove-member-clicked": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "rename-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "ungroup-clicked": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, *, name, icon_name, has_level, removable, members, is_group):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("openwave-strip")
        self.add_css_class("card")
        # Fixed-width channel (like a real mixer). Setting hexpand explicitly also
        # stops the master slider's hexpand from propagating up and stretching the
        # strip to fill the row. Clip so the accent bar follows the card corners.
        self.set_size_request(150, -1)
        self.set_hexpand(False)
        self.set_valign(Gtk.Align.FILL)
        self.set_overflow(Gtk.Overflow.HIDDEN)

        self._name = name
        self._members = tuple(members)
        self._is_group = is_group
        self._removable = removable
        self._active_mix = MIX_IDS[0]
        self._cells = {m: {"volume": 0.0, "muted": False} for m in MIX_IDS}
        self._master = {"volume": 1.0, "muted": False}

        # Thin accent bar across the top; its colour tracks the selected mix (the
        # strip-row container carries an openwave-mix-<id> class that drives it).
        accent_bar = Gtk.Box()
        accent_bar.add_css_class("openwave-accentbar")
        self.append(accent_bar)

        inner = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10,
            margin_start=10, margin_end=10, margin_top=8, margin_bottom=12,
            vexpand=True,
        )
        self.append(inner)

        # User channels get a visible drag-handle grip (full-width so it's easy to
        # grab) that reorders the strip; the mic/System specials are fixed, so no
        # grip. MixMatrix attaches the drag source + grab cursor to it.
        if removable:
            self.drag_handle = Gtk.Box(hexpand=True)
            self.drag_handle.add_css_class("openwave-grip")
            grip_icon = Gtk.Image.new_from_icon_name("drag-handle-symbolic")
            grip_icon.set_halign(Gtk.Align.CENTER)
            grip_icon.set_hexpand(True)
            self.drag_handle.append(grip_icon)
            inner.append(self.drag_handle)
        else:
            self.drag_handle = None

        inner.append(self._build_header(icon_name, removable))
        inner.append(self._build_fader(has_level))
        inner.append(Gtk.Separator())
        inner.append(self._build_master())

        self._reflect_cell()
        self._reflect_master()

    # ----- header (icon, name, group pill, remove) -----
    def _build_header(self, icon_name, removable):
        # One tidy title row: icon chip (count badge for groups) · name · ⋮.
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        # icon chip + name grouped so the ⋮ sits at the row's trailing edge.
        title = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, hexpand=True)

        self._chip = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self._chip.add_css_class("openwave-strip-icon")
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(16)
        self._chip.append(icon)
        # Count badge pinned to the chip corner — the group tell; hidden for singles.
        overlay = Gtk.Overlay()
        overlay.set_child(self._chip)
        self._badge = Gtk.Label(
            halign=Gtk.Align.END, valign=Gtk.Align.END, visible=False)
        self._badge.add_css_class("openwave-count-badge")
        overlay.add_overlay(self._badge)
        title.append(overlay)

        self._name_lbl = Gtk.Label(
            label=self._name, xalign=0, hexpand=True, ellipsize=3,
            tooltip_text=self._name)
        self._name_lbl.add_css_class("heading")
        title.append(self._name_lbl)
        head.append(title)

        # Manage menu (⋮): every user channel has one (rename / members / ungroup /
        # remove, and lets a single grow into a group); the mic/System specials
        # don't. A flat circular button — no pill background.
        if removable:
            self._manage_btn = Gtk.MenuButton(
                icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                tooltip_text="Manage channel")
            self._manage_btn.add_css_class("flat")
            self._manage_btn.add_css_class("circular")
            self._member_pop = Gtk.Popover()
            self._manage_btn.set_popover(self._member_pop)
            head.append(self._manage_btn)
        else:
            self._manage_btn = None
            self._member_pop = None

        self._refresh_manage()
        return head

    def _refresh_manage(self):
        # Group = accent-tinted chip + count badge; single/special = plain chip.
        if self._is_group:
            self._chip.remove_css_class("openwave-strip-icon-plain")
            self._badge.set_label(str(len(self._members)))
            self._badge.set_visible(True)
        else:
            self._chip.add_css_class("openwave-strip-icon-plain")
            self._badge.set_visible(False)
        if self._member_pop is not None:
            self._member_pop.set_child(self._build_member_manager())

    def _build_member_manager(self):
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8,
            margin_start=12, margin_end=12, margin_top=12, margin_bottom=12,
        )
        box.set_size_request(240, -1)

        # Name first — it's the most common edit and reads as the channel's title.
        # Adw.EntryRow gives a visible "Channel name" label and an apply button
        # (checkmark) so it's clear this renames the channel and how to commit.
        name_title = Gtk.Label(label="Name", xalign=0)
        name_title.add_css_class("heading")
        box.append(name_title)

        name_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        name_list.add_css_class("boxed-list")
        rename_row = Adw.EntryRow(title="Channel name")
        rename_row.set_text(self._name)
        rename_row.set_show_apply_button(True)
        rename_row.connect(
            "apply", lambda r: self.emit("rename-requested", r.get_text().strip()))
        name_list.append(rename_row)
        box.append(name_list)

        box.append(Gtk.Separator())

        members_title = Gtk.Label(label="Members", xalign=0)
        members_title.add_css_class("heading")
        box.append(members_title)

        members_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        members_list.add_css_class("boxed-list")
        for app in self._members:
            row = Adw.ActionRow(title=app)
            rm = Gtk.Button(
                icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER,
                tooltip_text="Remove from group")
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _, a=app: self.emit("remove-member-clicked", a))
            row.add_suffix(rm)
            members_list.append(row)
        box.append(members_list)

        add_btn = Gtk.Button(label="Add app…")
        add_btn.add_css_class("flat")
        add_btn.connect("clicked", lambda _: self.emit("add-member-clicked"))
        box.append(add_btn)

        box.append(Gtk.Separator())
        # Splitting a group only makes sense once it actually holds several apps.
        if self._is_group:
            ungroup_btn = Gtk.Button(label="Ungroup")
            ungroup_btn.add_css_class("flat")
            ungroup_btn.connect("clicked", lambda _: self.emit("ungroup-clicked"))
            box.append(ungroup_btn)
        remove_btn = Gtk.Button(label="Remove channel")
        remove_btn.add_css_class("flat")
        remove_btn.add_css_class("destructive-action")
        remove_btn.connect("clicked", lambda _: self.emit("remove-clicked"))
        box.append(remove_btn)
        return box

    # ----- fader (send into the active mix) + meter -----
    def _build_fader(self, has_level):
        wrap = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            halign=Gtk.Align.CENTER, vexpand=True,
        )

        self._scale = Gtk.Scale(
            orientation=Gtk.Orientation.VERTICAL, inverted=True, draw_value=False,
            adjustment=Gtk.Adjustment(
                lower=0.0, upper=1.0, step_increment=0.01, page_increment=0.05),
            round_digits=2, vexpand=True,
            tooltip_text="Send into the selected mix",
        )
        self._scale.add_css_class("openwave-send-slider")
        self._scale.set_size_request(-1, 150)
        self._scale_handler = self._scale.connect("value-changed", self._on_cell_changed)
        wrap.append(self._scale)

        self._level = None
        if has_level:
            self._level = Gtk.LevelBar(
                orientation=Gtk.Orientation.VERTICAL, inverted=True,
                mode=Gtk.LevelBarMode.CONTINUOUS,
                min_value=0.0, max_value=1.0, vexpand=True,
            )
            self._level.set_size_request(10, 150)
            self._level.add_css_class("openwave-level")
            self._level.add_offset_value(Gtk.LEVEL_BAR_OFFSET_LOW, 0.70)
            self._level.add_offset_value(Gtk.LEVEL_BAR_OFFSET_HIGH, 0.90)
            self._level.add_offset_value(Gtk.LEVEL_BAR_OFFSET_FULL, 1.00)
            wrap.append(self._level)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, vexpand=True)
        outer.append(wrap)

        self._cell_value = Gtk.Label(label="100%")
        self._cell_value.add_css_class("caption")
        self._cell_value.add_css_class("monospace")
        self._cell_value.add_css_class("dim-label")
        outer.append(self._cell_value)

        self._cell_mute = Gtk.ToggleButton(
            label="Mute", halign=Gtk.Align.CENTER,
            tooltip_text="Mute this channel in the selected mix")
        self._cell_mute.add_css_class("openwave-mute")
        self._cell_mute_handler = self._cell_mute.connect("toggled", self._on_cell_mute)
        outer.append(self._cell_mute)
        return outer

    # ----- master trim (scales all sends) -----
    def _build_master(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("openwave-master-block")

        caption = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._master_mute = Gtk.ToggleButton(
            icon_name="audio-volume-high-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text="Mute this channel in every mix")
        self._master_mute.add_css_class("flat")
        self._master_mute.add_css_class("circular")
        self._master_mute_handler = self._master_mute.connect(
            "toggled", self._on_master_mute)
        caption.append(self._master_mute)
        cap_lbl = Gtk.Label(label="Master", xalign=0, hexpand=True)
        cap_lbl.add_css_class("caption")
        cap_lbl.add_css_class("dim-label")
        caption.append(cap_lbl)
        self._master_value = Gtk.Label(label="100%")
        self._master_value.add_css_class("caption")
        self._master_value.add_css_class("monospace")
        self._master_value.add_css_class("dim-label")
        caption.append(self._master_value)
        box.append(caption)

        self._master_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL, draw_value=False, hexpand=True,
            adjustment=Gtk.Adjustment(
                lower=0.0, upper=1.0, step_increment=0.01, page_increment=0.05),
            round_digits=2, tooltip_text="Master — scales all this channel's sends",
        )
        self._master_scale.add_css_class("openwave-master-slider")
        self._master_handler = self._master_scale.connect(
            "value-changed", self._on_master_changed)
        box.append(self._master_scale)
        return box

    # ----- state from the window (signal-blocked) -----
    def show_mix(self, mix_id):
        self._active_mix = mix_id
        self._reflect_cell()

    def set_cell(self, mix_id, volume, muted):
        if mix_id not in self._cells:
            return
        self._cells[mix_id] = {"volume": max(0.0, min(1.0, volume)), "muted": bool(muted)}
        if mix_id == self._active_mix:
            self._reflect_cell()

    def set_master(self, volume, muted):
        self._master = {"volume": max(0.0, min(1.0, volume)), "muted": bool(muted)}
        self._reflect_master()

    def set_level(self, value):
        if self._level is not None:
            self._level.set_value(max(0.0, min(1.0, value)))

    def set_members(self, members, is_group):
        self._members = tuple(members)
        self._is_group = is_group
        self._refresh_manage()

    def set_name(self, name):
        # Just update the header label; don't rebuild the popover (which would
        # yank the field the user just committed in). The manager seeds from
        # self._name next time it's rebuilt (e.g. on a member change).
        self._name = name
        self._name_lbl.set_label(name)
        self._name_lbl.set_tooltip_text(name)

    # ----- reflect cached state into widgets -----
    def _reflect_cell(self):
        c = self._cells[self._active_mix]
        with GObject.signal_handler_block(self._scale, self._scale_handler):
            self._scale.set_value(c["volume"])
        with GObject.signal_handler_block(self._cell_mute, self._cell_mute_handler):
            self._cell_mute.set_active(c["muted"])
        if c["muted"]:
            self._cell_value.set_label("Muted")
            self._cell_mute.add_css_class("error")
        else:
            self._cell_mute.remove_css_class("error")
            self._cell_value.set_label(
                "Off" if c["volume"] <= 0.0 else f"{round(c['volume'] * 100)}%")
        self._reflect_meter_state()

    def _reflect_master(self):
        with GObject.signal_handler_block(self._master_scale, self._master_handler):
            self._master_scale.set_value(self._master["volume"])
        self._master_value.set_label(f"{round(self._master['volume'] * 100)}%")
        muted = self._master["muted"]
        with GObject.signal_handler_block(self._master_mute, self._master_mute_handler):
            self._master_mute.set_active(muted)
        self._master_mute.set_child(Gtk.Image.new_from_icon_name(
            "audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic"))
        if muted:
            self._master_mute.add_css_class("error")
        else:
            self._master_mute.remove_css_class("error")
        self._reflect_meter_state()

    def _reflect_meter_state(self):
        if self._level is None:
            return
        silenced = self._master["muted"] or self._cells[self._active_mix]["muted"]
        if silenced:
            self._level.add_css_class("dim-label")
            self._level.remove_css_class("success")
        else:
            self._level.remove_css_class("dim-label")
            self._level.add_css_class("success")

    # ----- widget handlers -----
    def _on_cell_changed(self, scale):
        v = scale.get_value()
        self._cells[self._active_mix]["volume"] = v
        if not self._cells[self._active_mix]["muted"]:
            self._cell_value.set_label("Off" if v <= 0.0 else f"{round(v * 100)}%")
        self.emit("cell-volume-changed", self._active_mix, v)

    def _on_cell_mute(self, btn):
        muted = btn.get_active()
        self._cells[self._active_mix]["muted"] = muted
        self._reflect_cell()
        self.emit("cell-mute-toggled", self._active_mix, muted)

    def _on_master_changed(self, scale):
        v = scale.get_value()
        self._master["volume"] = v
        self._master_value.set_label(f"{round(v * 100)}%")
        self.emit("master-volume-changed", v)

    def _on_master_mute(self, btn):
        muted = btn.get_active()
        self._master["muted"] = muted
        self._reflect_master()
        self.emit("master-mute-toggled", muted)
