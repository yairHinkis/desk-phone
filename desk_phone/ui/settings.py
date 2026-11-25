"""Desk Phone's settings window."""

from gi.repository import Gtk

from .. import APP_NAME, desktop_contacts
from ..desktop_contacts import gnome_contacts_app
from ..hostsetup import install_hint

POSITION_CHOICES = [("bottom-right", "Bottom right"), ("bottom-left", "Bottom left"),
                    ("top-right", "Top right"), ("top-left", "Top left")]
AUDIO_CHOICES = [("answer", "Where I answer (computer or phone)"),
                 ("computer", "Always on the computer, like a car kit")]


class SettingsWindow(Gtk.Window):
    def __init__(self, config, refresh_contacts=None, forget_contacts=None, autostart_sync=None,
                 phone_contacts=None, copied=None):
        super().__init__(title=f"{APP_NAME} Settings")
        self.config = config
        self.set_default_size(460, -1)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        grid = Gtk.Grid(row_spacing=14, column_spacing=18, margin=22)
        self.add(grid)
        row = 0

        def label(text, sub=None):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            box.pack_start(Gtk.Label(label=text, xalign=0), False, False, 0)
            if sub:
                s = Gtk.Label(label=sub, xalign=0, wrap=True, max_width_chars=44)
                s.get_style_context().add_class("dim-label")
                box.pack_start(s, False, False, 0)
            return box

        combo = Gtk.ComboBoxText()
        for key, text in AUDIO_CHOICES:
            combo.append(key, text)
        combo.set_active_id(config["audio_mode"])
        combo.connect("changed", lambda c: config.set("audio_mode", c.get_active_id()))
        grid.attach(label("Call audio", "Where a call is heard and spoken while the phone is connected."),
                    0, row, 2, 1)
        row += 1
        grid.attach(combo, 0, row, 2, 1)
        row += 1

        def switch(key, text, sub, after=None, sensitive=True):
            nonlocal row
            sw = Gtk.Switch(active=bool(config[key]), valign=Gtk.Align.CENTER, sensitive=sensitive)

            def changed(s, _p):
                config.set(key, s.get_active())
                if after:
                    after(s.get_active())
            sw.connect("notify::active", changed)
            grid.attach(label(text, sub), 0, row, 1, 1)
            grid.attach(sw, 1, row, 1, 1)
            row += 1

        switch("echo_cancel", "Echo cancelling",
               "Stops the caller from hearing themselves through your speakers. Not needed with headphones.")
        switch("ringtone", "Ring on the computer", "The phone rings too, either way.")
        switch("contacts", "Caller names from the phone",
               "Reads the phone's contacts over Bluetooth; they stay on this computer.",
               after=lambda on: (not on) and forget_contacts and forget_contacts())
        switch("call_history", "Recent calls",
               "Keeps the last 50 calls on this computer, for the card's recent-calls button. "
               "Turning it off deletes the list.")
        pos = Gtk.ComboBoxText(valign=Gtk.Align.CENTER)
        for key, text in POSITION_CHOICES:
            pos.append(key, text)
        pos.set_active_id(config["card_position"])
        pos.connect("changed", lambda c: config.set("card_position", c.get_active_id()))
        grid.attach(label("Where the card opens", "Every time it opens, even after you dragged it "
                          "somewhere else."), 0, row, 1, 1)
        grid.attach(pos, 1, row, 1, 1)
        row += 1
        switch("remember_card_position", "Open where I last moved it",
               "Instead of the corner above.")
        switch("autostart", "Start at login", "Desk Phone waits in the background for calls.",
               after=lambda on: autostart_sync and autostart_sync(on))

        have_app = gnome_contacts_app() is not None
        switch("gnome_contacts_button", "Contacts button opens GNOME Contacts",
               "Instead of the list read from the phone. Its call buttons then dial through Desk Phone."
               if have_app else f"GNOME Contacts isn't installed ({install_hint('gnome-contacts')}).",
               sensitive=have_app or config["gnome_contacts_button"])
        have_eds = desktop_contacts.available()
        switch("gnome_contacts_search", "Use GNOME Contacts for search and caller names",
               "The dialer's matches and the names of who's calling come from this computer's "
               "contacts (and its Google account, if added in Online Accounts) instead of the phone's."
               if have_eds else f"Needs evolution-data-server ({install_hint('evolution-data-server')}).",
               sensitive=have_eds or config["gnome_contacts_search"])

        if refresh_contacts:
            b = Gtk.Button(label="Read contacts again", halign=Gtk.Align.START)
            b.connect("clicked", lambda *_: refresh_contacts())
            grid.attach(b, 0, row, 2, 1)
            row += 1

        # copying the phone's contacts into GNOME Contacts (a Google address book syncs on to Google)
        self.phone_contacts, self.copied = phone_contacts, copied
        grid.attach(label("Copy the phone's contacts to GNOME Contacts",
                          "Adds the ones whose numbers aren't in any address book yet; never changes "
                          "or deletes anything. The phone's contacts can only be read over Bluetooth, "
                          "so this goes one way."), 0, row, 2, 1)
        row += 1
        line = Gtk.Box(spacing=8)
        self.books = Gtk.ComboBoxText(hexpand=True)
        self.copy_btn = Gtk.Button(label="Copy")
        self.copy_btn.connect("clicked", self._on_copy)
        line.pack_start(self.books, True, True, 0)
        line.pack_start(self.copy_btn, False, False, 0)
        grid.attach(line, 0, row, 2, 1)
        row += 1
        self.copy_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=50)
        self.copy_status.get_style_context().add_class("dim-label")
        grid.attach(self.copy_status, 0, row, 2, 1)
        row += 1
        self._online = {}
        self.copy_btn.set_sensitive(False)
        if phone_contacts is None:
            self.copy_status.set_text("Not in the demo.")
        elif not have_eds:
            self.copy_status.set_text("Needs evolution-data-server.")
        else:
            self.copy_status.set_text("Finding address books…")
            desktop_contacts.in_thread(desktop_contacts.writable_books, self._got_books)
        self.show_all()

    def _got_books(self, books, error):
        if error is not None or not books:
            self.copy_status.set_text(f"No address book to copy into ({error})." if error
                                      else "No address book to copy into.")
            return
        for uid, label, online in books:
            self.books.append(uid, label + ("  ·  syncs online" if online else ""))
            self._online[uid] = (label, online)
        self.books.set_active(0)               # an online one (Google…) comes first
        self.copy_btn.set_sensitive(True)
        self.copy_status.set_text("")

    def _on_copy(self, *_):
        uid = self.books.get_active_id()
        entries = self.phone_contacts() if self.phone_contacts else []
        if not uid:
            return
        if not entries:
            self.copy_status.set_text("No contacts read from the phone yet (see “Caller names from the phone”).")
            return
        self.copy_btn.set_sensitive(False)
        self.copy_status.set_text("Comparing with the address books…")
        desktop_contacts.in_thread(
            lambda: desktop_contacts.missing_from(entries, desktop_contacts.read_address_books()),
            lambda missing, error: self._confirm(uid, missing, len(entries), error))

    def _confirm(self, uid, missing, total, error):
        if error is not None:
            self.copy_btn.set_sensitive(True)
            self.copy_status.set_text(f"Couldn't read the address books: {error}")
            return
        if not missing:
            self.copy_btn.set_sensitive(True)
            self.copy_status.set_text(f"All {total} of the phone's contacts are already in GNOME Contacts.")
            return
        label, online = self._online.get(uid, ("the address book", False))
        names = ", ".join(f"\u2068{n}\u2069" for n, _ in missing[:5])   # (each name its own direction) + ("…" if len(missing) > 5 else "")
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE,
                                   text=f"Add {len(missing)} contacts to {label}?")
        dialog.format_secondary_text(
            f"{names}\n\n" + ("1 is already there and is skipped." if total - len(missing) == 1
                                else f"{total - len(missing)} are already there and are skipped.")
            + ("\n\nThis address book is online: the contacts will appear in that account "
               "everywhere, e.g. on your phones that sync it." if online else ""))
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Add", Gtk.ResponseType.OK)
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.OK:
            self.copy_btn.set_sensitive(True)
            self.copy_status.set_text("")
            return
        self.copy_status.set_text(f"Adding {len(missing)} contacts…")
        desktop_contacts.in_thread(lambda: desktop_contacts.add_to_book(uid, missing),
                                   lambda added, err: self._copied(added, len(missing), err))

    def _copied(self, added, wanted, error):
        self.copy_btn.set_sensitive(True)
        if error is not None:
            self.copy_status.set_text(f"Couldn't add the contacts: {error}")
        else:
            self.copy_status.set_text(f"Added {added} contacts." if added == wanted
                                      else f"Added {added} of {wanted} contacts; see the log for the rest.")
        if self.copied:
            self.copied()
