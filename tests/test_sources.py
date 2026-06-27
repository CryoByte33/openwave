"""Unit tests for the grouped-channel data model (no GTK, no PipeWire).

Run: python3 tests/test_sources.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wavexlr import sources
from wavexlr.sources import Source, SourceSet


def test_migrates_legacy_single_app():
    s = Source.from_dict({"id": "a", "name": "Discord", "match_app_name": "Discord"})
    assert s.members == ("Discord",)
    assert not s.is_group


def test_new_group_is_group():
    g = Source.new(name="Games", members=["Minecraft", "Forza", "Crimson Desert"])
    assert g.is_group
    single = Source.new(name="Cider", members=["Cider"])
    assert not single.is_group


def test_round_trip_dict():
    g = Source.new(name="Chat", members=["Discord", "Slack", "TeamSpeak"])
    assert Source.from_dict(g.to_dict()) == g


def test_match_any_member():
    g = Source.new(name="Games", members=["Minecraft", "Forza"])
    ss = SourceSet([g])
    assert ss.source_for({"app_name": "Forza"}) == g.id
    assert ss.source_for({"app_name": "Minecraft"}) == g.id
    assert ss.source_for({"app_name": "Firefox"}) is None


def test_streams_for_collects_all_members():
    g = Source.new(name="Games", members=["Minecraft", "Forza"])
    ss = SourceSet([g])
    streams = [
        {"id": 1, "app_name": "Minecraft"},
        {"id": 2, "app_name": "Forza"},
        {"id": 3, "app_name": "Firefox"},
    ]
    got = {st["id"] for st in ss.streams_for(g.id, streams)}
    assert got == {1, 2}


def test_bound_apps_unions_members():
    ss = SourceSet([
        Source.new(name="Games", members=["Minecraft", "Forza"]),
        Source.new(name="Cider", members=["Cider"]),
    ])
    assert ss.bound_apps() == {"Minecraft", "Forza", "Cider"}


def test_add_remove_member():
    g = Source.new(name="Chat", members=["Discord"])
    ss = SourceSet([g])
    ss.add_member(g.id, "Slack")
    assert set(ss.get(g.id).members) == {"Discord", "Slack"}
    ss.add_member(g.id, "Slack")  # idempotent
    assert ss.get(g.id).members.count("Slack") == 1
    ss.remove_member(g.id, "Discord")
    assert ss.get(g.id).members == ("Slack",)


def test_rename():
    g = Source.new(name="Old", members=["Discord"])
    ss = SourceSet([g])
    ss.rename(g.id, "New")
    assert ss.get(g.id).name == "New"


def test_ungroup_splits_and_preserves_icon():
    g = Source.new(name="Games", members=["Minecraft", "Forza"], icon_name="games")
    ss = SourceSet([g])
    new = ss.ungroup(g.id)
    assert g.id not in ss                       # group gone
    assert len(new) == 2
    assert {n.name for n in new} == {"Minecraft", "Forza"}
    assert all(not n.is_group and n.icon_name == "games" for n in new)
    assert {a for n in new for a in n.members} == {"Minecraft", "Forza"}


def test_reorder():
    a = Source.new(name="A", members=["a"])
    b = Source.new(name="B", members=["b"])
    c = Source.new(name="C", members=["c"])
    ss = SourceSet([a, b, c])
    ss.reorder([c.id, a.id, b.id])
    assert ss.ids() == [c.id, a.id, b.id]
    # ids not listed keep their relative order at the end
    ss.reorder([b.id])
    assert ss.ids()[0] == b.id and set(ss.ids()[1:]) == {c.id, a.id}


def test_ungroup_noop_on_single():
    s = Source.new(name="Cider", members=["Cider"])
    ss = SourceSet([s])
    assert ss.ungroup(s.id) == []
    assert s.id in ss


def test_persistence_round_trip(tmp_path=None):
    path = tempfile.mktemp(suffix=".json")
    old = sources.CONFIG_PATH
    sources.CONFIG_PATH = path
    try:
        ss = SourceSet([
            Source.new(name="Games", members=["Minecraft", "Forza"]),
            Source.new(name="Cider", members=["Cider"]),
        ])
        ss.save()
        back = SourceSet.load()
        assert {tuple(sorted(s.members)) for s in back} == {
            ("Forza", "Minecraft"), ("Cider",)}
    finally:
        sources.CONFIG_PATH = old
        if os.path.exists(path):
            os.remove(path)


def test_load_migrates_legacy_file(tmp_path=None):
    path = tempfile.mktemp(suffix=".json")
    old = sources.CONFIG_PATH
    sources.CONFIG_PATH = path
    try:
        with open(path, "w") as f:
            f.write('{"x": {"id": "x", "name": "Discord", "match_app_name": "Discord"}}')
        back = SourceSet.load()
        s = back.get("x")
        assert s is not None and s.members == ("Discord",)
    finally:
        sources.CONFIG_PATH = old
        os.remove(path)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
