#!/bin/bash
# Run Desk Phone from this folder without installing it.
#   ./run.sh            start, or show the status card if it's running
#   ./run.sh --demo     try the card with a pretend phone (no Bluetooth needed)
#   ./run.sh --check    is everything in place for calls?
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m desk_phone "$@"
