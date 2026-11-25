"""Recent calls: the calls the phone had while Desk Phone was running, newest first.

They're kept on this computer only, in ~/.local/state/desk-phone/calls.json, readable by you
alone, like the cached contacts. Nothing is read from the phone's own call log, so calls made
while the phone was away or Desk Phone wasn't running aren't in the list.
"""

import json
import logging
import os
import time
from pathlib import Path

from .config import state_dir

log = logging.getLogger(__name__)

MAX_CALLS = 50
# in: answered (here or on the phone) · out: dialed · missed: rang and wasn't answered ·
# declined: declined from Desk Phone · call: already going when Desk Phone started
KINDS = ("in", "out", "missed", "declined", "call")
KIND_TEXT = {"in": "Incoming", "out": "Outgoing", "missed": "Missed", "declined": "Declined",
             "call": "Call"}


def format_when(when: float, now: float | None = None) -> str:
    """Short and relative: 14:32 today, Yesterday, Mon within a week, 12 Nov, 12 Nov 2025."""
    now = time.time() if now is None else now
    t, n = time.localtime(when), time.localtime(now)
    days = (time.mktime((n.tm_year, n.tm_mon, n.tm_mday, 12, 0, 0, 0, 0, -1))
            - time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 12, 0, 0, 0, 0, -1))) / 86400
    days = round(days)
    if days <= 0:
        return time.strftime("%H:%M", t)
    if days == 1:
        return "Yesterday"
    if days < 7:
        return time.strftime("%a", t)
    day = f"{t.tm_mday} {time.strftime('%b', t)}"
    return day if t.tm_year == n.tm_year else f"{day} {t.tm_year}"


def describe(entry: dict, now: float | None = None) -> str:
    """One line about a call, for a tooltip: 'Missed · Yesterday 14:32'."""
    t = time.localtime(entry["when"])
    when = format_when(entry["when"], now)
    clock = time.strftime("%H:%M", t)
    parts = [KIND_TEXT.get(entry["kind"], "Call"), when if when == clock else f"{when} {clock}"]
    if entry.get("seconds"):
        m, s = divmod(int(entry["seconds"]), 60)
        parts.append(f"{m} min {s:02d} s" if m else f"{s} s")
    elif entry["kind"] == "out":
        parts.append("no answer")
    return " · ".join(parts)


def _clean(entry) -> dict | None:
    """A stored entry as the rest of the app expects it, or None when it isn't one."""
    if not isinstance(entry, dict) or not isinstance(entry.get("when"), (int, float)):
        return None
    kind = entry.get("kind")
    seconds = entry.get("seconds", 0)
    return {"number": str(entry.get("number") or ""), "name": str(entry.get("name") or ""),
            "kind": kind if kind in KINDS else "call", "when": float(entry["when"]),
            "seconds": int(seconds) if isinstance(seconds, (int, float)) else 0}


class CallHistory:
    def __init__(self, folder: Path | None = None, limit: int = MAX_CALLS, persist: bool = True):
        self.path = (folder or state_dir()) / "calls.json"
        self.limit = limit
        self.persist = persist            # False in the demo: its made-up calls aren't written
        self.on_changed = None
        self.entries: list[dict] = self._load() if persist else []

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("can't read %s (%s); starting an empty list", self.path, e)
            return []
        calls = data.get("calls") if isinstance(data, dict) else None
        if not isinstance(calls, list):
            return []
        return [e for e in map(_clean, calls) if e is not None][:self.limit]

    def _save(self):
        if not self.persist:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"calls": self.entries}, f)
            os.chmod(tmp, 0o600)          # (also when a leftover .tmp was there with other rights)
            tmp.replace(self.path)
        except OSError as e:
            log.warning("can't save the recent calls: %s", e)

    def all(self) -> list[dict]:
        """Newest first."""
        return list(self.entries)

    def set_entries(self, entries):
        self.entries = [e for e in map(_clean, entries) if e is not None][:self.limit]
        self._save()
        if self.on_changed:
            self.on_changed()

    def add(self, number: str, name: str, kind: str, when: float, seconds: float = 0):
        entry = _clean({"number": number, "name": name, "kind": kind, "when": when,
                        "seconds": max(0, int(seconds))})
        self.entries = ([entry] + self.entries)[:self.limit]
        self._save()
        if self.on_changed:
            self.on_changed()

    def clear(self):
        """Forgets every call and deletes the file."""
        self.entries = []
        if self.persist:
            for path in (self.path, self.path.with_suffix(".tmp")):
                try:
                    path.unlink(missing_ok=True)
                except OSError as e:
                    log.warning("can't delete %s: %s", path, e)
        if self.on_changed:
            self.on_changed()
