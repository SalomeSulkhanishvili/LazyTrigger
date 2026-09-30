#!/usr/bin/env python3
"""
Verify the protocol definitions, the firmware and the docs agree.

Three checks, each covering a drift that is silent at runtime:

  1. configurator/protocol.js vs firmware/protocol.py. The firmware and the
     web UI can't share a module, so the constants exist twice.
  2. Every Command constant is actually handled in firmware/code.py. A
     command the UI can send but the device doesn't implement comes back as
     "unknown command", which looks like a dead button rather than a bug.
  3. Every Command and Event appears in PROTOCOL.md, so the reference
     doesn't quietly describe a protocol that no longer exists.

Usage: python3 tools/check_protocol.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "firmware"))

import protocol  # noqa: E402

JS_PATH = ROOT / "configurator" / "protocol.js"
CODE_PATH = ROOT / "firmware" / "code.py"
DOC_PATH = ROOT / "PROTOCOL.md"


def parse_js_object(source, name):
    """Pull `const <name> = Object.freeze({...})` (or a plain object) out of
    the JS and return it as a dict of NAME -> "value"."""
    match = re.search(
        rf"const {name} = (?:Object\.freeze\()?\{{(.*?)\}}\)?;", source, re.S
    )
    if not match:
        raise SystemExit(f"{name} not found in {JS_PATH.name}")
    return dict(re.findall(r"(\w+):\s*\"([^\"]*)\"", match.group(1)))


def constants_of(cls):
    return {
        key: value
        for key, value in vars(cls).items()
        if key.isupper() and isinstance(value, str)
    }


def main():
    source = JS_PATH.read_text()
    failures = []

    for name, cls in [
        ("ActionType", protocol.ActionType),
        ("Command", protocol.Command),
        ("Event", protocol.Event),
        ("NotifyMode", protocol.NotifyMode),
        ("TapMode", protocol.TapMode),
    ]:
        py = constants_of(cls)
        js = parse_js_object(source, name)

        for key in sorted(set(py) | set(js)):
            if key not in js:
                failures.append(f"{name}.{key} missing from protocol.js")
            elif key not in py:
                failures.append(f"{name}.{key} missing from protocol.py")
            elif py[key] != js[key]:
                failures.append(
                    f"{name}.{key} differs: py={py[key]!r} js={js[key]!r}"
                )

    handled = CODE_PATH.read_text()
    for name in sorted(constants_of(protocol.Command)):
        if not re.search(rf"Command\.{name}\b", handled):
            failures.append(
                f"Command.{name} is defined but never handled in code.py"
            )

    doc = DOC_PATH.read_text()
    for cls_name, cls in [("Command", protocol.Command), ("Event", protocol.Event)]:
        for key, value in sorted(constants_of(cls).items()):
            if f"`{value}`" not in doc:
                failures.append(
                    f"{cls_name}.{key} ({value!r}) is not documented in {DOC_PATH.name}"
                )

    if failures:
        print("Protocol definitions are out of sync:")
        for line in failures:
            print("  - " + line)
        return 1

    print("protocol.py, protocol.js, code.py and PROTOCOL.md agree "
          f"({len(constants_of(protocol.ActionType))} action types, "
          f"{len(constants_of(protocol.Command))} commands, "
          f"{len(constants_of(protocol.Event))} events)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
