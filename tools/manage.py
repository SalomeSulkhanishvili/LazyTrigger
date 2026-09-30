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
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "bridge" / "requirements.txt"
VENV_MARKER = VENV / ".installed"

# The steps shared with the lazyTrigger app live next to the bridge.
sys.path.insert(0, str(ROOT / "bridge"))
import device_setup  # noqa: E402
from device_setup import (BOOTLOADER_DRIVES, IS_WINDOWS, PICO_USB_VENDORS,  # noqa: E402
                          SYSTEM, SetupError, drive_writable, find_drive, log_file)

# Same variable the bridge reads, so `make status` looks in the right place.
BRIDGE_URL = "http://127.0.0.1:{}".format(os.environ.get("LAZYTRIGGER_BRIDGE_PORT", "8787"))


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


def bridge_reachable():
    """Returns the bridge's /ports answer as text, or None if not running."""
    try:
        with urllib.request.urlopen(BRIDGE_URL + "/ports", timeout=2) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
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
    try:
        flashed = device_setup.flash_circuitpython(getattr(args, "board", None), say=say)
    except SetupError as e:
        fail(str(e))
    if not flashed:
        say("No Pico in bootloader mode found. To install CircuitPython: unplug the "
            "Pico, hold its BOOTSEL button while plugging it back in, then run this again.")
    return flashed


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
    step("Installing lazyTrigger on " + str(drive))
    try:
        device_setup.install_firmware(drive, say=say)
    except SetupError as e:
        fail(str(e))
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

    cmd = [str(venv_python()), "-u", str(ROOT / "bridge" / "serial_bridge.py"),
           "--exit-when-unplugged"]
    with device_setup.open_log() as out:
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

def cmd_autostart(_args=None):
    """At each login, check once for the Pico and start the bridge if it's there."""
    cmd_setup()
    item = device_setup.install_login_item(
        [venv_python(windowless=True), ROOT / "tools" / "manage.py", "start", "--at-login"], ROOT)
    say("Login check installed: " + str(item))
    say("At each login it looks for the Pico once, starts the bridge if it's "
        "plugged in, and exits. Nothing keeps running when it isn't.")


def cmd_autostart_remove(_args=None):
    """Remove the login check."""
    device_setup.remove_login_item()
    say("Login check removed.")


def cmd_status(_args=None):
    """Is the bridge running, and is the Pico connected?"""
    if device_setup.login_item_installed():
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


def cmd_app(_args=None):
    """Build the double-click app for this computer into dist/."""
    cmd_setup()
    step("Installing the build tools")
    run([venv_python(), "-m", "pip", "install", "-q", "-r", ROOT / "app" / "requirements.txt"])
    step("Building the app")
    run([venv_python(), ROOT / "tools" / "build_app.py"])


def cmd_clean(_args=None):
    """Delete .venv, app builds and Python caches."""
    for folder in (VENV, ROOT / "build", ROOT / "dist"):
        shutil.rmtree(folder, ignore_errors=True)
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    say("Removed .venv, build/, dist/ and caches.")


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
    "app": cmd_app,
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
