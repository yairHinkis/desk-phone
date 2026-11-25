"""The contacts window: everything read from the phone, searchable; click a number to call it."""

from gi.repository import Gtk, Pango

from .. import APP_NAME
from ..contacts import digits
from ..controller import format_number


class ContactsWindow(Gtk.Window):
    def __init__(self, get_contacts, on_call, refresh=None):
        super().__init__(title=f"Contacts — {APP_NAME}")
        self.get_contacts, self.on_call, self.refresh = get_contacts, on_call, refresh
        self.set_default_size(400, 560)
        self.set_keep_above(True)
        self.set_position(Gtk.WindowPosition.CENTER)

        header = Gtk.HeaderBar(show_close_button=True, title="Contacts")
        if refresh:
            again = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
            again.set_tooltip_text("Read the contacts from the phone again")
            again.connect("clicked", self._on_refresh)
            header.pack_end(again)
        self.set_titlebar(header)
        self.header = header

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        self.search = Gtk.SearchEntry(placeholder_text="Search by name or number")
        self.search.connect("search-changed", lambda *_: self.list.invalidate_filter())
        box.pack_start(self.search, False, False, 0)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.set_filter_func(self._filter)
        self.empty = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, margin=24)
        self.empty.get_style_context().add_class("dim-label")
        self.list.set_placeholder(self.empty)
        self.empty.show()
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.add(self.list)
        box.pack_start(scroll, True, True, 0)
        self.add(box)
        self.fill()
        self.show_all()
        self.search.grab_focus()

    def fill(self):
        for row in self.list.get_children():
            row.destroy()
        contacts = self.get_contacts()
        self.header.set_subtitle(f"{len(contacts)} from the phone" if contacts else "")
        self.empty.set_text("No contacts yet.\n\nOn the phone, turn on “Contact sharing” for this "
                            "computer in its Bluetooth settings (and allow access if it asks), "
                            "then press ⟳ above." if not contacts else "No match.")
        for name, numbers in contacts:
            row = Gtk.ListBoxRow(activatable=False)
            row.key = (name.casefold(), " ".join(digits(n) for n in numbers))
            line = Gtk.Box(spacing=8, margin_top=4, margin_bottom=4, margin_start=4, margin_end=4)
            label = Gtk.Label(label=name, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            line.pack_start(label, True, True, 0)
            nums = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for n in numbers:
                b = Gtk.Button()
                b.set_relief(Gtk.ReliefStyle.NONE)
                inner = Gtk.Box(spacing=6)
                inner.pack_start(Gtk.Label(label=format_number(n)), False, False, 0)
                inner.pack_start(Gtk.Image.new_from_icon_name("call-start-symbolic", Gtk.IconSize.BUTTON),
                                 False, False, 0)
                b.add(inner)
                b.set_tooltip_text(f"Call {name}")
                b.connect("clicked", lambda _b, num=n: self.on_call(num))
                nums.pack_start(b, False, False, 0)
            line.pack_start(nums, False, False, 0)
            row.add(line)
            self.list.add(row)
        self.list.show_all()

    def _filter(self, row):
        q = self.search.get_text().strip()
        if not q:
            return True
        name, nums = row.key
        dq = digits(q)
        return q.casefold() in name or (len(dq) >= 2 and dq in nums)

    def _on_refresh(self, *_):
        if self.refresh:
            self.refresh()
        self.fill()
