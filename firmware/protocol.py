"""
Canonical definitions for the wire protocol and action model.

This is the single source of truth for every string that crosses the serial
link: action types, command names, event names, and config keys. The
configurator (configurator/protocol.js) mirrors these, and
tools/check_protocol.py fails if the two drift apart.

CircuitPython has no `enum` module, so these are plain classes of constants
-- the usual idiom on this platform. They cost nothing at runtime and give
one place to look when adding a new action or command.
"""


class ActionType:
    """What a single action does when its tag is tapped."""

    UNLOCK = "unlock"
    LOCK = "lock"
    KEYS = "keys"
    TEXT = "text"
    SECRET = "secret"
    WAIT = "wait"
    NOTIFY = "notify"
    OPEN_URL = "open_url"

    ALL = (UNLOCK, LOCK, KEYS, TEXT, SECRET, WAIT, NOTIFY, OPEN_URL)

    #: Actions handled by the host (bridge), not by emitting keystrokes.
    #: The device only reports that they fired.
    HOST_SIDE = (NOTIFY, OPEN_URL)

    #: Actions that emit keystrokes into whatever window has focus. The
    #: configurator delays testing these so you can focus the right window
    #: first; the others are safe to fire immediately.
    TYPES_KEYSTROKES = (UNLOCK, LOCK, KEYS, TEXT, SECRET)

    #: Actions whose value is encrypted with the key held on the tag, and so
    #: can only run while that tag is on the reader.
    NEEDS_TAG_KEY = (UNLOCK, SECRET)


class Command:
    """Requests the host can send over the data serial port."""

    PING = "ping"
    GET_CONFIG = "get_config"
    SET_PASSWORD = "set_password"
    RESET_PASSWORD = "reset_password"
    SET_SECRET = "set_secret"
    REMOVE_SECRET = "remove_secret"
    SET_LOCK_DEBOUNCE_MS = "set_lock_debounce_ms"
    DETACH_UID = "detach_uid"
    REMOVE_SET = "remove_set"
    RENAME_SET = "rename_set"
    SET_TAG_ACTIONS = "set_tag_actions"
    NEW_SET = "new_set"
    START_PAIRING = "start_pairing"
    CANCEL_PAIRING = "cancel_pairing"
    READ_TAG_DATA = "read_tag_data"
    READ_TAG_ALL = "read_tag_all"
    WRITE_TAG_ALL = "write_tag_all"
    WRITE_TAG_DATA = "write_tag_data"
    TEST_ACTION = "test_action"
    SET_LOCK_STATE = "set_lock_state"
    SET_THEME = "set_theme"
    SET_DEBUG_LOG = "set_debug_log"
    SET_KEYBOARD_LAYOUT = "set_keyboard_layout"
    FACTORY_RESET = "factory_reset"

    ALL = (
        PING, GET_CONFIG, SET_PASSWORD, RESET_PASSWORD, SET_SECRET, REMOVE_SECRET,
        SET_LOCK_DEBOUNCE_MS, DETACH_UID, REMOVE_SET, RENAME_SET,
        SET_TAG_ACTIONS, NEW_SET,
        START_PAIRING, CANCEL_PAIRING, READ_TAG_DATA, READ_TAG_ALL, WRITE_TAG_ALL,
        WRITE_TAG_DATA, TEST_ACTION, SET_LOCK_STATE, SET_THEME, SET_DEBUG_LOG,
        SET_KEYBOARD_LAYOUT,
        FACTORY_RESET,
    )


class Event:
    """Messages the device pushes without being asked."""

    TAP = "tap"
    ACTION_FIRED = "action_fired"
    REMOVE = "remove"
    UNKNOWN_TAG = "unknown_tag"
    TAG_PAIRED = "tag_paired"
    TAG_DATA_READ = "tag_data_read"
    TAG_DUMP = "tag_dump"
    TAG_DATA_WRITTEN = "tag_data_written"
    TAG_COPIED = "tag_copied"
    TAG_DATA_ERROR = "tag_data_error"
    PASSWORD_SET = "password_set"
    PASSWORD_ERROR = "password_error"
    UNLOCK_BLOCKED = "unlock_blocked"
    LOCK_STATE_NEEDED = "lock_state_needed"
    SECRET_SET = "secret_set"
    CONFIG = "config"

    ALL = (
        TAP, ACTION_FIRED, REMOVE, UNKNOWN_TAG, TAG_PAIRED, TAG_DATA_READ, TAG_DUMP,
        TAG_DATA_WRITTEN, TAG_COPIED, TAG_DATA_ERROR, PASSWORD_SET, PASSWORD_ERROR,
        SECRET_SET, CONFIG, UNLOCK_BLOCKED, LOCK_STATE_NEEDED,
    )


class TapMode:
    """How a tag's on_tap list is interpreted.

    SEQUENCE runs every action in order on each tap ("unlock, then open my
    dashboard"). CYCLE runs one action per tap and advances, wrapping around
    ("tap to switch between layouts 1, 2, 3"). Both are useful; which one a
    tag uses is a per-tag setting rather than a global rule.
    """

    SEQUENCE = "sequence"
    CYCLE = "cycle"

    ALL = (SEQUENCE, CYCLE)


class NotifyMode:
    """How a reminder action computes its message."""

    SINCE = "since"
    UNTIL = "until"

    ALL = (SINCE, UNTIL)


class ConfigKey:
    """Keys in config.json."""

    PASSWORD_ENC = "password_enc"
    PASSWORD_IV = "password_iv"
    PASSWORD_HASH = "password_hash"
    #: Public inputs to the password check value (see config.py).
    PASSWORD_SALT = "password_salt"
    PASSWORD_KDF = "password_kdf"
    SECRETS = "secrets"
    TAGS = "tags"
    LOCK_DEBOUNCE_MS = "lock_debounce_ms"

    #: Never sent back over the serial link by get_config.
    PRIVATE = (PASSWORD_ENC, PASSWORD_IV, PASSWORD_HASH, SECRETS)
