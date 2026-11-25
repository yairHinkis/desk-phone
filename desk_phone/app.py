"""The running app: one background process that waits for calls and shows the call card."""

import logging
from pathlib import Path

from gi.repository import Gdk, Gio, GLib, Gtk

from . import APP_ID
from .audio import AudioRouter, FakeRouter
from .levels import FakeLevelMeter, LevelMeter
from .bluez import Bluez
from .config import Config
from .contacts import Contacts, number_from_uri
from .controller import Controller
from .desktop_contacts import DesktopContacts, gnome_contacts_app
from .history import CallHistory
from .hostsetup import TEL_SCHEMES, launcher
from .hostsetup import sync_autostart as write_autostart
from .sounds import Ringer
from .telephony import SERVICE, Telephony
from .ui.card import CallCard
from .ui.theme import install_window_css
from .ui.settings import SettingsWindow

log = logging.getLogger(__name__)

DEMO_SERVICE = f"{APP_ID}.Demo.Telephony"


def make_tel_handler():
    """Desk Phone becomes the app for tel: links, so GNOME Contacts' call buttons dial through it."""
    try:
        info = Gio.DesktopAppInfo.new(f"{APP_ID}.desktop")
    except TypeError:
        info = None
    if info is None:
        log.info("Desk Phone's app entry isn't installed (run desk-phone --setup): can't take tel: links")
        return
    for scheme in TEL_SCHEMES:
        try:
            info.set_as_default_for_type(scheme)
        except GLib.Error as e:
            log.info("tel: links: %s", e.message)


def sync_autostart(enabled: bool):
    """The Settings switch: start at login, through whichever `desk-phone` command is installed."""
    try:
        if enabled and not launcher().exists():
            log.info("autostart: Desk Phone isn't installed (it runs from its folder), so nothing to start")
            return
        write_autostart(enabled)
    except OSError as e:
        log.warning("autostart: %s", e)


class DeskPhoneApp(Gtk.Application):
    def __init__(self, demo=False):
        super().__init__(application_id=APP_ID + (".Demo" if demo else ""),
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.demo = demo
        self.ctrl = None
        self.card = None
        self.settings_win = None
        self.fake = None
        for name, desc in (("background", "Start without showing anything"),
                           ("answer", "Answer the ringing call here, hang up the call, or open the dialer"),
                           ("decline", "Decline the ringing call"),
                           ("mute", "Mute the call, or silence the computer's ringtone"),
                           ("settings", "Open the settings"),
                           ("quit", "Quit the running Desk Phone"),
                           ("demo", "Try the card with a pretend phone"),
                           ("check", "Check that everything is in place for calls"),
                           ("connect", "Ask the paired phone to connect for calls"),
                           ("setup", "Set up the keys, start at login and the PipeWire setting"),
                           ("uninstall", "Remove Desk Phone and what it set up"),
                           ("version", "Show the version"),
                           ("debug", "More detail in the log")):
            self.add_main_option(name, 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, desc, None)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        # the settings and contacts windows follow the Omarchy theme, like the call card
        install_window_css()
        self.hold()                       # keep running with no window: we wait for calls
        self.config = Config()
        if self.demo:
            from .fake_telephony import FakePhone
            self.fake = FakePhone(DEMO_SERVICE)
            tel = Telephony(service=DEMO_SERVICE)
            router = FakeRouter()
            router.on_release = lambda _a: self.fake.release_audio()
            bluez, phone_name = None, (lambda _a: "Demo phone")
            contacts = Contacts(folder=Path(GLib.get_tmp_dir()) / "desk-phone-demo")
            contacts.set_entries([("Mom", ["+972501234567"]), ("דנה כהן", ["052-765-4321"]),
                                  ("Dana Levi", ["054-222-3333", "03-555-1212"]),
                                  ("Dentist", ["03-777-8888"]), ("Dad", ["050-765-4321"])])
            contacts.refresh = lambda *a, **k: None
            history = CallHistory(persist=False)          # made-up calls, never written
            now = GLib.get_real_time() / 1e6
            history.set_entries([
                {"number": "03-777-8888", "kind": "out", "when": now - 40 * 60, "seconds": 95},
                {"number": "+972501234567", "kind": "missed", "when": now - 3 * 3600},
                {"number": "054-222-3333", "kind": "in", "when": now - 26 * 3600, "seconds": 432},
                {"number": "02-555-0199", "kind": "missed", "when": now - 2 * 86400},
                {"number": "050-765-4321", "kind": "out", "when": now - 3 * 86400, "seconds": 0},
                {"number": "052-765-4321", "kind": "declined", "when": now - 9 * 86400}])
        else:
            tel = Telephony(service=SERVICE)
            router = AudioRouter()
            bluez = Bluez()
            phone_name = bluez.alias
            contacts = Contacts()
            history = CallHistory()
        self.card = CallCard(lambda: self.ctrl, open_settings=self.open_settings,
                             open_contacts=self.open_contacts)
        self.contacts_win = None
        self.card.corner = self.config.get("card_corner")
        self.card.on_corner_changed = lambda c: self.config.set("card_corner", c)
        self.card.default_corner = self.config["card_position"]
        self.card.remember_position = self.config["remember_card_position"]
        self.add_window(self.card)
        meter = (FakeLevelMeter if self.demo else LevelMeter)(self.card.set_levels)
        router.on_routed = meter.start
        router.on_stopped = meter.stop
        self.meter = meter
        self.ctrl = Controller(tel, router, Ringer(), contacts, self.config, self.card,
                               phone_name=phone_name, bluez=bluez, history=history)
        self.contacts = contacts
        self.history = history
        if self.demo:
            self.desktop_contacts = DesktopContacts(reader=lambda: [
                ("Grandma Rina", ["+972 3-555-0100"]), ("Daniel (work)", ["+972 54-999-0000"])])
        else:
            self.desktop_contacts = DesktopContacts()
        self.ctrl.desktop_contacts = self.desktop_contacts
        self.desktop_contacts.on_changed = self.ctrl.render
        if self.config["gnome_contacts_search"]:
            self.desktop_contacts.refresh()
        if self.config["gnome_contacts_button"] and not self.demo:
            make_tel_handler()
        self.config.observe(self._on_config)
        if contacts is not None:
            render = contacts.on_changed
            contacts.on_changed = lambda: (render(), self.contacts_win and self.contacts_win.fill())
        for name, fn in (("answer-or-hangup", self.answer_or_dial),
                         ("decline-or-show", self.decline_or_show),
                         ("decline", lambda: self.ctrl.key_decline()),
                         ("mute", lambda: self.ctrl.key_mute()),
                         ("show", self.show_dialer)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, f=fn: f())
            self.add_action(action)
        if self.demo:
            self._demo_script()

    def _demo_script(self):
        def connect():
            self.fake.connect_gateway()
            return False

        def ring():
            self.fake.incoming("+972501234567")
            return False
        GLib.timeout_add(300, connect)
        GLib.timeout_add(1500, ring)

    def _on_config(self, key, value):
        if key == "card_position":
            self.card.default_corner = value
            if not self.card.remember_position:
                self.card.corner = None
                self.card.place()              # (it moves there now if it's open)
        elif key == "remember_card_position":
            self.card.remember_position = value
        if key == "gnome_contacts_search" and value:
            self.desktop_contacts.refresh(force=True)
        elif key == "gnome_contacts_button" and value and not self.demo:
            make_tel_handler()

    def do_command_line(self, cmdline):
        opts = cmdline.get_options_dict().end().unpack()
        numbers = [n for n in map(number_from_uri, cmdline.get_arguments()[1:]) if n]
        if numbers:                       # a tel: link, e.g. a call button in GNOME Contacts
            self.ctrl.dial(numbers[0])
            self.show_dialer()
        elif opts.get("quit"):
            self.quit_app()
        elif opts.get("answer"):
            self.answer_or_dial()
        elif opts.get("decline"):
            self.ctrl.key_decline()
        elif opts.get("mute"):
            self.ctrl.key_mute()
        elif opts.get("settings"):
            self.open_settings()
        elif not opts.get("background"):
            self.show_dialer()
        return 0

    def answer_or_dial(self):
        """The phone key: answer / hang up; with no call it opens the dialer, or closes it."""
        if self.ctrl.key_answer_or_hangup():
            return
        m = self.card.model
        if self.card.get_visible() and m is not None and m.mode in ("idle", "ended"):
            self.ctrl.dismiss()
        else:
            self.show_dialer()

    def decline_or_show(self):
        if not self.ctrl.key_decline_or_show() and self.ctrl.calls():
            self.show_dialer()            # in a call: bring the call card forward

    def open_contacts(self):
        if self.config["gnome_contacts_button"]:
            app = gnome_contacts_app()
            if app is not None:
                display = Gdk.Display.get_default()
                app.launch([], display.get_app_launch_context() if display else None)
                return
            log.warning("GNOME Contacts isn't installed; showing Desk Phone's contacts")
        from .ui.contacts_window import ContactsWindow
        if self.contacts_win is None:
            def call(number):
                self.contacts_win.destroy()
                self.ctrl.dial(number)
            self.contacts_win = ContactsWindow(
                self.ctrl.all_contacts, call,
                refresh=lambda: self.ctrl.gw and self.contacts.refresh(self.ctrl.gw.address, force=True))
            self.contacts_win.connect("destroy", lambda *_: setattr(self, "contacts_win", None))
            self.add_window(self.contacts_win)
        self.contacts_win.present()

    def show_dialer(self):
        """The card with the dialer (or the call in progress), with the keyboard in it."""
        self.ctrl.show_status()
        if self.config["gnome_contacts_search"]:
            self.desktop_contacts.refresh()      # (only when it's older than a minute)
        if self.card.get_visible():
            self.card.focus_now()
            if self.card.model and self.card.model.mode == "idle":
                self.card.entry.grab_focus()

    def open_settings(self):
        if self.settings_win is None:
            self.settings_win = SettingsWindow(
                self.config,
                refresh_contacts=lambda: self.ctrl.gw and self.contacts.refresh(self.ctrl.gw.address, force=True),
                forget_contacts=self.contacts.forget,
                autostart_sync=sync_autostart,
                # (not in the demo: its made-up contacts mustn't land in a real address book)
                phone_contacts=None if self.demo else self.contacts.all,
                copied=lambda: self.desktop_contacts.refresh(force=True))
            self.settings_win.connect("destroy", lambda *_: setattr(self, "settings_win", None))
            self.add_window(self.settings_win)
        self.settings_win.present()

    def quit_app(self):
        if self.ctrl is not None:
            self.ctrl.shutdown()
        # let the last D-Bus calls (RejectSCO back on) go out before leaving
        GLib.timeout_add(300, lambda: (self.release(), self.quit(), False)[2])
