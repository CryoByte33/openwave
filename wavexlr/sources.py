"""User-defined matrix sources — a typed Source plus the SourceSet that owns
the collection, the stream→source match, and persistence to
~/.config/openwave/sources.json.

A source binds to a PipeWire application.name; any current or future stream from
that application is matched to the source and routed through its row.
"""

import json
import os
import uuid
from dataclasses import dataclass

CONFIG_PATH = os.path.expanduser("~/.config/openwave/sources.json")
DEFAULT_ICON = "applications-multimedia-symbolic"


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    match_app_name: str
    icon_name: str = DEFAULT_ICON

    @classmethod
    def new(cls, *, name, match_app_name, icon_name=DEFAULT_ICON):
        return cls(uuid.uuid4().hex[:12], name, match_app_name, icon_name)

    @classmethod
    def from_dict(cls, d):
        """Build from a persisted dict, tolerating missing optional fields. The
        id is required — a source without one is meaningless and is rejected."""
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            match_app_name=d.get("match_app_name", ""),
            icon_name=d.get("icon_name", DEFAULT_ICON),
        )

    def to_dict(self):
        return {"id": self.id, "name": self.name,
                "match_app_name": self.match_app_name, "icon_name": self.icon_name}


class SourceSet:
    """The user's app sources: identity, the stream→source match, persistence.

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
        """Application names already claimed by a source. One source per app —
        a second binding would steal the first's streams (matching is by name)."""
        return {s.match_app_name for s in self._by_id.values()}

    def add(self, source):
        self._by_id[source.id] = source

    def discard(self, source_id):
        self._by_id.pop(source_id, None)

    # ----- matching -----
    def source_for(self, stream):
        """id of the source bound to this stream's application, or None. On a
        tie (two sources on the same app) the last-added wins — preserving the
        previous dict-overwrite behaviour."""
        app = stream.get("app_name")
        match = None
        for s in self._by_id.values():
            if s.match_app_name == app:
                match = s.id
        return match

    def streams_for(self, source_id, streams):
        """The streams (from an iterable) belonging to source_id."""
        s = self._by_id.get(source_id)
        if s is None:
            return []
        return [st for st in streams if st.get("app_name") == s.match_app_name]

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
