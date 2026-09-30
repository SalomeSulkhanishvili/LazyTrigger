"""
The lazyTrigger app: what runs when someone double-clicks it.

    lazyTrigger                 start the bridge in the background if it
                                isn't running, then open the page
    lazyTrigger --at-login      the login item's one-off check: start the
                                bridge only if the Pico is plugged in
    lazyTrigger --bridge ...    the background bridge itself (started by
                                the two above, never by hand)

Everything else, from setting up a new Pico to the login item, is done from
the page's Setup section, so nobody needs a terminal. tools/build_app.py
packages this with PyInstaller; `python3 app/lazytrigger_app.py` runs it
from a checkout.
"""

import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

import device_setup  # noqa: E402

PORT = int(os.environ.get("LAZYTRIGGER_BRIDGE_PORT", "8787"))
URL = "http://127.0.0.1:{}".format(PORT)


def bridge_running():
    try:
        with urllib.request.urlopen(URL + "/ports", timeout=2):
            return True
    except Exception:
        return False


def pico_plugged_in():
    from serial.tools import list_ports
    return any(p.vid in device_setup.PICO_USB_VENDORS for p in list_ports.comports())


def start_bridge(exit_flag):
    """Start the bridge as its own background process, so it outlives this one."""
    cmd = [sys.executable] if device_setup.FROZEN else [sys.executable, str(Path(__file__).resolve())]
    cmd += ["--bridge", exit_flag]
    env = dict(os.environ)
    # A one-file build unpacks itself to a temporary folder that is deleted
    # when this process exits; the bridge must unpack its own copy.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, env=env, cwd=str(Path.home()))
    if device_setup.IS_WINDOWS:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def run_bridge(args):
    # The bridge reads its flags from sys.argv when it is imported.
    sys.argv = [sys.argv[0]] + args
    log = device_setup.open_log()
    sys.stdout = sys.stderr = log
    import serial_bridge
    serial_bridge.main()


def main():
    args = sys.argv[1:]
    if "--bridge" in args:
        args.remove("--bridge")
        run_bridge(args)
        return

    if "--at-login" in args:
        if not bridge_running() and pico_plugged_in():
            start_bridge("--exit-when-unplugged")
        return

    if not bridge_running():
        start_bridge("--exit-when-idle")
        for _ in range(40):
            time.sleep(0.25)
            if bridge_running():
                break
    webbrowser.open(URL)


if __name__ == "__main__":
    main()
