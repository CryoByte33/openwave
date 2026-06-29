"""Best-effort friendly app names from the X11 window manager.

Apps that play audio through the ALSA->PulseAudio bridge report a generic
PipeWire name ("ALSA plug-in [java]"), but their owning X11 window usually
carries the real one ("RuneLite"). We bridge the two the way KDE does: match the
audio stream's ``application.process.id`` to a window's ``_NET_WM_PID``, then read
that window's name. For sandboxed apps (Flatpak) the stream PID and the window
PID are the same namespaced value, so they still match even though the host
``/proc`` knows nothing about it.

X11/XWayland only. Every failure path returns an empty map so callers fall back
to the PipeWire name; native-Wayland apps (no X11 window) just don't get enriched
and usually report a sane name already.
"""

import logging

_log = logging.getLogger("openwave.wmnames")


def pid_names():
    """{pid (int): window name (str)} for current top-level X11 windows."""
    try:
        from Xlib import X, display
        from Xlib.error import XError
    except Exception:
        return {}
    try:
        d = display.Display()
    except Exception:
        return {}
    try:
        root = d.screen().root
        a_clients = d.intern_atom("_NET_CLIENT_LIST")
        a_pid = d.intern_atom("_NET_WM_PID")
        a_name = d.intern_atom("_NET_WM_NAME")
        a_utf8 = d.intern_atom("UTF8_STRING")

        clients = root.get_full_property(a_clients, X.AnyPropertyType)
        if clients is None:
            return {}
        out = {}
        for wid in clients.value:
            try:
                w = d.create_resource_object("window", wid)
                pidp = w.get_full_property(a_pid, X.AnyPropertyType)
                if not pidp or not pidp.value:
                    continue
                pid = int(pidp.value[0])
                if pid in out:
                    continue
                name = _window_name(w, a_name, a_utf8)
                if name:
                    out[pid] = name
            except (XError, Exception):  # noqa: BLE001 — one bad window shouldn't sink the rest
                continue
        return out
    except Exception:
        return {}
    finally:
        try:
            d.close()
        except Exception:
            pass


def _window_name(w, a_name, a_utf8):
    """Prefer _NET_WM_NAME (e.g. "RuneLite"); fall back to WM_CLASS res_class."""
    try:
        p = w.get_full_property(a_name, a_utf8)
        if p and p.value:
            v = p.value
            text = v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)
            text = text.strip()
            if text:
                return text
    except Exception:
        pass
    try:
        cls = w.get_wm_class()  # (res_name, res_class)
        if cls and cls[1]:
            return cls[1]
    except Exception:
        pass
    return ""
