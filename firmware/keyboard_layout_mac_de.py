"""
German keyboard layout for macOS.

Built on the Windows German layout (keyboard_layout_win_de.py, from
Neradoc's Circuitpython_Keyboard_Layouts). Letters, digits, umlauts and the
Shift symbols sit on the same keys on a Mac. What differs are the symbols
typed with the right Alt key on Windows, which a Mac puts elsewhere behind
Option (the device presses the right Alt key, which a Mac reads as Option).
"""

from keyboard_layout_win_de import KeyboardLayout as _WindowsGerman

_SHIFT = 0x80


def _patched(table, changes):
    table = bytearray(table)
    for char, keycode in changes.items():
        table[ord(char)] = keycode
    return bytes(table)


class KeyboardLayout(_WindowsGerman):
    ASCII_TO_KEYCODE = _patched(_WindowsGerman.ASCII_TO_KEYCODE, {
        "@": 0x0F,            # Option+L
        "[": 0x22,            # Option+5
        "]": 0x23,            # Option+6
        "|": 0x24,            # Option+7
        "\\": 0x24 | _SHIFT,  # Option+Shift+7
        "{": 0x25,            # Option+8
        "}": 0x26,            # Option+9
        "~": 0x00,            # a dead key on the Mac, see COMBINED_KEYS
    })
    NEED_ALTGR = "@[\\]{|}µ€"
    # ² and ³ have no key on the Mac German layout.
    HIGHER_ASCII = {code: key for code, key in _WindowsGerman.HIGHER_ASCII.items()
                    if code not in (0xB2, 0xB3)}
    COMBINED_KEYS = dict(_WindowsGerman.COMBINED_KEYS)
    # Option+N starts a tilde, and a space finishes it on its own.
    COMBINED_KEYS[ord("~")] = (0x11 << 8) | 0x80 | ord(" ")
