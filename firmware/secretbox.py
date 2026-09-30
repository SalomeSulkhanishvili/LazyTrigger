"""
Encrypting the unlock password with a key that lives on the RFID tag.

The password can't be hashed, the device has to reproduce the exact
characters to type them at a lock screen. So instead of storing it in the
clear, it's encrypted (AES-128-CBC) with a random key that is written onto
the tag itself and never stored on the Pico.

The practical effect:
  * Someone who dumps the Pico's flash gets ciphertext and an IV, no key.
  * Someone who clones/steals the tag gets a key but no ciphertext.
  * Unlocking needs both, which costs nothing in practice because the tag
    already has to be on the reader for an unlock to happen at all.

Losing or reformatting the tag means the password has to be set again
there is deliberately no recovery path, since one would defeat the point.
"""

import os

import aesio

KEY_SIZE = 16
BLOCK_SIZE = 16


def random_key():
    return os.urandom(KEY_SIZE)


def _pad(data):
    """Null-pad to the AES block size. Passwords don't end in NUL bytes, so
    stripping them on the way back out is unambiguous."""
    return data + b"\x00" * ((-len(data)) % BLOCK_SIZE)


def encrypt(plaintext, key):
    """Returns (ciphertext, iv). The IV isn't secret and is stored alongside
    the ciphertext; it just has to differ per encryption."""
    iv = os.urandom(BLOCK_SIZE)
    padded = _pad(plaintext.encode("utf-8"))
    out = bytearray(len(padded))
    aesio.AES(key, aesio.MODE_CBC, iv).encrypt_into(padded, out)
    return bytes(out), iv


def decrypt(ciphertext, key, iv):
    """Returns the plaintext password, or "" if the key/IV don't fit the
    ciphertext (wrong tag, corrupted block, etc)."""
    if not ciphertext or len(ciphertext) % BLOCK_SIZE:
        return ""
    out = bytearray(len(ciphertext))
    try:
        aesio.AES(key, aesio.MODE_CBC, iv).decrypt_into(ciphertext, out)
        return bytes(out).rstrip(b"\x00").decode("utf-8")
    except (ValueError, UnicodeError):
        return ""
