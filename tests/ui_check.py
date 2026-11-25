"""Drives the real call card through the recent-calls list, with a pretend phone, and checks it.

It opens the card's window, so run it on an off-screen display, in its own D-Bus session:

    xvfb-run -a dbus-run-session -- python3 tests/ui_check.py        (or: make ui-check)

It's silent, keeps its settings in a temporary home folder, and has its own name on the bus, so
it doesn't touch a Desk Phone that's running. pytest doesn't pick it up (it needs a display).
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="desk-phone-ui-check-")
os.environ["HOME"] = HOME
for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
    os.environ.pop(var, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib  # noqa: E402

GLib.set_prgname("io.github.yairhinkis.DeskPhone")
from gi.repository import Gdk  # noqa: E402

import desk_phone.app as appmod  # noqa: E402
from desk_phone.sounds import SilentRinger  # noqa: E402

appmod.Ringer = SilentRinger            # the demo rings out loud otherwise
app = appmod.DeskPhoneApp(demo=True)
# the pretend phone connects, but nobody calls until the steps below say so
app._demo_script = lambda: GLib.timeout_add(300, lambda: app.fake.connect_gateway() and False)
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name, detail, flush=True)


def key(val):
    """Presses a key in the dialer's box."""
    return app.card._on_entry_key(None, type("E", (), {"keyval": val})())


def card():
    return app.card


def rows():
    return card().matches.get_children()


def s1():
    app.show_dialer()

def s2():
    m = card().model
    check("idle card shows the recent-calls button", m.mode == "idle" and m.recents and card().recent_btn.get_visible())
    check("list hidden until asked", not card().matches_visible())
    check("Down in the empty box opens the list", key(Gdk.KEY_Down) is True and card().matches_visible())

def s3():
    r = rows()
    check("six seeded calls, newest first", len(r) == 6 and r[0].number == "03-777-8888", str([x.number for x in r]))
    check("first row is selected", card().matches.get_selected_row() is r[0])
    key(Gdk.KEY_Down)
    key(Gdk.KEY_Down)
    check("Down moves the selection", card().matches.get_selected_row() is r[2])
    key(Gdk.KEY_Up)
    check("Up moves it back", card().matches.get_selected_row() is r[1])
    card().entry.emit("activate")          # Enter

def s4():
    c = app.ctrl.primary_call()
    check("Enter calls the selected one back", c is not None and c.number == "+972501234567", str(c and c.number))
    check("list closes when the call starts", not card().matches_visible() and not card().recent_btn.get_visible())
    app.ctrl.hangup()

def s5():
    app.show_dialer()

def s6():
    card()._toggle_recents()

def s7():
    r = rows()
    check("the call just made is on top", len(r) == 7 and r[0].number == "+972501234567" and card().matches.get_selected_row() is None)
    check("its tooltip tells the story", "Outgoing" in r[0].get_tooltip_text() and "no answer" in r[0].get_tooltip_text(), r[0].get_tooltip_text())
    card().entry.set_text("da")

def s8():
    check("typing swaps the list for contact matches", card().matches_visible() and not card()._recents_shown and len(rows()) == 3)
    card().entry.set_text("")

def s9():
    check("clearing the box hides the matches", not card().matches_visible())
    # a long list: 40 calls, some with very long names
    now = time.time()
    app.history.set_entries([{"number": f"050-555-{i:04d}", "name": ("A very long caller name that goes on and on " * 2 if i == 3 else f"Caller {i}"),
                              "kind": ("missed", "in", "out")[i % 3], "when": now - i * 5000} for i in range(40)] +
                            [{"number": "", "name": "", "kind": "missed", "when": now - 999999}])
    card()._toggle_recents()

def s10():
    a = card().match_rev.get_allocation()
    w = card().get_allocation()
    check("a long list is capped and scrolls", 250 < a.height <= 320 and card().match_scroll.get_vadjustment().get_upper() > 1000, f"popup h={a.height} upper={card().match_scroll.get_vadjustment().get_upper()}")
    check("long names don't widen the window", w.width == 320 + 200 + 44 and a.width <= 320 + 200, f"win={w.width} popup={a.width}")
    cx, cy, cw, ch = card().card_rect()
    check("the card itself didn't move or resize", cw == 320)
    for _ in range(25):
        key(Gdk.KEY_Down)

def s11():
    adj = card().match_scroll.get_vadjustment()
    sel = card().matches.get_selected_row()
    al = sel.get_allocation()
    check("arrow keys scroll the selected row into view", adj.get_value() > 0 and adj.get_value() <= al.y and al.y + al.height <= adj.get_value() + adj.get_page_size() + 1, f"value={adj.get_value()} row y={al.y}")
    last = rows()[-1]
    check("a hidden number can't be picked", not last.get_selectable() and not last.get_activatable() and last.number == "")
    app.config.set("call_history", False)

def s12():
    check("turning the setting off hides the list and the button", not card().matches_visible() and not card().recent_btn.get_visible() and app.history.all() == [])
    check("Down then does nothing", key(Gdk.KEY_Down) is False)
    app.config.set("call_history", True)

def s13():
    card()._toggle_recents()

def s14():
    r = rows()
    check("empty list says so", len(r) == 1 and not r[0].get_selectable())
    card().entry.emit("activate")
    check("Enter on the empty list dials nothing", app.ctrl.primary_call() is None)
    app.quit_app()

steps = [(1200, s1), (700, s2), (500, s3), (800, s4), (3200, s5), (600, s6), (500, s7), (500, s8),
         (500, s9), (700, s10), (500, s11), (500, s12), (400, s13), (500, s14)]   # (ms to wait, step)


def run(i=0):
    if i >= len(steps):
        return False
    delay, step = steps[i]

    def go():
        try:
            step()
        except Exception:  # noqa: BLE001 - a broken step is a failed check, and the rest still run
            import traceback
            traceback.print_exc()
            results.append((step.__name__, False, "exception"))
        run(i + 1)
        return False
    GLib.timeout_add(delay, go)
    return False


GLib.idle_add(run)
app.run(["desk-phone", "--background"])
shutil.rmtree(HOME, ignore_errors=True)
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed or len(results) < 22 else 0)
