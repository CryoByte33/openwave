"""User-defined matrix channels — a typed Source plus the SourceSet that owns
the collection, the stream→source match, and persistence to
~/.config/openwave/sources.json.

A source is one matrix channel. It matches one or more PipeWire
application.name values — its *members*; any current or future stream from a
member app is matched to the source and routed through its row. A source with
two or more members is a **group**: every member follows the one set of mix
levels (there are no per-member levels). One app belongs to at most one source —
a second binding would steal the first's streams, since matching is by name.
"""

import json
import os
import uuid
from dataclasses import dataclass, replace

CONFIG_PATH = os.path.expanduser("~/.config/openwave/sources.json")
DEFAULT_ICON = "applications-multimedia-symbolic"


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    members: tuple          # app names this channel matches (1..N); >=2 is a group
    icon_name: str = DEFAULT_ICON

    @classmethod
    def new(cls, *, name, members, icon_name=DEFAULT_ICON):
        return cls(uuid.uuid4().hex[:12], name, tuple(members), icon_name)

    @classmethod
    def from_dict(cls, d):
        """Build from a persisted dict, tolerating missing optional fields and
        the pre-grouping schema (a single `match_app_name`). The id is required —
        a source without one is meaningless and is rejected."""
        members = d.get("members")
        if members is None:                      # migrate the old single-app form
            one = d.get("match_app_name", "")
            members = [one] if one else []
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            members=tuple(members),
            icon_name=d.get("icon_name", DEFAULT_ICON),
        )

    def to_dict(self):
        return {"id": self.id, "name": self.name,
                "members": list(self.members), "icon_name": self.icon_name}

    @property
    def is_group(self):
        return len(self.members) >= 2


class SourceSet:
    """The user's app channels: identity, the stream→source match, persistence.

    The match rule lives here once; the mixer, the window, and the add-source
    dialog ask it instead of re-deriving 'does this stream belong to this
    source'. Construct from any iterable of Source (including another SourceSet,
    which copies)."""

    def __init__(self, sources=()):
        self._by_id = {s.id: s for s in sources}

    # ----- collection -----
    def __iter__(self):
        return iter(self._by_id.values())

    def __contains__(self, source_id):
        return source_id in self._by_id

    def __len__(self):
        return len(self._by_id)

    def ids(self):
        return list(self._by_id)

    def get(self, source_id):
        return self._by_id.get(source_id)

    def bound_apps(self):
        """Application names already claimed by some source's membership. One app
        per source — a second binding would steal the first's streams."""
        return {app for s in self._by_id.values() for app in s.members}

    def add(self, source):
        self._by_id[source.id] = source

    def discard(self, source_id):
        self._by_id.pop(source_id, None)

    # ----- membership edits (frozen Source, so each returns a replaced copy) -----
    def add_member(self, source_id, app_name):
        s = self._by_id.get(source_id)
        if s is None or app_name in s.members:
            return
        self._by_id[source_id] = replace(s, members=s.members + (app_name,))

    def remove_member(self, source_id, app_name):
        """Drop an app from a source. The caller decides what to do if this
        empties the source (the window removes the now-memberless channel)."""
        s = self._by_id.get(source_id)
        if s is None or app_name not in s.members:
            return
        self._by_id[source_id] = replace(
            s, members=tuple(m for m in s.members if m != app_name))

    def rename(self, source_id, name):
        s = self._by_id.get(source_id)
        if s is not None:
            self._by_id[source_id] = replace(s, name=name)

    def reorder(self, ordered_ids):
        """Reorder the channels to match ordered_ids; any id not listed keeps its
        relative order at the end. Persisted order is just dict insertion order,
        so saving after this makes the new order stick across restarts."""
        new = {sid: self._by_id[sid] for sid in ordered_ids if sid in self._by_id}
        for sid, s in self._by_id.items():
            new.setdefault(sid, s)
        self._by_id = new

    def ungroup(self, source_id):
        """Split a group into one single-app channel per member. Removes the
        group and returns the new Sources, so the caller can copy the group's
        mix levels onto them (audio shouldn't jump). No-op for a non-group."""
        s = self._by_id.get(source_id)
        if s is None or not s.is_group:
            return []
        self.discard(source_id)
        new = [Source.new(name=app, members=[app], icon_name=s.icon_name)
               for app in s.members]
        for ns in new:
            self.add(ns)
        return new

    # ----- matching -----
    def source_for(self, stream):
        """id of the source whose membership claims this stream's application, or
        None. On a tie (an app claimed by two sources, which the UI prevents) the
        last-added wins — preserving the previous dict-overwrite behaviour."""
        app = stream.get("app_name")
        match = None
        for s in self._by_id.values():
            if app in s.members:
                match = s.id
        return match

    def streams_for(self, source_id, streams):
        """The streams (from an iterable) belonging to source_id's members."""
        s = self._by_id.get(source_id)
        if s is None:
            return []
        return [st for st in streams if st.get("app_name") in s.members]

    # ----- persistence -----
    @classmethod
    def load(cls):
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        out = []
        for v in data.values():
            try:
                out.append(Source.from_dict(v))
            except (KeyError, TypeError, AttributeError):
                continue  # skip a malformed entry rather than crash the app
        return cls(out)

    def save(self):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({s.id: s.to_dict() for s in self._by_id.values()}, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
