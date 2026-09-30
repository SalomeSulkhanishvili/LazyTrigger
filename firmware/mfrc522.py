"""
MFRC522 (RC522) RFID reader driver for CircuitPython, using busio.SPI.

Supports detecting a card and reading its UID (used for the tap-to-unlock
whitelist), plus MIFARE Classic authentication and 16-byte block read/write
(used to store optional data on the tag itself, e.g, a label).
"""

import time

import digitalio


# MFRC522 register map (subset)
_COMMAND_REG = 0x01
_COM_IEN_REG = 0x02
_COM_IRQ_REG = 0x04
_DIV_IRQ_REG = 0x05
_ERROR_REG = 0x06
_FIFO_DATA_REG = 0x09
_FIFO_LEVEL_REG = 0x0A
_CONTROL_REG = 0x0C
_COLL_REG = 0x0E
_BIT_FRAMING_REG = 0x0D
_MODE_REG = 0x11
_TX_CONTROL_REG = 0x14
_TX_ASK_REG = 0x15
_CRC_RESULT_REG_M = 0x21
_CRC_RESULT_REG_L = 0x22
_RF_CFG_REG = 0x26
_T_MODE_REG = 0x2A
_T_PRESCALER_REG = 0x2B
_T_RELOAD_REG_H = 0x2C
_T_RELOAD_REG_L = 0x2D
_VERSION_REG = 0x37

# PCD (reader) commands
_PCD_IDLE = 0x00
_PCD_AUTHENT = 0x0E
_PCD_TRANSCEIVE = 0x0C
_PCD_RESETPHASE = 0x0F
_PCD_CALCCRC = 0x03

# PICC (card) commands
_PICC_REQIDL = 0x26
# WUPA. Unlike REQA (0x26), which only answers from cards in IDLE, this also
# wakes a card that was HALTed by a previous operation, which is every card
# still sitting on the reader after a read, a write, or a key lookup.
_PICC_WUPA = 0x52
_PICC_ANTICOLL = 0x93
_PICC_SELECTTAG = 0x93
_PICC_AUTHENT1A = 0x60
_PICC_AUTHENT1B = 0x61
_PICC_READ = 0x30
_PICC_WRITE = 0xA0
_PICC_HALT = 0x50

#: SPI clock. The MFRC522 is rated to 10MHz, but 1MHz measured more
#: reliable on breadboard jumper wires (key-block reads 10/10 vs 8/10 at
#: 4MHz on a weak tag), and costs almost nothing: per-register time is
#: dominated by Python overhead, not the clock.
SPI_BAUD = 1_000_000

#: Default MIFARE Classic factory key (Key A), used by most blank/new tags.
DEFAULT_KEY = bytes([0xFF] * 6)


class MFRC522:
    def __init__(self, spi, cs, rst):
        self._spi = spi
        self._cs = cs
        self._rst = rst

        self._cs.direction = digitalio.Direction.OUTPUT
        self._cs.value = True

        self._rst.direction = digitalio.Direction.OUTPUT
        self._rst.value = True

        # The reader is the only device on this bus, so take the lock and set
        # the clock once. Locking and reconfiguring on every register access
        # cost ~335us each, and a single poll makes dozens of them.
        while not self._spi.try_lock():
            pass
        self._spi.configure(baudrate=SPI_BAUD, polarity=0, phase=0)
        self._tx = bytearray(2)
        self._rx = bytearray(2)

        self.init_chip()

    def init_chip(self):
        """Reset the chip and apply the working configuration.

        Resetting the MFRC522 drops every register back to power-on
        defaults: the auto-timeout timer is off and, more importantly, so is
        the antenna. A chip left in that state answers register reads
        perfectly while never seeing a single card, so every path that
        resets it must come back through here.
        """
        self._reset()
        # Nothing below is worth writing until the chip is actually answering:
        # a register write into a chip that is still starting up is dropped,
        # and a register read comes back as 0x00 or 0xFF, which is garbage
        # that later checks would happily believe.
        self.awake = self._await_chip()

        # TAuto: start the timer automatically at the end of transmission, so
        # `_to_card` gets a TimerIRq instead of spinning its retry counter.
        self._write(_T_MODE_REG, 0x8D)
        self._write(_T_PRESCALER_REG, 0x3E)
        self._write(_T_RELOAD_REG_L, 30)
        self._write(_T_RELOAD_REG_H, 0)
        self._write(_TX_ASK_REG, 0x40)
        self._write(_MODE_REG, 0x3D)
        # Receiver gain (RFCfgReg) is deliberately left at the chip's reset
        # default (0x44, ~33dB). Forcing the maximum (0x70, 48dB) overdrives
        # the receiver on these modules: measured side by side on the same
        # tag, 0x70 read 0/20 where the default read 20/20. The reset above
        # restores the default, so there is nothing to write.
        # Only trust the antenna check on a chip that answered: a missing
        # chip reads back 0xFF, which looks like both driver bits are set.
        self.antenna_ok = self.awake and self._antenna_on()
        return self.awake and self.antenna_ok

    def healthy(self):
        """True while the chip answers over SPI with its antenna on.

        A loose wire or a brown-out leaves the chip unreachable or reset to
        defaults (antenna off), and nothing else notices: every poll just
        finds no card. The main loop checks this and re-runs init_chip()."""
        if self._read(_VERSION_REG) in (0x00, 0xFF):
            return False
        return (self._read(_TX_CONTROL_REG) & 0x03) == 0x03

    # low level SPI helpers
    def _write(self, addr, val):
        self._tx[0] = (addr << 1) & 0x7E
        self._tx[1] = val & 0xFF
        self._cs.value = False
        try:
            self._spi.write(self._tx)
        finally:
            self._cs.value = True

    def _read(self, addr):
        self._tx[0] = ((addr << 1) & 0x7E) | 0x80
        self._tx[1] = 0
        self._cs.value = False
        try:
            self._spi.write_readinto(self._tx, self._rx)
        finally:
            self._cs.value = True
        return self._rx[1]

    def _write_fifo(self, data):
        """Load the FIFO in one SPI burst: after the address byte the chip
        keeps writing the same register for every byte that follows."""
        buf = bytearray(len(data) + 1)
        buf[0] = (_FIFO_DATA_REG << 1) & 0x7E
        buf[1:] = bytes(data)
        self._cs.value = False
        try:
            self._spi.write(buf)
        finally:
            self._cs.value = True

    def _read_fifo(self, n):
        """Drain n FIFO bytes in one burst: repeating the read address
        clocks out one byte per address, the final 0x00 ends the burst."""
        addr = ((_FIFO_DATA_REG << 1) & 0x7E) | 0x80
        tx = bytearray([addr] * n + [0])
        rx = bytearray(n + 1)
        self._cs.value = False
        try:
            self._spi.write_readinto(tx, rx)
        finally:
            self._cs.value = True
        return list(rx[1:])

    def _set_bits(self, addr, mask):
        self._write(addr, self._read(addr) | mask)

    def _clear_bits(self, addr, mask):
        self._write(addr, self._read(addr) & (~mask & 0xFF))

    def _reset(self):
        self._rst.value = False
        time.sleep(0.001)
        self._rst.value = True
        time.sleep(0.001)
        self._write(_COMMAND_REG, _PCD_RESETPHASE)
        time.sleep(0.05)

    def _await_chip(self, timeout=0.5):
        """Wait for the chip to answer with a plausible version byte.

        After a reset the MFRC522 needs a moment before its SPI interface
        returns anything meaningful. Until then every read is 0x00 or 0xFF.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._read(_VERSION_REG) not in (0x00, 0xFF):
                return True
            time.sleep(0.01)
        return False

    def _antenna_on(self):
        """Switch the RF field on, and confirm the bits actually took.

        This used to skip the write when TxControlReg already looked like it
        had the driver bits set. A chip that is not answering yet reads back
        as 0xFF, and 0xFF & 0x03 passes that test, so the antenna was left
        off while every other register read fine once the chip woke up. The
        reader then reported a healthy version byte and a working SPI path
        while powering no card at all, which looks exactly like a tag that
        will not read.
        """
        for _ in range(5):
            self._set_bits(_TX_CONTROL_REG, 0x03)
            if (self._read(_TX_CONTROL_REG) & 0x03) == 0x03:
                return True
            time.sleep(0.01)
        return False

    # card protocol
    def _to_card(self, command, send_data):
        back_data = []
        back_len = 0
        error = False
        irq_en = 0x77
        wait_irq = 0x30

        self._write(_COM_IEN_REG, irq_en | 0x80)
        self._clear_bits(_COM_IRQ_REG, 0x80)
        self._set_bits(_FIFO_LEVEL_REG, 0x80)
        self._write(_COMMAND_REG, _PCD_IDLE)

        self._write_fifo(send_data)

        self._write(_COMMAND_REG, command)
        if command == _PCD_TRANSCEIVE:
            self._set_bits(_BIT_FRAMING_REG, 0x80)

        i = 2000
        while True:
            n = self._read(_COM_IRQ_REG)
            i -= 1
            if not ((i != 0) and not (n & 0x01) and not (n & wait_irq)):
                break

        self._clear_bits(_BIT_FRAMING_REG, 0x80)

        if i == 0:
            return False, back_data, back_len

        if self._read(_ERROR_REG) & 0x1B:
            return False, back_data, back_len

        if n & irq_en & 0x01:
            error = True

        if command == _PCD_TRANSCEIVE:
            n = self._read(_FIFO_LEVEL_REG)
            last_bits = self._read(_CONTROL_REG) & 0x07
            back_len = (n - 1) * 8 + last_bits if last_bits else n * 8
            if n == 0:
                n = 1
            # Drain the whole FIFO, not the first 16 bytes: a block read
            # answers with 18 (16 data + 2 CRC). Clamping to 16 here while
            # back_len was computed from the real count made every read look
            # like a length mismatch. 64 is the MFRC522's FIFO size.
            if n > 64:
                n = 64
            back_data = self._read_fifo(n)

        return (not error), back_data, back_len

    def request(self, mode=_PICC_REQIDL):
        """Ask for any card in range. Returns True if a card responded.

        REQA (the default) is answered only by cards in IDLE; pass
        `_PICC_WUPA` to reach a HALTed card as well."""
        self._write(_BIT_FRAMING_REG, 0x07)
        ok, _, back_len = self._to_card(_PCD_TRANSCEIVE, [mode])
        # A card answers with a 2-byte ATQA. Noise or a garbled reply can
        # raise RxIRq with nothing in the FIFO, which is not a card.
        return ok and back_len == 16

    def wake(self):
        """Get any card in the field to answer, whether IDLE or HALTed.

        WUPA covers both states, so it goes first and a single exchange is
        the normal case; the REQA retry is only there for clone cards that
        are fussy about WUPA."""
        return self.request(_PICC_WUPA) or self.request(_PICC_REQIDL)

    def anticoll(self):
        """Run anticollision and return the card UID as a bytearray, or None."""
        self._write(_BIT_FRAMING_REG, 0x00)
        # ValuesAfterColl off, as the datasheet's anticollision flow expects.
        self._clear_bits(_COLL_REG, 0x80)
        ok, back_data, _ = self._to_card(_PCD_TRANSCEIVE, [_PICC_ANTICOLL, 0x20])
        if not ok or len(back_data) != 5:
            return None

        checksum = 0
        for b in back_data[:4]:
            checksum ^= b
        if checksum != back_data[4]:
            return None

        return bytearray(back_data[:4])

    def read_uid(self):
        """Non-blocking single attempt: returns a UID bytearray or None."""
        if not self.wake():
            return None
        return self.anticoll()

    def _calculate_crc(self, data):
        self._clear_bits(_DIV_IRQ_REG, 0x04)
        self._set_bits(_FIFO_LEVEL_REG, 0x80)
        self._write_fifo(data)
        self._write(_COMMAND_REG, _PCD_CALCCRC)

        i = 0xFF
        while i:
            n = self._read(_DIV_IRQ_REG)
            if n & 0x04:
                break
            i -= 1

        return bytes([self._read(_CRC_RESULT_REG_L), self._read(_CRC_RESULT_REG_M)])

    def select_tag(self, uid):
        """Select a card by its 4-byte UID. Must be called before auth()."""
        # anticoll() normally leaves this at 0, but a bare select after a
        # request() would otherwise still be framed as REQA's 7 bits.
        self._write(_BIT_FRAMING_REG, 0x00)
        buf = bytearray([_PICC_SELECTTAG, 0x70])
        buf.extend(uid)
        checksum = 0
        for b in uid:
            checksum ^= b
        buf.append(checksum)
        buf.extend(self._calculate_crc(bytes(buf)))

        ok, back_data, back_len = self._to_card(_PCD_TRANSCEIVE, buf)
        return ok and back_len == 24  # SAK response is 3 bytes = 24 bits

    def reselect(self, uid):
        """Bring a card back to the selected state, ready for auth().

        A card that has already been through one operation is in one of three
        states: still selected (from the tap that detected it), HALTed (every
        block read/write ends with halt()), or IDLE (a failed authentication
        drops it there). SELECT only works from the third of those, so this
        halts whatever is there, wakes it with WUPA, and runs the full
        anticollision + select sequence from a known starting point. Without
        it, the second operation on a tag that never left the reader fails at
        select or auth even though nothing is wrong with the tag.
        """
        for _ in range(3):
            self.halt()
            if not self.wake():
                continue
            found = self.anticoll()
            if found is None or bytes(found) != bytes(uid):
                continue
            if self.select_tag(uid):
                return True
        return False

    def auth(self, block_addr, key, uid, mode="A"):
        """
        Authenticate to the sector containing `block_addr`.

        `key` is a 6-byte key (DEFAULT_KEY for a factory-fresh tag). Call
        select_tag(uid) first. Must be followed by stop_crypto1() once done
        with this card.
        """
        cmd = _PICC_AUTHENT1A if mode == "A" else _PICC_AUTHENT1B
        buf = bytearray([cmd, block_addr])
        buf.extend(key)
        buf.extend(uid)

        ok, _, _ = self._to_card(_PCD_AUTHENT, buf)
        if not ok:
            return False
        return bool(self._read(_ERROR_REG) == 0 and (self._status2() & 0x08))

    def _status2(self):
        return self._read(0x08)  # Status2Reg

    def stop_crypto1(self):
        self._clear_bits(0x08, 0x08)  # Status2Reg, clear MFCrypto1On

    def end_session(self):
        """Close an authenticated session so the next reselect() works.

        HALT has to go out while encryption is still on: an authenticated
        tag only accepts encrypted commands and ignores a plain HALT, so
        turning encryption off first (as this driver did) left the tag busy
        and the next select failing. This is the order NXP's application
        notes and the Arduino MFRC522 library use."""
        self.halt()
        self.stop_crypto1()

    def read_block(self, block_addr):
        """Read 16 bytes from `block_addr`. Requires a prior successful auth()."""
        buf = bytearray([_PICC_READ, block_addr])
        buf.extend(self._calculate_crc(bytes(buf)))

        ok, back_data, back_len = self._to_card(_PCD_TRANSCEIVE, buf)
        # The card answers with 16 data bytes followed by 2 CRC bytes.
        if not ok or back_len != 18 * 8 or len(back_data) < 16:
            return None
        return bytearray(back_data[:16])

    def write_block(self, block_addr, data):
        """Write 16 bytes to `block_addr`. Requires a prior successful auth()."""
        if len(data) != 16:
            raise ValueError("block data must be exactly 16 bytes")

        buf = bytearray([_PICC_WRITE, block_addr])
        buf.extend(self._calculate_crc(bytes(buf)))
        ok, back_data, back_len = self._to_card(_PCD_TRANSCEIVE, buf)
        if not ok or back_len != 4 or (back_data[0] & 0x0F) != 0x0A:
            return False

        payload = bytearray(data)
        payload.extend(self._calculate_crc(bytes(data)))
        ok, back_data, back_len = self._to_card(_PCD_TRANSCEIVE, payload)
        return ok and back_len == 4 and (back_data[0] & 0x0F) == 0x0A

    def halt(self):
        buf = bytearray([_PICC_HALT, 0])
        buf.extend(self._calculate_crc(bytes(buf)))
        self._to_card(_PCD_TRANSCEIVE, buf)

    @staticmethod
    def uid_to_str(uid):
        return "".join("{:02X}".format(b) for b in uid)
