"""
Loading, saving, and defaulting the runtime configuration (config.json).

The firmware has no built-in notion of "Windows" / "Mac" / "Linux" or of any
particular shortcut or layout, that logic lives entirely in the browser
configurator (../configurator/index.html), which sends this device plain
keycode/text actions to store. This file just knows the shape of that data.
"""

import json
import time

CONFIG_PATH = "/config.json"

DEFAULT_CONFIG = {
    # The unlock password, AES-128-CBC encrypted with a key that lives on the
    # RFID tag rather than on this device (see secretbox.py). Stored as hex.
    #
    # It can't be hashed, the device has to reproduce the exact characters
    # to type them at a lock screen, and hashing is one-way. Encrypting with
    # an off-device key is the next best thing: a flash dump yields only
    # ciphertext, and the tag that holds the key has to be on the reader
    # anyway for an unlock to happen.
    #
    # It is also WRITE-ONLY over the serial protocol, get_config strips it
    # and reports only `has_password`, so nothing talking to this device can
    # read it back. Changing it requires proving knowledge of the current one
    # (see password_hash).
    "password_enc": "",
    # The AES IV for password_enc, hex. Not secret; just has to be unique.
    "password_iv": "",
    # Additional named secrets, each encrypted exactly like the unlock
    # password (same tag-held key, its own IV) and each typed by a
    # {"type": "secret", "id": ...} action:
    #   [{"id": "s1", "label": "Work VPN", "enc": "...", "iv": "..."}]
    # Like the password, the ciphertext and IV are stripped by get_config
    # only ids and labels are ever readable off the device.
    "secrets": [],
    # Check value for the password, used purely to verify that whoever is
    # changing it knows the current one. Computed by the configurator, never
    # here: PBKDF2-SHA256 over the password and `password_salt`, with the
    # round count named in `password_kdf` (e.g. "pbkdf2-sha256-600000"). The
    # salt and slowness make guessing the password from a flash dump
    # expensive. An empty `password_kdf` marks the original unsalted SHA-256,
    # still accepted so a password set before salts existed can be changed.
    "password_hash": "",
    # Public (sent by get_config): the configurator needs both to recompute
    # the check value from a typed password.
    "password_salt": "",
    "password_kdf": "",
    # How long (ms) a tag must be absent before we run its on_remove
    # actions. Avoids flicker/false-triggers from brief read gaps.
    "lock_debounce_ms": 1500,
    # Configurator appearance: "auto" follows the computer's setting,
    # "light" and "dark" pin it. Kept here so the choice follows the
    # device between computers and browsers.
    "theme": "auto",
    # Configurator log: when true it also shows routine acknowledgements
    # ({"ok": true}), which arrive every few seconds while the bridge runs.
    # Stored here, like the theme, so it follows the device.
    "debug_log": False,
    # Action sets. Each has a list of tag UIDs that trigger it, which may be
    # empty (an unassigned set, configured but not yet attached to any tag):
    #   [{"id": "s1", "uids": ["04A1B2C3", ...], "label": "...",
    #     "on_tap": [...], "on_remove": [...], "tap_mode": "sequence"}]
    # Several tags can share one set, and a tag can be detached without
    # losing the actions.
    #
    # `on_tap` is a list of actions, run according to the tag's `tap_mode`:
    #   "sequence" (default), every action runs, in order, on each tap.
    #                           e.g, unlock, then open a dashboard.
    #   "cycle"               one action per tap, advancing and wrapping,
    #                           e.g, tap to step through window layouts.
    # `on_remove` always runs in full whenever the tag is taken away,
    # typically to lock the screen.
    #
    # Action shapes:
    #
    #   {"type": "unlock", "wake_keys": ["SHIFT"], "delay": 0.8}
    #       Wakes the screen, waits, types the global password, presses Enter.
    #       Refuses to run when the host reports the screen is already
    #       unlocked, so the password cannot land in the wrong window.
    #
    #   {"type": "lock", "chords": [["CONTROL","GUI","Q"], ["GUI","L"]]}
    #       A labelled key chord, or several in sequence. Which combo locks a
    #       screen is OS specific, so the keys come from config rather than
    #       being hardcoded. The default "universal" form sends the mac combo
    #       then the Windows/Linux one, so it works on any host without being
    #       told which: the wrong combo lands on an already locked screen and
    #       does nothing.
    #
    #   {"type": "keys", "keys": ["CONTROL", "ALT", "L"]}
    #       Press a chord of adafruit_hid Keycode names together.
    #
    #   {"type": "text", "text": "...", "enter": true}
    #       Type literal text, optionally followed by Enter.
    #
    #   {"type": "secret", "id": "s1", "only_in_app": "Safari"}
    #       Type a saved secret. With only_in_app set, it types only when
    #       that app has focus.
    #
    #   {"type": "wait", "seconds": 0.3}
    #       Pause. Only meaningful in a sequence, since a cycle runs one
    #       action per tap.
    #
    #   {"type": "notify", "mode": "since"|"until", "fact": "...",
    #    "date": "2022-09-01", "title": "..."}
    #       No keystrokes. Reported over serial so the bridge or the
    #       configurator page can render a real popup, since only the host
    #       has a reliable clock and a way to draw one.
    #
    #   {"type": "open_url", "url": "https://..."}
    #       No keystrokes. The bridge opens it in the default browser.
    "tags": [],
}


def load():
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}

    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    merged["tags"] = cfg.get("tags", [])
    merged["secrets"] = cfg.get("secrets", [])

    sets = [_normalize_set(dict(e), i) for i, e in enumerate(cfg.get("tags", []))]
    # Fold any sets from the old separate "library" list in as unassigned.
    offset = len(sets)
    for i, entry in enumerate(cfg.get("library", [])):
        entry = dict(entry)
        entry["uids"] = []
        sets.append(_normalize_set(entry, offset + i))
    merged["tags"] = sets
    merged.pop("library", None)
    return merged


def _normalize_set(entry, index):
    """Bring older records up to the current shape.

    Early versions stored a single "uid" per set and kept unassigned sets in
    a separate "library" list. Both collapse into a uids list, where empty
    simply means nothing is attached yet."""
    if "uids" not in entry:
        entry["uids"] = [entry["uid"]] if entry.get("uid") else []
    entry.pop("uid", None)
    if not entry.get("id"):
        entry["id"] = "s{}".format(index)
    entry.setdefault("on_tap", [])
    entry.setdefault("on_remove", [])
    entry.setdefault("tap_mode", "sequence")
    entry.setdefault("label", "")
    return entry


def find_set(cfg, set_id):
    for entry in cfg.get("tags", []):
        if entry.get("id") == set_id:
            return entry
    return None


def attach_uid(cfg, set_id, uid_str):
    """Add a tag to a set. A uid belongs to at most one set, so it is
    detached from any other first."""
    target = find_set(cfg, set_id)
    if target is None:
        return False
    detach_uid(cfg, uid_str, save_after=False)
    if uid_str not in target["uids"]:
        target["uids"].append(uid_str)
    save(cfg)
    return True


def detach_uid(cfg, uid_str, save_after=True):
    """Remove one tag from whichever set holds it. The set itself stays,
    keeping its actions, and simply becomes unassigned if that was its last
    tag."""
    found = False
    for entry in cfg.get("tags", []):
        if uid_str in entry.get("uids", []):
            entry["uids"] = [u for u in entry["uids"] if u != uid_str]
            found = True
    if found and save_after:
        save(cfg)
    return found


def remove_set(cfg, set_id):
    cfg["tags"] = [e for e in cfg.get("tags", []) if e.get("id") != set_id]
    save(cfg)


def find_secret(cfg, secret_id):
    for secret in cfg.get("secrets", []):
        if secret.get("id") == secret_id:
            return secret
    return None


def remove_secret(cfg, secret_id):
    cfg["secrets"] = [s for s in cfg.get("secrets", []) if s.get("id") != secret_id]
    save(cfg)


def save(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f)


def find_tag(cfg, uid_str):
    """The set triggered by this tag, or None if the tag isn't attached."""
    for entry in cfg["tags"]:
        if uid_str in entry.get("uids", []):
            return entry
    return None


def new_set(cfg, label="", on_tap=None, on_remove=None, tap_mode=None, uids=None):
    """Create a set, with or without tags attached."""
    entry = {
        "id": "s{}".format(int(time.monotonic() * 1000)),
        "uids": list(uids or []),
        "label": label,
        "on_tap": on_tap if on_tap is not None else [{"type": "unlock"}],
        "on_remove": on_remove if on_remove is not None else [],
        "tap_mode": tap_mode or "sequence",
    }
    cfg["tags"].append(entry)
    save(cfg)
    return entry


def add_tag(cfg, uid_str, label="", on_tap=None, on_remove=None, tap_mode=None):
    """Attach a freshly tapped tag: to the set that already claims it if
    there is one, otherwise to a new set."""
    existing = find_tag(cfg, uid_str)
    if existing:
        existing["label"] = label or existing.get("label", "")
        save(cfg)
        return existing
    return new_set(cfg, label, on_tap, on_remove, tap_mode, uids=[uid_str])


def set_tag_actions(cfg, set_id, on_tap, on_remove, tap_mode=None):
    entry = find_set(cfg, set_id)
    if entry is None:
        return False
    entry["on_tap"] = on_tap
    entry["on_remove"] = on_remove
    if tap_mode:
        entry["tap_mode"] = tap_mode
    save(cfg)
    return True


def rename_set(cfg, set_id, label):
    entry = find_set(cfg, set_id)
    if entry is None:
        return False
    entry["label"] = label
    save(cfg)
    return True
