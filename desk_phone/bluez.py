"""BlueZ (system bus): the phone's name, and connecting its hands-free profile from here."""

import logging

from gi.repository import Gio, GLib

from .telephony import error_text

log = logging.getLogger(__name__)

HFP_AG = "0000111f-0000-1000-8000-00805f9b34fb"   # the phone's side of hands-free calls
HFP_HF = "0000111e-0000-1000-8000-00805f9b34fb"   # the computer's side (PipeWire registers it)
PBAP_PSE = "0000112f-0000-1000-8000-00805f9b34fb" # the phone shares its contacts


class Bluez:
    def __init__(self, connection=None):
        self._conn = connection

    @property
    def conn(self):
        if self._conn is None:
            self._conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        return self._conn

    def objects(self) -> dict:
        try:
            r = self.conn.call_sync("org.bluez", "/", "org.freedesktop.DBus.ObjectManager",
                                    "GetManagedObjects", None, None, Gio.DBusCallFlags.NONE, 3000, None)
            return r.unpack()[0]
        except GLib.Error as e:
            log.debug("bluez: %s", e.message)
            return {}

    def adapters(self) -> list[dict]:
        return [dict(ifs["org.bluez.Adapter1"], path=p) for p, ifs in self.objects().items()
                if "org.bluez.Adapter1" in ifs]

    def devices(self) -> list[dict]:
        return [dict(ifs["org.bluez.Device1"], path=p) for p, ifs in self.objects().items()
                if "org.bluez.Device1" in ifs]

    def phones(self) -> list[dict]:
        """Paired devices that can send calls (they offer the hands-free gateway)."""
        return [d for d in self.devices() if d.get("Paired") and HFP_AG in (d.get("UUIDs") or [])]

    def alias(self, address: str) -> str:
        for d in self.devices():
            if d.get("Address", "").upper() == (address or "").upper():
                return d.get("Alias") or d.get("Name") or ""
        return ""

    def connect_phones(self, done):
        """ConnectProfile(HFP AG) on each paired phone; done(message) with what happened."""
        phones = self.phones()
        if not phones:
            done("No paired phone offers calls. Pair the phone in Settings → Bluetooth first.")
            return
        pending = {"n": len(phones), "ok": [], "err": []}

        def finish(phone):
            def cb(conn, res):
                name = phone.get("Alias") or phone.get("Address")
                try:
                    conn.call_finish(res)
                    pending["ok"].append(name)
                except GLib.Error as e:
                    pending["err"].append(f"{name}: {error_text(e)}")
                pending["n"] -= 1
                if pending["n"] == 0:
                    if pending["ok"]:
                        done(f"Connected {', '.join(pending['ok'])}.")
                    else:
                        done("The phone refused: " + "; ".join(pending["err"])
                             + ". On the phone, turn on “Phone calls” for this computer.")
            return cb

        for p in phones:
            self.conn.call("org.bluez", p["path"], "org.bluez.Device1", "ConnectProfile",
                           GLib.Variant("(s)", (HFP_AG,)), None, Gio.DBusCallFlags.NONE, 30_000,
                           None, finish(p))
