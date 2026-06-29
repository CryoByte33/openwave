# OpenWave

Linux control application for the **Elgato Wave XLR** microphone interface. Device controls plus a Wave-Link-style submixer, built with GTK4 + Adwaita and reverse-engineered from the Elgato app.

## Features

- **Submixer** — Send any audio source to three independent mixes: Personal (what you hear), Chat, and Record. Each source has a fader per mix plus a master that scales all three, the way a GoXLR channel works.
- **Virtual mics** — The Chat and Record mixes show up as capture devices ("OpenWave Chat" and "OpenWave Record"), so Discord, OBS, and anything else can pick them like a normal microphone.
- **Source groups** — Put several apps under one channel — say, a "Games" group — so they share a single set of levels. Add or drop members, rename, or split the group from its menu.
- **Channel strips** — Vertical faders with live meters, mute, and drag-to-reorder; the tabs up top switch which mix you're editing.
- **Device controls** — Mic gain and mute (synced with the hardware button), headphone volume (synced with the knob), and low-impedance mode. The original Wave XLR and the MK.2 are both detected automatically.
- **Capture fix** — A background service (systemd or runit) keeps the mic stream alive to dodge a firmware race that otherwise drops capture to silence.
- **System tray** — Stays out of the way in the tray; mute from its menu.
- **First-run setup** — Handles USB permissions and the audio service for you.

## How it works

### USB control

The original Wave XLR (`0fd9:007d`) is a USB Audio Class 1 device and takes its settings over vendor control transfers on endpoint 0. On Linux `snd-usb-audio` normally blocks these, because `wIndex=0x3300` routes through interface 0, which the audio driver owns. OpenWave sends `wIndex=0x3303` instead: the firmware only checks the `0x33` prefix, while the kernel sees interface 3 (unclaimed) and lets the transfer through. No driver detach, no interrupted audio.

The MK.2 (`0fd9:00b6`) is a USB Audio Class 2 device with a different control scheme — standard class requests with `wIndex=0x0203` — so it gets its own backend. The app checks which one is plugged in and loads the matching one.

### Mixing

Each app's audio is moved onto its own PipeWire null sink, and a small `pw-loopback` carries that sink's monitor into each mix at the fader's volume. Pulling a fader down drops that source out of the mix rather than ducking everything. The Chat and Record mixes are null sinks too, with their monitors published as capture devices so other apps can record them. The mic is read straight from the hardware, and Personal feeds your headphones.

## Install

One-liner — detects Arch, Debian/Ubuntu, Fedora, openSUSE, or Void; installs deps and OpenWave:

```bash
curl -fsSL https://raw.githubusercontent.com/rikkichy/openwave/main/install.sh | sh
```

Or from a checkout:

```bash
git clone https://github.com/rikkichy/openwave.git
cd openwave
./install.sh                  # default PREFIX=/usr/local
PREFIX=/usr ./install.sh      # for packaging-style layout
```

Uninstall:

```bash
sudo make -C /path/to/openwave uninstall PREFIX=/usr/local
```

### Requirements

- Python 3.10+
- GTK4, libadwaita
- PipeWire (for the mixer and the capture fix)
- libusb 1.0

## Usage

```bash
openwave            # if installed via install.sh / PKGBUILD
python3 -m openwave  # from a checkout, no install needed
```

On first launch, OpenWave will prompt to set up USB permissions (via polkit) and install the audio service.

### Init systems

OpenWave detects your init system at runtime:

- **systemd** — the GUI installs a user unit at `~/.config/systemd/user/openwave.service` and enables it. No root needed for install or status checks.
- **runit** (Artix, Void, Devuan-runit) — the GUI cannot install the system service itself (writing to `/etc/sv` requires root). Create an `openwave-audio` service directory at `/etc/sv/openwave-audio/` whose `run` script execs `python3 -m openwave.daemon` as your user (typically via `chpst -u`), then enable it with `ln -s /etc/sv/openwave-audio /var/service/`.

  Status detection from the non-root GUI uses `sv check`; on stock Void the supervise FIFO is mode 0700, so OpenWave falls back to scanning `/proc` for the daemon process.

- **other** (macOS, Windows, no init detected) — the capture-fix section is disabled.

### Start hidden in tray
```bash
python3 -m openwave -- --hide
```

### Desktop entry
Copy `openwave.desktop` to `~/.local/share/applications/` for app launcher integration.

## Architecture

```
openwave/
  device.py           — USB backends for MK.1 and MK.2 (raw libusb via ctypes)
  app.py              — GTK4/Adwaita window; device pane and 10 Hz polling
  devicecontroller.py — device connect/poll/reconnect, kept off the UI thread
  mixmatrix.py        — the channel-strip mixer widget
  mixer.py            — submix engine: per-source sinks, loopbacks, capture devices
  routing.py          — pure routing: sources + levels in, a plan the mixer diffs out
  sources.py          — user channels, groups, and the stream→source match
  pipewire.py         — one adapter over pw-loopback / pw-cli / wpctl
  audio.py, daemon.py — the capture keepalive and its service entry point
  setup.py, service.py — first-run setup and init-system detection
  tray.py             — StatusNotifierItem tray icon over D-Bus
```

## Credits

USB protocol reverse-engineered from the macOS Wave Link application using Frida. Inspired by [GoXLR-on-Linux/goxlr-utility](https://github.com/GoXLR-on-Linux/goxlr-utility).

## License

MIT
