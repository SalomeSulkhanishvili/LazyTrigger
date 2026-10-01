"""
lazyTrigger: RFID tap-driven keystroke device.

Reads an MFRC522 RFID reader over SPI. Each whitelisted tag has its own list
of actions to run when tapped (cycling to the next one on each repeated tap,
so a single tag can step through several shortcuts/layouts) and its own
action to run when taken away.

The firmware has no built-in idea of "Windows", "Mac", "Linux", "unlock", or
any particular shortcut/layout, it only knows how to play back a handful
of generic action primitives (press a key chord, type text, wait, or type
the stored password). All of the OS- and workflow-specific decisions (which
keys mean "lock" on your machine, which shortcut opens which window layout)
are made in the browser configurator and sent down as data. See
PROTOCOL.md for the wire format and ../configurator/index.html for the UI.

Configuration is fully runtime-editable over a USB CDC serial channel, with
no reflashing required.
"""

import json
import os
import time

import board
import busio
import digitalio
import usb_cdc
import usb_hid
from adafruit_hid.keyboard import Keyboard
from adafruit_hid.keyboard_layout_us import KeyboardLayoutUS
from adafruit_hid.keycode import Keycode

import config
import secretbox
from protocol import ActionType, Command, ConfigKey, Event, TapMode
from mfrc522 import DEFAULT_KEY, MFRC522

# Per-build settings come from settings.toml (see that file): the reader's
# pins and a few timings. Each falls back to the default below, so a missing
# file or line behaves exactly as before.


def _setting_pin(name, default):
    value = os.getenv(name, default)
    try:
        return getattr(board, value)
    except (AttributeError, TypeError):
        raise ValueError("settings.toml: {} = {!r} is not a pin on this board "
                         "(use a GPIO name like \"GP18\")".format(name, value))


def _setting_int(name, default):
    value = os.getenv(name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("settings.toml: {} = {!r} must be a whole number".format(name, value))


# Pin wiring (see README.md for the wiring diagram). SCK/MOSI/MISO must all
# belong to the same hardware SPI block; the defaults are SPI0's SCK/TX/RX.
# CS and RST are plain GPIOs driven by hand, so they can be any free pin.
# Note that "GP20" and "pin 20" are different things: GP20 is physical pin 26.
SPI_SCK = _setting_pin("RFID_SCK", "GP18")
SPI_MOSI = _setting_pin("RFID_MOSI", "GP19")
SPI_MISO = _setting_pin("RFID_MISO", "GP16")
RFID_CS = _setting_pin("RFID_CS", "GP17")
RFID_RST = _setting_pin("RFID_RST", "GP20")
# The reader's IRQ pin is left unconnected: CircuitPython has no GPIO
# interrupt mechanism, so watching it would just mean polling a different
# pin. The main loop polls the reader directly instead.
LED_PIN = board.LED

PAIRING_TIMEOUT_S = _setting_int("PAIRING_TIMEOUT_S", 15)
# An armed read or write waits this long for a tag, then gives up. Without a
# limit it stayed armed forever and would silently consume a later tap that
# was meant to pair a tag or run its actions.
PENDING_OP_TIMEOUT_S = 20
# 50ms between polls keeps a tap feeling instant; a poll with no tag present
# costs one short RF timeout, so this is cheap.
POLL_INTERVAL_S = _setting_int("POLL_INTERVAL_MS", 50) / 1000
# How often the main loop confirms the reader is still answering, and
# re-initialises it if not (see MFRC522.healthy).
READER_CHECK_INTERVAL_S = 2.0
UNLOCK_WAKE_DELAY_S = 0.8

# MIFARE Classic data block used to store an optional label written onto the
# tag itself (sector 1, first data block, avoids the manufacturer block 0
# and every sector trailer, which hold keys/access bits).
DEFAULT_DATA_BLOCK = 4

# Block holding the 16-byte AES key that decrypts the unlock password. Kept
# on the tag so the Pico's flash never contains it (see secretbox.py).
# Block 5 is sector 1's second data block; block 7 is that sector's trailer.
KEY_BLOCK = 5


def hex_to_bytes(s):
    s = s.strip()
    return bytes(int(s[i : i + 2], 16) for i in range(0, len(s), 2))


def bytes_to_hex(b):
    return "".join("{:02X}".format(x) for x in b)


def pad_to_block(data_bytes):
    buf = bytearray(16)
    n = min(len(data_bytes), 16)
    buf[:n] = data_bytes[:n]
    return bytes(buf)


def unpad_block(data_bytes):
    return bytes(data_bytes).rstrip(b"\x00")


# Keyboard layouts the device can type through, so text comes out right on
# a computer set to that layout. The German ones are separate files, loaded
# only when chosen.
KEYBOARD_LAYOUTS = {
    "us": None,
    "de_mac": "keyboard_layout_mac_de",
    "de_win": "keyboard_layout_win_de",
    "ka": "keyboard_layout_ka",
}


def make_layout(keyboard, name):
    module = KEYBOARD_LAYOUTS.get(name)
    if module:
        try:
            return __import__(module).KeyboardLayout(keyboard)
        except ImportError as e:
            print("keyboard layout {} unavailable ({}); typing as US".format(name, e))
    return KeyboardLayoutUS(keyboard)


def setup_hardware(cfg):
    spi = busio.SPI(SPI_SCK, MOSI=SPI_MOSI, MISO=SPI_MISO)
    cs = digitalio.DigitalInOut(RFID_CS)
    rst = digitalio.DigitalInOut(RFID_RST)
    reader = MFRC522(spi, cs, rst)

    led = digitalio.DigitalInOut(LED_PIN)
    led.direction = digitalio.Direction.OUTPUT

    keyboard = Keyboard(usb_hid.devices)
    layout = make_layout(keyboard, cfg.get("keyboard_layout"))

    return reader, led, keyboard, layout


def resolve_keycode(name):
    """Look up a key name (e.g. "CONTROL", "L", "ONE") on adafruit_hid's
    Keycode class. Returns None for anything unrecognized so a bad name from
    the config never crashes the device, it's just silently skipped."""
    try:
        return getattr(Keycode, str(name).upper())
    except AttributeError:
        return None


# How long a tag's key stays cached in RAM after it was last read, so that
# saving several secrets in a row doesn't need a tap each time. This is a
# deliberate convenience/secrecy trade: the key is in volatile memory only
# (never written to flash) and is gone on unplug, reboot, or this timeout.
# Set to 0 to require a tap for every single operation.
KEY_CACHE_SECONDS = _setting_int("KEY_CACHE_S", 300)

# The host (bridge) reports whether the screen is locked, because the device
# cannot see the screen. HID only sends keystrokes. A report older than
# this is treated as unknown, so a bridge that died can't leave the device
# believing the screen is still locked forever.
LOCK_STATE_MAX_AGE_S = 10


def cache_tag_key(state, uid_bytes, key):
    state["cached_key"] = key
    state["cached_key_uid"] = MFRC522.uid_to_str(uid_bytes) if uid_bytes else None
    state["cached_key_until"] = time.monotonic() + KEY_CACHE_SECONDS


def cached_tag_key(state):
    """The key from a recent tap, or None once it has expired."""
    if not state.get("cached_key"):
        return None
    if time.monotonic() > state.get("cached_key_until", 0):
        state["cached_key"] = None
        state["cached_key_uid"] = None
        return None
    return state["cached_key"]


# Reading or writing the key block takes several exchanges in a row (select,
# authenticate, read/write), and a tag at the edge of the field can miss any
# one of them. Each attempt starts over from selecting the tag.
TAG_KEY_ATTEMPTS = 3


def read_tag_key_checked(reader, uid_bytes):
    """Read the tag's key block. Returns (read_ok, key).

    read_ok False means the tag couldn't be read, which says nothing about
    whether it holds a key. (True, None) means it was read and is empty.
    Callers that might write a new key must tell these apart."""
    for _ in range(TAG_KEY_ATTEMPTS):
        if not reader.reselect(uid_bytes):
            continue
        if not reader.auth(KEY_BLOCK, DEFAULT_KEY, uid_bytes):
            reader.stop_crypto1()
            continue
        try:
            data = reader.read_block(KEY_BLOCK)
        finally:
            reader.end_session()
        if data is None:
            continue
        if not any(data):
            return True, None
        return True, bytes(data[: secretbox.KEY_SIZE])
    return False, None


def read_tag_key(reader, uid_bytes):
    """Read the AES key stored on the tag, or None if it isn't there or the
    tag couldn't be read."""
    if reader is None or uid_bytes is None:
        return None
    return read_tag_key_checked(reader, uid_bytes)[1]


def decrypt_with_tag(enc_hex, iv_hex, reader, uid_bytes):
    """Decrypt something using the key held on the tag currently on the
    reader. Returns "" if the tag can't supply the right key, which is the
    intended outcome for a tag that isn't the one it was encrypted with."""
    if not enc_hex:
        return ""
    key = read_tag_key(reader, uid_bytes)
    if key is None:
        return ""
    return secretbox.decrypt(hex_to_bytes(enc_hex), key, hex_to_bytes(iv_hex))


def unlock_password(cfg, reader, uid_bytes, state=None):
    key = read_tag_key(reader, uid_bytes)
    if key is None:
        return ""
    if state is not None:
        cache_tag_key(state, uid_bytes, key)
    if not cfg.get("password_enc"):
        return ""
    return secretbox.decrypt(
        hex_to_bytes(cfg["password_enc"]), key, hex_to_bytes(cfg["password_iv"])
    )


def screen_locked(state):
    """True/False as last reported by the host, or None if we don't know
    (no bridge running, or the last report has gone stale)."""
    if state is None:
        return None
    reported_at = state.get("lock_state_at", 0)
    if not reported_at or time.monotonic() - reported_at > LOCK_STATE_MAX_AGE_S:
        return None
    return state.get("lock_state")


def chord_keycodes(chord, layout):
    """Keycodes for a shortcut. A letter names the character, as shortcuts
    do (Cmd+Z is undo), and which key types it depends on the layout: Z and
    Y trade places on a German keyboard."""
    codes = []
    for name in chord:
        name = str(name)
        if len(name) == 1 and name.isalpha():
            found = layout.keycodes(name.lower())
            if len(found) == 1:
                codes.append(found[0])
                continue
        code = resolve_keycode(name)
        if code is not None:
            codes.append(code)
    return codes


def run_action(keyboard, layout, cfg, action, reader=None, uid_bytes=None, state=None):
    """Play back a single generic action. Unknown types/keys are ignored
    rather than raising, since these come from user-editable config."""
    action_type = action.get("type")

    if action_type == ActionType.UNLOCK:
        # Refuse to type the password into an already-unlocked session: that
        # sends it to whatever window happens to have focus (a chat, a shared
        # screen). The device can't see the screen, so it relies on the host
        # reporting lock state; `require_locked` decides what to do when no
        # report is available.
        locked = screen_locked(state)
        require_locked = cfg.get("require_locked", True)
        if locked is False or (locked is None and require_locked):
            return {
                "event": Event.UNLOCK_BLOCKED,
                "reason": "screen is unlocked" if locked is False
                          else "lock state unknown (is the bridge running?)",
            }

        # Decrypt before waking the screen: reading the key off the tag
        # takes a moment, and doing it first keeps the delay between the
        # wake keypress and the typing consistent.
        password = unlock_password(cfg, reader, uid_bytes)

        # Nothing to type means nothing to do. Pressing Enter anyway (as this
        # used to) submits an empty password, which the computer counts as a
        # failed login attempt every time the wrong tag is tapped.
        if not password:
            return {
                "event": Event.UNLOCK_BLOCKED,
                "reason": "no unlock password is set" if not cfg.get("password_enc")
                          else "this tag can't decrypt the password (only the tag "
                               "tapped when the password was set can)",
            }

        # The wake key just has to rouse the display and bring up the
        # password field. It must not be ESCAPE on macOS, where it cancels
        # the login prompt and can bounce back to user selection. SHIFT is
        # the safe default everywhere: it wakes the screen, types nothing,
        # and cancels nothing.
        wake_keys = action.get("wake_keys") or ["SHIFT"]
        codes = [resolve_keycode(k) for k in wake_keys]
        codes = [c for c in codes if c is not None]
        if codes:
            keyboard.press(*codes)
            keyboard.release_all()

        try:
            delay = float(action.get("delay", UNLOCK_WAKE_DELAY_S))
        except (TypeError, ValueError):
            delay = UNLOCK_WAKE_DELAY_S
        time.sleep(delay)

        if password:
            layout.write(password)
        keyboard.press(Keycode.ENTER)
        keyboard.release_all()

    elif action_type in (ActionType.KEYS, ActionType.LOCK):
        # "lock" is just a labelled key chord, the combo that locks a
        # screen differs per OS (Win+L, Ctrl+Cmd+Q, Super+L), so the actual
        # keys still come from the config rather than being hardcoded here.
        #
        # `chords` sends several combos in sequence, which is how the
        # "universal" lock works without knowing which OS it's plugged into:
        # the combo for the wrong OS lands on an already-locked screen and
        # does nothing. `keys` is the single-chord form.
        chords = action.get("chords") or [action.get("keys", [])]
        for index, chord in enumerate(chords):
            codes = chord_keycodes(chord, layout)
            if not codes:
                continue
            keyboard.press(*codes)
            keyboard.release_all()
            if index + 1 < len(chords):
                time.sleep(0.4)

    elif action_type == ActionType.TEXT:
        text = action.get("text", "")
        if text:
            layout.write(text)
        if action.get("enter"):
            keyboard.press(Keycode.ENTER)
            keyboard.release_all()

    elif action_type == ActionType.SECRET:
        # A secret is meant to be typed INTO an unlocked session (a VPN
        # dialog, a login form), so unlike `unlock` it must not require the
        # screen to be locked. The risk here is a different one: typing it
        # into the wrong window. If the action names an app, only type when
        # that app actually has focus.
        want_app = (action.get("only_in_app") or "").strip()
        if want_app:
            front = (state or {}).get("front_app")
            fresh = screen_locked(state) is not None or (state or {}).get("lock_state_at")
            if not front or not fresh:
                return {"event": Event.UNLOCK_BLOCKED,
                        "reason": "can't see which app has focus (is the bridge running?)"}
            if front.lower() != want_app.lower():
                return {"event": Event.UNLOCK_BLOCKED,
                        "reason": "focused app is " + front + ", expected " + want_app}

        # A named secret, encrypted with the same tag-held key as the unlock
        # password. Typed like text, but never stored or transmitted in the
        # clear, it only exists decrypted for the moment it's being typed.
        secret = config.find_secret(cfg, action.get("id", ""))
        if secret:
            value = decrypt_with_tag(
                secret.get("enc", ""), secret.get("iv", ""), reader, uid_bytes
            )
            if value:
                layout.write(value)
                if action.get("enter"):
                    keyboard.press(Keycode.ENTER)
                    keyboard.release_all()

    elif action_type == ActionType.WAIT:
        try:
            time.sleep(float(action.get("seconds", 0.2)))
        except (TypeError, ValueError):
            pass

    elif action_type == ActionType.OPEN_URL:
        # Handled by the host, like NOTIFY: opening a URL in the default
        # browser is something the OS does, not something a keyboard can do
        # reliably. The device just reports that it fired and the bridge
        # hands the URL to the OS. See ActionType.HOST_SIDE.
        pass

    elif action_type == ActionType.NOTIFY:
        # No keystrokes, this is a message for a human, not the computer.
        # The device just reports that it fired (see the "tap"/"action_fired"
        # events, which include the full action); whatever's listening on
        # the data serial port (bridge/serial_bridge.py, or the configurator
        # page itself) does the date math and shows the actual notification,
        # since only the host has a reliable clock and a way to draw a popup.
        pass


def run_actions(keyboard, layout, cfg, actions, reader=None, uid_bytes=None, state=None):
    for action in actions:
        run_action(keyboard, layout, cfg, action, reader, uid_bytes, state)


def blink(led, times, on_time=0.1, off_time=0.1):
    for _ in range(times):
        led.value = True
        time.sleep(on_time)
        led.value = False
        time.sleep(off_time)


# MIFARE Classic 1K: 16 sectors of 4 blocks, the last of each the trailer.
TAG_BLOCKS = 64


def printable(data):
    """Block bytes as text, with anything that isn't printable ASCII as ".".

    Not bytes.decode: CircuitPython ignores decode's "replace" argument and
    raises UnicodeError on binary data such as the manufacturer block."""
    return "".join(chr(c) if 32 <= c < 127 else "." for c in data)


def _block_kind(block):
    if block == 0:
        return "manufacturer"
    if (block + 1) % 4 == 0:
        return "trailer"
    if block == KEY_BLOCK:
        return "key"
    if block == DEFAULT_DATA_BLOCK:
        return "label"
    return "data"


def _read_sector(reader, uid_bytes, key, first):
    """The four blocks of one sector (the key block as None, never read), or
    None if the sector couldn't be read in TAG_KEY_ATTEMPTS tries."""
    for _ in range(TAG_KEY_ATTEMPTS):
        if not reader.reselect(uid_bytes):
            continue
        if not reader.auth(first, key, uid_bytes):
            reader.stop_crypto1()
            continue
        try:
            rows = []
            for block in range(first, first + 4):
                if block == KEY_BLOCK:
                    rows.append(None)
                    continue
                data = reader.read_block(block)
                if data is None:
                    break
                rows.append(data)
            else:
                return rows
        finally:
            reader.end_session()
    return None


def dump_tag(reader, uid_bytes, key):
    """Read every block of the tag in one tap.

    The key block (KEY_BLOCK) is never read here, so the key that decrypts
    the password and secrets can't end up on screen or in a log. Sectors
    that can't be read (a changed sector key, or the tag moving away) are
    reported per block, and the rest are still returned."""
    uid_str = MFRC522.uid_to_str(uid_bytes)
    blocks = []
    unreadable = 0
    try:
        for first in range(0, TAG_BLOCKS, 4):
            rows = _read_sector(reader, uid_bytes, key, first)
            if rows is None:
                unreadable += 1
            for offset in range(4):
                block = first + offset
                entry = {"block": block, "kind": _block_kind(block)}
                if block == KEY_BLOCK:
                    entry["hidden"] = True
                elif rows is None:
                    entry["error"] = "unreadable"
                else:
                    data = rows[offset]
                    entry["hex"] = bytes_to_hex(data)
                    entry["text"] = printable(data)
                blocks.append(entry)
    finally:
        reader.halt()
    if unreadable == TAG_BLOCKS // 4:
        return {"ok": False, "event": Event.TAG_DATA_ERROR, "uid": uid_str,
                "error": "couldn't read the tag. Hold it flat and still on the reader"}
    return {"ok": True, "event": Event.TAG_DUMP, "uid": uid_str, "blocks": blocks,
            "unreadable_sectors": unreadable}


def execute_data_op(reader, uid_bytes, op):
    """
    Run a pending read/write against the card currently in the field.

    `op` is {"type": "read"|"write", "block": int, "key": bytes,
    "data": bytes (16, write only)}.

    Each attempt starts over from selecting the tag, since a tag at the edge
    of the field can miss any one exchange (see TAG_KEY_ATTEMPTS). The error
    reported is the furthest step reached, so "auth failed" only appears
    when the tag answered but refused the key.
    """
    if op["type"] == "dump":
        return dump_tag(reader, uid_bytes, op["key"])
    uid_str = MFRC522.uid_to_str(uid_bytes)
    error = "couldn't select the tag. Hold it flat and still on the reader"
    try:
        for _ in range(TAG_KEY_ATTEMPTS):
            if not reader.reselect(uid_bytes):
                continue
            if not reader.auth(op["block"], op["key"], uid_bytes):
                reader.stop_crypto1()
                # The tag was selected, so the radio link works; a refused
                # login usually means a sector key changed from the default,
                # or a tag that isn't MIFARE Classic (NTAG/Ultralight answer
                # UID reads but have no sector keys).
                error = ("auth failed for block {} (wrong key, or this tag is "
                         "not MIFARE Classic)".format(op["block"]))
                continue
            try:
                if op["type"] == "read":
                    data = reader.read_block(op["block"])
                    if data is None:
                        error = "read failed. Hold the tag still and try again"
                        continue
                    return {
                        "ok": True,
                        "event": Event.TAG_DATA_READ,
                        "uid": uid_str,
                        "block": op["block"],
                        "data_hex": bytes_to_hex(data),
                        "text": printable(unpad_block(data)),
                    }
                if not reader.write_block(op["block"], op["data"]):
                    error = "write failed. Hold the tag still and try again"
                    continue
                return {"ok": True, "event": Event.TAG_DATA_WRITTEN, "uid": uid_str,
                        "block": op["block"]}
            finally:
                reader.end_session()
        return {"ok": False, "event": Event.TAG_DATA_ERROR, "uid": uid_str, "error": error}
    finally:
        reader.halt()


class SerialLink:
    """Line-buffered JSON request/response protocol over usb_cdc.data."""

    def __init__(self, stream):
        self._stream = stream
        self._buf = b""
        # Bare {"ok": true} replies only say "command received". The bridge's
        # screen-lock reports draw one every few seconds, so outside debug
        # mode (config "debug_log") they aren't sent at all.
        self.send_bare_acks = False

    def poll(self):
        if self._stream is None:
            return None
        n = self._stream.in_waiting
        if n:
            self._buf += self._stream.read(n)
        if b"\n" not in self._buf:
            return None
        line, self._buf = self._buf.split(b"\n", 1)
        line = line.strip()
        if not line:
            return None
        try:
            return json.loads(line.decode("utf-8"))
        except ValueError:
            return {"cmd": "__invalid__"}

    def send(self, obj):
        if self._stream is None:
            return
        if not self.send_bare_acks and obj == {"ok": True}:
            return
        self._stream.write((json.dumps(obj) + "\n").encode("utf-8"))


def get_or_create_tag_key(reader, uid_bytes):
    """Return the tag's existing key, or write a fresh one if it has none.

    Reusing an existing key matters: a tag may already hold the key for the
    unlock password, and overwriting it would silently orphan every secret
    encrypted with it."""
    read_ok, existing = read_tag_key_checked(reader, uid_bytes)
    if not read_ok:
        # A failed read is not an empty tag. Writing a fresh key here (as
        # this once did) replaced a perfectly good key after one missed
        # exchange, and orphaned everything encrypted with it.
        return None, False
    if existing is not None:
        return existing, True

    key = secretbox.random_key()
    for _ in range(TAG_KEY_ATTEMPTS):
        if not reader.reselect(uid_bytes):
            continue
        if not reader.auth(KEY_BLOCK, DEFAULT_KEY, uid_bytes):
            reader.stop_crypto1()
            continue
        try:
            wrote = reader.write_block(KEY_BLOCK, pad_to_block(key))
        finally:
            reader.end_session()
        if wrote:
            reader.halt()
            return key, True
    reader.halt()
    return None, False


def store_password_with_tag_key(cfg, reader, uid_bytes, pending, state=None):
    """Encrypt a password/secret with the tag's key, keeping only the
    resulting ciphertext on this device."""
    key, ok = get_or_create_tag_key(reader, uid_bytes)
    if not ok or key is None:
        return {"ok": False, "event": Event.PASSWORD_ERROR,
                "error": "couldn't read this tag reliably. Hold it flat and still "
                         "on the reader, then try again"}
    if state is not None:
        cache_tag_key(state, uid_bytes, key)
    return encrypt_into_config(cfg, key, pending, MFRC522.uid_to_str(uid_bytes))


def encrypt_into_config(cfg, key, pending, uid_str):
    """Encrypt `pending` with `key` and save the ciphertext. Split out from
    the tap path so a cached key can reuse it without a tag present."""
    ciphertext, iv = secretbox.encrypt(pending["value"], key)

    if pending.get("secret_id"):
        secret = config.find_secret(cfg, pending["secret_id"])
        if secret is None:
            secret = {"id": pending["secret_id"]}
            cfg["secrets"].append(secret)
        secret["label"] = pending.get("label", "")
        secret["enc"] = bytes_to_hex(ciphertext)
        secret["iv"] = bytes_to_hex(iv)
        config.save(cfg)
        return {"ok": True, "event": Event.SECRET_SET, "id": pending["secret_id"], "uid": uid_str}

    cfg["password_enc"] = bytes_to_hex(ciphertext)
    cfg["password_iv"] = bytes_to_hex(iv)
    cfg["password_hash"] = pending.get("new_hash", "")
    # Public: the configurator needs both to recompute the check value from a
    # typed password. Neither helps an attacker without the hash itself.
    cfg["password_salt"] = pending.get("new_salt", "")
    cfg["password_kdf"] = pending.get("new_kdf", "")
    config.save(cfg)
    return {"ok": True, "event": Event.PASSWORD_SET, "uid": uid_str}


def public_config(cfg):
    """The config as anything outside the device is allowed to see it.

    The password is write-only: it is never sent back over the serial link,
    so neither the configurator nor anything else listening on the port can
    read it. Callers get only whether one is set."""
    safe = {}
    for key, value in cfg.items():
        if key in ConfigKey.PRIVATE:
            continue
        safe[key] = value
    safe["has_password"] = bool(cfg.get("password_enc"))
    # Named secrets: ids and labels are fine to expose, ciphertext is not.
    safe["secrets"] = [
        {"id": s.get("id"), "label": s.get("label", "")}
        for s in cfg.get("secrets", [])
    ]
    return safe


def handle_command(msg, cfg, link, state):
    cmd = msg.get("cmd")

    if cmd == Command.PING:
        link.send({"ok": True, "pong": True})

    elif cmd == Command.GET_CONFIG:
        link.send({"ok": True, "config": public_config(cfg)})

    elif cmd == Command.SET_PASSWORD:
        # Changing the password requires proving you know the current one.
        # The client computes the check value (salted PBKDF2, see config.py)
        # and the device only compares it, so the old password never crosses
        # the wire and the device never needs to hash anything itself.
        stored_hash = cfg.get("password_hash", "")
        if stored_hash and msg.get("old_hash", "") != stored_hash:
            link.send({"ok": False, "error": "current password is incorrect"})
            return
        # The encryption key goes on the tag, not on this device, so we need
        # a tag on the reader to finish. Arm it and let the main loop
        # complete the job on the next tap.
        state["pending_password"] = {
            "value": msg.get("value", ""),
            "new_hash": msg.get("new_hash", ""),
            "new_salt": msg.get("new_salt", ""),
            "new_kdf": msg.get("new_kdf", ""),
        }
        link.send({"ok": True, "waiting_for_tag": True,
                   "note": "tap the tag that should be able to unlock"})

    elif cmd == Command.RESET_PASSWORD:
        # Forgets the unlock password only; tags, actions and saved secrets
        # stay. Safe without proof of the old password: the password can't
        # be read either way, and setting a new one still needs a tag tap.
        for key in (ConfigKey.PASSWORD_ENC, ConfigKey.PASSWORD_IV, ConfigKey.PASSWORD_HASH,
                    ConfigKey.PASSWORD_SALT, ConfigKey.PASSWORD_KDF):
            cfg[key] = ""
        # A password change armed earlier would otherwise complete on the
        # next tap and bring a password back.
        pending = state.get("pending_password")
        if pending is not None and not pending.get("secret_id"):
            state["pending_password"] = None
        config.save(cfg)
        link.send({"ok": True, "config": public_config(cfg)})

    elif cmd == Command.SET_SECRET:
        # Same encryption path as the password, just stored under a label.
        secret_id = msg.get("id") or "s{}".format(int(time.monotonic() * 1000))
        pending = {
            "value": msg.get("value", ""),
            "secret_id": secret_id,
            "label": msg.get("label", ""),
        }
        # If a tag was tapped recently its key is still in RAM, so this can
        # finish now instead of asking for another tap.
        key = cached_tag_key(state)
        if key is not None:
            link.send(encrypt_into_config(cfg, key, pending, state.get("cached_key_uid")))
            link.send({"event": Event.CONFIG, "config": public_config(cfg)})
            return
        state["pending_password"] = pending
        link.send({"ok": True, "waiting_for_tag": True, "id": secret_id,
                   "note": "tap the tag that should be able to type this"})

    elif cmd == Command.REMOVE_SECRET:
        config.remove_secret(cfg, msg.get("id", ""))
        link.send({"ok": True, "config": public_config(cfg)})

    elif cmd == Command.SET_LOCK_DEBOUNCE_MS:
        cfg["lock_debounce_ms"] = int(msg.get("value", cfg["lock_debounce_ms"]))
        config.save(cfg)
        link.send({"ok": True})

    elif cmd == Command.DETACH_UID:
        # Removes one tag from its set. The set keeps its actions and simply
        # becomes unassigned if that was its last tag.
        uid = msg.get("uid", "")
        config.detach_uid(cfg, uid)
        state["cycle_index"].pop(uid, None)
        link.send({"ok": True, "config": public_config(cfg)})

    elif cmd == Command.REMOVE_SET:
        config.remove_set(cfg, msg.get("id", ""))
        link.send({"ok": True, "config": public_config(cfg)})

    elif cmd == Command.RENAME_SET:
        if not config.rename_set(cfg, msg.get("id", ""), msg.get("label", "")):
            link.send({"ok": False, "error": "unknown set"})
            return
        link.send({"ok": True, "config": public_config(cfg)})

    elif cmd == Command.SET_TAG_ACTIONS:
        # Replaces a set's action lists. The configurator sends this when you
        # save an edited tag, and when you move one action out into a set of
        # its own, so without it every edit came back "unknown command".
        if not config.set_tag_actions(
            cfg,
            msg.get("id", ""),
            msg.get("on_tap") or [],
            msg.get("on_remove") or [],
            msg.get("tap_mode"),
        ):
            link.send({"ok": False, "error": "unknown set"})
            return
        # The cycle position points into the old list, which may now be
        # shorter; start the new list from the beginning.
        for uid in config.find_set(cfg, msg.get("id", "")).get("uids", []):
            state["cycle_index"].pop(uid, None)
        link.send({"ok": True, "config": public_config(cfg)})

    elif cmd == Command.NEW_SET:
        # A set with no tags yet; attach one later via start_pairing.
        entry = config.new_set(
            cfg,
            msg.get("label", ""),
            msg.get("on_tap") or [],
            msg.get("on_remove") or [],
            msg.get("tap_mode"),
        )
        link.send({"ok": True, "id": entry["id"], "config": public_config(cfg)})

    elif cmd == Command.START_PAIRING:
        state["pairing_until"] = time.monotonic() + PAIRING_TIMEOUT_S
        state["pairing_label"] = msg.get("label", "")
        state["pairing_on_tap"] = msg.get("on_tap") or [{"type": "unlock"}]
        state["pairing_on_remove"] = msg.get("on_remove") or []
        state["pairing_tap_mode"] = msg.get("tap_mode") or TapMode.SEQUENCE
        # When set, the tapped tag joins this existing set instead of
        # creating a new one, which is how one set gets several tags.
        state["pairing_set_id"] = msg.get("set_id")
        # Optionally also write the label onto the card itself (block 4) the
        # moment it's tapped, so the tag carries its own identity/data.
        state["pairing_write_label"] = bool(msg.get("write_label", False))
        # The main loop only reacts to a *change* of tag, so a tag already
        # sitting on the reader when pairing starts would never be seen: it
        # was latched as "present" by the tap that got here. Drop the latch so
        # the very next poll counts as a fresh tap. Without this, pairing a
        # tag you left on the reader silently times out, which looks exactly
        # like a reader that cannot detect the tag.
        state["rescan"] = True
        link.send({"ok": True, "pairing_seconds": PAIRING_TIMEOUT_S})

    elif cmd == Command.CANCEL_PAIRING:
        state["pairing_until"] = 0
        link.send({"ok": True})

    elif cmd == Command.READ_TAG_ALL:
        try:
            key = hex_to_bytes(msg.get("key_hex", "")) or DEFAULT_KEY
        except ValueError:
            key = DEFAULT_KEY
        if len(key) != 6:
            key = DEFAULT_KEY
        state["pending_data_op"] = {"type": "dump", "key": key}
        state["pending_data_op_until"] = time.monotonic() + PENDING_OP_TIMEOUT_S
        link.send({"ok": True, "waiting_for_tag": True})

    elif cmd in (Command.READ_TAG_DATA, Command.WRITE_TAG_DATA):
        try:
            block = int(msg.get("block", DEFAULT_DATA_BLOCK))
            key = hex_to_bytes(msg.get("key_hex", "")) or DEFAULT_KEY
            if len(key) != 6:
                key = DEFAULT_KEY
        except ValueError:
            link.send({"ok": False, "error": "bad block/key_hex"})
            return

        # Sector trailer blocks (every 4th block, e.g. 3, 7, 11...) hold the
        # keys/access bits, never let the API touch them.
        if (block + 1) % 4 == 0:
            link.send({"ok": False, "error": "block is a sector trailer, refusing"})
            return

        # Block 5 holds the AES key for this tag's password and secrets.
        # Overwriting it makes all of them permanently undecryptable, so it
        # takes an explicit acknowledgement rather than a stray block number.
        if (cmd == Command.WRITE_TAG_DATA and block == KEY_BLOCK
                and not msg.get("confirm_key_overwrite")):
            link.send({
                "ok": False,
                "error": "block {} holds this tag's encryption key; writing to it "
                         "would permanently destroy the stored password and secrets. "
                         "Resend with confirm_key_overwrite to proceed.".format(KEY_BLOCK),
                "needs_confirmation": "key_overwrite",
                "block": block,
            })
            return

        if cmd == Command.READ_TAG_DATA:
            state["pending_data_op"] = {"type": "read", "block": block, "key": key}
            state["pending_data_op_until"] = time.monotonic() + PENDING_OP_TIMEOUT_S
        else:
            try:
                raw = msg.get("text")
                data = raw.encode("utf-8") if raw is not None else hex_to_bytes(msg.get("data_hex", ""))
            except ValueError:
                link.send({"ok": False, "error": "bad data_hex"})
                return
            state["pending_data_op"] = {
                "type": "write",
                "block": block,
                "key": key,
                "data": pad_to_block(data),
            }
            state["pending_data_op_until"] = time.monotonic() + PENDING_OP_TIMEOUT_S

        link.send({"ok": True, "waiting_for_tag": True})

    elif cmd == Command.SET_THEME:
        value = msg.get("value", "auto")
        cfg["theme"] = value if value in ("auto", "light", "dark") else "auto"
        config.save(cfg)
        link.send({"ok": True})

    elif cmd == Command.SET_KEYBOARD_LAYOUT:
        value = msg.get("value", "us")
        cfg["keyboard_layout"] = value if value in KEYBOARD_LAYOUTS else "us"
        config.save(cfg)
        state["layout"] = make_layout(state["keyboard"], cfg["keyboard_layout"])
        link.send({"ok": True})

    elif cmd == Command.SET_DEBUG_LOG:
        cfg["debug_log"] = bool(msg.get("value", False))
        config.save(cfg)
        link.send_bare_acks = cfg["debug_log"]
        link.send({"ok": True})

    elif cmd == Command.SET_LOCK_STATE:
        state["lock_state"] = msg.get("locked")
        state["front_app"] = msg.get("app")
        state["lock_state_at"] = time.monotonic()
        link.send({"ok": True})

    elif cmd == Command.FACTORY_RESET:
        cfg.clear()
        cfg.update(config.DEFAULT_CONFIG)
        cfg["tags"] = []
        config.save(cfg)
        link.send_bare_acks = bool(cfg.get("debug_log"))
        state["layout"] = make_layout(state["keyboard"], cfg.get("keyboard_layout"))
        state["cycle_index"] = {}
        link.send({"ok": True, "config": public_config(cfg)})

    elif cmd == Command.TEST_ACTION:
        # Lets the configurator preview a single action before it's saved to
        # any tag, fires it exactly like a real tap would, without needing
        # a tag present. "notify" actions produce no keystrokes (see
        # run_action), so this is safe to fire while the configurator page
        # itself has focus; anything else (keys/text/unlock) really does
        # type into whatever window currently has focus, same as a real tap.
        action = msg.get("action", {})
        link.send({"event": Event.ACTION_FIRED, "action": action})
        blocked = run_action(state["keyboard"], state["layout"], cfg, action,
                             None, None, state)
        if blocked:
            link.send(blocked)
        link.send({"ok": True})

    else:
        link.send({"ok": False, "error": "unknown command"})


def main():
    cfg = config.load()
    reader, led, keyboard, layout = setup_hardware(cfg)
    link = SerialLink(usb_cdc.data)
    link.send_bare_acks = bool(cfg.get("debug_log"))

    state = {
        "reader": reader,
        "keyboard": keyboard,
        "layout": layout,
        "pairing_until": 0,
        "pairing_label": "",
        "pairing_on_tap": [{"type": "unlock"}],
        "pairing_on_remove": [],
        "pairing_tap_mode": TapMode.SEQUENCE,
        "pairing_set_id": None,
        "pairing_write_label": False,
        "pending_data_op": None,
        "pending_data_op_until": 0,
        "pending_password": None,
        # Tag key held in RAM only (never flash) after a tap, so several
        # secrets can be saved without re-tapping. See KEY_CACHE_SECONDS.
        "cached_key": None,
        "cached_key_uid": None,
        "cached_key_until": 0,
        # Screen lock state as last reported by the host; see screen_locked().
        "lock_state": None,
        "front_app": None,
        "lock_state_at": 0,
        # Per-uid index into that tag's on_tap list, so repeated taps cycle
        # through it (1, 2, 3, 1, 2, 3, ...). Not persisted across reboots.
        "cycle_index": {},
    }

    present_uid = None
    last_seen = 0.0
    next_reader_check = 0.0

    while True:
        try:
            now = time.monotonic()

            # The reader is set up once at boot. If it wasn't answering then (a
            # wire not yet seated) or drops out later, it would stay dead until a
            # reboot, silently finding no tags. Check it and bring it back.
            if now >= next_reader_check:
                next_reader_check = now + READER_CHECK_INTERVAL_S
                if not reader.healthy():
                    reader.init_chip()

            msg = link.poll()
            if msg is not None:
                handle_command(msg, cfg, link, state)

            # A command can ask for the "tag already present" latch to be
            # dropped, so a tag that never left the reader reads as a new tap.
            if state.pop("rescan", False):
                present_uid = None

            # An armed read/write that nobody ever tapped for: clear it and say
            # so, rather than leaving the configurator waiting forever and
            # letting the op swallow a later tap.
            if (state["pending_data_op"] is not None
                    and now >= state.get("pending_data_op_until", 0)):
                state["pending_data_op"] = None
                link.send({"ok": False, "event": Event.TAG_DATA_ERROR, "uid": "",
                           "error": "timed out waiting for a tag"})

            pairing_active = state["pairing_until"] > now
            led.value = pairing_active and (int(now * 4) % 2 == 0)

            uid_bytes = reader.read_uid()
            if uid_bytes is not None:
                uid_str = MFRC522.uid_to_str(uid_bytes)
                last_seen = now

                if state["pending_password"] is not None:
                    # Disarmed before running, like pending_data_op below.
                    pending = state["pending_password"]
                    state["pending_password"] = None
                    result = store_password_with_tag_key(cfg, reader, uid_bytes, pending, state)
                    link.send(result)
                    link.send({"event": Event.CONFIG, "config": public_config(cfg)})
                    blink(led, 3, 0.05, 0.05)

                elif (state["pending_data_op"] is not None
                      and now < state.get("pending_data_op_until", 0)):
                    # Disarm first: if the op raises, the main loop's error
                    # handler must not find it still armed and rerun it on
                    # every poll (which flooded the log with the same error).
                    op = state["pending_data_op"]
                    state["pending_data_op"] = None
                    result = execute_data_op(reader, uid_bytes, op)
                    link.send(result)
                    blink(led, 1, 0.15, 0.0)
                    # Claim this tap so the tag's own actions don't also fire on
                    # the next pass: presenting a tag to read or write its memory
                    # is a maintenance operation, not a request to unlock anything.
                    present_uid = uid_str

                elif present_uid != uid_str:
                    present_uid = uid_str

                    if pairing_active:
                        if state.get("pairing_set_id"):
                            config.attach_uid(cfg, state["pairing_set_id"], uid_str)
                        else:
                            config.add_tag(
                                cfg,
                                uid_str,
                                state["pairing_label"],
                                on_tap=state["pairing_on_tap"],
                                on_remove=state["pairing_on_remove"],
                                tap_mode=state.get("pairing_tap_mode"),
                            )
                        state["pairing_set_id"] = None
                        if state["pairing_write_label"]:
                            write_result = execute_data_op(
                                reader,
                                uid_bytes,
                                {
                                    "type": "write",
                                    "block": DEFAULT_DATA_BLOCK,
                                    "key": DEFAULT_KEY,
                                    "data": pad_to_block(state["pairing_label"].encode("utf-8")),
                                },
                            )
                            link.send(write_result)
                        state["pairing_until"] = 0
                        state["pairing_write_label"] = False
                        link.send({"event": Event.TAG_PAIRED, "uid": uid_str})
                        # Ship the updated config with it, so the configurator's
                        # tag list shows the new tag without needing a reload.
                        link.send({"event": Event.CONFIG, "config": public_config(cfg)})
                        blink(led, 3, 0.05, 0.05)
                    else:
                        tag = config.find_tag(cfg, uid_str)
                        if tag is not None:
                            on_tap = tag.get("on_tap") or []
                            if on_tap:
                                # SEQUENCE: every action runs, in order, on each
                                # tap ("unlock, then open my dashboard").
                                # CYCLE: one action per tap, advancing ("tap to
                                # switch between layouts"). Default to sequence,
                                # which is what a list of steps usually means.
                                if tag.get("tap_mode", TapMode.SEQUENCE) == TapMode.CYCLE:
                                    idx = state["cycle_index"].get(uid_str, 0) % len(on_tap)
                                    chosen = [on_tap[idx]]
                                    state["cycle_index"][uid_str] = idx + 1
                                else:
                                    idx = 0
                                    chosen = on_tap

                                for offset, act in enumerate(chosen):
                                    link.send(
                                        {
                                            "event": Event.TAP,
                                            "uid": uid_str,
                                            "action_index": idx + offset,
                                            "action_count": len(on_tap),
                                            "action": act,
                                        }
                                    )
                                    blocked = run_action(keyboard, state["layout"], cfg, act, reader, uid_bytes, state)
                                    if blocked:
                                        link.send(blocked)
                        else:
                            link.send({"event": Event.UNKNOWN_TAG, "uid": uid_str})
            else:
                if present_uid is not None:
                    elapsed_ms = (now - last_seen) * 1000
                    if elapsed_ms >= cfg["lock_debounce_ms"]:
                        tag = config.find_tag(cfg, present_uid)
                        if tag is not None and tag.get("on_remove"):
                            link.send({"event": Event.REMOVE, "uid": present_uid})
                            # The tag has left the field by definition, so there is no UID to
                            # pass: anything needing the tag key cannot run here.
                            run_actions(keyboard, state["layout"], cfg, tag["on_remove"], reader, None, state)
                        present_uid = None

        except Exception as e:  # pylint: disable=broad-except
            # One bad poll (a SPI glitch, a malformed config entry, a USB
            # write that fails mid-replug) must not kill the firmware: an
            # uncaught exception stops code.py until someone power-cycles
            # the board. Report it, and re-check the reader on the next pass
            # (re-initialising it if unhealthy) in case the chip failed.
            print("main loop error:", repr(e))
            try:
                link.send({"ok": False, "error": "internal: " + repr(e)})
            except Exception:  # pylint: disable=broad-except
                pass
            next_reader_check = 0.0

        time.sleep(POLL_INTERVAL_S)


main()
