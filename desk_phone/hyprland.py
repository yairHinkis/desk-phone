"""What Desk Phone asks of Hyprland while it runs (the keys and window rules are in shortcuts.py).

The card is pinned, so it shows on every workspace. Hyprland keeps a pinned window that has the
keyboard on the regular workspace, also while a special workspace (Omarchy's scratchpad, Super+S)
is open above it. When that window then goes away, Hyprland gives the keyboard to the next window
of the regular workspace, and that closes the special workspace: clicking the card's × threw you
out of the scratchpad. So before the card hides, the keyboard goes back to the window you were at
in the open special workspace.
"""

import json
import logging
import os
import subprocess

log = logging.getLogger(__name__)


def on_hyprland() -> bool:
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def _hyprctl(*args: str) -> str:
    return subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=1).stdout


def _ask(what: str):
    return json.loads(_hyprctl("-j", what))


def _focus(address: str) -> bool:
    """Hyprland with a Lua config takes Lua here; an older one takes the dispatcher's name."""
    lua = f'hl.dsp.focus({{ window = hl.get_window("address:{address}") }})'
    return any(_hyprctl("dispatch", *form).strip() == "ok"
               for form in ((lua,), ("focuswindow", f"address:{address}")))


def window_to_return_to(active: dict, monitors: list, clients: list, pid: int) -> str | None:
    """The address of the window that should get the keyboard before our window hides: the one
    last used in the special workspace that's open on our monitor. None when ours doesn't have
    the keyboard, or no special workspace is open, or it has no other window."""
    if not active or active.get("pid") != pid:
        return None
    special = next((m.get("specialWorkspace", {}).get("name") for m in monitors
                    if m.get("id") == active.get("monitor")), None)
    if not special:
        return None
    there = [c for c in clients if c.get("workspace", {}).get("name") == special
             and c.get("pid") != pid and c.get("mapped", True) and not c.get("hidden")]
    if not there:
        return None
    return min(there, key=lambda c: c.get("focusHistoryID", 1 << 30)).get("address")


def hand_focus_back():
    """Call just before one of our pinned windows hides. Does nothing outside Hyprland."""
    if not on_hyprland():
        return
    try:
        address = window_to_return_to(_ask("activewindow"), _ask("monitors"), _ask("clients"),
                                      os.getpid())
        if address and not _focus(address):
            log.info("hyprland: couldn't give the keyboard back to %s", address)
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError, TypeError) as e:
        log.info("hyprland: %s", e)
