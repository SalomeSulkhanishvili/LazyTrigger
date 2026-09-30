#!/usr/bin/env python3
"""
Push firmware files to the Pico over its REPL console.

Normally you'd just copy files onto the CIRCUITPY drive, but boot.py
remounts the filesystem writable for the microcontroller, which makes the
drive read-only to the host. This writes the files through the REPL
instead, which still has full write access.

Two gotchas this handles that a naive copy/paste into the REPL does not:
  * Files must be explicitly flushed and closed, or CircuitPython can leave
    the write sitting in a RAM cache that a reset then discards.
  * A hard reset (microcontroller.reset()) can drop those cached writes, so
    this triggers a *soft* reload (Ctrl-D) instead, which also clears the
    module cache so edited modules are re-imported.

Usage:
    python3 bridge/upload_firmware.py                 # upload all
    python3 bridge/upload_firmware.py code.py         # upload specific files
    python3 bridge/upload_firmware.py --port /dev/cu.usbmodem101
                                                      # skip port detection
"""

import base64
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    raise SystemExit("pyserial is required: pip3 install pyserial")

from device_setup import FIRMWARE_DIR

DEFAULT_FILES = ["settings.toml", "boot.py", "protocol.py", "config.py", "mfrc522.py",
                 "secretbox.py", "code.py"]
BAUD = 115200
# base64 characters per REPL line; small enough to arrive intact.
CHUNK_CHARS = 1024


def find_console_port(data_port=None):
    """The REPL console is the Pico port that does NOT answer our JSON
    protocol, the data port does. Probe to tell them apart rather than
    relying on port numbering, which isn't stable across reboots.

    data_port: the data port when the caller already has it open (the
    bridge), so it is left alone instead of probed."""
    candidates = [
        p.device
        for p in list_ports.comports()
        if "usbmodem" in p.device or "ttyACM" in p.device or p.device.startswith("COM")
    ]
    for device in candidates:
        if data_port is not None:
            break
        try:
            # write_timeout: a port nobody drains can block a write forever,
            # which hung the whole upload before anything was sent.
            with serial.Serial(device, BAUD, timeout=0.6, write_timeout=0.6) as probe:
                probe.reset_input_buffer()
                probe.write(b'{"cmd":"ping"}\n')
                deadline = time.time() + 0.6
                while time.time() < deadline:
                    if b"pong" in probe.read(256):
                        data_port = device
                        break
        except (OSError, serial.SerialException):
            continue
    consoles = [c for c in candidates if c != data_port]
    if not consoles:
        raise SystemExit(f"No console port found (candidates: {candidates})")
    # With the firmware down nothing answers the ping, so the data port can't
    # be identified by elimination. CircuitPython enumerates the console
    # first, so the lowest-numbered port is the right guess; picking the data
    # port instead means writing REPL commands into a void.
    def port_order(name):
        digits = "".join(ch for ch in name if ch.isdigit())
        return int(digits) if digits else 0
    consoles.sort(key=port_order)
    return consoles[0]


def read_until(ser, marker, timeout):
    """Read until `marker` shows up or we time out. The REPL echoes every
    byte we send, so a plain "sleep then read" often catches the tail of our
    own (very long) command instead of its result."""
    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            buf += chunk.decode("utf-8", "ignore")
            if marker in buf:
                return buf
        else:
            time.sleep(0.05)
    return buf


def run_command(ser, command, wait=1.0):
    ser.write((command + "\r\n").encode("utf-8"))
    ser.flush()
    time.sleep(wait)
    return ser.read(ser.in_waiting or 1).decode("utf-8", "ignore")


def upload(ser, filename):
    source = (FIRMWARE_DIR / filename).read_bytes()
    encoded = base64.b64encode(source).decode()

    run_command(ser, "import binascii", wait=0.6)
    # Confirm the import actually landed before relying on it, the first
    # command after an interrupt is sometimes swallowed.
    check = run_command(ser, "print('BINASCII', binascii is not None)", wait=0.6)
    if "BINASCII True" not in check:
        run_command(ser, "import binascii", wait=1.0)

    # Send the file in small pieces, each confirmed before the next. One
    # ~50KB line used to get garbled in transit now and then, and nothing
    # noticed until the decode failed on the Pico.
    run_command(ser, "_c=[]", wait=0.3)
    chunks = [encoded[k : k + CHUNK_CHARS] for k in range(0, len(encoded), CHUNK_CHARS)]
    for n, chunk in enumerate(chunks, 1):
        # The marker is built on the Pico ("CK%d:"), so our own echoed command
        # text can't be mistaken for the confirmation.
        ser.write(f"_c.append(b'{chunk}');print('CK%d:'%len(_c))\r\n".encode("utf-8"))
        ser.flush()
        if f"CK{n}:" not in read_until(ser, f"CK{n}:", timeout=5):
            print(f"  {filename}: FAILED (piece {n} of {len(chunks)} not confirmed)")
            return False

    # Decode before opening: open(..., 'w') truncates the file at once, so a
    # bad transfer must fail before the existing file is touched.
    write_cmd = (
        "d=binascii.a2b_base64(b''.join(_c)); _c=None; "
        f"f=open('/{filename}','wb'); f.write(d); f.flush(); f.close(); "
        "print('WROTE', len(d), sum(d), 'END'); d=None"
    )
    ser.write((write_cmd + "\r\n").encode("utf-8"))
    ser.flush()
    result = read_until(ser, "END\r", timeout=15)
    # Size and byte sum, as reported by the Pico about what it wrote. Take
    # the LAST "WROTE " so the echoed command can't be mistaken for it.
    if "WROTE " in result:
        fields = result.rsplit("WROTE ", 1)[1].split()
        if (len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit()
                and int(fields[0]) == len(source) and int(fields[1]) == sum(source)):
            print(f"  {filename}: {len(source)} bytes OK")
            return True
    print(f"  {filename}: FAILED (expected {len(source)} bytes, got: ...{result.strip()[-100:]})")
    return False


def main(argv=None, data_port=None):
    args = list(sys.argv[1:] if argv is None else argv)
    port = None
    if "--port" in args:
        i = args.index("--port")
        if i + 1 >= len(args):
            raise SystemExit("--port needs a device path")
        port = args[i + 1]
        del args[i : i + 2]
    files = args or DEFAULT_FILES
    port = port or find_console_port(data_port)
    print(f"Console port: {port}")

    with serial.Serial(port, BAUD, timeout=1) as ser:
        ser.write(b"\x03\x03")  # interrupt whatever's running
        time.sleep(1.0)
        ser.reset_input_buffer()

        ok = all([upload(ser, name) for name in files])

        if ok:
            print("Soft-reloading...")
            ser.write(b"\x04")
            ser.flush()
            time.sleep(4)
            print(ser.read(ser.in_waiting or 1).decode("utf-8", "ignore")[-400:])
        else:
            raise SystemExit("Upload failed; not reloading.")


if __name__ == "__main__":
    main()
