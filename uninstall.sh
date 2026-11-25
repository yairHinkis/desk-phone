#!/bin/bash
# Removes Desk Phone for the current user: the keys, start at login, the PipeWire setting, the app
# entry, the copy of the app, and its settings, cached contacts, recent calls and log.
# The same as `desk-phone --uninstall`. This folder itself stays.
#   ./uninstall.sh --yes         don't ask first
#   ./uninstall.sh --keep-data   keep the settings, cached contacts, recent calls and log
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHONPATH="$APP_DIR" exec /usr/bin/python3 -P -m desk_phone --uninstall "$@"
