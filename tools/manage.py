#!/usr/bin/env python3
"""
One entry point for setting up and running the project on macOS, Windows
and Linux. The Makefile (and make.cmd on Windows) only forward to this, so
every platform runs the same logic and nothing depends on sed, launchctl,
systemd or a POSIX shell being present.

    python3 tools/manage.py              # set up everything (same as `install`)
    python3 tools/manage.py help         # list commands

Only the standard library is used here, so it runs on a bare Python 3.8+
before the project's own virtual environment exists.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "bridge" / "requirements.txt"
VENV_MARKER = VENV / ".installed"

SYSTEM = platform.system()  # "Darwin", "Windows" or "Linux"
IS_WINDOWS = SYSTEM == "Windows"
IS_MAC = SYSTEM == "Darwin"

# Same variable the bridge reads, so `make status` looks in the right place.
BRIDGE_URL = "http://127.0.0.1:{}".format(os.environ.get("LAZYTRIGGER_BRIDGE_PORT", "8787"))

# boot.py goes last: it only runs at power-up, but if the board resets
# mid-copy the rest of the firmware should already be in place.
FIRMWARE_FILES = ["settings.toml", "protocol.py", "config.py", "secretbox.py", "mfrc522.py",
                  "code.py", "boot.py"]

# Drive names the RP2040 / RP2350 bootloader shows when BOOTSEL is held,
# and the CircuitPython board each one gets by default. Pico W / Pico 2 W
# need their own build: pass --board raspberry_pi_pico_w (or pico2_w).
BOOTLOADER_DRIVES = {"RPI-RP2": "raspberry_pi_pico", "RP2350": "raspberry_pi_pico2"}

LOG_MAX_BYTES = 1_000_000


# --------------------------------------------------------------------------
# small helpers

def say(msg=""):
    print(msg, flush=True)


def step(msg):
    say("\n==> " + msg)


def fail(msg):
    say("\nERROR: " + msg)
    sys.exit(1)


def venv_python(windowless=False):
    if IS_WINDOWS:
        return VENV / "Scripts" / ("pythonw.exe" if windowless else "python.exe")
    return VENV / "bin" / "python"


def run(cmd, check=True, **kwargs):
    return subprocess.run([str(c) for c in cmd], check=check, **kwargs)


def log_file():
    if IS_MAC:
        return Path.home() / "Library" / "Logs" / "lazytrigger-bridge.log"
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "lazytrigger" / "bridge.log"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "lazytrigger" / "bridge.log"


def bridge_reachable():
    """Returns the bridge's /ports answer as text, or None if not running."""
    try:
        with urllib.request.urlopen(BRIDGE_URL + "/ports", timeout=2) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


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


# --------------------------------------------------------------------------
# commands

def cmd_setup(_args=None):
    """Create .venv with the bridge's dependencies (skipped when current)."""
    if (VENV_MARKER.exists() and venv_python().exists()
            and VENV_MARKER.stat().st_mtime >= REQUIREMENTS.stat().st_mtime):
        say("Python environment is up to date (.venv).")
        return
    step("Creating the Python environment in .venv")
    try:
        run([sys.executable, "-m", "venv", VENV])
    except subprocess.CalledProcessError:
        hint = ""
        if SYSTEM == "Linux":
            hint = "\nOn Debian/Ubuntu install it first: sudo apt install python3-venv"
        fail("could not create a virtual environment." + hint)
    run([venv_python(), "-m", "pip", "install", "-q", "--upgrade", "pip"])
    run([venv_python(), "-m", "pip", "install", "-q", "-r", REQUIREMENTS])
    VENV_MARKER.touch()
    say("Done.")


def cmd_flash(args):
    """Install CircuitPython on a Pico that is in bootloader (BOOTSEL) mode."""
    found = [(label, find_drive(label)) for label in BOOTLOADER_DRIVES]
    found = [(label, drive) for label, drive in found if drive is not None]
    if not found:
        say("No Pico in bootloader mode found. To install CircuitPython: unplug the "
            "Pico, hold its BOOTSEL button while plugging it back in, then run this again.")
        return False
    label, drive = found[0]
    board = getattr(args, "board", None) or BOOTLOADER_DRIVES[label]

    step("Installing CircuitPython for " + board)
    try:
        import json
        with urllib.request.urlopen(
                "https://api.github.com/repos/adafruit/circuitpython/releases/latest",
                timeout=15) as resp:
            version = json.load(resp)["tag_name"]
    except Exception as e:
        fail("could not look up the latest CircuitPython version: {}".format(e))
    url = ("https://downloads.circuitpython.org/bin/{b}/en_US/"
           "adafruit-circuitpython-{b}-en_US-{v}.uf2").format(b=board, v=version)
    say("Downloading CircuitPython {} ...".format(version))
    uf2 = Path(tempfile.gettempdir()) / "circuitpython-{}-{}.uf2".format(board, version)
    try:
        urllib.request.urlretrieve(url, uf2)
    except Exception as e:
        fail("download failed ({}): {}".format(url, e))

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
        fail("CIRCUITPY did not appear. Unplug and replug the Pico, then run `make` again.")
    say("CircuitPython is installed.")
    return True


def cmd_install_firmware(_args=None):
    """First-time copy of the firmware onto a freshly flashed CIRCUITPY drive."""
    drive = find_drive("CIRCUITPY")
    if drive is None:
        say("No CIRCUITPY drive found: is the Pico plugged in with CircuitPython on it?")
        return False
    if not drive_writable(drive):
        say("The firmware is already installed (CIRCUITPY is read-only to this "
            "computer). Use `make upload` to update it.")
        return False
    cmd_setup()
    step("Installing the adafruit_hid library")
    run([venv_python(), "-m", "pip", "install", "-q", "circup"])
    run([venv_python(), "-m", "circup", "--path", drive, "install", "adafruit_hid"])
    step("Copying the firmware to " + str(drive))
    for name in FIRMWARE_FILES:
        shutil.copyfile(ROOT / "firmware" / name, drive / name)
        say("  " + name)
    if hasattr(os, "sync"):
        os.sync()
    say("\nFirmware installed. Unplug the Pico and plug it back in to start it.")
    return True


def cmd_upload(args):
    """Push firmware changes over USB (the drive is read-only by now)."""
    cmd_setup()
    cmd = [venv_python(), "-u", ROOT / "bridge" / "upload_firmware.py"]
    if args.port:
        cmd += ["--port", args.port]
    cmd += args.files
    run(cmd)


def cmd_bridge(_args=None):
    """Run the bridge in this terminal until Ctrl-C."""
    cmd_setup()
    try:
        run([venv_python(), "-u", ROOT / "bridge" / "serial_bridge.py"], cwd=ROOT)
    except KeyboardInterrupt:
        pass


# USB vendor ids a Pico running CircuitPython can report: Raspberry Pi's
# own, and Adafruit's (used by many CircuitPython builds).
PICO_USB_VENDORS = {0x2E8A, 0x239A}


def pico_plugged_in():
    """Whether a Pico is attached, from the OS's USB device list alone.
    Nothing is opened, so this is cheap enough to run every few seconds."""
    try:
        from serial.tools import list_ports
    except ImportError:
        # `make status` and friends run under the system Python, which may
        # not have pyserial; ask the project's environment instead.
        if not venv_python().exists():
            return False
        code = ("from serial.tools import list_ports; import sys; "
                "sys.exit(0 if any(p.vid in {} for p in list_ports.comports()) else 1)"
                ).format(sorted(PICO_USB_VENDORS))
        return run([venv_python(), "-c", code], check=False).returncode == 0
    return any(p.vid in PICO_USB_VENDORS for p in list_ports.comports())


def stop_bridges():
    """Stop any running bridge (started by `make start` or by hand)."""
    if IS_WINDOWS:
        ps = ("Get-CimInstance Win32_Process | Where-Object { "
              "$_.CommandLine -like '*serial_bridge.py*' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
        run(["powershell", "-NoProfile", "-Command", ps], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        run(["pkill", "-f", "bridge/serial_bridge.py"], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)


def cmd_start(args=None):
    """Start the bridge in the background; it exits after the Pico is unplugged."""
    # --at-login: the one-off check the login item runs. No Pico means there
    # is nothing to do, so exit quietly instead of reporting an error.
    at_login = bool(getattr(args, "at_login", False))
    if not at_login:
        cmd_setup()
    if bridge_reachable():
        if not at_login:
            say("The bridge is already running.")
            cmd_status()
        return
    if not pico_plugged_in():
        if at_login:
            return
        fail("the Pico isn't plugged in. Plug it in, then run `make start` again.")

    log = log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
        log.replace(log.with_suffix(".old.log"))
    cmd = [str(venv_python()), "-u", str(ROOT / "bridge" / "serial_bridge.py"),
           "--exit-when-unplugged"]
    with open(log, "a") as out:
        out.write("--- started {}\n".format(time.ctime()))
        out.flush()
        if IS_WINDOWS:
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW:
            # keeps running after this console closes, with no window of its own.
            subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT,
                             creationflags=0x00000008 | 0x00000200 | 0x08000000)
        else:
            # A new session, so closing this terminal doesn't stop it.
            subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, start_new_session=True)

    # The web server answers a moment before the bridge has found the Pico,
    # so wait for the connection, not just the server.
    if at_login:
        return
    for _ in range(15):
        answer = bridge_reachable()
        if answer and '"connected": null' not in answer:
            break
        time.sleep(1)
    cmd_status()
    say("It stops by itself about 10 seconds after the Pico is unplugged.")


def cmd_stop(_args=None):
    """Stop the bridge now."""
    stop_bridges()
    say("Bridge stopped.")


# --------------------------------------------------------------------------
# optional login item: one check at login, then it's gone

LOGIN_ITEM_NAME = "lazytrigger-bridge"
MAC_LOGIN_LABEL = "local.lazytrigger.bridge"


def _login_item_path():
    if IS_MAC:
        return Path.home() / "Library" / "LaunchAgents" / (MAC_LOGIN_LABEL + ".plist")
    if IS_WINDOWS:
        appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return (appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
                / (LOGIN_ITEM_NAME + ".vbs"))
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config / "autostart" / (LOGIN_ITEM_NAME + ".desktop")


def cmd_autostart(_args=None):
    """At each login, check once for the Pico and start the bridge if it's there."""
    cmd_setup()
    cmd_autostart_remove(quiet=True)
    item = _login_item_path()
    item.parent.mkdir(parents=True, exist_ok=True)
    python = str(venv_python(windowless=True))
    manage = str(ROOT / "tools" / "manage.py")

    if IS_MAC:
        from xml.sax.saxutils import escape
        # RunAtLoad without KeepAlive: launchd runs it once per login. The
        # bridge it starts is in its own session, and AbandonProcessGroup
        # stops launchd from killing it when this one-off check exits.
        item.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            "    <key>Label</key><string>{label}</string>\n"
            "    <key>ProgramArguments</key>\n    <array>\n"
            "        <string>{python}</string>\n"
            "        <string>{manage}</string>\n"
            "        <string>start</string>\n"
            "        <string>--at-login</string>\n"
            "    </array>\n"
            "    <key>WorkingDirectory</key><string>{root}</string>\n"
            "    <key>RunAtLoad</key><true/>\n"
            "    <key>AbandonProcessGroup</key><true/>\n"
            "</dict>\n</plist>\n".format(
                label=MAC_LOGIN_LABEL, python=escape(python), manage=escape(manage),
                root=escape(str(ROOT))))
        run(["launchctl", "bootstrap", "gui/{}".format(os.getuid()), item])
    elif IS_WINDOWS:
        # Style 0: no window. `"` is doubled inside VBScript strings.
        item.write_text(
            'Set shell = CreateObject("WScript.Shell")\r\n'
            'shell.CurrentDirectory = "{root}"\r\n'
            'shell.Run """{python}"" ""{manage}"" start --at-login", 0, False\r\n'.format(
                root=str(ROOT), python=python, manage=manage))
    else:
        item.write_text(
            "[Desktop Entry]\nType=Application\nName=lazyTrigger bridge (login check)\n"
            'Exec="{python}" "{manage}" start --at-login\nPath={root}\n'
            "NoDisplay=true\nX-GNOME-Autostart-enabled=true\n".format(
                python=python, manage=manage, root=str(ROOT)))
    say("Login check installed: " + str(item))
    say("At each login it looks for the Pico once, starts the bridge if it's "
        "plugged in, and exits. Nothing keeps running when it isn't.")


def cmd_autostart_remove(_args=None, quiet=False):
    """Remove the login check."""
    item = _login_item_path()
    if IS_MAC:
        run(["launchctl", "bootout", "gui/{}/{}".format(os.getuid(), MAC_LOGIN_LABEL)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if item.exists():
        item.unlink()
    if not quiet:
        say("Login check removed.")


def cmd_status(_args=None):
    """Is the bridge running, and is the Pico connected?"""
    if _login_item_path().exists():
        say("Autostart:         enabled (`make autostart-remove` to disable)")
    else:
        say("Autostart:         disabled (`make autostart` to enable)")
    say("                   At each login, checks once whether the Pico is plugged in")
    say("                   and starts the bridge if it is; otherwise does nothing.")
    say("                   Nothing keeps running or checks again after login.")
    answer = bridge_reachable()
    if answer is None:
        say("Bridge:            not running (plug in the Pico and run `make start`)")
        return
    say("Bridge:            running, configurator at " + BRIDGE_URL)
    try:
        import json
        connected = json.loads(answer).get("connected")
    except ValueError:
        connected = None
    say("Pico:              " + ("connected on " + connected if connected
                                 else "not connected (plug it in; the bridge finds it)"))


def cmd_logs(_args=None):
    """Follow the bridge's log (Ctrl-C to stop)."""
    log = log_file()
    if not log.exists():
        fail("no log yet at {}. Has `make start` been run?".format(log))
    say("Following {} (Ctrl-C to stop)\n".format(log))
    with open(log, "r", errors="replace") as fh:
        fh.seek(max(0, log.stat().st_size - 4000))
        try:
            while True:
                line = fh.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass


def cmd_check(_args=None):
    """Verify firmware, configurator and docs agree."""
    cmd_setup()
    run([venv_python(), ROOT / "tools" / "check_protocol.py"])


def cmd_clean(_args=None):
    """Delete .venv and Python caches."""
    shutil.rmtree(VENV, ignore_errors=True)
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    say("Removed .venv and caches.")


def cmd_install(args):
    """Set up everything (default): Python env, firmware, then start the bridge.

    Installs CircuitPython and the firmware when a new Pico is plugged in,
    then starts the bridge if the Pico is connected."""
    cmd_setup()

    if any(find_drive(label) for label in BOOTLOADER_DRIVES):
        cmd_flash(args)

    installed_now = False
    drive = find_drive("CIRCUITPY")
    if drive is not None and drive_writable(drive):
        installed_now = cmd_install_firmware()
    elif drive is None:
        say("\nNo Pico found. Plug it in (hold BOOTSEL while plugging in if it "
            "is brand new) and run `make` again to install the firmware.")
    else:
        say("\nFirmware is already on the Pico (use `make upload` after changing it).")

    if drive is not None and not installed_now:
        say("")
        cmd_start()

    say("\nAll set.")
    if installed_now:
        say("  - Unplug the Pico and plug it back in, then run `make start`.")
    say("  - Each time you plug in the Pico, run `make start` so reminders, links")
    say("    and unlock work. Keyboard actions work without it.")
    say("  - To add or change tag actions, open {} while it runs.".format(BRIDGE_URL))


COMMANDS = {
    "install": cmd_install,
    "setup": cmd_setup,
    "flash": cmd_flash,
    "install-firmware": cmd_install_firmware,
    "upload": cmd_upload,
    "start": cmd_start,
    "stop": cmd_stop,
    "autostart": cmd_autostart,
    "autostart-remove": cmd_autostart_remove,
    "status": cmd_status,
    "logs": cmd_logs,
    "bridge": cmd_bridge,
    "check": cmd_check,
    "clean": cmd_clean,
}


def main():
    parser = argparse.ArgumentParser(
        prog="make",
        description="Set up and run lazyTrigger. With no command, sets up everything.")
    sub = parser.add_subparsers(dest="command", metavar="command")
    for name, func in COMMANDS.items():
        p = sub.add_parser(name, help=(func.__doc__ or "").strip().splitlines()[0])
        if name == "upload":
            p.add_argument("files", nargs="*", help="firmware files (default: all)")
            p.add_argument("--port", help="console serial port, skips detection")
        if name == "start":
            p.add_argument("--at-login", action="store_true", help=argparse.SUPPRESS)
        if name in ("install", "flash"):
            p.add_argument("--board", help="CircuitPython board id, e.g. raspberry_pi_pico2_w")
    sub.add_parser("help", help="show this list")

    args = parser.parse_args()
    if args.command == "help":
        parser.print_help()
        return
    COMMANDS[args.command or "install"](args)


if __name__ == "__main__":
    main()
