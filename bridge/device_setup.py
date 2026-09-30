"""
Setup steps shared by `make` (tools/manage.py) and the lazyTrigger app
(the bridge's Setup section): finding the Pico's USB drives, installing
CircuitPython and the firmware, and the optional login check.

Only the standard library is used, so tools/manage.py can import this on a
bare Python before the project's virtual environment exists, and the
packaged app needs nothing beyond what it bundles.
"""

import io
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

SYSTEM = platform.system()  # "Darwin", "Windows" or "Linux"
IS_WINDOWS = SYSTEM == "Windows"
IS_MAC = SYSTEM == "Darwin"

# True inside the packaged app (PyInstaller), where the configurator and
# firmware files are unpacked next to the program instead of in a checkout.
FROZEN = getattr(sys, "frozen", False)


def resource_root():
    """The folder holding configurator/ and firmware/."""
    if FROZEN:
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


FIRMWARE_DIR = resource_root() / "firmware"

# boot.py goes last: it only runs at power-up, but if the board resets
# mid-copy the rest of the firmware should already be in place.
FIRMWARE_FILES = ["settings.toml", "protocol.py", "config.py", "secretbox.py", "mfrc522.py",
                  "code.py", "boot.py"]

# Drive names the RP2040 / RP2350 bootloader shows when BOOTSEL is held,
# and the CircuitPython board each one gets by default. Pico W / Pico 2 W
# need their own build: pass board="raspberry_pi_pico_w" (or pico2_w).
BOOTLOADER_DRIVES = {"RPI-RP2": "raspberry_pi_pico", "RP2350": "raspberry_pi_pico2"}

# USB vendor ids a Pico running CircuitPython can report: Raspberry Pi's
# own, and Adafruit's (used by many CircuitPython builds).
PICO_USB_VENDORS = {0x2E8A, 0x239A}

LOG_MAX_BYTES = 1_000_000


class SetupError(Exception):
    """A setup step failed; the message says what to do about it."""


# --------------------------------------------------------------------------
# downloads

def _ssl_context():
    # The packaged macOS app has no system certificate store Python can
    # find, so it ships certifi's. A checkout uses the system's.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _fetch(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout, context=_ssl_context()) as resp:
        return resp.read()


def _latest_release(repo):
    return json.loads(_fetch("https://api.github.com/repos/{}/releases/latest".format(repo), 15))


# --------------------------------------------------------------------------
# finding the Pico's USB drives

def find_drive(label):
    """Mount point of a USB drive by its volume label, or None."""
    if IS_WINDOWS:
        import ctypes
        import string

        kernel32 = ctypes.windll.kernel32
        kernel32.SetErrorMode(1)  # no "insert a disk" popups for empty readers
        mask = kernel32.GetLogicalDrives()
        name = ctypes.create_unicode_buffer(261)
        for i, letter in enumerate(string.ascii_uppercase):
            if not mask & (1 << i):
                continue
            root = letter + ":\\"
            if kernel32.GetVolumeInformationW(root, name, 261, None, None, None, None, 0):
                if name.value.upper() == label.upper():
                    return Path(root)
        return None

    if IS_MAC:
        path = Path("/Volumes") / label
        return path if path.is_dir() else None

    try:
        with open("/proc/mounts") as mounts:
            for line in mounts:
                mount_point = line.split()[1].replace("\\040", " ")
                if Path(mount_point).name == label:
                    return Path(mount_point)
    except OSError:
        pass
    return None


def drive_writable(path):
    """Whether this computer may write to the drive. Once boot.py has run,
    CIRCUITPY is read-only to the host, which is how an already-installed
    Pico is recognised."""
    probe = path / ".lazytrigger_write_test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def wait_for_drive(label, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        drive = find_drive(label)
        if drive is not None:
            return drive
        time.sleep(1)
    return None


def bootloader_drive():
    """(label, mount point) of a Pico in BOOTSEL mode, or (None, None)."""
    for label in BOOTLOADER_DRIVES:
        drive = find_drive(label)
        if drive is not None:
            return label, drive
    return None, None


def device_state():
    """What is plugged in, for the Setup section. Reads only: it runs every
    couple of seconds, and a write to CIRCUITPY would restart the board.

    bootloader: the BOOTSEL drive's label, when a Pico is waiting for
                CircuitPython
    circuitpy:  "new" when CircuitPython is there without lazyTrigger,
                "installed" when the firmware files are on it, else None"""
    label, _ = bootloader_drive()
    drive = find_drive("CIRCUITPY")
    circuitpy = None
    if drive is not None:
        installed = all((drive / name).exists() for name in ("protocol.py", "secretbox.py"))
        circuitpy = "installed" if installed else "new"
    return {"bootloader": label, "circuitpy": circuitpy}


# --------------------------------------------------------------------------
# installing

def flash_circuitpython(board=None, say=print):
    """Install CircuitPython on a Pico in bootloader (BOOTSEL) mode and wait
    for its CIRCUITPY drive. Returns False when no such Pico is plugged in."""
    label, drive = bootloader_drive()
    if drive is None:
        return False
    board = board or BOOTLOADER_DRIVES[label]

    say("Installing CircuitPython for " + board)
    try:
        version = _latest_release("adafruit/circuitpython")["tag_name"]
    except Exception as e:
        raise SetupError("could not look up the latest CircuitPython version "
                         "(is this computer online?): {}".format(e))
    url = ("https://downloads.circuitpython.org/bin/{b}/en_US/"
           "adafruit-circuitpython-{b}-en_US-{v}.uf2").format(b=board, v=version)
    say("Downloading CircuitPython {} ...".format(version))
    uf2 = Path(tempfile.gettempdir()) / "circuitpython-{}-{}.uf2".format(board, version)
    try:
        uf2.write_bytes(_fetch(url, 120))
    except Exception as e:
        raise SetupError("download failed ({}): {}".format(url, e))

    say("Copying to {} ...".format(drive))
    try:
        shutil.copyfile(uf2, drive / uf2.name)
    except OSError:
        # The bootloader reboots the board as soon as the image is written,
        # which can yank the drive away before the copy call returns.
        if find_drive(label) is not None:
            raise
    say("Waiting for the CIRCUITPY drive ...")
    if wait_for_drive("CIRCUITPY", 90) is None:
        raise SetupError("CIRCUITPY did not appear. Unplug the Pico, plug it back in "
                         "and try again.")
    say("CircuitPython is installed.")
    return True


def circuitpython_major(drive):
    """Major CircuitPython version on the drive, from boot_out.txt."""
    try:
        text = (drive / "boot_out.txt").read_text(errors="replace")
    except OSError:
        return None
    match = re.search(r"CircuitPython (\d+)\.", text)
    return match.group(1) if match else None


def install_hid_library(drive, say=print):
    """Copy adafruit_hid (the USB keyboard library) into CIRCUITPY/lib.

    Takes the build compiled for the board's CircuitPython version, or the
    plain .py one if there is none, straight from the library's release, so
    it needs neither pip nor circup."""
    say("Installing the adafruit_hid library")
    try:
        release = _latest_release("adafruit/Adafruit_CircuitPython_HID")
    except Exception as e:
        raise SetupError("could not look up the adafruit_hid library "
                         "(is this computer online?): {}".format(e))
    assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
    major = circuitpython_major(drive)
    wanted = [name for name in assets if major and "-{}.x-mpy-".format(major) in name]
    wanted += [name for name in assets if "-py-" in name and name.endswith(".zip")]
    if not wanted:
        raise SetupError("no usable adafruit_hid download found in release "
                         + release.get("tag_name", "?"))
    try:
        archive = zipfile.ZipFile(io.BytesIO(_fetch(assets[wanted[0]], 60)))
    except Exception as e:
        raise SetupError("adafruit_hid download failed: {}".format(e))

    copied = 0
    for entry in archive.namelist():
        # Entries look like <release-name>/lib/adafruit_hid/keyboard.mpy.
        parts = entry.split("/")
        if "lib" not in parts or entry.endswith("/"):
            continue
        rel = Path(*parts[parts.index("lib"):])
        target = drive / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(entry))
        copied += 1
    if not copied:
        raise SetupError("the adafruit_hid download had no library files in it")
    say("  lib/adafruit_hid ({})".format(wanted[0]))


def install_firmware(drive, say=print):
    """First-time copy of the library and firmware onto a writable CIRCUITPY."""
    if not drive_writable(drive):
        raise SetupError("lazyTrigger is already installed on this Pico (its drive is "
                         "read-only to this computer). Use the update instead.")
    install_hid_library(drive, say)
    say("Copying the firmware to " + str(drive))
    for name in FIRMWARE_FILES:
        shutil.copyfile(FIRMWARE_DIR / name, drive / name)
        say("  " + name)
    if hasattr(os, "sync"):
        os.sync()


# --------------------------------------------------------------------------
# the bridge's log

def log_file():
    if IS_MAC:
        return Path.home() / "Library" / "Logs" / "lazytrigger-bridge.log"
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "lazytrigger" / "bridge.log"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "lazytrigger" / "bridge.log"


def open_log():
    """The bridge's log for appending, rotated once it grows past 1 MB."""
    log = log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
        log.replace(log.with_suffix(".old.log"))
    out = open(log, "a", buffering=1)
    out.write("--- started {}\n".format(time.ctime()))
    return out


# --------------------------------------------------------------------------
# optional login item: one check at login, then it's gone

LOGIN_ITEM_NAME = "lazytrigger-bridge"
MAC_LOGIN_LABEL = "local.lazytrigger.bridge"


def login_item_path():
    if IS_MAC:
        return Path.home() / "Library" / "LaunchAgents" / (MAC_LOGIN_LABEL + ".plist")
    if IS_WINDOWS:
        appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return (appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
                / (LOGIN_ITEM_NAME + ".vbs"))
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config / "autostart" / (LOGIN_ITEM_NAME + ".desktop")


def login_item_installed():
    return login_item_path().exists()


def autostart_command():
    """What the login item runs, as (argv, working directory): the app
    itself when packaged, otherwise `manage.py start --at-login` under the
    Python running this (the project's .venv, for the bridge)."""
    if FROZEN:
        return [sys.executable, "--at-login"], Path.home()
    root = resource_root()
    python = Path(sys.executable)
    if IS_WINDOWS and python.with_name("pythonw.exe").exists():
        python = python.with_name("pythonw.exe")  # no console window at login
    return [python, root / "tools" / "manage.py", "start", "--at-login"], root


def install_login_item(argv, workdir):
    """Run argv once at each login. Replaces any earlier login item."""
    remove_login_item()
    item = login_item_path()
    item.parent.mkdir(parents=True, exist_ok=True)
    argv = [str(a) for a in argv]
    workdir = str(workdir)

    if IS_MAC:
        from xml.sax.saxutils import escape
        # RunAtLoad without KeepAlive: launchd runs it once per login. The
        # bridge it starts is in its own session, and AbandonProcessGroup
        # stops launchd from killing it when this one-off check exits.
        args = "".join("        <string>{}</string>\n".format(escape(a)) for a in argv)
        item.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            "    <key>Label</key><string>{label}</string>\n"
            "    <key>ProgramArguments</key>\n    <array>\n{args}    </array>\n"
            "    <key>WorkingDirectory</key><string>{root}</string>\n"
            "    <key>RunAtLoad</key><true/>\n"
            "    <key>AbandonProcessGroup</key><true/>\n"
            "</dict>\n</plist>\n".format(label=MAC_LOGIN_LABEL, args=args, root=escape(workdir)))
        subprocess.run(["launchctl", "bootstrap", "gui/{}".format(os.getuid()), str(item)],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif IS_WINDOWS:
        # Style 0: no window. `"` is doubled inside VBScript strings.
        command = " ".join('"{}"'.format(a) for a in argv).replace('"', '""')
        item.write_text(
            'Set shell = CreateObject("WScript.Shell")\r\n'
            'shell.CurrentDirectory = "{root}"\r\n'
            'shell.Run "{command}", 0, False\r\n'.format(root=workdir, command=command))
    else:
        item.write_text(
            "[Desktop Entry]\nType=Application\nName=lazyTrigger (login check)\n"
            "Exec={command}\nPath={root}\n"
            "NoDisplay=true\nX-GNOME-Autostart-enabled=true\n".format(
                command=" ".join('"{}"'.format(a) for a in argv), root=workdir))
    return item


def remove_login_item():
    item = login_item_path()
    if IS_MAC:
        subprocess.run(["launchctl", "bootout", "gui/{}/{}".format(os.getuid(), MAC_LOGIN_LABEL)],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if item.exists():
        item.unlink()
