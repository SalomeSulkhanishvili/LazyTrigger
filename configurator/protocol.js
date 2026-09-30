// Mirror of firmware/protocol.py. Keep the two in sync, tools/check_protocol.py
// compares them and fails if they drift.
//
// Object.freeze so a typo like ActionType.UNLCOK throws at the point of the
// mistake rather than silently producing `undefined` and a broken action.

const ActionType = Object.freeze({
  UNLOCK: "unlock",
  LOCK: "lock",
  KEYS: "keys",
  TEXT: "text",
  SECRET: "secret",
  WAIT: "wait",
  NOTIFY: "notify",
  OPEN_URL: "open_url",
});

// Actions that emit keystrokes into whatever window has focus, so testing
// them gets a countdown to let you focus the right window first.
const TYPES_KEYSTROKES = Object.freeze([
  ActionType.UNLOCK,
  ActionType.LOCK,
  ActionType.KEYS,
  ActionType.TEXT,
  ActionType.SECRET,
]);

const Command = Object.freeze({
  PING: "ping",
  GET_CONFIG: "get_config",
  SET_PASSWORD: "set_password",
  RESET_PASSWORD: "reset_password",
  SET_SECRET: "set_secret",
  REMOVE_SECRET: "remove_secret",
  SET_LOCK_DEBOUNCE_MS: "set_lock_debounce_ms",
  DETACH_UID: "detach_uid",
  REMOVE_SET: "remove_set",
  RENAME_SET: "rename_set",
  SET_TAG_ACTIONS: "set_tag_actions",
  NEW_SET: "new_set",
  START_PAIRING: "start_pairing",
  CANCEL_PAIRING: "cancel_pairing",
  READ_TAG_DATA: "read_tag_data",
  READ_TAG_ALL: "read_tag_all",
  WRITE_TAG_DATA: "write_tag_data",
  TEST_ACTION: "test_action",
  SET_LOCK_STATE: "set_lock_state",
  SET_THEME: "set_theme",
  SET_DEBUG_LOG: "set_debug_log",
  FACTORY_RESET: "factory_reset",
});

const Event = Object.freeze({
  TAP: "tap",
  ACTION_FIRED: "action_fired",
  REMOVE: "remove",
  UNKNOWN_TAG: "unknown_tag",
  TAG_PAIRED: "tag_paired",
  TAG_DATA_READ: "tag_data_read",
  TAG_DUMP: "tag_dump",
  TAG_DATA_WRITTEN: "tag_data_written",
  TAG_DATA_ERROR: "tag_data_error",
  PASSWORD_SET: "password_set",
  PASSWORD_ERROR: "password_error",
  UNLOCK_BLOCKED: "unlock_blocked",
  SECRET_SET: "secret_set",
  CONFIG: "config",
});

// Actions that only work while their tag is on the reader, because their
// value is encrypted with that tag's key. They cannot live in the library.
const NEEDS_TAG_KEY = Object.freeze([ActionType.UNLOCK, ActionType.SECRET]);

const TapMode = Object.freeze({
  SEQUENCE: "sequence",
  CYCLE: "cycle",
});

const NotifyMode = Object.freeze({
  SINCE: "since",
  UNTIL: "until",
});

// Which chord locks the screen, per OS. The device stores whichever keys
// you pick here, it has no built-in idea of "lock".
//
// "universal" sends the macOS combo first and the Windows/Linux one second.
// Whichever machine it's plugged into, one of them locks the screen and the
// other arrives at an already-locked screen, where it does nothing. Mac
// goes first because Ctrl+Win+Q isn't bound on Windows/Linux, whereas the
// reverse order would fire Cmd+L into a still-unlocked Mac app.
const LOCK_COMBOS = {
  windows: [["GUI", "L"]],
  mac: [["CONTROL", "GUI", "Q"]],
  linux: [["GUI", "L"]],
  universal: [["CONTROL", "GUI", "Q"], ["GUI", "L"]],
};

// How to wake each OS's lock screen before typing the password.
//
// ESCAPE is deliberately absent for macOS: at the login window it cancels
// the password prompt and can return to user selection, so an unlock that
// used it appeared to do nothing. SHIFT wakes the display on all three
// without typing or cancelling anything, so it is also the universal default.
// The delay is how long to wait for the password field to actually appear
// waking from real sleep is slower than dismissing a screensaver.
const UNLOCK_PRESETS = {
  universal: { wake_keys: ["SHIFT"], delay: 0.8 },
  mac:       { wake_keys: ["SHIFT"], delay: 0.8 },
  windows:   { wake_keys: ["SHIFT"], delay: 0.6 },
  linux:     { wake_keys: ["SHIFT"], delay: 0.6 },
};

// Display metadata for the action editor: label shown in the type dropdown,
// and the factory for a fresh action of that type. Keeping this beside the
// type definitions means adding an action type is one edit, not five.
const ACTION_META = Object.freeze({
  [ActionType.UNLOCK]: {
    label: "Unlock (type password)",
    make: () => ({ type: ActionType.UNLOCK, os: "universal", ...UNLOCK_PRESETS.universal }),
  },
  [ActionType.LOCK]: {
    label: "Lock screen",
    make: () => ({ type: ActionType.LOCK, os: "universal", chords: LOCK_COMBOS.universal.map((c) => c.slice()) }),
  },
  [ActionType.KEYS]: {
    label: "Keyboard shortcut",
    make: () => ({ type: ActionType.KEYS, keys: [] }),
  },
  [ActionType.TEXT]: {
    label: "Type text",
    make: () => ({ type: ActionType.TEXT, text: "", enter: false }),
  },
  [ActionType.SECRET]: {
    label: "Type a saved secret",
    make: () => ({ type: ActionType.SECRET, id: "", enter: false }),
  },
  [ActionType.NOTIFY]: {
    label: "Reminder / notification",
    make: () => ({ type: ActionType.NOTIFY, mode: NotifyMode.SINCE, fact: "", date: "", title: "Reminder" }),
  },
  [ActionType.OPEN_URL]: {
    label: "Open a link in the browser",
    make: () => ({ type: ActionType.OPEN_URL, url: "" }),
  },
  [ActionType.WAIT]: {
    label: "Wait",
    make: () => ({ type: ActionType.WAIT, seconds: 0.3 }),
  },
});
