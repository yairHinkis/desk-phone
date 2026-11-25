"""What happens when the phone rings: the call card's contents, the ringtone, where the audio goes.

The controller doesn't draw anything. It turns the telephony state into a CardModel for the
view and turns the card's buttons into telephony and audio commands.

Where the audio goes: PipeWire's RejectSCO flag decides whether the phone may move the call's
audio to the computer. In "answer" mode the flag stays on until a call is answered (or dialed)
here; then it's turned off and the phone is asked to send the audio over (Activate). "To phone"
turns it back on and lets go of the audio link, so the phone takes the call back.
"""

import logging
import re
import time
from dataclasses import dataclass

from gi.repository import GLib

from .telephony import (ACTIVE, ALERTING, DIALING, DISCONNECTED, HELD, INCOMING, WAITING, Call,
                        Gateway, Telephony)

log = logging.getLogger(__name__)

ENDED_SHOW_S = 2.5
ERROR_SHOW_S = 6.0
ENSURE_AUDIO_AFTER_MS = 2000   # after answering/dialing here, ask for the audio if it hasn't come
CONNECTING_MAX_S = 8.0         # after that, show the audio where it really is
MAX_TONES_SHOWN = 24
DIAL_CHARS = re.compile(r"[^0-9+*#]")


@dataclass
class CardModel:
    mode: str                  # idle | ringing | call | ended
    title: str = ""
    subtitle: str = ""
    status: str = ""
    audio: str = ""            # computer | phone | connecting
    muted: bool = False
    tones: str = ""
    waiting: str | None = None # caller waiting while in a call
    held: str | None = None    # a second call on hold
    phone_name: str = ""
    phone_ready: bool = False
    hint: str = ""
    error: str = ""
    can_dial: bool = False
    recents: bool = False      # the recent-calls list is on: the card shows its button
    focus: bool = False        # the call was just answered here: give the card the keyboard


def format_number(number: str) -> str:
    """Readable phone numbers: +972 50-123-4567, 050-123-4567; others as they come."""
    n = number.strip()
    m = re.fullmatch(r"\+972(\d{1,2})(\d{3})(\d{4})", n)
    if m:
        return f"+972 {m[1]}-{m[2]}-{m[3]}"
    m = re.fullmatch(r"0(\d{1,2})(\d{3})(\d{4})", n)
    if m:
        return f"0{m[1]}-{m[2]}-{m[3]}"
    return n


def known_number(number: str) -> str:
    """The number, or "" when the caller hid it."""
    n = (number or "").strip()
    return "" if n.lower() in ("withheld", "unknown", "private") else n


def format_duration(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Controller:
    def __init__(self, tel: Telephony, router, ringer, contacts, config, view,
                 phone_name=lambda address: "", bluez=None, clock=time.monotonic,
                 history=None, now=time.time):
        self.tel, self.router, self.ringer = tel, router, ringer
        self.contacts, self.config, self.view = contacts, config, view
        self.history, self.now = history, now     # recent calls (history.CallHistory), wall clock
        self.desktop_contacts = None    # GNOME Contacts, for the dialer when the setting says so
        self.phone_name, self.bluez, self.clock = phone_name, bluez, clock
        self.gw: Gateway | None = None
        self.idle_requested = False
        self.want_here = False         # the user wants this call's audio on the computer
        self.want_since = 0.0
        self.dialing_here = False
        self.muted = False
        self.tones = ""
        self.dismissed: set[str] = set()
        self.silenced: set[str] = set()       # ringing calls whose computer ringtone was hushed
        self.focus_request = False            # answered here: the call card should take the keyboard
        self.ended: CardModel | None = None
        self.error = ("", 0.0)
        self.hint_override = ""
        self._timers: dict[str, int] = {}
        self._last_primary: Call | None = None
        self._shown_calls: set[str] = set()   # calls the card showed (for "Call ended"/"Missed")

        tel.connect("service", lambda *_: self.render())
        tel.connect("gateway-added", self._on_gateway_added)
        tel.connect("gateway-changed", self._on_gateway_changed)
        tel.connect("gateway-removed", self._on_gateway_removed)
        tel.connect("call-added", self._on_call_added)
        tel.connect("call-changed", self._on_call_changed)
        tel.connect("call-removed", self._on_call_removed)
        tel.connect("error", self._on_error)
        config.observe(self._on_config)
        if contacts is not None:
            contacts.on_changed = self.render

    # -- timers ---------------------------------------------------------------------------------
    def _later(self, name, ms, fn):
        self._cancel(name)

        def fire():
            self._timers.pop(name, None)
            fn()
            return False
        self._timers[name] = GLib.timeout_add(ms, fire)

    def _cancel(self, name):
        tid = self._timers.pop(name, None)
        if tid:
            GLib.source_remove(tid)

    def shutdown(self):
        """On quit: never leave the phone's calls coming to a computer nobody watches."""
        for name in list(self._timers):
            self._cancel(name)
        self.ringer.stop()
        self.router.stop()
        for gw in self.tel.gateways.values():
            if not gw.reject_sco and not self.tel.calls_of(gw):
                self.tel.set_reject_sco(gw, True)

    # -- state ----------------------------------------------------------------------------------
    def calls(self) -> list[Call]:
        """The calls still going (a hung-up call shows as disconnected before it disappears)."""
        calls = self.tel.calls.values() if self.gw is None else self.tel.calls_of(self.gw)
        return [c for c in calls if c.state != DISCONNECTED]

    def _by_state(self):
        out = {}
        for c in self.calls():
            out.setdefault(c.state, []).append(c)
        return out

    def ringing_call(self) -> Call | None:
        s = self._by_state()
        busy = s.get(ACTIVE) or s.get(DIALING) or s.get(ALERTING) or s.get(HELD)
        if s.get(INCOMING) and not busy:
            return s[INCOMING][0]
        return None

    def primary_call(self) -> Call | None:
        s = self._by_state()
        for state in (ACTIVE, DIALING, ALERTING, HELD, INCOMING, WAITING):
            if s.get(state):
                return s[state][0]
        return None

    def waiting_call(self) -> Call | None:
        if self.ringing_call():
            return None
        s = self._by_state()
        cands = s.get(WAITING, []) + s.get(INCOMING, [])
        return cands[0] if cands else None

    def display_name(self, call: Call) -> tuple[str, str]:
        """(title, subtitle) for a call: contact name and number, or the number alone."""
        number = known_number(call.number)
        if not number:
            return (call.name or "Unknown caller"), ""
        name = self.contact_name(number, call.name)
        if name:
            return name, format_number(number)
        return format_number(number), ""

    def contact_name(self, number: str, fallback: str = "") -> str:
        """The contact with that number, or the fallback (the name the network sent, if any)."""
        name = None
        if self.config["gnome_contacts_search"] and self.desktop_contacts is not None:
            name = self.desktop_contacts.lookup(number)     # GNOME Contacts first, then the phone's
        if not name and self.contacts is not None:
            name = self.contacts.lookup(number)
        return name or fallback or ""

    def audio_where(self) -> str:
        gw = self.gw
        if gw is None:
            return ""
        if gw.transport_state == "active":
            return "computer"
        if gw.transport_state == "pending":
            return "connecting"
        if self.want_here and self.clock() - self.want_since < CONNECTING_MAX_S:
            return "connecting"
        return "phone"

    def model(self) -> CardModel | None:
        calls = self.calls()
        if not calls:
            if self.ended is not None:
                return self.ended
            if self.idle_requested:
                return self._idle_model()
            return None
        if all(c.path in self.dismissed for c in calls):
            return None
        error = self.error[0] if self.clock() < self.error[1] else ""
        ringing = self.ringing_call()
        if ringing is not None:
            title, subtitle = self.display_name(ringing)
            status = "Incoming call · silenced" if ringing.path in self.silenced else "Incoming call"
            return CardModel("ringing", title, subtitle, status, self.audio_where(),
                             phone_name=self._phone_label(), error=error)
        call = self.primary_call()
        title, subtitle = self.display_name(call)
        status = {
            DIALING: "Calling…",
            ALERTING: "Ringing…",
            HELD: "On hold",
            ACTIVE: format_duration(self.clock() - call.extra.get("since", self.clock())),
        }.get(call.state, "")
        if call.multiparty:
            title = "Conference call"
        waiting = self.waiting_call()
        held = next((c for c in calls if c.state == HELD and c is not call), None)
        return CardModel(
            "call", title, subtitle, status, self.audio_where(), self.muted,
            self.tones[-MAX_TONES_SHOWN:],
            waiting=self.display_name(waiting)[0] if waiting else None,
            held=self.display_name(held)[0] if held else None,
            phone_name=self._phone_label(), error=error, focus=self.focus_request)

    def _phone_label(self) -> str:
        if self.gw is None:
            return ""
        return self.phone_name(self.gw.address) or self.gw.address or "Phone"

    def _idle_model(self) -> CardModel:
        error = self.error[0] if self.clock() < self.error[1] else ""
        recents = self.history is not None and bool(self.config["call_history"])
        if not self.tel.available:
            return CardModel("idle", "Desk Phone", "", "Not ready", phone_ready=False, error=error,
                             recents=recents,
                             hint="PipeWire's call service isn't running. Desk Phone needs "
                                  "PipeWire 1.4 or newer; `desk-phone --check` says more.")
        if self.gw is None:
            return CardModel("idle", "Desk Phone", "", "No phone connected", phone_ready=False,
                             error=error, recents=recents, hint=self.hint_override or (
                                 "Turn on “Phone calls” for this computer in the phone's "
                                 "Bluetooth settings, then press Connect."))
        name = self._phone_label()
        return CardModel("idle", "Desk Phone", "", f"Connected to {name}", phone_name=name,
                         phone_ready=True, can_dial=True, error=error, recents=recents,
                         hint=f"Calls to {name} ring here. Type a name or number to call.")

    # -- rendering ------------------------------------------------------------------------------
    def render(self):
        m = self.model()
        self.view.render(m)
        if m is not None and m.mode == "call" and m.focus:
            self.focus_request = False
        if m is not None:
            self._shown_calls.update(c.path for c in self.calls() if c.path not in self.dismissed)
        ticking = m is not None and m.mode == "call" and any(
            c.state == ACTIVE for c in self.calls())
        if ticking and "tick" not in self._timers:
            self._tick()
        elif not ticking:
            self._cancel("tick")
        self._update_ringer()

    def _tick(self):
        self._later("tick", 1000, self._tick_fire)

    def _tick_fire(self):
        self.render()
        if "tick" not in self._timers and any(c.state == ACTIVE for c in self.calls()):
            self._tick()

    def _update_ringer(self):
        ringing = self.ringing_call()
        waiting = self.waiting_call()
        in_band = self.gw is not None and self.gw.transport_state == "active"
        if ringing is not None and ringing.path not in self.dismissed:
            if self.config["ringtone"] and not in_band and ringing.path not in self.silenced:
                self.ringer.ring()
            else:
                self.ringer.stop()
        elif waiting is not None and waiting.path not in (self.dismissed | self.silenced):
            self.ringer.waiting()
        else:
            self.ringer.stop()

    # -- telephony events -----------------------------------------------------------------------
    def _on_gateway_added(self, _tel, gw: Gateway):
        if self.gw is None:
            self.gw = gw
        self.hint_override = ""
        self._apply_audio_policy(gw)
        if self.contacts is not None and self.config["contacts"] and gw.address:
            self.contacts.refresh(gw.address)
        self.render()

    def _on_gateway_changed(self, _tel, gw: Gateway):
        if gw is self.gw:
            if gw.transport_state == "active":
                if not self.router.active:
                    self.router.start(gw.address, muted=self.muted,
                                      echo_cancel=self.config["echo_cancel"])
            elif self.router.active:
                self.router.stop()
        self.render()

    def _on_gateway_removed(self, tel, gw: Gateway):
        if gw is self.gw:
            self.gw = tel.first_gateway()
            self.router.stop()
            self._reset_call_state()
        self.render()

    def _on_call_added(self, _tel, call: Call):
        if self.config["gnome_contacts_search"] and self.desktop_contacts is not None:
            self.desktop_contacts.refresh()     # if it's old; the name updates when it's read
        call.extra["last_state"] = call.state
        call.extra["started"] = self.now()
        call.extra["direction"] = ("in" if call.state in (INCOMING, WAITING) else
                                   "out" if call.state in (DIALING, ALERTING) else "")
        if call.state == ACTIVE:
            call.extra["since"] = self.clock()
        if call.state in (DIALING, ALERTING) and self.dialing_here:
            call.extra["dialed_here"] = True
            self.dialing_here = False
            self._later("ensure-audio", ENSURE_AUDIO_AFTER_MS, self._ensure_audio_here)
        if self.config["audio_mode"] == "computer" and call.state != INCOMING:
            self._want_audio_here()
        self.ended = None
        self._cancel("ended")
        self.render()

    def _on_call_changed(self, _tel, call: Call):
        if call.state != DISCONNECTED:
            call.extra["last_state"] = call.state
        if call.state == ACTIVE and "since" not in call.extra:
            call.extra["since"] = self.clock()
            if call.extra.get("answered_here") or call.extra.get("dialed_here"):
                self._later("ensure-audio", ENSURE_AUDIO_AFTER_MS, self._ensure_audio_here)
        self.render()

    def _on_call_removed(self, _tel, call: Call):
        self._record(call)
        was_shown = call.path in self._shown_calls and call.path not in self.dismissed
        self._shown_calls.discard(call.path)
        self.dismissed.discard(call.path)
        self.silenced.discard(call.path)
        remaining = self.calls()
        if not remaining:
            if was_shown:
                title, subtitle = self.display_name(call)
                if "since" in call.extra:
                    status = "Call ended · " + format_duration(self.clock() - call.extra["since"])
                elif call.extra.get("declined_here"):
                    status = "Declined"
                elif call.extra.get("last_state") in (INCOMING, WAITING):
                    status = "Missed call"
                else:
                    status = "Call ended"
                self.ended = CardModel("ended", title, subtitle, status)
                self._later("ended", int(ENDED_SHOW_S * 1000), self._end_shown)
            self._reset_call_state()
            if self.gw is not None:
                self._apply_audio_policy(self.gw)
        self.render()

    def _record(self, call: Call):
        """Adds a finished call to the recent calls. Never lets a problem there touch the call."""
        if self.history is None or not self.config["call_history"]:
            return
        try:
            answered = "since" in call.extra
            direction = call.extra.get("direction", "")
            if direction == "in":
                kind = ("in" if answered else
                        "declined" if call.extra.get("declined_here") else "missed")
            else:
                kind = "out" if direction == "out" else "call"
            number = known_number(call.number)
            self.history.add(number, self.contact_name(number, call.name) if number else call.name,
                             kind, call.extra.get("started", self.now()),
                             self.clock() - call.extra["since"] if answered else 0)
        except Exception:  # noqa: BLE001
            log.exception("couldn't add the call to the recent calls")

    def recent_calls(self) -> list[dict]:
        """The recent calls, newest first, each with the name its number has now (`title`)."""
        if self.history is None or not self.config["call_history"]:
            return []
        out = []
        for e in self.history.all():
            number = e["number"]
            name = (self.contact_name(number, e["name"]) if number else e["name"])
            out.append(dict(e, title=name or format_number(number) or "Unknown caller",
                            named=bool(name)))
        return out

    def _end_shown(self):
        self.ended = None
        self.render()

    def _on_error(self, _tel, what, message):
        self.error = (f"Couldn't {what}: {message}", self.clock() + ERROR_SHOW_S)
        self.render()
        self._later("error", int(ERROR_SHOW_S * 1000) + 50, self.render)

    def _on_config(self, key, value):
        if key == "audio_mode" and self.gw is not None and not self.calls():
            self._apply_audio_policy(self.gw)
        elif key == "contacts" and value and self.gw is not None and self.contacts is not None:
            self.contacts.refresh(self.gw.address, force=True)
        elif key == "call_history" and not value and self.history is not None:
            self.history.clear()                 # turning the list off forgets it
        self.render()

    def _reset_call_state(self):
        self.want_here = False
        self.dialing_here = False
        self.muted = False
        self.tones = ""
        self.focus_request = False
        self._cancel("ensure-audio")

    def _apply_audio_policy(self, gw: Gateway):
        reject = self.config["audio_mode"] == "answer"
        if gw.reject_sco != reject:
            self.tel.set_reject_sco(gw, reject)

    def _want_audio_here(self):
        self.want_here = True
        self.want_since = self.clock()

    def _ensure_audio_here(self):
        gw = self.gw
        if gw is None or not self.want_here or gw.transport_state in ("active", "pending"):
            return
        if any(c.state in (ACTIVE, DIALING, ALERTING) for c in self.calls()):
            log.info("the phone kept the audio; asking it to send it here")
            self.tel.activate_audio(gw)

    # -- card actions ---------------------------------------------------------------------------
    def answer(self):
        call = self.ringing_call()
        if call is None or self.gw is None:
            return
        gw = self.gw
        call.extra["answered_here"] = True
        self.focus_request = True
        self._want_audio_here()
        self.ringer.stop()
        if gw.reject_sco:
            # The phone must be allowed to move the audio before the call goes live.
            self.tel.set_reject_sco(gw, False, then=lambda ok: self.tel.answer(call))
        else:
            self.tel.answer(call)
        self.render()

    def decline(self):
        call = self.ringing_call() or self.waiting_call()
        if call is None:
            return
        call.extra["declined_here"] = True
        self.ringer.stop()
        self.tel.hangup(call)

    def answer_waiting(self):
        if self.gw is not None and self.waiting_call() is not None:
            self.tel.hold_and_answer(self.gw)

    def decline_waiting(self):
        call = self.waiting_call()
        if call is not None:
            call.extra["declined_here"] = True
            self.tel.hangup(call)

    def swap(self):
        if self.gw is not None:
            self.tel.swap_calls(self.gw)

    def hangup(self):
        call = self.primary_call()
        if call is None:
            return
        if call.multiparty and self.gw is not None:
            self.tel.hangup_all(self.gw)
        else:
            self.tel.hangup(call)

    def press_key(self, key: str):
        call = self.primary_call()
        if key not in "0123456789*#" or len(key) != 1 or call is None or call.state != ACTIVE:
            return
        self.tones += key
        self.tel.send_tones(self.gw, key)
        self.render()

    def toggle_mute(self):
        self.muted = not self.muted
        self.router.set_mute(self.muted)
        self.render()

    # -- global keys (GNOME shortcuts → the app's D-Bus actions) --------------------------------
    def key_answer_or_hangup(self) -> bool:
        """Answers a ringing call on the computer; during a call, hangs up. Returns False when
        there's no call, and the app opens the dialer instead."""
        self.dismissed.clear()
        if self.ringing_call() is not None:
            self.answer()
        elif self.primary_call() is not None:
            self.hangup()
        else:
            return False
        return True

    def key_decline(self):
        self.dismissed.clear()
        self.decline()

    def key_decline_or_show(self) -> bool:
        """Declines a ringing (or waiting) call and returns True; otherwise returns False and the
        app brings the call card forward (during a call)."""
        if self.ringing_call() is None and self.waiting_call() is None:
            return False
        self.key_decline()
        return True

    def key_mute(self):
        """Ringing: silences only the computer's ringtone. In a call: mutes your mic for the call."""
        self.dismissed.clear()
        ringing = self.ringing_call()
        waiting = self.waiting_call()
        if ringing is not None or (waiting is not None and waiting.path not in self.silenced):
            self.silenced.add((ringing or waiting).path)
            self.render()
        elif self.primary_call() is not None:
            if self.audio_where() == "computer" or self.muted:
                self.toggle_mute()
            else:
                self._on_error(None, "mute", "the call is on the phone; mute it there")
        else:
            self.render()

    def to_phone(self):
        gw = self.gw
        if gw is None:
            return
        self.want_here = False
        self.tel.set_reject_sco(gw, True, then=lambda ok: self.router.release(gw.address))
        self.render()

    def to_computer(self):
        gw = self.gw
        if gw is None:
            return
        self._want_audio_here()

        def allowed(ok):
            self.router.prepare(gw.address, then=lambda: self.tel.activate_audio(gw))
        if gw.reject_sco:
            self.tel.set_reject_sco(gw, False, then=allowed)
        else:
            allowed(True)
        self.render()

    def dial(self, number: str):
        number = DIAL_CHARS.sub("", number)
        if not number:
            return
        gw = self.gw
        if gw is None:
            self._on_error(None, "call", "no phone is connected")
            return
        self._want_audio_here()
        self.dialing_here = True
        self.idle_requested = False
        if gw.reject_sco:
            self.tel.set_reject_sco(gw, False, then=lambda ok: self.tel.dial(gw, number))
        else:
            self.tel.dial(gw, number)
        self.render()

    def all_contacts(self) -> list[tuple[str, list[str]]]:
        if self.contacts is None or not self.config["contacts"]:
            return []
        return self.contacts.all()

    def search_contacts(self, query: str) -> list[tuple[str, str]]:
        if self.config["gnome_contacts_search"] and self.desktop_contacts is not None:
            return self.desktop_contacts.search(query)
        if self.contacts is None or not self.config["contacts"]:
            return []
        return self.contacts.search(query)

    def dismiss(self):
        """The card's ×: hide it for the calls on now; the phone keeps ringing."""
        self.dismissed.update(c.path for c in self.calls())
        self.idle_requested = False
        self.ended = None
        self._cancel("ended")
        self.render()

    def show_status(self):
        self.idle_requested = True
        self.dismissed.clear()
        self.ended = None                    # the dialer replaces a "Call ended" card at once
        self._cancel("ended")
        self.render()

    def connect_phone(self):
        """Asks BlueZ to connect the hands-free profile of the paired phones."""
        if self.bluez is None:
            return
        self.hint_override = "Connecting…"
        self.render()

        def done(message):
            self.hint_override = message
            self.render()
        self.bluez.connect_phones(done)
