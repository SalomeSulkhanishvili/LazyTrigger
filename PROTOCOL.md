# Configuration protocol

The device exposes a second USB CDC serial port (separate from the REPL
console) that speaks line-delimited JSON, request/response style. The
browser configurator in [configurator/](configurator/index.html) talks to
this port over WebSerial; you can also drive it by hand with any serial
terminal (screen, PuTTY, `pyserial`'s `miniterm`, etc.), one JSON object per
line.

On most systems this shows up as a *second* serial device next to the REPL
console, e.g. `/dev/tty.usbmodemXXXX1` (console) and `...XXXX3` (data) on
macOS, or `COM5`/`COM6` on Windows.

The firmware itself has no concept of "Windows/Mac/Linux", "lock", or
"unlock" baked in, it only plays back generic actions. All OS- and
workflow-specific decisions (which key combo locks your screen, which
shortcut switches to which window layout) are made by the web configurator
and sent down as plain data. This means new behaviors (a new shortcut, a new
per-app layout) never require reflashing the device.

All action types, command names and event names are defined once in
[`firmware/protocol.py`](firmware/protocol.py) and mirrored in
[`configurator/protocol.js`](configurator/protocol.js); `tools/check_protocol.py`
fails if the two drift. Use those constants rather than literal strings.

## Action objects

Every tag has an `on_tap` list (cycled: each new tap advances to the next
action and wraps around) and an `on_remove` list (run in full whenever the
tag is taken away). Each action is one of:

```jsonc
{"type": "unlock"}
// Wakes the screen (Escape), types the device's stored password, presses Enter.

{"type": "keys", "keys": ["CONTROL", "ALT", "L"]}
// Presses the given keys together as a chord, then releases them.
// Names must match a CircuitPython adafruit_hid.keycode.Keycode attribute,
// e.g. "GUI", "COMMAND", "CONTROL", "ALT", "SHIFT", "A".."Z", "ONE".."ZERO",
// "F1".."F24", "ESCAPE", "ENTER", "TAB", "LEFT_ARROW", etc. Unrecognized
// names are silently skipped rather than erroring.

{"type": "text", "text": "hello", "enter": true}
// Types literal text (optionally followed by Enter).

{"type": "lock", "chords": [["CONTROL","GUI","Q"], ["GUI","L"]]}
// Presses each chord in turn, 0.4s apart. The default "universal" form sends
// the macOS combo then the Windows/Linux one, so it locks any host without
// knowing which it's plugged into, the wrong combo lands on an
// already-locked screen and does nothing.

{"type": "secret", "id": "s1", "enter": false}
// Types a saved named secret (see set_secret). Encrypted with the key held on
// the tag, so it only works while that tag is on the reader, and only ever
// exists decrypted for the moment it is being typed.

{"type": "wait", "seconds": 0.3}
// Pauses. Only meaningful in `on_remove`, which runs its whole list in order.
// In `on_tap` it does nothing useful: that list is a CYCLE, one action per
// tap, so a wait would consume an entire tap doing nothing visible. The
// configurator only offers it for `on_remove` for this reason.

{"type": "notify", "mode": "since", "fact": "you started your Bachelor's degree", "date": "2022-09-01", "title": "Reminder"}
// No keystrokes. The device only reports that this action fired (see the
// "tap"/"action_fired" events below, which include the full action)
// it has no reliable clock or way to draw a popup, so whatever's listening
// on the data serial port (bridge/serial_bridge.py, or the configurator
// page itself) computes the message and shows a real OS notification.
// `fact` is free text you write yourself (not a template); `mode` is
// "since" (count days since a past `date`, e.g. "It's been 900 days since
// you started your Bachelor's degree.") or "until" (countdown to a future
// `date`, e.g. "12 days until the deadline."). `title` is optional,
// defaults to "Reminder". Requires bridge/serial_bridge.py running in the
// background to actually show anything, see README.md.
```

**`on_tap` and `on_remove` run differently, which matters when composing them:**

* `on_tap` is a **cycle**: one action per tap, advancing each time and
  wrapping around. Three actions means three taps to get through them.
* `on_remove` is a **sequence**: the entire list runs, in order, on every
  removal.

This is why `wait` belongs only in `on_remove`.

A tag's `on_tap` list can hold several actions to build the "cycle" pattern
(dev workspace tag: tap 1 -> layout shortcut A, tap 2 -> layout shortcut B,
tap 3 -> layout shortcut C, tap 4 -> back to A). `on_remove` is typically a
single `keys` action for whatever your OS/window manager binds to "lock",
but can be any sequence.

## Requests

| Command | Fields | Effect |
|---|---|---|
| `ping` |. | Replies `{"ok": true, "pong": true}` |
| `get_config` |. | Returns the config **with all secret material stripped**: no password ciphertext, IV, hash, or secret values; only `has_password` and secret ids/labels |
| `set_password` | `value`, `old_hash` (check value of the current password, required once one is set), `new_hash`, `new_salt`, `new_kdf` | Arms a password change. Completes on the next tag tap, which supplies the encryption key. The password is **write-only**. `get_config` never returns it. Check values are computed by the client: PBKDF2-SHA256 over the password and a random 16-byte salt (hex), with the round count named in `new_kdf` (`pbkdf2-sha256-600000`). `old_hash` uses the stored `password_salt` / `password_kdf` from `get_config`; an empty `password_kdf` means the original unsalted SHA-256 |
| `reset_password` |. | Forgets the unlock password (ciphertext, IV, check value, salt) and cancels an armed password change. Tags, actions and saved secrets are kept. Needs no proof of the old password: it can't be read either way, and setting a new one still needs a tag tap. Replies with the updated config |
| `set_secret` | `label`, `value`, `id` (optional, to replace) | Same as above for a named secret |
| `remove_secret` | `id` | Deletes a saved secret |
| `set_lock_debounce_ms` | `value`: int | ms a tag must be absent before `on_remove` runs |
| `detach_uid` | `uid`: hex string | Removes one tag from its set. The set keeps its actions and becomes unassigned if that was its last tag |
| `remove_set` | `id`: set id | Deletes a set and every tag assignment in it |
| `rename_set` | `id`, `label` | Renames a set |
| `new_set` | `label`, `on_tap`, `on_remove`, `tap_mode` (all optional) | Creates a set with no tag attached yet; replies with its `id`. Attach a tag later with `start_pairing` + `set_id` |
| `set_tag_actions` | `id`: set id, `on_tap`: [action], `on_remove`: [action], `tap_mode` | Replaces a set's action lists and resets its cycle position |
| `start_pairing` | `label` (optional), `on_tap`/`on_remove` (optional, default `on_tap=[{"type":"unlock"}]`), `tap_mode` (optional), `set_id` (optional), `write_label` (optional bool) | Arms pairing mode for 15s; the next tag tapped is whitelisted with the given actions, or attached to `set_id` if given |
| `cancel_pairing` |. | Cancels pairing mode early |
| `read_tag_data` | `block` (default 4), `key_hex` (default `FFFFFFFFFFFF`) | Arms a one-shot read; the next tag tapped is read and reported |
| `read_tag_all` | `key_hex` (default `FFFFFFFFFFFF`) | Arms a one-shot read of every block (MIFARE Classic 1K: 64 blocks, one login per sector); the next tag tapped is read and reported as `tag_dump`. The key block (5) is never read |
| `write_tag_data` | `block`, `text` or `data_hex`, `key_hex`, `confirm_key_overwrite` | Arms a one-shot write of up to 16 bytes to the next tag tapped. Writing to block 5 is refused (`needs_confirmation: "key_overwrite"`) unless `confirm_key_overwrite` is set, since that block holds the tag's encryption key |
| `test_action` | `action` | Fires one action immediately (no tag needed) so the configurator can preview it. `notify` actions produce no keystrokes; everything else (keys/text/unlock/wait) really runs, into whatever window has focus |
| `set_lock_state` | `locked`: bool, `app` (optional) | The host reports whether the screen is locked; the device cannot see this itself. Reports older than 10s are treated as unknown |
| `set_theme` | `value`: `auto`\|`light`\|`dark` | Stores the configurator's theme preference on the device |
| `set_debug_log` | `value`: `true`\|`false` | Debug mode. Off (the default), the device doesn't send bare `{"ok": true}` acknowledgements at all; replies that carry data or an error are always sent. Stored on the device like the theme |
| `set_keyboard_layout` | `value`: `us`\|`de_mac`\|`de_win`\|`ka` | The keyboard layout the computer uses (US, German on a Mac, German on Windows/Linux, Georgian QWERTY). Text, secrets and the password are typed through it, and shortcut letters follow it (Z and Y trade places in German). Stored on the device like the theme |
| `factory_reset` |. | Clears all tags and resets config to defaults |

## Async events (pushed without a matching request)

| Event | Fields | Meaning |
|---|---|---|
| `tap` | `uid`, `action_index`, `action_count`, `action` | A whitelisted tag was tapped; `on_tap[action_index]` (included in full as `action`) was run |
| `action_fired` | `action` | A `test_action` request fired this action |
| `remove` | `uid` | A whitelisted tag with a non-empty `on_remove` was taken away; those actions were run |
| `unknown_tag` | `uid` | A tag was tapped that isn't whitelisted |
| `tag_paired` | `uid` | Pairing mode successfully whitelisted a new tag |
| `tag_data_read` | `uid`, `block`, `data_hex`, `text` | Result of a `read_tag_data` request |
| `tag_dump` | `uid`, `blocks`, `unreadable_sectors` | Result of `read_tag_all`. Each of `blocks` is `{block, kind}` (`manufacturer`, `trailer`, `key`, `label` or `data`) plus `hex` and `text` (non-printable bytes as `.`), or `error: "unreadable"` for a sector that couldn't be read, or `hidden: true` for the key block |
| `tag_data_written` | `uid`, `block` | Result of a successful `write_tag_data` request |
| `tag_data_error` | `uid`, `error` | A read/write op failed (bad auth, wrong block, etc.) |
| `password_set` | `uid` | A `set_password` completed; the key was written to that tag |
| `password_error` | `error` | A `set_password`/`set_secret` could not use that tag |
| `secret_set` | `id`, `uid` | A `set_secret` completed |
| `unlock_blocked` | `reason` | An `unlock` action was skipped because its guard didn't hold: the screen is already unlocked, the lock state is unknown, the focused app isn't the expected one, no password is set, or the tapped tag can't decrypt it. Nothing is typed, not even Enter, so a wrong tag never causes a failed login attempt |
| `config` | `config` | The config changed (pairing, edits, factory reset); sent so the configurator can refresh without polling |

Both `tap` and `action_fired` carry the fired action's full definition. Anything
listening on the data port (bridge/serial_bridge.py, or the configurator page)
watches for `action.type == "notify"` on either event to render a reminder;
see the `notify` action above.

## Notes on `read_tag_data` / `write_tag_data`

These use MIFARE Classic sector authentication with Key A (default factory
key `FFFFFFFFFFFF` unless the tag's keys were changed). Block 4 (sector 1's
first data block) is used by default for storing an optional free-text label
directly on the tag; sector trailer blocks (3, 7, 11, ...) are rejected
since they hold the sector's keys and access bits, not data.

## Password and secret storage

The unlock password and every saved secret are AES-128-CBC encrypted with a
random key that is written to **block 5 of the RFID tag** and never stored on
the Pico (see `firmware/secretbox.py`). Consequences worth knowing:

* A dump of the Pico's flash yields ciphertext and an IV, but no key.
* A cloned tag yields a key, but no ciphertext.
* Decryption therefore needs both, which costs nothing in practice since the
  tag must be on the reader for the action to fire anyway.
* Nothing can read these values back out over the serial link. `get_config`
  strips them. They are write-only by construction, not by UI convention.
* **They cannot be hashed.** The device must reproduce the exact characters to
  type them, and hashing is one-way. `password_hash` exists only to verify
  that whoever is changing the password knows the current one. It is a
  salted PBKDF2 (600,000 rounds), so guessing the password from a flash dump
  costs 600,000 hash computations per guess rather than one.
* Losing or reformatting the tag means re-entering the password. There is
  deliberately no recovery path.
