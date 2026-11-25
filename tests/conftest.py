import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gi.repository import GLib  # noqa: E402

if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
    pytest.exit("run the tests in a D-Bus session: dbus-run-session -- python3 -m pytest tests", 2)


def spin(cond=None, timeout=3.0):
    """Runs the GLib main loop until cond() is true (or for `timeout` seconds without one)."""
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        while ctx.pending():
            ctx.iteration(False)
        if cond is not None and cond():
            return True
        time.sleep(0.01)
    if cond is not None:
        raise AssertionError("timed out waiting")
    return True


@pytest.fixture
def spin_fn():
    return spin
