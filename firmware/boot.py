"""
Runs once at power-up, before code.py.

Sets up the USB composite device: keyboard HID (to type the unlock password
and send lock shortcuts) plus a second CDC serial ("data") channel that the
browser-based configurator talks to over WebSerial, separate from the REPL
console.
"""

import storage
import usb_cdc
import usb_hid

# code.py needs to write config.json (tags, password, OS) at runtime, so the
# filesystem must be writable from the microcontroller side. This makes the
# CIRCUITPY drive read-only when plugged into a computer, which is fine:
# configuration happens over the data serial port, not by editing files.
storage.remount("/", readonly=False)

usb_cdc.enable(console=True, data=True)
usb_hid.enable((usb_hid.Device.KEYBOARD,))
