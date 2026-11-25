"""The computer's own address book: what GNOME Contacts shows (evolution-data-server).

Used instead of the phone's contacts for the dialer's matches when that's turned on in the
settings. Read in a thread (the address books answer over D-Bus) and again when it's older
than a minute, so new contacts show up without restarting.
"""

import logging
import threading
import time

from gi.repository import Gio, GLib

from .contacts import Contacts

log = logging.getLogger(__name__)

GNOME_CONTACTS = "org.gnome.Contacts.desktop"
MAX_AGE_S = 60


def _modules():
    import gi
    gi.require_version("EDataServer", "1.2")
    gi.require_version("EBook", "1.2")
    gi.require_version("EBookContacts", "1.2")
    from gi.repository import EBook, EBookContacts, EDataServer
    return EDataServer, EBook, EBookContacts


def available() -> bool:
    """Whether evolution-data-server's Python bindings are installed (they are on Fedora GNOME)."""
    try:
        _modules()
        return True
    except (ImportError, ValueError):
        return False


def gnome_contacts_app():
    """GNOME Contacts' app entry, or None if it isn't installed."""
    try:
        return Gio.DesktopAppInfo.new(GNOME_CONTACTS)
    except TypeError:
        return None


def tel_numbers(EBookContacts, contact) -> list[str]:
    """A contact's phone numbers. Called through EVCard on purpose: newer evolution-data-server
    (Fedora 44) exposes Contact.get_attributes() as EVCard's, with no field argument, while
    older ones (Debian 12) take the field; the vCard's own attribute list works on both."""
    return [a.get_value() or "" for a in EBookContacts.VCard.get_attributes(contact)
            if (a.get_name() or "").upper() == "TEL"]


def read_address_books() -> list[tuple[str, list[str]]]:
    """[(name, [numbers])] of every contact with a phone number, from every enabled address book."""
    EDataServer, EBook, EBookContacts = _modules()
    registry = EDataServer.SourceRegistry.new_sync(None)
    entries = []
    for source in registry.list_enabled(EDataServer.SOURCE_EXTENSION_ADDRESS_BOOK):
        try:
            client = EBook.BookClient.connect_sync(source, 10, None)
            _ok, found = client.get_contacts_sync('(exists "tel")', None)
        except GLib.Error as e:
            log.info("address book %s: %s", source.get_display_name(), e.message)
            continue
        for c in found or []:
            numbers = tel_numbers(EBookContacts, c)
            numbers = [n.strip() for n in numbers if n and n.strip()]
            name = (c.get_property("full-name") or c.get_property("file-as") or "").strip()
            if numbers:
                entries.append((name or numbers[0], numbers))
    return entries


class DesktopContacts(Contacts):
    def __init__(self, reader=read_address_books):
        super().__init__()
        self.reader = reader
        self._loaded_at = None
        self._loading = False

    def refresh(self, force=False):
        if self._loading:
            return
        if not force and self._loaded_at is not None and time.monotonic() - self._loaded_at < MAX_AGE_S:
            return
        self._loading = True
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            entries = self.reader()
        except Exception as e:                      # bindings missing, no address book service…
            log.warning("can't read GNOME Contacts: %s", e)
            entries = None
        GLib.idle_add(self._read_done, entries)

    def _read_done(self, entries):
        self._loading = False
        self._loaded_at = time.monotonic()
        if entries is not None:
            log.info("%d contacts with numbers in GNOME Contacts", len(entries))
            self.set_entries(entries)
        return False


# -- copying the phone's contacts into an address book ------------------------------------------
def _vcard_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def to_vcard(name: str, numbers: list[str]) -> str:
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"FN:{_vcard_text(name)}", f"N:;{_vcard_text(name)};;;"]
    lines += [f"TEL;TYPE=CELL:{_vcard_text(n)}" for n in numbers]
    lines.append("END:VCARD")
    return "\r\n".join(lines)


def missing_from(entries, existing) -> list[tuple[str, list[str]]]:
    """The entries none of whose numbers is already in `existing` (other address books' entries),
    each once."""
    from .contacts import match_key
    have = {match_key(n) for _name, nums in existing for n in nums}
    out = []
    for name, nums in entries:
        keys = {match_key(n) for n in nums} - {""}
        if keys and not keys & have:
            out.append((name, list(nums)))
            have |= keys
    return out


def writable_books() -> list[tuple[str, str, bool]]:
    """[(uid, label, online)] of the address books contacts can be added to; online ones (a Google
    account from GNOME Online Accounts…) first. Blocking: run it in a thread."""
    EDataServer, EBook, _ = _modules()
    registry = EDataServer.SourceRegistry.new_sync(None)
    books = []
    for source in registry.list_enabled(EDataServer.SOURCE_EXTENSION_ADDRESS_BOOK):
        try:
            client = EBook.BookClient.connect_sync(source, 10, None)
        except GLib.Error as e:
            log.info("address book %s: %s", source.get_display_name(), e.message)
            continue
        if client.is_readonly():
            continue
        parent = registry.ref_source(source.get_parent()) if source.get_parent() else None
        online = parent is not None and source.get_parent() not in ("local-stub", "system-address-book")
        label = source.get_display_name()
        if online and parent.get_display_name() and parent.get_display_name() != label:
            label = f"{label} ({parent.get_display_name()})"
        books.append((source.get_uid(), label, online))
    books.sort(key=lambda b: (not b[2], b[1].casefold()))
    return books


def add_to_book(uid: str, entries) -> int:
    """Adds the contacts to the address book `uid`; returns how many were added. Blocking."""
    EDataServer, EBook, EBookContacts = _modules()
    registry = EDataServer.SourceRegistry.new_sync(None)
    source = registry.ref_source(uid)
    if source is None:
        raise RuntimeError("that address book is gone")
    client = EBook.BookClient.connect_sync(source, 10, None)
    added = 0
    for name, numbers in entries:
        contact = EBookContacts.Contact.new_from_vcard(to_vcard(name, numbers))
        try:
            try:
                client.add_contact_sync(contact, EBookContacts.BookOperationFlags.NONE, None)
            except TypeError:                       # an evolution-data-server without the flags
                client.add_contact_sync(contact, None)
            added += 1
        except GLib.Error as e:
            log.warning("couldn't add %s: %s", name, e.message)
    return added


def in_thread(fn, done):
    """Runs fn() in a thread and done(result, error) back on the main loop."""
    def run():
        try:
            result, error = fn(), None
        except Exception as e:                      # noqa: BLE001 – shown to the user
            result, error = None, e
        GLib.idle_add(lambda: (done(result, error), False)[1])
    threading.Thread(target=run, daemon=True).start()
