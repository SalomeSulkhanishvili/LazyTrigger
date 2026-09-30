# lazyTrigger
#
#   make            set up everything (Python env, firmware on a new Pico)
#   make start      after plugging in the Pico: run the bridge in the background
#   make autostart  optional: check once at each login and start it if plugged in
#   make app        build the double-click app for this computer (into dist/)
#   make help       list every command
#
# Works on macOS, Linux and Windows. All the logic lives in tools/manage.py
# so each platform runs the same steps; this file only forwards to it. On
# Windows without GNU make, make.cmd accepts the same commands.

ifeq ($(OS),Windows_NT)
PYTHON ?= $(if $(shell where py 2>NUL),py -3,python)
else
PYTHON ?= python3
endif

MANAGE := $(PYTHON) tools/manage.py

# Optional arguments:
#   make upload FILES="code.py mfrc522.py" PORT=/dev/cu.usbmodem101
#   make flash BOARD=raspberry_pi_pico2_w
FILES ?=
PORT  ?=
BOARD ?=

.DEFAULT_GOAL := install
.PHONY: install setup flash install-firmware upload start stop autostart autostart-remove \
        status logs bridge check app clean help

install:
	@$(MANAGE) install $(if $(BOARD),--board $(BOARD))

flash:
	@$(MANAGE) flash $(if $(BOARD),--board $(BOARD))

upload:
	@$(MANAGE) upload $(if $(PORT),--port $(PORT)) $(FILES)

setup install-firmware start stop autostart autostart-remove status logs bridge check app clean help:
	@$(MANAGE) $@
