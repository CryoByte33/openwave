"""'Add Source' picker. Pick one app for a single channel, or several to make a
group; then name + icon. A lighter PickAppsDialog adds apps to an existing group.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GObject  # noqa: E402

from .pipewire import output_streams

ICON_CHOICES = (
    ("applications-multimedia-symbolic", "Generic"),
    ("applications-games-symbolic", "Games"),
    ("input-gaming-symbolic", "Controller"),
    ("audio-x-generic-symbolic", "Music"),
    ("multimedia-player-symbolic", "Player"),
    ("user-available-symbolic", "Voice"),
    ("system-users-symbolic", "Chat"),
    ("web-browser-symbolic", "Browser"),
    ("video-display-symbolic", "Video"),
    ("preferences-desktop-multimedia-symbolic", "Media"),
    ("audio-headphones-symbolic", "Headphones"),
    ("microphone-sensitivity-high-symbolic", "Mic"),
)


def _available_apps(exclude_apps):
    """{app_name: sample subtitle} for apps currently playing audio that aren't
    already claimed by a source."""
    exclude = set(exclude_apps)
    apps = {}
    for s in output_streams():
        app = s.get("app_name")
        if not app or app in exclude or app in apps:
            continue
        apps[app] = s.get("media_name") or s.get("node_name", "")
    return apps


def _app_check_list(apps, on_change):
    """A boxed list of check-button rows, one per app. Calls on_change(set) with
    the currently-checked app names whenever a row toggles."""
    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")
    checked = set()

    def toggled(btn, app):
        checked.add(app) if btn.get_active() else checked.discard(app)
        on_change(set(checked))

    for app in sorted(apps):
        row = Adw.ActionRow(title=app)
        if apps[app]:
            row.set_subtitle(apps[app])
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        check.connect("toggled", toggled, app)
        row.add_prefix(check)
        row.set_activatable_widget(check)
        listbox.append(row)
    return listbox


class AddSourceDialog(Adw.Dialog):
    __gsignals__ = {
        # (display_name, members: list[str], icon_name)
        "source-confirmed": (
            GObject.SignalFlags.RUN_FIRST, None, (str, GObject.TYPE_PYOBJECT, str)),
    }

    def __init__(self, exclude_apps=()):
        super().__init__()
        # Apps already bound to a source — hidden so an app can't be claimed
        # twice (the second binding would steal the first's streams).
        self._exclude_apps = set(exclude_apps)
        self.set_title("Add Source")
        self.set_content_width(480)
        self.set_content_height(560)

        self._nav = Adw.NavigationView()
        self.set_child(self._nav)

        self._checked = set()
        self._selected_icon = ICON_CHOICES[0][0]

        self._nav.push(self._build_picker_page())

    # ------------------------------------------------------------ page 1
    def _build_picker_page(self):
        page = Adw.NavigationPage(title="Pick Applications")
        view = Adw.ToolbarView()
        page.set_child(view)

        header = Adw.HeaderBar()
        view.add_top_bar(header)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        self._next_btn = Gtk.Button(label="Next")
        self._next_btn.add_css_class("suggested-action")
        self._next_btn.set_sensitive(False)
        self._next_btn.connect("clicked", self._on_next)
        header.pack_end(self._next_btn)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        view.set_content(scroll)

        clamp = Adw.Clamp(
            maximum_size=440,
            margin_start=12, margin_end=12, margin_top=12, margin_bottom=12,
        )
        scroll.set_child(clamp)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        clamp.set_child(outer)

        hint = Gtk.Label(
            label="Pick an app that's currently playing audio — or check several "
                  "to group them under one set of mix levels. OpenWave routes any "
                  "future streams from these apps through the new channel.",
            wrap=True, xalign=0,
        )
        hint.add_css_class("dim-label")
        outer.append(hint)

        apps = _available_apps(self._exclude_apps)
        if not apps:
            empty = Adw.ActionRow(title="No new apps playing audio")
            empty.set_subtitle("Every app currently playing is already a source, "
                               "or nothing is playing. Start an app, then try again.")
            empty.set_sensitive(False)
            box = Gtk.ListBox()
            box.add_css_class("boxed-list")
            box.append(empty)
            outer.append(box)
        else:
            outer.append(_app_check_list(apps, self._on_checked_changed))
        return page

    def _on_checked_changed(self, checked):
        self._checked = checked
        self._next_btn.set_sensitive(bool(checked))

    def _on_next(self, _btn):
        if self._checked:
            self._nav.push(self._build_config_page())

    # ------------------------------------------------------------ page 2
    def _build_config_page(self):
        page = Adw.NavigationPage(title="Name and Icon")
        view = Adw.ToolbarView()
        page.set_child(view)

        header = Adw.HeaderBar()
        view.add_top_bar(header)

        is_group = len(self._checked) > 1
        add_btn = Gtk.Button(label="Create Group" if is_group else "Add Source")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self._on_confirm)
        header.pack_end(add_btn)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        view.set_content(scroll)

        clamp = Adw.Clamp(
            maximum_size=440,
            margin_start=12, margin_end=12, margin_top=12, margin_bottom=12,
        )
        scroll.set_child(clamp)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        clamp.set_child(outer)

        name_group = Adw.PreferencesGroup(
            title="Name",
            description=("Grouping " + ", ".join(sorted(self._checked))) if is_group else None,
        )
        outer.append(name_group)

        self._name_row = Adw.EntryRow(title="Channel name")
        default = "" if is_group else next(iter(self._checked))
        self._name_row.set_text(default)
        name_group.add(self._name_row)

        icon_group = Adw.PreferencesGroup(title="Icon")
        outer.append(icon_group)

        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.SINGLE,
            max_children_per_line=6, min_children_per_line=4,
            column_spacing=6, row_spacing=6,
            margin_start=4, margin_end=4, margin_top=8, margin_bottom=8,
            homogeneous=True,
        )
        flow.add_css_class("openwave-icon-picker")
        first_child = None
        for icon_name, tooltip in ICON_CHOICES:
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(28)
            child = Gtk.FlowBoxChild()
            child.set_child(img)
            child.set_tooltip_text(tooltip)
            child._icon_name = icon_name  # noqa: SLF001
            flow.append(child)
            if first_child is None:
                first_child = child
        flow.connect("selected-children-changed", self._on_icon_selected)
        icon_group.add(flow)

        if first_child is not None:
            flow.select_child(first_child)
            self._selected_icon = first_child._icon_name  # noqa: SLF001
        return page

    def _on_icon_selected(self, flow):
        sel = flow.get_selected_children()
        if sel:
            self._selected_icon = getattr(sel[0], "_icon_name", self._selected_icon)

    def _on_confirm(self, _btn):
        if not self._checked:
            return
        members = sorted(self._checked)
        name = self._name_row.get_text().strip() or (
            "Group" if len(members) > 1 else members[0])
        self.emit("source-confirmed", name, members, self._selected_icon)
        self.close()


class PickAppsDialog(Adw.Dialog):
    """Minimal multi-select app picker used to add apps to an existing group."""

    __gsignals__ = {
        "apps-picked": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
    }

    def __init__(self, exclude_apps=()):
        super().__init__()
        self.set_title("Add Apps")
        self.set_content_width(440)
        self.set_content_height(480)
        self._checked = set()

        view = Adw.ToolbarView()
        self.set_child(view)
        header = Adw.HeaderBar()
        view.add_top_bar(header)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _: self.close())
        header.pack_start(cancel)
        self._add = Gtk.Button(label="Add")
        self._add.add_css_class("suggested-action")
        self._add.set_sensitive(False)
        self._add.connect("clicked", self._on_add)
        header.pack_end(self._add)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        view.set_content(scroll)
        clamp = Adw.Clamp(
            maximum_size=400,
            margin_start=12, margin_end=12, margin_top=12, margin_bottom=12,
        )
        scroll.set_child(clamp)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        clamp.set_child(outer)

        apps = _available_apps(exclude_apps)
        if not apps:
            row = Adw.ActionRow(title="No other apps playing audio")
            row.set_sensitive(False)
            box = Gtk.ListBox()
            box.add_css_class("boxed-list")
            box.append(row)
            outer.append(box)
        else:
            outer.append(_app_check_list(apps, self._on_checked_changed))

    def _on_checked_changed(self, checked):
        self._checked = checked
        self._add.set_sensitive(bool(checked))

    def _on_add(self, _btn):
        if self._checked:
            self.emit("apps-picked", sorted(self._checked))
        self.close()
