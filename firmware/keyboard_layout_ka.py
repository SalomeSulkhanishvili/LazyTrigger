"""
Georgian keyboard layout, for a computer set to the Georgian QWERTY input
source ("Georgian – QWERTY" on macOS, "Georgian (QWERTY)" on Windows).

That layout puts each Georgian letter on the Latin key that sounds like it
(ა on A, ბ on B, ...), seven of them with Shift, and leaves digits and
punctuation where the US layout has them. So this is the US layout plus the
Georgian letters. While that input source is active, Latin letters can't be
typed: their keys produce Georgian ones.
"""

from adafruit_hid.keyboard_layout_us import KeyboardLayoutUS

# Each Georgian letter and the key (as a US character) that types it.
_LETTERS = ("აa ბb გg დd ეe ვv ზz თT იi კk ლl მm ნn ოo პp ჟJ რr სs ტt უu ფf "
            "ქq ღR ყy შS ჩC ცc ძZ წw ჭW ხx ჯj ჰh")


class KeyboardLayout(KeyboardLayoutUS):
    HIGHER_ASCII = {ord(pair[0]): KeyboardLayoutUS.ASCII_TO_KEYCODE[ord(pair[1])]
                    for pair in _LETTERS.split()}
