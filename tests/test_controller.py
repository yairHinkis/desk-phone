"""The controller against the pretend phone, over a real D-Bus session."""
import itertools

import pytest

from conftest import spin
from desk_phone.audio import FakeRouter
from desk_phone.config import Config
from desk_phone.contacts import Contacts
from desk_phone.controller import Controller
from desk_phone.fake_telephony import FakePhone
from desk_phone.sounds import SilentRinger
from desk_phone.telephony import ACTIVE, Telephony

_n = itertools.count()


class View:
    def __init__(self):
        self.models = []

    def render(self, m):
        self.models.append(m)

    @property
    def last(self):
        return self.models[-1] if self.models else None


@pytest.fixture
def rig(tmp_path):
    def make(mode="answer", reject_default=True):
        name = f"io.github.yairhinkis.DeskPhoneTest{next(_n)}"
        phone = FakePhone(name)
        phone.reject_sco_default = reject_default
        tel = Telephony(service=name)
        cfg = Config(tmp_path / f"c{next(_n)}.json")
        cfg["audio_mode"] = mode
        contacts = Contacts(folder=tmp_path)
        contacts.set_entries([("Mom", ["050-123-4567"])])
        contacts.refresh = lambda *a, **k: None
        router, ringer, view = FakeRouter(), SilentRinger(), View()
        router.on_release = lambda _a: phone.release_audio()
        ctrl = Controller(tel, router, ringer, contacts, cfg, view)
        spin(lambda: tel.available)
        phone.connect_gateway()
        spin(lambda: ctrl.gw is not None)
        return phone, tel, ctrl, router, ringer, view
    return make


def test_ringing_shows_contact_name_and_rings(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    phone.incoming("+972501234567")
    spin(lambda: view.last and view.last.mode == "ringing")
    assert view.last.title == "Mom" and view.last.subtitle == "+972 50-123-4567"
    assert ringer.playing == "ring"


def test_answer_here_allows_audio_before_answering(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    spin(lambda: tel.first_gateway().reject_sco)
    call = phone.incoming("0521112222")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.answer()
    spin(lambda: router.active and view.last.mode == "call" and view.last.audio == "computer")
    names = [e[0] for e in phone.log if e[0] in ("Set", "Answer")]
    assert names.index("Set") < names.index("Answer")
    assert phone.calls[call]["State"] == ACTIVE
    assert ringer.playing is None
    assert view.last.title == "052-111-2222"


def test_answer_on_phone_keeps_audio_on_phone(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    spin(lambda: tel.first_gateway().reject_sco)
    call = phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    phone.answer_on_phone(call)
    spin(lambda: view.last.mode == "call")
    spin(timeout=0.5)
    assert not router.active and view.last.audio == "phone"
    assert ringer.playing is None


def test_keypad_sends_tones_and_shows_them(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.answer()
    spin(lambda: view.last.mode == "call" and view.last.status.startswith("00:"))
    for k in "12#x":
        ctrl.press_key(k)
    spin(lambda: len(phone.methods("SendTones")) == 3)
    assert [m[2] for m in phone.methods("SendTones")] == [("1",), ("2",), ("#",)]
    assert view.last.tones == "12#"


def test_move_to_phone_and_back(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.answer()
    spin(lambda: router.active)
    ctrl.to_phone()
    spin(lambda: ("release", "AA:BB:CC:00:11:22") in router.log and view.last.audio == "phone")
    assert tel.first_gateway().reject_sco and not router.active
    ctrl.to_computer()
    spin(lambda: router.active and view.last.audio == "computer")
    assert phone.methods("Activate")


def test_hangup_shows_ended_then_hides_and_rejects_again(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.answer()
    spin(lambda: router.active)
    ctrl.hangup()
    spin(lambda: view.last and view.last.mode == "ended")
    assert view.last.status.startswith("Call ended")
    spin(lambda: view.last is None, timeout=4)
    assert tel.first_gateway().reject_sco and not router.active


def test_missed_and_declined(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    call = phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    phone.end_call(call)
    spin(lambda: view.last and view.last.status == "Missed call")
    phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.decline()
    spin(lambda: view.last and view.last.status == "Declined")


def test_dismiss_hides_card_and_silences_ring(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    phone.incoming("0501234567")
    spin(lambda: ringer.playing == "ring")
    ctrl.dismiss()
    assert view.last is None and ringer.playing is None
    phone.incoming("0539998888")      # waiting call → a new call shows the card again
    spin(lambda: view.last is not None)


def test_computer_mode_lets_phone_send_audio(rig):
    phone, tel, ctrl, router, ringer, view = rig(mode="computer")
    spin(lambda: not tel.first_gateway().reject_sco)
    call = phone.incoming("0501234567")
    spin(lambda: router.active)          # in-band ringing reaches the computer
    assert ringer.playing is None        # so the phone's own ringtone plays, not ours
    phone.answer_on_phone(call)
    spin(lambda: view.last.mode == "call" and view.last.audio == "computer")


def test_call_waiting_banner_and_hold_answer(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.answer()
    spin(lambda: view.last.mode == "call")
    phone.incoming("0539998888")
    spin(lambda: view.last.waiting == "053-999-8888")
    assert ringer.playing == "waiting"
    ctrl.answer_waiting()
    spin(lambda: view.last.held == "Mom" and view.last.title == "053-999-8888")


def test_dial_from_computer(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    spin(lambda: tel.first_gateway().reject_sco)
    ctrl.dial("050-123 4567")
    spin(lambda: view.last and view.last.mode == "call" and view.last.status == "Ringing…")
    assert phone.methods("Dial")[0][2] == ("0501234567",)
    spin(lambda: router.active)


def test_phone_disconnect_clears_everything(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    phone.disconnect_gateway()
    spin(lambda: ctrl.gw is None)
    assert ringer.playing is None and not ctrl.calls()


def test_idle_card_states(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    ctrl.show_status()
    assert view.last.mode == "idle" and view.last.phone_ready and view.last.can_dial
    phone.disconnect_gateway()
    spin(lambda: view.last.mode == "idle" and not view.last.phone_ready)
    assert "Phone calls" in view.last.hint


def test_errors_show_on_card(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.answer()
    spin(lambda: view.last.mode == "call")
    tel.send_tones(tel.first_gateway(), "1")   # fine
    phone.set_state(next(iter(phone.calls)), "held")
    spin(lambda: view.last.status == "On hold")
    tel.send_tones(tel.first_gateway(), "1")   # no active call → error
    spin(lambda: view.last.error.startswith("Couldn't keypad"))


def test_keys_answer_then_hangup(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    call = phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.dismiss()                                   # works even with the card hidden
    ctrl.key_answer_or_hangup()
    spin(lambda: router.active and view.last and view.last.mode == "call")
    ctrl.key_answer_or_hangup()
    spin(lambda: call not in phone.calls)


def test_key_decline(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    call = phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.key_decline()
    spin(lambda: view.last and view.last.status == "Declined")
    assert call not in phone.calls and ctrl.key_decline() is None   # nothing left: no error


def test_key_mute_silences_ring_only_then_mutes_call(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    call = phone.incoming("0501234567")
    spin(lambda: ringer.playing == "ring")
    ctrl.key_mute()
    assert ringer.playing is None and view.last.status == "Incoming call · silenced"
    assert phone.calls[call]["State"] == "incoming" and not router.log   # the call itself untouched
    ctrl.key_mute()                                  # still ringing: stays silenced, no mic toggle
    assert ringer.playing is None and not ctrl.muted
    ctrl.key_answer_or_hangup()
    spin(lambda: router.active)
    ctrl.key_mute()
    assert ctrl.muted and ("mute", True) in router.log and view.last.muted
    ctrl.key_mute()
    assert not ctrl.muted and router.log[-1] == ("mute", False)


def test_key_mute_with_audio_on_phone_explains(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    call = phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    phone.answer_on_phone(call)
    spin(lambda: view.last.mode == "call")
    spin(timeout=0.3)
    ctrl.key_mute()
    assert not ctrl.muted and "on the phone" in view.last.error


def test_waiting_call_ring_can_be_silenced(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.answer()
    spin(lambda: router.active)
    phone.incoming("0539998888")
    spin(lambda: ringer.playing == "waiting")
    ctrl.key_mute()
    assert ringer.playing is None and not ctrl.muted


def test_dialer_replaces_call_ended_card(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.answer()
    spin(lambda: router.active)
    ctrl.hangup()
    spin(lambda: view.last and view.last.mode == "ended")
    ctrl.show_status()
    assert view.last.mode == "idle" and view.last.can_dial
    spin(timeout=3)                      # the old "ended" timer must not hide the dialer
    assert view.last.mode == "idle"


def test_shift_key_declines_only_while_ringing(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    assert ctrl.key_decline_or_show() is False             # nothing ringing → nothing to decline
    assert ctrl.key_answer_or_hangup() is False            # F11 with no call → the app opens the dialer
    call = phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    assert ctrl.key_decline_or_show() is True
    spin(lambda: call not in phone.calls and not tel.calls)
    phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.answer()
    spin(lambda: view.last.mode == "call" and ctrl.primary_call().state == "active")
    assert ctrl.key_decline_or_show() is False             # in a call: the app brings the card forward
    waiting = phone.incoming("0539998888")
    spin(lambda: view.last.waiting)
    assert ctrl.key_decline_or_show() is True              # a waiting call is declined
    spin(lambda: waiting not in phone.calls)
    assert ctrl.primary_call() is not None


def test_answer_here_asks_for_focus_once(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.key_answer_or_hangup()
    spin(lambda: view.last.mode == "call")
    focused = [m for m in view.models if m and m.mode == "call" and m.focus]
    assert len(focused) == 1
    ctrl.render()
    assert not view.last.focus


def test_answer_on_phone_does_not_take_focus(rig):
    phone, tel, ctrl, router, ringer, view = rig()
    call = phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    phone.answer_on_phone(call)
    spin(lambda: view.last.mode == "call")
    assert not any(m and m.focus for m in view.models)


def test_dialer_matches_come_from_gnome_contacts_when_set(rig):
    from desk_phone.desktop_contacts import DesktopContacts
    phone, tel, ctrl, router, ringer, view = rig()
    desk = DesktopContacts(reader=lambda: [("Yael", ["052-111-2222"])])
    ctrl.desktop_contacts = desk
    desk.refresh()
    spin(lambda: desk.all())
    assert [n for n, _ in ctrl.search_contacts("mo")] == ["Mom"]       # off by default: the phone's
    ctrl.config.set("gnome_contacts_search", True)
    assert ctrl.search_contacts("mo") == []
    assert ctrl.search_contacts("ya") == [("Yael", "052-111-2222")]


def test_caller_name_from_gnome_contacts_when_set(rig):
    from desk_phone.desktop_contacts import DesktopContacts
    phone, tel, ctrl, router, ringer, view = rig()
    desk = DesktopContacts(reader=lambda: [("Ima", ["+972 50-123-4567"]), ("Yael", ["052-111-2222"])])
    desk.set_entries(desk.reader())
    desk.refresh = lambda *a, **k: None
    ctrl.desktop_contacts = desk
    phone.incoming("+972501234567")
    spin(lambda: view.last and view.last.mode == "ringing")
    assert view.last.title == "Mom"                    # off: the phone's name
    ctrl.config.set("gnome_contacts_search", True)
    assert view.last.title == "Ima"                    # on: GNOME Contacts' name
    desk.set_entries([("Yael", ["052-111-2222"])])
    ctrl.render()
    assert view.last.title == "Mom"                    # not there: still the phone's
