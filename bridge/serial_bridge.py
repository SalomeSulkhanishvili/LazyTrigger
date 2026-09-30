#!/usr/bin/env python3
"""
Local USB-serial bridge for the lazyTrigger configurator.

configurator/index.html normally talks to the Pico directly via the Web
Serial API, but that API only exists in Chromium-based browsers (Chrome,
Edge, Brave). Safari and Firefox don't implement it, and Apple/WebKit has
publicly ruled out ever adding it to Safari. This script fills that gap: it
owns the real serial connection to the device and re-exposes it over plain
localhost HTTP (POST to send a command, Server-Sent Events to receive
messages), which every browser can do without any special permissions.

It also serves the configurator page itself, so the whole thing lives at
one same-origin URL and there's nothing for a browser to treat as
cross-origin.

It also does one more thing the device physically can't: whenever a tag
fires a "notify" action (see PROTOCOL.md), the device can only report that
it happened, it has no reliable clock and no way to draw a popup. This
script does the date math and shows a real OS notification, using whatever
your platform provides (macOS `osascript`, Windows via PowerShell/.NET, or
notify-send/zenity on Linux). For this to actually alert you, this script
needs to be running continuously, not just while you're configuring.

Usage:
    pip3 install pyserial
    python3 bridge/serial_bridge.py

Then open http://127.0.0.1:8787 in any browser (including Safari) and click
"Connect via local bridge".
"""

import datetime
import json
import mimetypes
import os
import platform
import queue
import shutil
import sys
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The firmware's protocol definitions are the source of truth; import them
# rather than repeating the strings here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "firmware"))

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    raise SystemExit("pyserial is required: pip3 install pyserial")

from protocol import ActionType, Event, NotifyMode

HOST = "127.0.0.1"
# Override with the LAZYTRIGGER_BRIDGE_PORT environment variable if 8787 is taken,
# e.g. `LAZYTRIGGER_BRIDGE_PORT=8790 make start`.
HTTP_PORT = int(os.environ.get("LAZYTRIGGER_BRIDGE_PORT", "8787"))
BAUD = 115200
PING_TIMEOUT = 0.6

CONFIGURATOR_DIR = Path(__file__).resolve().parent.parent / "configurator"

ser_lock = threading.Lock()
ser = None
ser_port_name = None
subscribers = []  # list[queue.Queue[str]], one per connected SSE client
subscribers_lock = threading.Lock()


def candidate_ports():
    """USB serial ports worth probing. Only real USB devices qualify (they
    report a vendor id): Bluetooth serial ports such as headphones have none,
    and opening one makes the OS try to reach that device, which is slow and
    happened every 2 seconds while the Pico was unplugged."""
    return [p.device for p in list_ports.comports() if p.vid is not None]


def probe_port(device):
    """Open a port and ask it to ping. The Pico exposes two serial ports
    (a REPL console and this project's JSON data channel) that look
    identical from the OS's port list, so we identify the right one by
    actually talking to it rather than by name/order."""
    try:
        with serial.Serial(device, BAUD, timeout=PING_TIMEOUT) as probe:
            probe.reset_input_buffer()
            probe.write(b'{"cmd":"ping"}\n')
            deadline = time.time() + PING_TIMEOUT
            buf = b""
            while time.time() < deadline:
                chunk = probe.read(256)
                if not chunk:
                    continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line.decode("utf-8", "ignore"))
                    except ValueError:
                        continue
                    if msg.get("pong"):
                        return True
    except (OSError, serial.SerialException):
        pass
    return False


def auto_detect():
    for device in candidate_ports():
        if probe_port(device):
            return device
    return None


def open_serial(device=None):
    global ser, ser_port_name
    with ser_lock:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
            ser = None
        target = device or auto_detect()
        if not target:
            raise RuntimeError("no device responded to a ping on any serial port")
        # A long timeout is fine: reader_loop asks only for what is waiting,
        # so data is handled the moment it arrives, and the idle loop wakes
        # once a second instead of five times.
        ser = serial.Serial(target, BAUD, timeout=1.0)
        ser_port_name = target
        new_ser = ser
    threading.Thread(target=reader_loop, args=(new_ser,), daemon=True).start()
    return target


def _plural(n):
    return "day" if abs(n) == 1 else "days"


def render_reminder(action):
    """Build the notification text using this machine's real clock, the
    device itself has no reliable notion of the date. `mode` is "since"
    (count days since a past fact, e.g, an anniversary) or "until"
    (countdown to a future date, e.g, a deadline); `fact` is free text the
    user wrote themselves, not a template string. Mirrors the JS
    renderReminder() in configurator/index.html so the live preview there
    matches what actually pops up here."""
    title = action.get("title") or "Reminder"
    fact = action.get("fact") or "something"
    date_str = action.get("date")
    if not date_str:
        return title, fact

    try:
        target = datetime.date.fromisoformat(date_str)
    except ValueError:
        return title, fact

    delta = (target - datetime.date.today()).days  # future = positive

    if action.get("mode") == NotifyMode.UNTIL:
        if delta > 0:
            body = f"{delta} {_plural(delta)} until {fact}."
        elif delta == 0:
            body = f"Today: {fact}!"
        else:
            body = f"{fact} was {-delta} {_plural(delta)} ago."
    else:
        days_since = -delta
        if days_since >= 0:
            body = f"It's been {days_since} {_plural(days_since)} since {fact}."
        else:
            body = f"{fact} is in {-days_since} {_plural(days_since)}."
    return title, body


def _applescript_string(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _powershell_string(s):
    return "'" + s.replace("'", "''") + "'"


def show_notification(title, body):
    system = platform.system()
    try:
        if system == "Darwin":
            script = "display notification {} with title {}".format(
                _applescript_string(body), _applescript_string(title)
            )
            subprocess.run(["osascript", "-e", script], check=False)
        elif system == "Windows":
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "[System.Windows.Forms.MessageBox]::Show({}, {}) | Out-Null"
            ).format(_powershell_string(body), _powershell_string(title))
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps])
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body], check=False)
        elif shutil.which("zenity"):
            subprocess.Popen(["zenity", "--info", "--title=" + title, "--text=" + body])
        else:
            print(f"[reminder] {title}: {body}  "
                  f"(install notify-send or zenity to see this as a real popup)")
    except Exception as e:
        print(f"[reminder] failed to show notification: {e}")


def screen_is_locked():
    """Whether the screen is currently locked, or None if we can't tell.

    The device can't work this out on its own. HID only sends keystrokes,
    it can't see the screen, so the bridge reports it. None (unknown) is
    deliberately distinct from False, because the device's policy for
    "unknown" differs from its policy for "definitely unlocked"."""
    system = platform.system()
    try:
        if system == "Darwin":
            import plistlib
            out = subprocess.run(
                ["ioreg", "-n", "Root", "-d1", "-a"], capture_output=True, timeout=4
            ).stdout
            if not out:
                return None
            users = plistlib.loads(out).get("IOConsoleUsers", [])
            return any(u.get("CGSSessionScreenIsLocked") for u in users)

        if system == "Windows":
            # LogonUI.exe owns the screen whenever the workstation is locked.
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq LogonUI.exe"],
                capture_output=True, text=True, timeout=4,
            ).stdout
            return "LogonUI.exe" in out

        # Linux: whichever screensaver interface exists.
        if shutil.which("loginctl"):
            out = subprocess.run(
                ["loginctl", "show-session", "self", "-p", "LockedHint"],
                capture_output=True, text=True, timeout=4,
            ).stdout
            if "LockedHint=" in out:
                return "yes" in out.split("LockedHint=")[1].lower()
        if shutil.which("gnome-screensaver-command"):
            out = subprocess.run(
                ["gnome-screensaver-command", "-q"],
                capture_output=True, text=True, timeout=4,
            ).stdout
            return "is active" in out
    except Exception:
        return None
    return None


def frontmost_app():
    """Name of the app that currently has focus, or None if unknown.

    Used to stop a secret being typed into the wrong window. On macOS this
    deliberately uses `lsappinfo` rather than AppleScript: querying System
    Events needs Automation permission, which most people never granted, and
    a permission prompt mid-tap would be worse than no check at all."""
    system = platform.system()
    try:
        if system == "Darwin":
            front = subprocess.run(["lsappinfo", "front"],
                                   capture_output=True, text=True, timeout=3).stdout.strip()
            if not front:
                return None
            info = subprocess.run(["lsappinfo", "info", "-only", "name", front],
                                  capture_output=True, text=True, timeout=3).stdout.strip()
            # info looks like: "LSDisplayName"="Safari"
            if '"=' in info:
                return info.split('"=', 1)[1].strip().strip('"') or None
            return None

        if system == "Windows":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Add-Type -AssemblyName Microsoft.VisualBasic;"
                 "$h=[void];(Get-Process | Where-Object {$_.MainWindowHandle -eq "
                 "(Add-Type -MemberDefinition '[DllImport(\"user32.dll\")]"
                 "public static extern IntPtr GetForegroundWindow();' "
                 "-Name W -PassThru)::GetForegroundWindow()}).ProcessName"],
                capture_output=True, text=True, timeout=5).stdout.strip()
            return out or None

        if shutil.which("xdotool"):
            out = subprocess.run(["xdotool", "getactivewindow", "getwindowclassname"],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
            return out or None
    except Exception:
        return None
    return None


# Each poll shells out to ioreg and lsappinfo, so polling every second cost
# three process spawns per second and showed up as constant CPU load. The
# device treats a report older than LOCK_STATE_MAX_AGE_S (10s) as unknown and
# fails closed, so anything comfortably under that is safe.
LOCK_POLL_SECONDS = 4


def lock_state_watcher():
    """Tell the device when the lock state changes, so it can refuse to type
    the password into an already-unlocked session."""
    while True:
        try:
            locked = screen_is_locked()
            # Only worth knowing while unlocked: a secret is never typed into
            # a locked screen, so this query can be skipped entirely then.
            app = frontmost_app() if locked is False else None
            with ser_lock:
                active = ser
            if active is not None:
                # Sent every poll, not only on change. The device expires a
                # report after LOCK_STATE_MAX_AGE_S and then refuses to type
                # the password, so a screen left locked for a while would
                # otherwise go stale and unlock would stop working.
                payload = json.dumps(
                    {"cmd": "set_lock_state",
                     "locked": locked if locked is not None else None,
                     "app": app}
                ) + "\n"
                try:
                    active.write(payload.encode("utf-8"))
                except (OSError, serial.SerialException):
                    pass
        except Exception:
            pass
        time.sleep(LOCK_POLL_SECONDS)


def open_url(url):
    """Hand a URL to the OS so it opens in the default browser.

    Only http(s) URLs are accepted. The config this comes from is editable,
    and `open`/`xdg-open` will happily act on things like file:// paths or
    app arguments, so anything else is refused rather than passed through.
    Arguments are passed as a list (never a shell string), so there is no
    shell for a crafted URL to escape into."""
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        print(f"[open_url] refusing non-http(s) URL: {url!r}")
        return
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", url], check=False)
        elif system == "Windows":
            subprocess.run(["cmd", "/c", "start", "", url], check=False)
        elif shutil.which("xdg-open"):
            subprocess.run(["xdg-open", url], check=False)
        else:
            print(f"[open_url] no opener available; wanted to open {url}")
    except Exception as e:
        print(f"[open_url] failed: {e}")


def handle_host_side_action(text):
    """Run the actions the device can't: showing a notification and opening a
    URL both need the OS, so the device only reports that they fired."""
    try:
        msg = json.loads(text)
    except ValueError:
        return
    if msg.get("event") not in (Event.TAP, Event.ACTION_FIRED):
        return
    action = msg.get("action") or {}
    action_type = action.get("type")

    if action_type == ActionType.NOTIFY:
        title, body = render_reminder(action)
        threading.Thread(target=show_notification, args=(title, body), daemon=True).start()
    elif action_type == ActionType.OPEN_URL:
        threading.Thread(target=open_url, args=(action.get("url", ""),), daemon=True).start()


RECONNECT_INTERVAL_S = 2
# With --exit-when-unplugged: how long the Pico may be missing before the
# bridge exits. A firmware upload resets the Pico, which drops it for a few
# seconds, so this must outlast that.
UNPLUGGED_EXIT_S = 10

EXIT_WHEN_UNPLUGGED = "--exit-when-unplugged" in sys.argv


def connection_watcher():
    """Keep a connection to the Pico whenever it is plugged in.

    The bridge is meant to run in the background from login, so it has to
    cope with the Pico not being there yet, being unplugged, and resetting
    (every firmware upload does). A dead handle is dropped by reader_loop;
    this notices and finds the device again. Nobody has to click Connect.

    With --exit-when-unplugged (how the OS starts it when the Pico is plugged
    in), the bridge exits once the Pico has been gone for UNPLUGGED_EXIT_S,
    so nothing keeps running until it is plugged in again."""
    missing_since = time.time()
    while True:
        with ser_lock:
            connected = ser is not None
        if not connected:
            try:
                device = open_serial()
                print(f"Connected to {device}", flush=True)
                connected = True
            except Exception:
                pass
        if connected:
            missing_since = None
        elif missing_since is None:
            missing_since = time.time()
        elif EXIT_WHEN_UNPLUGGED and time.time() - missing_since > UNPLUGGED_EXIT_S:
            print("Pico unplugged; exiting until it is plugged in again.", flush=True)
            os._exit(0)
        time.sleep(RECONNECT_INTERVAL_S)


def reader_loop(my_ser):
    """Reads lines from one serial handle, fans them out to every connected
    SSE subscriber, and separately watches for "notify" actions to pop as
    real OS notifications. Exits quietly once a newer connection replaces
    this one (checked by identity, not truthiness)."""
    global ser
    buf = b""
    while True:
        with ser_lock:
            if ser is not my_ser:
                return
        try:
            chunk = my_ser.read(my_ser.in_waiting or 1)
        except (OSError, serial.SerialException):
            # The Pico reset or was unplugged. Drop the dead handle so
            # connection_watcher reconnects when it comes back.
            with ser_lock:
                if ser is my_ser:
                    ser = None
            try:
                my_ser.close()
            except Exception:
                pass
            print("Device disconnected; waiting for it to come back.", flush=True)
            return
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            text = line.decode("utf-8", "ignore")
            handle_host_side_action(text)
            with subscribers_lock:
                for q in subscribers:
                    q.put(text)


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/ports":
            self._json({"ports": candidate_ports(), "connected": ser_port_name})
            return
        if self.path == "/events":
            self._sse()
            return
        self._static()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}

        if self.path == "/connect":
            try:
                device = open_serial(body.get("port") or None)
                self._json({"ok": True, "port": device})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, status=400)
            return

        if self.path == "/send":
            with ser_lock:
                active = ser
            if active is None:
                self._json({"ok": False, "error": "not connected"}, status=400)
                return
            payload = (json.dumps(body) + "\n").encode("utf-8")
            try:
                active.write(payload)
                self._json({"ok": True})
            except (OSError, serial.SerialException) as e:
                # The handle goes stale when the Pico resets or is replugged
                # (Errno 6 on macOS). Find the device again and resend once,
                # instead of failing every send until someone clicks Connect.
                try:
                    open_serial()
                    with ser_lock:
                        active = ser
                    active.write(payload)
                    self._json({"ok": True, "reconnected": ser_port_name})
                except Exception as e2:
                    self._json({"ok": False, "error": f"{e}; reconnect failed: {e2}"},
                               status=500)
            return

        self._json({"ok": False, "error": "not found"}, status=404)

    def _sse(self):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = queue.Queue()
        with subscribers_lock:
            subscribers.append(q)
        try:
            while True:
                try:
                    line = q.get(timeout=15)
                    self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with subscribers_lock:
                if q in subscribers:
                    subscribers.remove(q)

    def _static(self):
        rel = self.path.lstrip("/") or "index.html"
        rel = rel.split("?", 1)[0]
        target = (CONFIGURATOR_DIR / rel).resolve()
        if CONFIGURATOR_DIR not in target.parents and target != CONFIGURATOR_DIR:
            self._json({"ok": False, "error": "not found"}, status=404)
            return
        if not target.is_file():
            self._json({"ok": False, "error": "not found"}, status=404)
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # The page is edited constantly during setup; a cached copy just looks
        # like the change didn't work.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status=200):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass


def main():
    threading.Thread(target=lock_state_watcher, daemon=True).start()
    # Connects now if the Pico is plugged in, and whenever it (re)appears.
    threading.Thread(target=connection_watcher, daemon=True).start()

    print(f"Open http://{HOST}:{HTTP_PORT} in any browser (Safari included).")
    server = ThreadingHTTPServer((HOST, HTTP_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
