"""First-run setup: udev rule, WirePlumber rule, audio service."""

import os
import subprocess

from . import pwnames, service
from .pipewire import SubprocessPipeWire

_pw = SubprocessPipeWire()

UDEV_RULES = (
    # Wave XLR
    'SUBSYSTEM=="usb", ATTR{idVendor}=="0fd9", ATTR{idProduct}=="007d", MODE="0666"',
    # Wave XLR mk 2
    'SUBSYSTEM=="usb", ATTR{idVendor}=="0fd9", ATTR{idProduct}=="00b6", MODE="0666"',
)
UDEV_PATH = "/etc/udev/rules.d/99-openwave.rules"
UDEV_PATH_OLD = "/etc/udev/rules.d/99-wavexlr.rules"

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIREPLUMBER_SOURCES = (
    os.path.join(_APP_DIR, "wireplumber", "51-openwave-wave-xlr.conf"),
    "/usr/local/share/openwave/wireplumber/51-openwave-wave-xlr.conf",
    "/usr/share/openwave/wireplumber/51-openwave-wave-xlr.conf",
)
WIREPLUMBER_PATH = os.path.expanduser(
    "~/.config/wireplumber/wireplumber.conf.d/51-openwave-wave-xlr.conf"
)

MIXES_SOURCES = (
    os.path.join(_APP_DIR, "pipewire", "52-openwave-mixes.conf"),
    "/usr/local/share/openwave/pipewire/52-openwave-mixes.conf",
    "/usr/share/openwave/pipewire/52-openwave-mixes.conf",
)
MIXES_PATH = os.path.expanduser(
    "~/.config/pipewire/pipewire.conf.d/52-openwave-mixes.conf"
)


def udev_installed():
    # Require a rule covering BOTH product IDs — an older 007d-only rule must be
    # upgraded so the MK.2 (00b6) also gets 0666 access for the vendor protocol.
    needed = ("007d", "00b6")
    for path in (UDEV_PATH, UDEV_PATH_OLD):
        try:
            with open(path) as f:
                content = f.read().lower()
        except (FileNotFoundError, PermissionError):
            continue
        if all(pid in content for pid in needed):
            return True
    return False


def service_installed():
    return service.is_installed()


def service_current():
    return service.is_current()


def _read_first(sources):
    """Return the content of the first existing source template, or None."""
    for src in sources:
        try:
            with open(src) as f:
                return f.read()
        except (FileNotFoundError, PermissionError):
            continue
    return None


def _installed_current(path, sources):
    """True if `path` exists and matches the current source template, so a
    re-run upgrades a stale file in place instead of skipping it. Falls back to
    a plain existence check when the template can't be located."""
    want = _read_first(sources)
    try:
        with open(path) as f:
            have = f.read()
    except (FileNotFoundError, PermissionError):
        return False
    return have == want if want is not None else True


def wireplumber_installed():
    return _installed_current(WIREPLUMBER_PATH, WIREPLUMBER_SOURCES)


def mixes_installed():
    return _installed_current(MIXES_PATH, MIXES_SOURCES)


def needs_setup():
    # Config currency is included (static templates → reliable), but service
    # *currency* is not: the unit's WorkingDirectory reflects the install
    # location (site-packages vs a dev checkout), which differs harmlessly and
    # would otherwise nag forever. A stale unit is still refreshed by run_setup.
    return (
        not udev_installed()
        or not service_installed()
        or not wireplumber_installed()
        or not mixes_installed()
    )


def install_udev():
    """Install udev rule via pkexec."""
    rules = "\n".join(UDEV_RULES)
    script = f"""#!/bin/sh
cat > {UDEV_PATH} <<'EOF'
{rules}
EOF
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --attr-match=idVendor=0fd9
# Also chmod the device node directly so no replug is needed
for dev in /dev/bus/usb/*/; do
    for f in "$dev"*; do
        if udevadm info --query=property "$f" 2>/dev/null | grep -q 'ID_VENDOR_ID=0fd9'; then
            chmod 0666 "$f"
        fi
    done
done
"""
    tmp = "/tmp/openwave-udev-setup.sh"
    with open(tmp, "w") as f:
        f.write(script)
    os.chmod(tmp, 0o755)

    r = subprocess.run(["pkexec", tmp], capture_output=True, text=True)
    os.unlink(tmp)
    return r.returncode == 0


def install_service():
    """Install and enable the audio service via the active backend."""
    service.install()
    return True


def install_wireplumber():
    """Drop the suspend-disable rule into the user's WirePlumber config."""
    content = _read_first(WIREPLUMBER_SOURCES)
    if content is None:
        raise FileNotFoundError(
            "WirePlumber rule source not found. Looked in: "
            + ", ".join(WIREPLUMBER_SOURCES)
        )
    os.makedirs(os.path.dirname(WIREPLUMBER_PATH), exist_ok=True)
    with open(WIREPLUMBER_PATH, "w") as f:
        f.write(content)
    return True


# (node name, description) for each mix-bus sink, from the shared vocabulary.
MIX_SINKS = tuple(
    (pwnames.MIX_SINKS[m], pwnames.MIX_SINK_DESCRIPTIONS[m])
    for m in ("personal", "chat", "record")
)


def _mix_sink_exists(name):
    """Return True if a PipeWire/Pulse sink with this name is already live."""
    return any(len(p) > 1 and p[1] == name for p in _pw.short_list("sinks"))


def _create_mix_sink_live(name, description):
    """Spawn a null sink immediately so it appears without a PipeWire restart.
    No-op (and harmless) if the config file's sink is already live, or if
    pw-cli can't reach PipeWire — the config takes effect on next load."""
    if not _mix_sink_exists(name):
        _pw.create_null_sink(name, description)


def install_mixes():
    """Drop the three virtual mix sinks into the user's PipeWire config."""
    content = _read_first(MIXES_SOURCES)
    if content is None:
        raise FileNotFoundError(
            "Mix sinks config source not found. Looked in: "
            + ", ".join(MIXES_SOURCES)
        )
    os.makedirs(os.path.dirname(MIXES_PATH), exist_ok=True)
    with open(MIXES_PATH, "w") as f:
        f.write(content)
    for name, desc in MIX_SINKS:
        _create_mix_sink_live(name, desc)
    return True


def run_setup():
    """Run full first-time setup. Returns (success, message)."""
    messages = []

    if not udev_installed():
        if install_udev():
            messages.append("USB permissions configured")
        else:
            return False, "Failed to set up USB permissions (pkexec cancelled?)"

    # Install the WirePlumber rule before starting the service so the daemon's
    # pw-cat attaches to a node that already has suspend disabled. The checks
    # are content-aware, so a re-run also refreshes a config that changed
    # between OpenWave versions instead of skipping it.
    if not wireplumber_installed():
        try:
            install_wireplumber()
            messages.append(
                "WirePlumber rule installed (restart wireplumber to apply)"
            )
        except Exception as e:
            return False, f"Failed to install WirePlumber rule: {e}"

    if not mixes_installed():
        try:
            install_mixes()
            messages.append(
                "Mix sinks installed (restart PipeWire to apply)"
            )
        except Exception as e:
            return False, f"Failed to install mix sinks: {e}"

    if not service_installed() or not service_current():
        try:
            install_service()
            messages.append("Audio service installed and started")
        except Exception as e:
            return False, f"Failed to install service: {e}"

    return True, ". ".join(messages) if messages else "Already configured"


def uninstall_service():
    """Stop, disable, and remove the audio service via the active backend."""
    service.uninstall()


def uninstall_wireplumber():
    """Remove the WirePlumber rule from the user's config."""
    try:
        os.unlink(WIREPLUMBER_PATH)
    except FileNotFoundError:
        return False
    return True


def uninstall_mixes():
    """Remove the mix sinks config from the user's PipeWire config."""
    try:
        os.unlink(MIXES_PATH)
    except FileNotFoundError:
        return False
    return True


def uninstall_udev():
    """Remove udev rule via pkexec."""
    script = f"""#!/bin/sh
rm -f {UDEV_PATH} {UDEV_PATH_OLD}
udevadm control --reload-rules
"""
    tmp = "/tmp/openwave-udev-remove.sh"
    with open(tmp, "w") as f:
        f.write(script)
    os.chmod(tmp, 0o755)
    r = subprocess.run(["pkexec", tmp], capture_output=True, text=True)
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    return r.returncode == 0


def run_uninstall():
    """Remove capture fix service, WirePlumber rule, and udev rule. Returns (success, message)."""
    messages = []

    if service_installed():
        try:
            uninstall_service()
            messages.append("Audio service removed")
        except Exception as e:
            return False, f"Failed to remove service: {e}"

    if wireplumber_installed():
        if uninstall_wireplumber():
            messages.append("WirePlumber rule removed")

    if mixes_installed():
        if uninstall_mixes():
            messages.append("Mix sinks removed")

    if udev_installed():
        if uninstall_udev():
            messages.append("USB permissions removed")
        else:
            return False, "Failed to remove USB permissions (pkexec cancelled?)"

    return True, ". ".join(messages) if messages else "Already uninstalled"
