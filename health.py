"""Keeps the listener honest about its own health.

The listener went silent for days at a time and every
time it was found by accident, with the evidence gone: the journal is volatile to spare the
SD card, so a reboot wiped it. This module is the fix on the Pi side:

- systemd watchdog: the listener proves each full listen cycle to systemd, so a wedged
  arecord or a hung model gets the process killed and restarted instead of sitting "active".
- health snapshot: every check-in carries mic/wifi/power/temperature/disk state and the last
  few errors, so the worker (and the alert it files) says *why* it went quiet.
- remote commands: the worker can hand back "restart" or "reboot" on a check-in, so a stuck
  listener can be kicked from the phone without SSH.
"""
import collections
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import time

VERSION_FILES = ("listener.py", "health.py")


def sd_notify(message):
    """Tell systemd something (READY=1, WATCHDOG=1, STATUS=...). A no-op outside systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(message.encode())
    except OSError:
        pass


def _read(path, default=""):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def _run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def code_version():
    """Short hash of the deployed code, so the worker can show when the Pi drifts from the repo."""
    here = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for name in VERSION_FILES:
        try:
            with open(os.path.join(here, name), "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass
    return h.hexdigest()[:10]


class Health:
    def __init__(self, mic_device):
        self.mic_device = mic_device
        self.started = time.time()
        self.cycles = 0
        self.mic_failures = 0          # consecutive failed recordings
        self.errors = collections.deque(maxlen=6)
        self.last_good = None
        self.version = code_version()
        self._ready = False

    # ---- bookkeeping -------------------------------------------------------------------
    def good_cycle(self):
        """A clip recorded and analysed. This is the only thing that feeds the watchdog."""
        self.cycles += 1
        self.mic_failures = 0
        self.last_good = time.time()
        if not self._ready:
            sd_notify("READY=1")
            self._ready = True
        sd_notify("WATCHDOG=1")
        sd_notify(f"STATUS=listening; {self.cycles} clips since start")

    def mic_failed(self, err):
        self.mic_failures += 1
        self.error(f"mic: {err}")

    def error(self, err):
        line = f"{time.strftime('%m-%d %H:%M')} {str(err)[:160]}"
        self.errors.append(line)
        print("error:", err, flush=True)

    # ---- what the Pi can say about itself ------------------------------------------------
    def mic_present(self):
        cards = _run(["arecord", "-l"])
        card = self.mic_device.split("CARD=")[-1].split(",")[0] if "CARD=" in self.mic_device else ""
        return bool(cards) and (card in cards if card else "card" in cards)

    @staticmethod
    def throttled():
        """vcgencmd get_throttled: under-voltage and throttling, now and since boot."""
        raw = _run(["vcgencmd", "get_throttled"]).split("=")[-1]
        try:
            bits = int(raw, 16)
        except ValueError:
            return None
        return {
            "raw": raw,
            "undervolt_now": bool(bits & 0x1), "throttled_now": bool(bits & 0x4),
            "undervolt_since_boot": bool(bits & 0x10000), "throttled_since_boot": bool(bits & 0x40000),
        }

    @staticmethod
    def wifi():
        # /proc/net/wireless: "wlan0: 0000   52.  -58.  -256 ..." (link quality, signal dBm)
        for line in _read("/proc/net/wireless").splitlines()[2:]:
            parts = line.split()
            if len(parts) >= 4 and parts[0].rstrip(":").startswith("wl"):
                try:
                    return {"iface": parts[0].rstrip(":"), "quality": float(parts[2].rstrip(".")),
                            "signal_dbm": float(parts[3].rstrip("."))}
                except ValueError:
                    pass
        return None

    def snapshot(self, queue_len=0):
        temp = _read("/sys/class/thermal/thermal_zone0/temp")
        disk = shutil.disk_usage("/")
        return {
            "ok": self.mic_failures == 0,
            "version": self.version,
            "host": socket.gethostname(),
            "uptime_s": int(float(_read("/proc/uptime", "0").split()[0] or 0)),
            "listener_up_s": int(time.time() - self.started),
            "cycles": self.cycles,
            "last_good": int(self.last_good * 1000) if self.last_good else None,
            "mic_device": self.mic_device,
            "mic_present": self.mic_present(),
            "mic_failures": self.mic_failures,
            "temp_c": round(int(temp) / 1000, 1) if temp.isdigit() else None,
            "throttled": self.throttled(),
            "wifi": self.wifi(),
            "disk_free_mb": disk.free // (1 << 20),
            "queue": queue_len,
            "errors": list(self.errors),
        }

    # ---- orders from the worker -----------------------------------------------------------
    def obey(self, command):
        """Act on a command handed back by the worker. Unknown commands are ignored."""
        if command == "restart":
            print("worker asked for a restart", flush=True)
            sys.exit(75)                 # systemd Restart=always brings us straight back
        if command == "reboot":
            print("worker asked for a reboot", flush=True)
            subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "reboot"], check=False)
            sys.exit(75)
