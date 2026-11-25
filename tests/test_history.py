"""The recent-calls list: what's stored, how it's kept private, and what the controller records."""
import itertools
import json
import os
import stat
import time

import pytest

from conftest import spin
from desk_phone.audio import FakeRouter
from desk_phone.config import Config
from desk_phone.contacts import Contacts
from desk_phone.controller import Controller
from desk_phone.fake_telephony import FakePhone
from desk_phone.history import CallHistory, describe, format_when
from desk_phone.sounds import SilentRinger
from desk_phone.telephony import Telephony

_n = itertools.count()


def at(*ymdhm):
    return time.mktime((*ymdhm, 0, 0, 0, -1))


# -- the list itself ------------------------------------------------------------------------------
def test_newest_first_and_capped(tmp_path):
    h = CallHistory(folder=tmp_path, limit=3)
    for i in range(5):
        h.add(f"050-000-000{i}", "", "in", 1000.0 + i, 10)
    assert [e["number"] for e in h.all()] == ["050-000-0004", "050-000-0003", "050-000-0002"]
    again = CallHistory(folder=tmp_path, limit=3)          # read back from the file
    assert again.all() == h.all()


def test_file_is_private_and_under_the_state_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    h = CallHistory()
    assert h.path == tmp_path / "state" / "desk-phone" / "calls.json"
    h.path.parent.mkdir(parents=True)
    h.path.with_suffix(".tmp").write_text("left over")     # with the default, wider rights
    h.add("0501234567", "Mom", "missed", 1000.0)
    assert stat.S_IMODE(os.stat(h.path).st_mode) == 0o600
    assert json.loads(h.path.read_text()) == {"calls": [
        {"number": "0501234567", "name": "Mom", "kind": "missed", "when": 1000.0, "seconds": 0}]}


def test_clear_deletes_the_file(tmp_path):
    h = CallHistory(folder=tmp_path)
    h.add("0501234567", "", "out", 1000.0, 5)
    assert h.path.exists()
    h.clear()
    assert h.all() == [] and not h.path.exists()
    assert CallHistory(folder=tmp_path).all() == []


def test_demo_list_is_never_written(tmp_path):
    h = CallHistory(folder=tmp_path, persist=False)
    h.set_entries([{"number": "1", "kind": "in", "when": 5.0}])
    h.add("2", "", "out", 6.0)
    assert len(h.all()) == 2 and not h.path.exists()


@pytest.mark.parametrize("content", ["", "not json", "[1, 2]", '{"calls": 7}',
                                     '{"calls": [3, {"number": "1"}, {"when": "x"}]}'])
def test_broken_file_gives_an_empty_list(tmp_path, content):
    (tmp_path / "calls.json").write_text(content)
    assert CallHistory(folder=tmp_path).all() == []


def test_odd_entries_are_tidied(tmp_path):
    (tmp_path / "calls.json").write_text(json.dumps({"calls": [
        {"number": None, "kind": "weird", "when": 12, "seconds": "x"}, "junk",
        {"number": "050", "name": "A", "kind": "out", "when": 11.5, "seconds": 3.9}]}))
    assert CallHistory(folder=tmp_path).all() == [
        {"number": "", "name": "", "kind": "call", "when": 12.0, "seconds": 0},
        {"number": "050", "name": "A", "kind": "out", "when": 11.5, "seconds": 3}]


def test_changes_are_announced(tmp_path):
    h = CallHistory(folder=tmp_path)
    seen = []
    h.on_changed = lambda: seen.append(len(h.all()))
    h.add("1", "", "in", 1.0)
    h.clear()
    assert seen == [1, 0]


# -- how a call's time reads ----------------------------------------------------------------------
def test_format_when():
    now = at(2026, 11, 25, 22, 0)                          # a Wednesday
    assert format_when(at(2026, 11, 25, 9, 5), now) == "09:05"
    assert format_when(at(2026, 11, 24, 23, 59), now) == "Yesterday"
    assert format_when(at(2026, 11, 23, 12, 0), now) == time.strftime("%a", time.localtime(at(2026, 11, 23, 12, 0)))
    assert format_when(at(2026, 11, 19, 12, 0), now) == time.strftime("%a", time.localtime(at(2026, 11, 19, 12, 0)))
    assert format_when(at(2026, 11, 18, 12, 0), now).startswith("18 ")      # a week ago: the date
    assert format_when(at(2025, 12, 31, 12, 0), now).endswith(" 2025")
    assert format_when(now + 60, now) == time.strftime("%H:%M", time.localtime(now + 60))  # clock skew


def test_describe():
    now = at(2026, 11, 25, 22, 0)
    call = {"number": "1", "name": "", "kind": "missed", "when": at(2026, 11, 24, 14, 32), "seconds": 0}
    assert describe(call, now) == "Missed · Yesterday 14:32"
    call = {"number": "1", "name": "", "kind": "in", "when": at(2026, 11, 25, 14, 32), "seconds": 125}
    assert describe(call, now) == "Incoming · 14:32 · 2 min 05 s"
    call = {"number": "1", "name": "", "kind": "out", "when": at(2026, 11, 25, 14, 32), "seconds": 0}
    assert describe(call, now) == "Outgoing · 14:32 · no answer"
    assert describe(dict(call, seconds=42), now) == "Outgoing · 14:32 · 42 s"


# -- what the controller records, against the pretend phone -----------------------------------------
class View:
    last = None

    def render(self, m):
        self.last = m


@pytest.fixture
def rig(tmp_path):
    def make(**config):
        name = f"io.github.yairhinkis.DeskPhoneTestHistory{next(_n)}"
        phone = FakePhone(name)
        phone.reject_sco_default = True
        tel = Telephony(service=name)
        cfg = Config(tmp_path / f"c{next(_n)}.json")
        cfg.update(config)
        contacts = Contacts(folder=tmp_path)
        contacts.set_entries([("Mom", ["050-123-4567"])])
        contacts.refresh = lambda *a, **k: None
        history = CallHistory(folder=tmp_path / f"h{next(_n)}")
        router, view = FakeRouter(), View()
        router.on_release = lambda _a: phone.release_audio()
        clock = {"t": 100.0}
        ctrl = Controller(tel, router, SilentRinger(), contacts, cfg, view, history=history,
                          clock=lambda: clock["t"], now=lambda: 5000.0 + clock["t"])
        spin(lambda: tel.available)
        phone.connect_gateway()
        spin(lambda: ctrl.gw is not None)
        return phone, ctrl, history, clock, cfg
    return make


def kinds(history):
    return [(e["kind"], e["number"], e["name"], e["seconds"]) for e in history.all()]


def test_answered_call_is_recorded_with_its_length(rig):
    phone, ctrl, history, clock, _cfg = rig()
    call = phone.incoming("+972501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.answer()
    spin(lambda: ctrl.view.last.mode == "call" and ctrl.view.last.audio == "computer")
    clock["t"] += 42
    ctrl.hangup()
    spin(lambda: call not in phone.calls and history.all())
    assert kinds(history) == [("in", "+972501234567", "Mom", 42)]
    assert history.all()[0]["when"] == 5100.0              # when it started ringing


def test_unanswered_call_is_missed_and_a_declined_one_is_declined(rig):
    phone, ctrl, history, _clock, _cfg = rig()
    call = phone.incoming("0521112222")
    spin(lambda: ctrl.ringing_call() is not None)
    phone.end_call(call)                                   # the caller gave up
    spin(lambda: len(history.all()) == 1)
    phone.incoming("0539998888")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.decline()
    spin(lambda: len(history.all()) == 2)
    assert kinds(history) == [("declined", "0539998888", "", 0), ("missed", "0521112222", "", 0)]


def test_call_answered_on_the_phone_counts_as_incoming(rig):
    phone, ctrl, history, clock, _cfg = rig()
    call = phone.incoming("0521112222")
    spin(lambda: ctrl.ringing_call() is not None)
    phone.answer_on_phone(call)
    spin(lambda: ctrl.view.last.mode == "call")
    clock["t"] += 7
    phone.end_call(call)
    spin(lambda: history.all())
    assert kinds(history) == [("in", "0521112222", "", 7)]


def test_dialed_calls_answered_or_not(rig):
    phone, ctrl, history, clock, _cfg = rig()
    ctrl.dial("050-123-4567")
    spin(lambda: phone.calls)
    path = next(iter(phone.calls))
    spin(lambda: ctrl.primary_call() is not None)
    phone.end_call(path)                                   # nobody picked up
    spin(lambda: len(history.all()) == 1)
    ctrl.dial("03-777-8888")
    spin(lambda: phone.calls and ctrl.primary_call() is not None)
    path = next(iter(phone.calls))
    phone.remote_answer(path)
    spin(lambda: "since" in ctrl.primary_call().extra)
    clock["t"] += 61
    ctrl.hangup()
    spin(lambda: len(history.all()) == 2)
    assert kinds(history) == [("out", "037778888", "", 61), ("out", "0501234567", "Mom", 0)]


def test_hidden_number_is_kept_without_a_number(rig):
    phone, ctrl, history, _clock, _cfg = rig()
    call = phone.incoming("withheld")
    spin(lambda: ctrl.ringing_call() is not None)
    phone.end_call(call)
    spin(lambda: history.all())
    assert kinds(history) == [("missed", "", "", 0)]
    assert ctrl.recent_calls()[0]["title"] == "Unknown caller"


def test_call_waiting_records_both_calls(rig):
    phone, ctrl, history, _clock, _cfg = rig()
    first = phone.incoming("0501234567")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.answer()
    spin(lambda: ctrl.view.last.mode == "call")
    second = phone.incoming("0521112222")
    spin(lambda: ctrl.waiting_call() is not None)
    phone.end_call(second)                                 # the second caller gave up
    spin(lambda: len(history.all()) == 1)
    phone.end_call(first)
    spin(lambda: len(history.all()) == 2)
    assert [(k, n) for k, n, _name, _s in kinds(history)] == [("in", "0501234567"), ("missed", "0521112222")]


def test_phone_disconnecting_mid_call_still_records_it(rig):
    phone, ctrl, history, clock, _cfg = rig()
    phone.incoming("0521112222")
    spin(lambda: ctrl.ringing_call() is not None)
    ctrl.answer()
    spin(lambda: ctrl.view.last.mode == "call")
    clock["t"] += 3
    phone.disconnect_gateway()
    spin(lambda: history.all())
    assert kinds(history) == [("in", "0521112222", "", 3)]


def test_switched_off_records_nothing_and_forgets_the_list(rig):
    phone, ctrl, history, _clock, cfg = rig()
    history.add("0501234567", "", "in", 1.0, 5)
    assert history.path.exists() and ctrl.recent_calls()
    cfg.set("call_history", False)
    assert history.all() == [] and not history.path.exists() and ctrl.recent_calls() == []
    call = phone.incoming("0521112222")
    spin(lambda: ctrl.ringing_call() is not None)
    phone.end_call(call)
    spin(lambda: ctrl.ringing_call() is None)
    spin(timeout=0.3)
    assert history.all() == []
    assert ctrl.view.last is None or not ctrl.view.last.recents


def test_recent_calls_show_the_name_the_number_has_now(rig):
    _phone, ctrl, history, _clock, _cfg = rig()
    history.add("+972 52 765 4321", "", "missed", 10.0)    # unknown when they called
    history.add("050-123-4567", "Mother", "in", 20.0, 5)   # saved under another name since
    assert [(c["title"], c["named"]) for c in ctrl.recent_calls()] == [("Mom", True), ("+972 52 765 4321", False)]
    ctrl.contacts.set_entries([("Dana", ["052-765-4321"])])
    assert [c["title"] for c in ctrl.recent_calls()] == ["Mother", "Dana"]   # the stored name is the fallback


def test_idle_card_offers_recent_calls(rig):
    _phone, ctrl, _history, _clock, _cfg = rig()
    ctrl.show_status()
    assert ctrl.view.last.mode == "idle" and ctrl.view.last.recents


def test_a_broken_list_never_disturbs_the_call(rig):
    phone, ctrl, history, _clock, _cfg = rig()
    history.add = lambda *a, **k: 1 / 0
    call = phone.incoming("0521112222")
    spin(lambda: ctrl.ringing_call() is not None)
    phone.end_call(call)
    spin(lambda: ctrl.view.last is not None and ctrl.view.last.mode == "ended")
    assert ctrl.view.last.status == "Missed call"
