"""Settings in ~/.config/desk-phone/config.json and the cache/state folders."""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {
    # "answer": a call's audio comes to the computer only when it's answered (or dialed) here.
    # "computer": like a car kit, every call's audio comes to the computer while the phone is connected.
    "audio_mode": "answer",
    "echo_cancel": True,     # PipeWire's echo canceller between the call and the speakers/mic
    "ringtone": True,        # ring on the computer too (the phone rings by itself)
    "contacts": True,        # caller names from the phone's contacts (Bluetooth phonebook)
    "call_history": True,    # the recent-calls list (kept on this computer, see history.py)
    "autostart": True,
    "gnome_contacts_button": False,  # the card's contacts button opens GNOME Contacts instead
    "gnome_contacts_search": False,  # the dialer's matches come from GNOME Contacts instead
    "card_corner": None,     # where the user last moved the call card (see CallCard.corner)
    "card_position": "bottom-right",   # where the card opens: bottom-right/-left, top-right/-left
    "remember_card_position": False,   # open where it was last moved instead
}
AUDIO_MODES = ("answer", "computer")


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "desk-phone"


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "desk-phone"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "desk-phone"


class Config(dict):
    def __init__(self, path: Path | None = None):
        super().__init__(DEFAULTS)
        self.path = path or config_dir() / "config.json"
        self._observers = []
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                self.update({k: v for k, v in data.items() if k in DEFAULTS})
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("can't read %s (%s); using defaults", self.path, e)
        if self.get("audio_mode") not in AUDIO_MODES:
            self["audio_mode"] = DEFAULTS["audio_mode"]

    def observe(self, fn):
        self._observers.append(fn)

    def set(self, key, value):
        if self.get(key) == value:
            return
        self[key] = value
        self.save()
        for fn in list(self._observers):
            fn(key, value)

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(dict(self), indent=2) + "\n")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("can't save %s: %s", self.path, e)
