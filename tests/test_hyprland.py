"""Closing the card must not close an open special workspace (Omarchy's scratchpad)."""
from desk_phone import hyprland
from desk_phone.hyprland import window_to_return_to

ME = 4242
MONITORS = [{"id": 0, "specialWorkspace": {"id": -97, "name": "special:scratchpad"}},
            {"id": 1, "specialWorkspace": {"id": 0, "name": ""}}]
CARD = {"address": "0xc", "pid": ME, "monitor": 0, "workspace": {"name": "1"}, "focusHistoryID": 0}
CLIENTS = [CARD,
           {"address": "0xa", "pid": 1, "workspace": {"name": "1"}, "focusHistoryID": 1},
           {"address": "0xb1", "pid": 2, "workspace": {"name": "special:scratchpad"}, "focusHistoryID": 3},
           {"address": "0xb2", "pid": 3, "workspace": {"name": "special:scratchpad"}, "focusHistoryID": 2},
           {"address": "0xb3", "pid": 4, "workspace": {"name": "special:scratchpad"}, "focusHistoryID": 1,
            "hidden": True}]


def test_goes_back_to_the_window_last_used_in_the_open_special_workspace():
    assert window_to_return_to(CARD, MONITORS, CLIENTS, ME) == "0xb2"


def test_nothing_when_the_card_does_not_have_the_keyboard():
    assert window_to_return_to(CLIENTS[1], MONITORS, CLIENTS, ME) is None
    assert window_to_return_to({}, MONITORS, CLIENTS, ME) is None


def test_nothing_without_an_open_special_workspace_or_a_window_in_it():
    assert window_to_return_to(dict(CARD, monitor=1), MONITORS, CLIENTS, ME) is None
    assert window_to_return_to(CARD, MONITORS, CLIENTS[:2], ME) is None


def test_hand_focus_back_asks_hyprland_and_survives_its_absence(monkeypatch):
    asked = []

    def hyprctl(*args):
        asked.append(args)
        if args[0] == "dispatch":
            return "ok" if args[1] == "focuswindow" else "error: not lua"
        import json
        return json.dumps({"activewindow": CARD, "monitors": MONITORS, "clients": CLIENTS}[args[1]])
    monkeypatch.setattr(hyprland.os, "getpid", lambda: ME)
    monkeypatch.setattr(hyprland, "_hyprctl", hyprctl)
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    hyprland.hand_focus_back()
    assert asked == []                                    # not on Hyprland: nothing is run
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "x")
    hyprland.hand_focus_back()
    assert asked[-1] == ("dispatch", "focuswindow", "address:0xb2")      # (the Lua form came first)
    assert "0xb2" in asked[-2][1] and asked[-2][1].startswith("hl.dsp.focus")

    def gone(*args):
        raise FileNotFoundError("hyprctl")
    monkeypatch.setattr(hyprland, "_hyprctl", gone)
    hyprland.hand_focus_back()                            # no hyprctl: no error


def test_focus_own_window_waits_until_hyprland_lists_it(monkeypatch):
    import json
    asked, clients = [], []

    def hyprctl(*args):
        asked.append(args)
        return "ok" if args[0] == "dispatch" else json.dumps(clients)
    monkeypatch.setattr(hyprland.os, "getpid", lambda: ME)
    monkeypatch.setattr(hyprland, "_hyprctl", hyprctl)
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "x")
    assert hyprland.focus_own_window("Desk Phone") is False           # not shown yet: ask again
    clients += [{"address": "0x5", "pid": ME, "title": "Desk Phone Settings"},
                {"address": "0x6", "pid": 7, "title": "Desk Phone"},
                {"address": "0x7", "pid": ME, "title": "Desk Phone"}]
    assert hyprland.focus_own_window("Desk Phone") is True
    assert asked[-1][0] == "dispatch" and "address:0x7" in asked[-1][1]
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE")
    asked.clear()
    assert hyprland.focus_own_window("Desk Phone") is True and asked == []
