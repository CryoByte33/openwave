"""Wave XLR USB device backend.

Uses raw libusb control transfers with wIndex=0x3303 to bypass the Linux
kernel's interface routing. The kernel sees interface 3 (unclaimed) and
lets the transfer through, while the firmware only checks the 0x33 prefix.
No driver detach needed — audio is never interrupted.
"""

import ctypes
import ctypes.util
import glob
import logging
import os
import re
import struct
import subprocess
import threading

_log = logging.getLogger("wavexlr.device")

VENDOR_ID = 0x0FD9
# Product IDs: MK1 = original UAC1 Wave XLR, MK2 = UAC2 Wave XLR MK.2. They
# speak different control protocols, so each has its own backend class below
# (WaveXLR for MK1, WaveXLRMk2 for MK2). detect_pid() picks the right one.
PID_MK1 = 0x007D
PID_MK2 = 0x00B6

BREQUEST_READ = 0x85
BREQUEST_WRITE = 0x05
WVALUE_CONFIG = 0x0000
WVALUE_METER = 0x0001
WVALUE_DEVINFO = 0x000A
WINDEX = 0x3303  # 0x3303 not 0x3300 — bypasses snd-usb-audio ownership check
CONFIG_LEN = 34
METER_LEN = 10

RT_CLASS_IN = 0xA1
RT_CLASS_OUT = 0x21

OFF_GAIN = 0
OFF_MUTE = 4
OFF_HP_VOL = 9
OFF_VOL_SELECT = 14
OFF_LOW_Z = 33

# --- Raw libusb setup ---
_lib_path = ctypes.util.find_library("usb-1.0") or "libusb-1.0.so.0"
_lib = ctypes.CDLL(_lib_path)

_lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
_lib.libusb_init.restype = ctypes.c_int
_lib.libusb_open_device_with_vid_pid.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
_lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
_lib.libusb_close.argtypes = [ctypes.c_void_p]
_lib.libusb_close.restype = None
_lib.libusb_control_transfer.argtypes = [
    ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8,
    ctypes.c_uint16, ctypes.c_uint16,
    ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint16, ctypes.c_uint,
]
_lib.libusb_control_transfer.restype = ctypes.c_int

_ctx = ctypes.c_void_p()
_lib.libusb_init(ctypes.byref(_ctx))


def _find_card():
    """Find the ALSA card number for the Wave XLR."""
    try:
        r = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=3)
        for line in r.stdout.splitlines():
            if "Wave XLR" in line or "Elgato" in line:
                return line.split(":")[0].split()[-1]
    except Exception:
        pass
    return None


def _amixer(card, *args):
    """Run amixer and return stdout."""
    try:
        r = subprocess.run(
            ["amixer", "-c", card, *args],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout
    except Exception:
        return ""


def _alsa_get(card):
    """Read ALSA mute and HP volume."""
    state = {}
    # Mute (numid=5)
    out = _amixer(card, "cget", "numid=5")
    state["mute"] = ": values=off" in out
    # HP volume (numid=4) — raw ALSA value 0-120
    out = _amixer(card, "cget", "numid=4")
    for line in out.splitlines():
        if ": values=" in line:
            try:
                state["hp_vol"] = int(line.split("=")[-1])
            except ValueError:
                pass
    return state


def _alsa_set_mute(card, muted):
    _amixer(card, "cset", "numid=5", "off" if muted else "on")


def _alsa_set_hp_vol(card, value):
    """Set ALSA HP volume (numid=4, 0-120)."""
    _amixer(card, "cset", "numid=4", str(max(0, min(120, value))))


def _fw_hp_to_alsa(fw_hp_raw):
    """Map firmware HP to ALSA (0-120).

    Firmware: int16 Q8.8, range -32768 (-128 dB) to 0 (0 dB).
    ALSA driver caps lower at 0 → -60 dB; anything below saturates.
    ALSA step = 0.5 dB, so dB = (value - 120) * 0.5 → value = dB / 0.5 + 120.
    """
    db = fw_hp_raw / 256.0
    return max(0, min(120, round(db / 0.5 + 120)))


def _alsa_hp_to_fw(alsa_hp):
    """Map ALSA HP (0-120) to firmware HP (int16 Q8.8)."""
    db = (alsa_hp - 120) * 0.5  # 0→-60, 120→0
    db = max(-128.0, min(0.0, db))  # firmware range
    return int(db * 256)


class WaveXLR:
    supports_low_impedance = True
    supports_volume_select = True
    hp_detents = None   # continuous dB slider (mk1)

    def format_gain(self, raw):
        """Human-readable gain for the UI (mk1 gain is an opaque raw value)."""
        return f"0x{raw:04X}"

    def __init__(self):
        self._handle = None
        self._lock = threading.Lock()
        self._card = None
        self._last_fw = None  # last known firmware state for change detection

    @property
    def connected(self):
        return self._handle is not None

    def connect(self):
        handle = _lib.libusb_open_device_with_vid_pid(_ctx, VENDOR_ID, PID_MK1)
        if not handle:
            raise RuntimeError("Wave XLR not found")
        self._handle = handle
        self._card = _find_card()

    def disconnect(self):
        if self._handle:
            _lib.libusb_close(self._handle)
            self._handle = None
        self._card = None
        self._last_fw = None

    def _ctrl_read(self, wValue, length):
        """USB control read — no detach needed."""
        buf = (ctypes.c_ubyte * length)()
        with self._lock:
            ret = _lib.libusb_control_transfer(
                self._handle, RT_CLASS_IN, BREQUEST_READ, wValue, WINDEX,
                buf, length, 1000,
            )
        if ret < 0:
            raise RuntimeError(f"USB read failed (err {ret})")
        return bytearray(buf[:ret])

    def _ctrl_write(self, wValue, data):
        """USB control write — no detach needed."""
        data = bytes(data)
        buf = (ctypes.c_ubyte * len(data))(*data)
        with self._lock:
            ret = _lib.libusb_control_transfer(
                self._handle, RT_CLASS_OUT, BREQUEST_WRITE, wValue, WINDEX,
                buf, len(data), 1000,
            )
        if ret < 0:
            raise RuntimeError(f"USB write failed (err {ret})")

    def read_config(self):
        return self._ctrl_read(WVALUE_CONFIG, CONFIG_LEN)

    def write_config(self, config):
        self._ctrl_write(WVALUE_CONFIG, config)

    def read_meters(self):
        data = self._ctrl_read(WVALUE_METER, METER_LEN)
        left = struct.unpack_from('<I', data, 0)[0]
        right = struct.unpack_from('<I', data, 4)[0]
        return left, right

    def read_device_info(self):
        """Read and parse the 51-byte device info block."""
        data = self._ctrl_read(WVALUE_DEVINFO, 51)
        serial = bytes(data[27:47]).decode('ascii', errors='replace').rstrip('\x00')
        return {
            "api_version": f"{data[0]}.{data[1]}",
            "fw_version": f"{data[6]}.{data[7]}.{data[8]}",
            "serial": serial,
        }

    # --- High-level getters ---

    def get_gain_raw(self):
        return struct.unpack_from('<H', self.read_config(), OFF_GAIN)[0]

    def get_mute(self):
        return bool(self.read_config()[OFF_MUTE])

    def get_hp_volume_db(self):
        raw = struct.unpack_from('<h', self.read_config(), OFF_HP_VOL)[0]
        return raw / 256.0

    def get_low_impedance(self):
        return bool(self.read_config()[OFF_LOW_Z])

    def get_volume_select(self):
        val = self.read_config()[OFF_VOL_SELECT]
        return "hp" if val == 2 else "gain"

    def get_all(self):
        config = self.read_config()
        fw_gain = struct.unpack_from('<H', config, OFF_GAIN)[0]
        fw_hp = struct.unpack_from('<h', config, OFF_HP_VOL)[0]
        fw_mute = bool(config[OFF_MUTE])

        fw_now = {"mute": fw_mute, "gain": fw_gain, "hp": fw_hp}

        # Sync firmware ↔ ALSA
        if self._card:
            alsa = _alsa_get(self._card)
            dirty = False  # whether we need to write config back

            if self._last_fw is not None:
                # --- Mute ---
                if fw_mute != self._last_fw["mute"]:
                    _alsa_set_mute(self._card, fw_mute)
                elif alsa.get("mute") is not None and alsa["mute"] != fw_mute:
                    config[OFF_MUTE] = 0x01 if alsa["mute"] else 0x00
                    fw_mute = alsa["mute"]
                    dirty = True

                # --- HP volume ---
                if fw_hp != self._last_fw["hp"]:
                    _alsa_set_hp_vol(self._card, _fw_hp_to_alsa(fw_hp))
                elif "hp_vol" in alsa and alsa["hp_vol"] != _fw_hp_to_alsa(self._last_fw["hp"]):
                    fw_hp = _alsa_hp_to_fw(alsa["hp_vol"])
                    struct.pack_into('<h', config, OFF_HP_VOL, fw_hp)
                    dirty = True

            else:
                # First poll — sync firmware state to ALSA
                _alsa_set_mute(self._card, fw_mute)
                _alsa_set_hp_vol(self._card, _fw_hp_to_alsa(fw_hp))

            if dirty:
                self.write_config(config)

            self._last_fw = {"mute": fw_mute, "gain": fw_gain, "hp": fw_hp}
        else:
            self._last_fw = fw_now

        return {
            "gain_raw": fw_gain,
            "mute": fw_mute,
            "hp_volume_db": fw_hp / 256.0,
            "volume_select": "hp" if config[OFF_VOL_SELECT] == 2 else "gain",
            "low_impedance": bool(config[OFF_LOW_Z]),
        }

    # --- High-level setters (read-modify-write) ---

    def set_gain_raw(self, value):
        value = max(0, min(0xFFFF, value))
        config = self.read_config()
        struct.pack_into('<H', config, OFF_GAIN, value)
        self.write_config(config)
        if self._last_fw:
            self._last_fw["gain"] = value

    def set_mute(self, muted):
        config = self.read_config()
        config[OFF_MUTE] = 0x01 if muted else 0x00
        self.write_config(config)
        if self._last_fw:
            self._last_fw["mute"] = muted
        if self._card:
            _alsa_set_mute(self._card, muted)

    def set_hp_volume_db(self, db):
        db = max(-128.0, min(0.0, db))
        raw = int(db * 256)
        config = self.read_config()
        struct.pack_into('<h', config, OFF_HP_VOL, raw)
        self.write_config(config)
        if self._last_fw:
            self._last_fw["hp"] = raw
        if self._card:
            _alsa_set_hp_vol(self._card, _fw_hp_to_alsa(raw))

    def set_low_impedance(self, enabled):
        config = self.read_config()
        config[OFF_LOW_Z] = 0x01 if enabled else 0x00
        self.write_config(config)


# ---------------------------------------------------------------------------
# Wave XLR MK.2 (0fd9:00b6) backend
#
# Unlike the original (UAC1 + vendor control transfers), the MK.2 is a UAC2
# device that exposes mic mute/gain and headphone volume as standard ALSA
# feature-unit controls. We drive those via amixer — no vendor protocol, no
# USB permissions needed for the core controls. Hardware button/dial changes
# surface in the same ALSA controls, so polling get_all() keeps the UI synced.
#
# Low impedance and volume-select live in the vendor settings block (read
# wValue 0x0004) and need a vendor *write* to change — not wired yet, so the
# backend reports them unsupported and the UI greys/hides them.

_MK2_UI_GAIN_MAX = 0x5000   # mirror app.GAIN_MAX: mic gain 0..max -> 0..this
_MK2_HP_DB_MIN = -60.0      # headphone dB range (matches device + UI slider)
_MK2_HP_DB_MAX = 0.0

# MK.2 vendor control protocol (decoded from a Wave Link USB capture).
#   read : bmRequestType=0xC1, bRequest=0x01, wValue=block, wIndex=0x0203
#   write: bmRequestType=0x41, bRequest=0x01, wValue=block, wIndex=0x0203, data=block
# The analog mic gain is byte 0 of settings block 0x0004 (0..80, == ALSA range).
# It is firmware-owned: ALSA writes to numid=4 get reverted by PipeWire, but a
# vendor write changes the real hardware gain (and the device's LED ring).
_MK2_VINDEX = 0x0203
_MK2_VREQ = 0x01
_MK2_RT_VREAD = 0xC1
_MK2_RT_VWRITE = 0x41
_MK2_BLOCK_SETTINGS = 0x0004   # byte0=gain, byte1=flags (bit0=mute)
_MK2_SETTINGS_LEN = 38
_MK2_GAIN_OFFSET = 0
_MK2_HW_GAIN_MAX = 80
_MK2_BLOCK_HP = 0x0005         # byte0=hp volume, byte1=flags (bit1=low impedance)
_MK2_HP_LEN = 2
_MK2_HP_VOL_OFFSET = 0         # block 0x0005 byte0 = hp volume (0=loudest..240=quietest)
_MK2_FLAGS_OFFSET = 1          # byte 1 of these blocks holds flag bits
_MK2_MUTE_MASK = 0x01          # block 0x0004 byte1 bit0: 1 = muted
_MK2_LOWZ_MASK = 0x02          # block 0x0005 byte1 bit1: 1 = low impedance on

# Headphone volume is non-linear: the hardware wheel steps through these native
# byte0 values (captured from a full wheel sweep), loudest(0) -> quietest(240).
# dB = -byte0/4. The UI slider steps through these so one notch = one hardware
# detent = one LED step. 240 (=-60 dB, the floor below the lowest wheel detent)
# is appended so the slider can reach minimum.
_MK2_HP_DETENTS = [
    0, 2, 3, 5, 7, 8, 10, 12, 13, 15, 17, 18, 20, 22, 25, 28, 30, 33, 35, 37,
    40, 43, 45, 48, 50, 53, 57, 60, 63, 67, 70, 73, 77, 80, 83, 87, 90, 97,
    103, 110, 117, 124, 133, 142, 155, 168, 187, 213, 240,
]
# dB per slider index, quietest(index 0) -> loudest(last). app.py uses this.
_MK2_HP_DETENTS_DB = [-b / 4.0 for b in reversed(_MK2_HP_DETENTS)]

# ALSA control name suffix -> role. The product-string prefix varies between
# units/firmware, the suffix does not, so match on the suffix.
_MK2_ROLE_BY_SUFFIX = {
    "Capture Switch": "mic_mute",
    "Capture Volume": "mic_gain",
    "Playback Switch": "hp_mute",
    "Playback Volume": "hp_vol",
}


def detect_pid():
    """Return the connected Wave XLR product id (PID_MK1/PID_MK2), or None.

    Reads sysfs only — no USB permissions needed, so this works even before the
    udev rule is installed."""
    wanted = {f"{PID_MK1:04x}", f"{PID_MK2:04x}"}
    for idp in glob.glob("/sys/bus/usb/devices/*/idProduct"):
        try:
            with open(idp) as f:
                pid = f.read().strip().lower()
            with open(os.path.join(os.path.dirname(idp), "idVendor")) as f:
                vid = f.read().strip().lower()
        except OSError:
            continue
        if vid == "0fd9" and pid in wanted:
            return int(pid, 16)
    return None


def _mk2_sysfs_dir():
    """sysfs device directory for the MK.2, or None."""
    target = f"{PID_MK2:04x}"
    for idp in glob.glob("/sys/bus/usb/devices/*/idProduct"):
        try:
            with open(idp) as f:
                pid = f.read().strip().lower()
            with open(os.path.join(os.path.dirname(idp), "idVendor")) as f:
                vid = f.read().strip().lower()
        except OSError:
            continue
        if vid == "0fd9" and pid == target:
            return os.path.dirname(idp)
    return None


def _mk2_find_card():
    """ALSA card index for the MK.2 via /proc/asound (no perms needed)."""
    target = f"0fd9:{PID_MK2:04x}"
    for path in glob.glob("/proc/asound/card*/usbid"):
        try:
            with open(path) as f:
                if f.read().strip().lower() == target:
                    return path.split("/card", 1)[1].split("/", 1)[0]
        except OSError:
            continue
    return None


def _mk2_vendor_read(wValue, length=64):
    """Best-effort vendor control-IN read (0xC1/0x01/wIndex=3). Needs the udev
    rule; raises on any failure so callers can degrade gracefully."""
    handle = _lib.libusb_open_device_with_vid_pid(_ctx, VENDOR_ID, PID_MK2)
    if not handle:
        raise RuntimeError("open failed")
    try:
        buf = (ctypes.c_ubyte * length)()
        ret = _lib.libusb_control_transfer(
            handle, 0xC1, 0x01, wValue, 0x0003, buf, length, 500
        )
        if ret < 0:
            raise RuntimeError(f"read failed ({ret})")
        return bytearray(buf[:ret])
    finally:
        _lib.libusb_close(handle)


class WaveXLRMk2:
    """ALSA-driven backend for the Wave XLR MK.2. Same surface as WaveXLR."""

    supports_low_impedance = True
    supports_volume_select = False
    hp_detents = _MK2_HP_DETENTS_DB   # discrete detent slider (quietest..loudest)

    def format_gain(self, raw):
        """Human-readable gain for the UI: the MK.2 mic gain is 0..80 dB."""
        return f"{round(raw / _MK2_UI_GAIN_MAX * _MK2_HW_GAIN_MAX)} dB"

    def __init__(self):
        self._card = None
        self._numid = {}   # role -> numid
        self._max = {}     # role -> control max value
        self._h = None     # libusb handle for the vendor protocol (gain)
        self._lock = threading.Lock()  # serialize vendor transfers (poll + writes)

    @property
    def connected(self):
        return self._card is not None

    def connect(self):
        card = _mk2_find_card()
        if card is None:
            raise RuntimeError("Wave XLR MK.2 not found")
        self._card = card  # set first so _amixer() works during discovery
        try:
            numid, maxv = self._discover()
        except Exception:
            self._card = None
            raise
        if not {"mic_mute", "mic_gain", "hp_vol"} <= set(numid):
            self._card = None
            raise RuntimeError("Wave XLR MK.2 ALSA controls not found")
        self._numid, self._max = numid, maxv
        # Best-effort vendor handle for gain read/write (needs the udev rule).
        self._h = _lib.libusb_open_device_with_vid_pid(_ctx, VENDOR_ID, PID_MK2) or None

    def disconnect(self):
        if self._h:
            _lib.libusb_close(self._h)
            self._h = None
        self._card = None

    # --- vendor control protocol (gain) ---

    def _vread(self, block, length):
        buf = (ctypes.c_ubyte * length)()
        with self._lock:
            ret = _lib.libusb_control_transfer(
                self._h, _MK2_RT_VREAD, _MK2_VREQ, block, _MK2_VINDEX, buf, length, 500)
        if ret < 0:
            raise RuntimeError(f"vendor read failed ({ret})")
        return bytearray(buf[:ret])

    def _vwrite(self, block, data):
        data = bytes(data)
        buf = (ctypes.c_ubyte * len(data))(*data)
        with self._lock:
            ret = _lib.libusb_control_transfer(
                self._h, _MK2_RT_VWRITE, _MK2_VREQ, block, _MK2_VINDEX, buf, len(data), 500)
        if ret < 0:
            raise RuntimeError(f"vendor write failed ({ret})")

    # --- amixer plumbing ---

    def _amixer(self, *args):
        try:
            r = subprocess.run(
                ["amixer", "-c", self._card, *args],
                capture_output=True, text=True, timeout=3,
            )
            return r.stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def _discover(self):
        """Map control roles to numids and capture each control's max, by
        scanning `amixer contents` and matching the control-name suffix."""
        numid, maxv = {}, {}
        cur_id = cur_name = None
        for line in self._amixer("contents").splitlines():
            s = line.strip()
            m = re.match(r"numid=(\d+),iface=(\w+),name='(.*)'", s)
            if m:
                cur_id, iface, cur_name = int(m.group(1)), m.group(2), m.group(3)
                if iface != "MIXER":
                    cur_id = cur_name = None
                continue
            if cur_id is not None and s.startswith("; type="):
                role = next((r for suf, r in _MK2_ROLE_BY_SUFFIX.items()
                             if cur_name.endswith(suf)), None)
                if role:
                    numid[role] = cur_id
                    mm = re.search(r"max=(\d+)", s)
                    if mm:
                        maxv[role] = int(mm.group(1))
        return numid, maxv

    def _values(self):
        """{numid: value_string} from one `amixer contents` call."""
        vals = {}
        cur_id = None
        for line in self._amixer("contents").splitlines():
            s = line.strip()
            m = re.match(r"numid=(\d+)", s)
            if m:
                cur_id = int(m.group(1))
            elif cur_id is not None and s.startswith(": values="):
                vals[cur_id] = s.split("values=", 1)[1].strip()
        return vals

    def _cset(self, role, value):
        nid = self._numid.get(role)
        if nid is not None:
            self._amixer("cset", f"numid={nid}", str(value))

    # --- interface expected by app.py ---

    def read_device_info(self):
        info = {"api_version": "—", "fw_version": "—", "serial": "—"}
        d = _mk2_sysfs_dir()
        if d:
            try:
                with open(os.path.join(d, "serial")) as f:
                    info["serial"] = f.read().strip()
            except OSError:
                pass
            try:
                with open(os.path.join(d, "bcdDevice")) as f:
                    bcd = f.read().strip()
                if len(bcd) >= 3:
                    info["fw_version"] = f"{int(bcd[:-2])}.{bcd[-2:]}"
            except (OSError, ValueError):
                pass
        try:
            blk = _mk2_vendor_read(0x0000)
            if len(blk) >= 2:
                info["api_version"] = f"{blk[0]}.{blk[1]}"
        except Exception:
            pass
        return info

    def _alsa_gain(self, vals):
        try:
            return int(vals.get(self._numid["mic_gain"], "0"))
        except ValueError:
            return 0

    def get_all(self):
        # Preferred path: read everything from the vendor blocks (authoritative,
        # matches the LEDs). gain+mute in 0x0004, hp+low-Z in 0x0005.
        if self._h:
            try:
                b4 = self._vread(_MK2_BLOCK_SETTINGS, _MK2_SETTINGS_LEN)
                b5 = self._vread(_MK2_BLOCK_HP, _MK2_HP_LEN)
                return {
                    "gain_raw": round(b4[_MK2_GAIN_OFFSET] / _MK2_HW_GAIN_MAX * _MK2_UI_GAIN_MAX),
                    "mute": bool(b4[_MK2_FLAGS_OFFSET] & _MK2_MUTE_MASK),
                    "hp_volume_db": -b5[_MK2_HP_VOL_OFFSET] / 4.0,
                    "volume_select": "gain",
                    "low_impedance": bool(b5[_MK2_FLAGS_OFFSET] & _MK2_LOWZ_MASK),
                }
            except Exception:
                pass

        # Fallback (no USB access): ALSA for gain/mute/hp; low-Z unavailable.
        vals = self._values()
        hpmax = self._max.get("hp_vol") or 240
        try:
            hpv = int(vals.get(self._numid["hp_vol"], "0"))
        except ValueError:
            hpv = 0
        span = _MK2_HP_DB_MAX - _MK2_HP_DB_MIN
        return {
            "gain_raw": round(self._alsa_gain(vals) / _MK2_HW_GAIN_MAX * _MK2_UI_GAIN_MAX),
            "mute": vals.get(self._numid.get("mic_mute"), "on") == "off",
            "hp_volume_db": _MK2_HP_DB_MIN + hpv / hpmax * span,
            "volume_select": "gain",
            "low_impedance": False,
        }

    def set_mute(self, muted):
        if self._h:
            # Mute = block 0x0004 byte1 bit0 (1 = muted).
            block = self._vread(_MK2_BLOCK_SETTINGS, _MK2_SETTINGS_LEN)
            if muted:
                block[_MK2_FLAGS_OFFSET] |= _MK2_MUTE_MASK
            else:
                block[_MK2_FLAGS_OFFSET] &= ~_MK2_MUTE_MASK
            self._vwrite(_MK2_BLOCK_SETTINGS, block)
            _log.info("set_mute(%s) -> block 0x04 byte1=0x%02x", muted, block[_MK2_FLAGS_OFFSET])
        else:
            self._cset("mic_mute", "off" if muted else "on")

    def set_gain_raw(self, value):
        value = max(0, min(_MK2_UI_GAIN_MAX, value))
        hw = round(value / _MK2_UI_GAIN_MAX * _MK2_HW_GAIN_MAX)  # 0..80
        if self._h:
            block = self._vread(_MK2_BLOCK_SETTINGS, _MK2_SETTINGS_LEN)
            block[_MK2_GAIN_OFFSET] = hw
            self._vwrite(_MK2_BLOCK_SETTINGS, block)
            _log.debug("set_gain_raw(%s) -> vendor block 0x04 gain=%d", value, hw)
        else:
            # No USB access — ALSA write (PipeWire usually reverts this).
            self._cset("mic_gain", hw)
            _log.warning("set_gain_raw: no USB handle; used ALSA (may not stick)")

    def set_hp_volume_db(self, db):
        # Snap to the nearest hardware detent and write the native value, so the
        # device/LEDs land exactly on a real wheel step.
        byte0 = min(_MK2_HP_DETENTS, key=lambda b: abs(-b / 4.0 - db))
        if self._h:
            block = self._vread(_MK2_BLOCK_HP, _MK2_HP_LEN)
            block[_MK2_HP_VOL_OFFSET] = byte0
            self._vwrite(_MK2_BLOCK_HP, block)
            _log.debug("set_hp_volume_db(%.1f) -> byte0=%d", db, byte0)
        else:
            self._cset("hp_vol", 240 - byte0)

    def set_low_impedance(self, enabled):
        if not self._h:
            return  # needs USB access; nothing we can do via ALSA
        # Low impedance = block 0x0005 byte1 bit1 (1 = on).
        block = self._vread(_MK2_BLOCK_HP, _MK2_HP_LEN)
        if enabled:
            block[_MK2_FLAGS_OFFSET] |= _MK2_LOWZ_MASK
        else:
            block[_MK2_FLAGS_OFFSET] &= ~_MK2_LOWZ_MASK
        self._vwrite(_MK2_BLOCK_HP, block)
        _log.info("set_low_impedance(%s) -> block 0x05 byte1=0x%02x", enabled, block[_MK2_FLAGS_OFFSET])
