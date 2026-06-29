"""Unit tests for the friendly-name classifier (no PipeWire, no X11).

Run: python3 tests/test_names.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openwave.pipewire import _binary_name, _is_generic


def test_name_matching_binary_is_not_generic():
    # The Zen regression: a good name that happens to equal its binary must NOT
    # be enriched (it was resolving to another sandbox's window, "Bolt Launcher").
    assert _is_generic("Zen", "zen") is False
    assert _is_generic("Discord", "discord") is False
    assert _is_generic("Firefox", "firefox") is False


def test_bridge_and_engine_labels_are_generic():
    assert _is_generic("ALSA plug-in [java]", "java") is True
    assert _is_generic("Chromium", "Cider") is True
    assert _is_generic("Electron", "someapp") is True


def test_runtime_names_are_generic():
    assert _is_generic("java", "java") is True
    assert _is_generic("python3", "python3") is True


def test_empty_and_unknown_are_generic():
    assert _is_generic("", "") is True
    assert _is_generic(None, None) is True
    assert _is_generic("Unknown", "") is True


def test_binary_name_prefers_real_binary():
    assert _binary_name("Cider", "Chromium") == "Cider"


def test_binary_name_rejects_runtimes_and_echoes():
    assert _binary_name("java", "ALSA plug-in [java]") is None   # runtime
    assert _binary_name("zen", "Zen") is None                    # just echoes name
    assert _binary_name("", "Chromium") is None                  # nothing to use


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
