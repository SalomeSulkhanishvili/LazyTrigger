#!/usr/bin/env python3
"""
Build the double-click lazyTrigger app for the computer this runs on.

    make app                          (or: python3 tools/manage.py app)

Needs the packages in app/requirements.txt. PyInstaller can only build for
the system it runs on, so each release is built on macOS, Windows and Linux
by .github/workflows/release.yml. The result lands in dist/:

    macOS    lazyTrigger-macOS-AppleSilicon.zip / lazyTrigger-macOS-Intel.zip
             (lazyTrigger.app inside)
    Windows  lazyTrigger-Windows.exe
    Linux    lazyTrigger-Linux
"""

import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
NAME = "lazyTrigger"

sys.path.insert(0, str(ROOT / "bridge"))
from device_setup import FIRMWARE_FILES  # noqa: E402

SYSTEM = platform.system()


def run(cmd):
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True)


def data(src, dest):
    # Absolute sources: PyInstaller reads relative ones from the spec's folder.
    return ["--add-data", "{}{}{}".format(ROOT / src, os.pathsep, dest)]


def pyinstaller():
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--name", NAME, "--windowed",
           "--distpath", DIST, "--workpath", BUILD, "--specpath", BUILD,
           # The bridge and the firmware's protocol constants are imported
           # at run time from the folders a checkout keeps them in.
           "--paths", ROOT / "bridge", "--paths", ROOT / "firmware",
           "--hidden-import", "serial_bridge", "--hidden-import", "upload_firmware",
           "--hidden-import", "protocol", "--hidden-import", "certifi",
           # PyInstaller turns the PNG into a macOS .icns or Windows .ico
           # (with Pillow); Linux executables carry no icon.
           "--icon", ROOT / "app" / "icon.png"]
    cmd += data("configurator", "configurator")
    for name in FIRMWARE_FILES:
        cmd += data(Path("firmware") / name, "firmware")
    if SYSTEM == "Darwin":
        cmd += ["--osx-bundle-identifier", "io.github.salomesulkhanishvili.lazytrigger"]
    else:
        cmd += ["--onefile"]  # one file to download and double-click
    cmd.append(ROOT / "app" / "lazytrigger_app.py")
    run(cmd)


def finish_mac():
    app = DIST / (NAME + ".app")
    # No Dock icon: the app only opens the page and starts the bridge in
    # the background, so an icon would just bounce and vanish.
    info = app / "Contents" / "Info.plist"
    with open(info, "rb") as fh:
        plist = plistlib.load(fh)
    plist["LSUIElement"] = True
    with open(info, "wb") as fh:
        plistlib.dump(plist, fh)
    # Editing Info.plist breaks PyInstaller's signature, and Apple Silicon
    # won't start unsigned code, so sign it again (ad hoc, no Apple account).
    run(["codesign", "--force", "--deep", "--sign", "-", app])
    arch = "AppleSilicon" if platform.machine() == "arm64" else "Intel"
    out = DIST / "{}-macOS-{}.zip".format(NAME, arch)
    out.unlink(missing_ok=True)
    # ditto keeps the bundle's symlinks and signature intact; zip does not.
    run(["ditto", "-c", "-k", "--keepParent", app, out])
    return out


def main():
    shutil.rmtree(DIST, ignore_errors=True)
    pyinstaller()
    if SYSTEM == "Darwin":
        out = finish_mac()
    elif SYSTEM == "Windows":
        out = DIST / (NAME + "-Windows.exe")
        (DIST / (NAME + ".exe")).replace(out)
    else:
        out = DIST / (NAME + "-Linux")
        (DIST / NAME).replace(out)
    print("\nBuilt " + str(out))


if __name__ == "__main__":
    main()
