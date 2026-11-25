"""Caller names from the phone's contacts, read over Bluetooth (PBAP, through BlueZ's obexd).

The first time, the phone asks whether this computer may read its contacts. The list is kept
on this computer only (~/.cache/desk-phone, readable by you alone) and refreshed when the phone
connects, at most every few hours.
"""

import json
import logging
import os
import quopri
import re
import time
from pathlib import Path

from gi.repository import Gio, GLib

from .telephony import error_text

from .config import cache_dir

log = logging.getLogger(__name__)

OBEX = "org.bluez.obex"
OBEX_PATH = "/org/bluez/obex"
REFRESH_AFTER_S = 6 * 3600
MATCH_DIGITS = 9


def digits(number: str) -> str:
    return re.sub(r"\D", "", number or "")


def match_key(number: str) -> str:
    """Numbers match on their last 9 digits: +972 50-123-4567 and 050-1234567 are one number."""
    d = digits(number)
    return d[-MATCH_DIGITS:] if len(d) >= 7 else d


def _unfold(text: str) -> list[str]:
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif lines and lines[-1].endswith("=") and "QUOTED-PRINTABLE" in lines[-1].upper():
            lines[-1] = lines[-1][:-1] + raw
        else:
            lines.append(raw)
    return lines


def _value(params: str, value: str) -> str:
    up = params.upper()
    if "QUOTED-PRINTABLE" in up:
        m = re.search(r"CHARSET=([^;:]+)", params, re.I)
        value = quopri.decodestring(value.encode()).decode(m[1] if m else "utf-8", "replace")
    return value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()


def parse_vcards(text: str) -> list[tuple[str, list[str]]]:
    """[(name, [numbers])] from vCard 2.1/3.0 text."""
    out = []
    name, n_name, numbers = "", "", []
    for line in _unfold(text):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        prop, _, params = key.partition(";")
        prop = prop.upper().split(".")[-1]
        if prop == "BEGIN":
            name, n_name, numbers = "", "", []
        elif prop == "FN":
            name = _value(params, value)
        elif prop == "N":
            parts = [p.strip() for p in _value(params, value).split(";")]
            n_name = " ".join(p for p in (parts[1:2] + parts[:1]) if p)
        elif prop == "TEL":
            num = _value(params, value)
            if digits(num):
                numbers.append(num)
        elif prop == "END":
            label = name or n_name
            if label and numbers:
                out.append((label, numbers))
    return out


class Contacts:
    def __init__(self, folder: Path | None = None, connection=None):
        self.folder = folder or cache_dir()
        self.conn = connection
        self.on_changed = None
        self._index: dict[str, str] = {}
        self._entries: list = []
        self._address = ""
        self._busy = False

    def _file(self, address):
        return self.folder / f"contacts-{address.replace(':', '').upper()}.json"

    def lookup(self, number: str) -> str | None:
        key = match_key(number)
        return self._index.get(key) if key else None

    def set_entries(self, entries):
        index = {}
        for name, numbers in entries:
            for num in numbers:
                index.setdefault(match_key(num), name)
        self._index = index
        self._entries = [(name, list(numbers)) for name, numbers in entries]
        if self.on_changed:
            self.on_changed()

    def all(self) -> list[tuple[str, list[str]]]:
        """Every contact with numbers, sorted by name."""
        return sorted(self._entries, key=lambda e: e[0].casefold())

    def search(self, query: str, limit: int = 5) -> list[tuple[str, str]]:
        """[(name, number)] whose name contains the query, or whose number contains its digits
        (3 or more). Names starting with it come first, then a word starting with it."""
        q = " ".join(query.split()).casefold()
        dq = digits(query)
        by_number = len(dq) >= 3 and not any(ch.isalpha() for ch in q)
        if not q:
            return []
        hits = []
        for name, numbers in self._entries:
            low = name.casefold()
            if by_number:
                rank = None
            elif low.startswith(q):
                rank = 0
            elif any(w.startswith(q) for w in low.split()):
                rank = 1
            elif q in low:
                rank = 2
            else:
                rank = None
            seen = set()
            for num in numbers:
                key = match_key(num)
                if key in seen:
                    continue
                seen.add(key)
                if rank is not None:
                    hits.append((rank, low, name, num))
                elif by_number and (dq in digits(num) or dq in key or key in dq and len(key) >= 7):
                    hits.append((3, low, name, num))
        hits.sort(key=lambda h: (h[0], h[1]))
        return [(name, num) for _r, _l, name, num in hits[:limit]]

    def load(self, address) -> float:
        """Loads the cached list; returns its age in seconds (inf when there's none)."""
        try:
            data = json.loads(self._file(address).read_text())
            self.set_entries(data["entries"])
            return time.time() - data.get("fetched", 0)
        except (OSError, ValueError, KeyError):
            return float("inf")

    def save(self, address, entries):
        path = self._file(address)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"address": address, "fetched": time.time(), "entries": entries}, f)
            path.with_suffix(".tmp").replace(path)
        except OSError as e:
            log.warning("can't save contacts: %s", e)

    def forget(self):
        for f in self.folder.glob("contacts-*.json"):
            f.unlink(missing_ok=True)
        self.set_entries([])

    def refresh(self, address: str, force: bool = False):
        if address != self._address:
            self._address = address
            self._index = {}
        age = self.load(address)
        if (age < REFRESH_AFTER_S and not force) or self._busy:
            return
        self._busy = True
        try:
            conn = self.conn or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as e:
            log.warning("contacts: %s", e.message)
            self._busy = False
            return
        PbapPull(conn, address, lambda text: self._pulled(address, text)).start()

    def _pulled(self, address, text):
        self._busy = False
        if text is None:
            return
        entries = parse_vcards(text)
        log.info("read %d contacts from the phone", len(entries))
        self.save(address, entries)
        if address == self._address:
            self.set_entries(entries)


class PbapPull:
    """CreateSession(pbap) → Select(int, pb) → PullAll → wait for the transfer → read the file."""

    def __init__(self, conn, address, done):
        self.conn, self.address, self.done = conn, address, done
        self.session = None
        self.sub = 0

    def _call(self, path, iface, method, params, reply_type, then):
        def finish(c, res):
            try:
                then(c.call_finish(res).unpack())
            except GLib.Error as e:
                log.warning("contacts: %s: %s", method, error_text(e))
                self._finish(None)
        self.conn.call(OBEX, path, iface, method, params,
                       GLib.VariantType(reply_type) if reply_type else None,
                       Gio.DBusCallFlags.NONE, 120_000, None, finish)

    def start(self):
        args = GLib.Variant("(sa{sv})", (self.address, {"Target": GLib.Variant("s", "pbap")}))
        self._call(OBEX_PATH, "org.bluez.obex.Client1", "CreateSession", args, "(o)", self._created)

    def _created(self, out):
        self.session = out[0]
        self._call(self.session, "org.bluez.obex.PhonebookAccess1", "Select",
                   GLib.Variant("(ss)", ("int", "pb")), None, self._selected)

    def _selected(self, _out):
        filters = {"Format": GLib.Variant("s", "vcard30"),
                   "Fields": GLib.Variant("as", ["VERSION", "FN", "N", "TEL"])}
        self._call(self.session, "org.bluez.obex.PhonebookAccess1", "PullAll",
                   GLib.Variant("(sa{sv})", ("", filters)), "(oa{sv})", self._pulling)

    def _pulling(self, out):
        transfer, props = out
        self.filename = props.get("Filename", "")
        self.sub = self.conn.signal_subscribe(OBEX, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                              transfer, None, Gio.DBusSignalFlags.NONE, self._changed)

    def _changed(self, _c, _s, _p, _i, _n, params):
        _iface, changed, _inv = params.unpack()
        status = changed.get("Status")
        if status == "complete":
            try:
                text = Path(self.filename).read_text(encoding="utf-8", errors="replace")
                Path(self.filename).unlink(missing_ok=True)
            except OSError as e:
                log.warning("contacts file %s: %s", self.filename, e)
                text = None
            self._finish(text)
        elif status == "error":
            log.warning("contacts: the phone refused or the transfer failed "
                        "(allow contact sharing for this computer on the phone)")
            self._finish(None)

    def _finish(self, text):
        if self.sub:
            self.conn.signal_unsubscribe(self.sub)
            self.sub = 0
        if self.session:
            self.conn.call(OBEX, OBEX_PATH, "org.bluez.obex.Client1", "RemoveSession",
                           GLib.Variant("(o)", (self.session,)), None, Gio.DBusCallFlags.NONE, -1, None, None)
            self.session = None
        done, self.done = self.done, None
        if done:
            done(text)


def number_from_uri(uri: str) -> str | None:
    """'tel:+972-50-123-4567;phone-context=…' (or callto:) → '+972-50-123-4567'; None if not one."""
    from urllib.parse import unquote
    m = re.match(r"^(?:tel|callto):(?://)?([^;?]*)", uri.strip(), re.IGNORECASE)
    if not m:
        return None
    number = unquote(m.group(1)).strip()
    return number if re.search(r"\d", number) else None
