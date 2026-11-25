"""The call card: a small window that stays on top in a corner of the screen.

It shows whatever CardModel the controller hands it: ringing (Decline/Answer), a call
(keypad, Mute, To phone/To computer, End), the idle status card with a dialer, or "Call ended".

The window itself never moves or resizes while it's up: it's a fixed, mostly transparent window
that only takes clicks on the card (an input shape), and the card grows and shrinks inside it
from the screen edge it sits against. So every change of state can animate in place.
"""

import hashlib
import math
import re

import gi

gi.require_version("PangoCairo", "1.0")
gi.require_foreign("cairo")
import cairo
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo

from .. import APP_NAME
from ..contacts import match_key
from ..controller import format_number
from ..history import describe, format_when
from ..hyprland import focus_own_window, hand_focus_back, on_hyprland
from .theme import css_rgba, watcher

WIDTH = 320                    # the card itself
SIDE, TOP, BOTTOM = 22, 16, 30  # room around it for the shadow; the window is that much bigger
WIN_H = 720                    # tall enough for a call with the keypad (grows if ever needed)
EXTRA = 200                    # spare width on the side away from the screen edge, for long matches
CORNERS = ("bottom-right", "bottom-left", "top-right", "top-left")
SLIDE = 12                     # how far the card rises in when it appears
AVATAR = 76                    # the avatar's drawing area (the circle is smaller; rings go outside)
KEYS = [("1", ""), ("2", "ABC"), ("3", "DEF"), ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
        ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"), ("*", ""), ("0", "+"), ("#", "")]
BAR_SHAPE = [0.55, 0.85, 1.0, 0.75, 0.5]
LIST_H = 300                   # the matches / recent calls scroll when they're taller than this
KIND_GLYPH = {"in": "↙", "missed": "↙", "declined": "↙", "out": "↗", "call": "•"}


def card_css(t):
    """The card in the Omarchy look: flat, the theme's colors and monospace font, Hyprland's
    border and corner rounding, outlined controls tinted like the Omarchy shell's."""
    fg, bg, ac, red, green = t.fg, t.bg, t.accent, t.red, t.green
    c = t.controls
    r = t.radius
    rs = max(0, r - 2)                    # controls inside the card: a little less round
    rgba = css_rgba
    return f"""
window.deskphone {{ background-color: transparent; }}
.card {{
  background-color: {rgba(bg, t.surface_alpha)}; background-image: none;
  color: {rgba(fg)}; border-radius: {r}px; {t.border_css()}
  box-shadow: none; padding: 12px 14px 14px 14px; font-family: monospace;
}}
.card label {{ color: {rgba(fg)}; }}
.card label.kicker {{ font-size: 8pt; font-weight: bold; letter-spacing: 1px; color: {rgba(ac)}; }}
.card label.title {{ font-size: 15pt; font-weight: bold; }}
.card label.dim {{ color: {rgba(fg, 0.62)}; }}
.card label.status {{ color: {rgba(fg, 0.62)}; font-feature-settings: "tnum"; }}
.card label.error {{ color: {rgba(red)}; }}
.card image.meter {{ color: {rgba(fg, 0.55)}; }}
.card image.meter.off {{ color: {rgba(red)}; }}
.tones {{ font-size: 17pt; letter-spacing: 3px; min-height: 30px; color: {rgba(ac)}; }}

button.key {{
  background-image: none; background-color: {rgba(fg, c['normal-fill-alpha'])};
  border: 1px solid {rgba(fg, c['normal-border-alpha'] * 0.45)}; box-shadow: none; text-shadow: none;
  border-radius: {rs}px; min-height: 44px; padding: 2px; color: {rgba(fg)};
  transition: background-color 140ms ease-out, border-color 140ms ease-out;
}}
button.key:hover {{
  background-color: {rgba(fg, c['hover-cursor-fill-alpha'])};
  border-color: {rgba(fg, c['normal-border-alpha'])};
}}
button.key:active, button.key.flash {{
  background-color: {rgba(ac, c['pressed-fill-alpha'])}; border-color: {rgba(ac)}; transition: none;
}}
button.key:active label, button.key.flash label {{ color: {rgba(ac)}; }}
button.key:disabled {{ background-color: transparent; border-color: {rgba(fg, 0.08)}; }}
button.key:disabled label {{ color: {rgba(fg, 0.35)}; }}
button.key label.digit {{ font-size: 15pt; }}
button.key label.letters {{ font-size: 7pt; font-weight: bold; letter-spacing: 1px; color: {rgba(fg, 0.5)}; }}

button.pill {{
  background-image: none; box-shadow: none; text-shadow: none; -gtk-icon-shadow: none;
  border-radius: {rs}px; min-height: 36px; padding: 0 10px; color: {rgba(fg)};
  background-color: {rgba(fg, c['normal-fill-alpha'])};
  border: 1px solid {rgba(fg, c['normal-border-alpha'])};
  transition: background-color 160ms ease-out, border-color 160ms ease-out, box-shadow 160ms ease-out;
}}
button.pill label {{ color: {rgba(fg)}; font-weight: bold; }}
button.pill image {{ color: {rgba(fg)}; }}
button.pill:hover {{ background-color: {rgba(fg, c['hover-cursor-fill-alpha'])}; }}
button.pill:active {{ background-color: {rgba(fg, c['pressed-fill-alpha'])}; }}
button.pill:disabled {{ background-color: transparent; border-color: {rgba(fg, 0.12)}; }}
button.pill:disabled label, button.pill:disabled image {{ color: {rgba(fg, 0.35)}; }}
button.pill.toggled {{
  background-color: {rgba(ac, c['selected-fill-alpha'])}; border-color: {rgba(ac)};
  animation: ping 420ms ease-out;
}}
button.pill.toggled label, button.pill.toggled image {{ color: {rgba(ac)}; }}
button.answer {{ background-color: {rgba(green, 0.16)}; border-color: {rgba(green)}; }}
button.answer:hover {{ background-color: {rgba(green, 0.28)}; }}
button.answer:active {{ background-color: {rgba(green, 0.40)}; }}
button.answer label, button.answer image {{ color: {rgba(green)}; }}
button.answer.breathe {{ animation: breathe 1.6s ease-out infinite; }}
button.hangup {{ background-color: {rgba(red, 0.16)}; border-color: {rgba(red)}; }}
button.hangup:hover {{ background-color: {rgba(red, 0.28)}; }}
button.hangup:active {{ background-color: {rgba(red, 0.40)}; }}
button.hangup label, button.hangup image {{ color: {rgba(red)}; }}
@keyframes breathe {{
  0%   {{ box-shadow: 0 0 0 0 {rgba(green, 0.55)}; }}
  70%  {{ box-shadow: 0 0 0 7px {rgba(green, 0)}; }}
  100% {{ box-shadow: 0 0 0 7px {rgba(green, 0)}; }}
}}
@keyframes ping {{
  from {{ box-shadow: 0 0 0 0 {rgba(ac, 0.55)}; }}
  to   {{ box-shadow: 0 0 0 7px {rgba(ac, 0)}; }}
}}

button.close {{
  background-image: none; background-color: transparent; border: 1px solid transparent;
  box-shadow: none; min-height: 24px; min-width: 24px; padding: 0; border-radius: {rs}px;
  color: {rgba(fg, 0.55)}; transition: background-color 140ms ease-out, border-color 140ms ease-out;
}}
button.close:hover {{
  background-color: {rgba(fg, c['hover-cursor-fill-alpha'])}; color: {rgba(fg)};
  border-color: {rgba(fg, c['hover-cursor-border-alpha'])};
}}
.banner {{
  background-color: {rgba(fg, c['normal-fill-alpha'])}; border: 1px solid {rgba(fg, 0.18)};
  border-radius: {rs}px; padding: 8px 10px;
}}
.card label.audio-chip {{
  font-size: 8pt; font-weight: bold; border-radius: {rs}px; padding: 2px 8px;
  border: 1px solid {rgba(fg, 0.3)}; background-color: transparent; color: {rgba(fg, 0.7)};
}}
.card label.audio-chip.here {{ border-color: {rgba(green)}; color: {rgba(green)}; background-color: {rgba(green, 0.10)}; }}
.card.popup {{ padding: 4px; }}
list.matches {{ background-color: transparent; }}
list.matches row {{
  border-radius: {rs}px; padding: 6px 10px; transition: background-color 120ms ease-out;
}}
list.matches row:selected, list.matches row:hover {{
  background-color: {rgba(fg, c['hover-cursor-fill-alpha'])};
  box-shadow: inset 0 0 0 1px {rgba(fg, c['hover-cursor-border-alpha'])};
}}
list.matches row:selected label, list.matches row:hover label {{ color: {rgba(ac)}; }}
.card list.matches label.number {{ color: {rgba(fg, 0.6)}; font-size: 9pt; font-feature-settings: "tnum"; }}
.card list.matches label.kind {{ color: {rgba(fg, 0.6)}; font-weight: bold; }}
.card list.matches label.kind.out {{ color: {rgba(green)}; }}
.card list.matches label.kind.missed, .card list.matches label.kind.declined {{ color: {rgba(red)}; }}
.card list.matches label.none {{ color: {rgba(fg, 0.6)}; }}
.card.popup scrolledwindow, .card.popup viewport {{ background-color: transparent; border: none; }}
entry.dial {{
  background-image: none; background-color: {rgba(fg, c['normal-fill-alpha'])}; color: {rgba(fg)};
  border: 1px solid {rgba(fg, c['normal-border-alpha'])}; border-radius: {rs}px; min-height: 36px;
  font-size: 13pt; padding: 0 10px; box-shadow: none; caret-color: {rgba(ac)};
  transition: border-color 140ms ease-out, background-color 140ms ease-out;
}}
entry.dial:focus {{ border-color: {rgba(ac)}; background-color: {rgba(fg, c['hover-cursor-fill-alpha'])}; }}
entry.dial selection {{ background-color: {rgba(ac, c['selection-fill-alpha'])}; color: {rgba(fg)}; }}
""".encode()


def initials(name: str) -> str:
    words = [w for w in name.replace("-", " ").split() if w[:1].isalpha()]
    if not words:
        return "#"
    return (words[0][0] + (words[1][0] if len(words) > 1 else "")).upper()


def avatar_index(name: str) -> int:
    """Which of the theme's colors a caller gets (the same one every time)."""
    return hashlib.md5(name.encode()).digest()[0]


def rounded_rect(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    if r < 0.5:
        cr.rectangle(x, y, w, h)
        return
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def ease_out(t):
    return 1 - (1 - t) ** 3


def animations_on() -> bool:
    settings = Gtk.Settings.get_default()
    return settings is None or settings.props.gtk_enable_animations


def _is_timer(text):
    return bool(re.match(r"^\d+:\d\d", text or ""))


def _label(text="", *classes, xalign=0.0, wrap=False):
    lab = Gtk.Label(label=text, xalign=xalign)
    for c in classes:
        lab.get_style_context().add_class(c)
    if wrap:
        lab.set_line_wrap(True)
        lab.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lab.set_max_width_chars(34)
    else:
        lab.set_ellipsize(Pango.EllipsizeMode.END)
    return lab


def reveal(widget, visible: bool):
    """Shows a no-show-all widget with its children (show_all() skips such widgets)."""
    widget.set_visible(visible)
    if visible and isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            child.show_all()


def _pill(text, icon=None, *classes):
    b = Gtk.Button()
    box = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
    b.icon = None
    if icon:
        b.icon = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        box.pack_start(b.icon, False, False, 0)
    lab = Gtk.Label(label=text)
    box.pack_start(lab, False, False, 0)
    b.add(box)
    b.label = lab
    b.set_can_focus(False)
    for c in ("pill",) + classes:
        b.get_style_context().add_class(c)
    return b


class Tween:
    """Runs step(eased 0..1) on every frame for `ms`, then done(). With animations turned off in
    the desktop's settings it jumps straight to the end."""

    def __init__(self, widget, ms, step, done=None, out=False):
        self.widget, self.ms, self.step, self.done, self.out = widget, ms, step, done, out
        self._id = 0
        self._start = None

    def start(self):
        if not animations_on() or not self.widget.get_mapped():
            self.finish()
        else:
            self._id = self.widget.add_tick_callback(self._tick)
        return self

    def _tick(self, _w, clock):
        now = clock.get_frame_time()
        if self._start is None:
            self._start = now
        t = min(1.0, (now - self._start) / 1000 / self.ms)
        self.step(ease_out(t))
        if t >= 1.0:
            self._id = 0
            if self.done:
                self.done()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def finish(self):
        self.cancel()
        self.step(1.0)
        if self.done:
            self.done()

    def cancel(self):
        if self._id:
            self.widget.remove_tick_callback(self._id)
            self._id = 0


class CallCard(Gtk.Window):
    def __init__(self, ctrl_getter, open_settings=None, open_contacts=None):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.ctrl = ctrl_getter        # callable → Controller (set after both exist)
        self.open_settings = open_settings
        self.open_contacts = open_contacts
        self.model = None
        self.corner = None             # [card x, card y, "bottom"|"top", "right"|"left"] once moved:
                                       # the card's edges on those sides
        self.on_corner_changed = None
        self.anchor = "bottom"          # the screen edge the card grows from
        self.hside = "right"            # the side it sits against; wide matches grow the other way
        self.default_corner = "bottom-right"
        self.remember_position = False  # False: every time it opens, it's back in default_corner
        self._moving = False
        self._move_timer = 0
        self._win_h = WIN_H
        self._fade = None              # the running show/hide Tween
        self._shown = 0.0              # 0 = hidden, 1 = fully in
        self._asking_hyprland = False  # focus_now is asking Hyprland for the keyboard
        self._tick_id = 0
        self._levels = [0.0, 0.0]      # what the meter reported (them, you)
        self._shown_levels = [0.0, 0.0]  # what's drawn (falls back slowly)
        self._ring_start = None
        self._last_tick = None
        self._flash_timers = {}

        self.set_title(APP_NAME)
        self.get_style_context().add_class("deskphone")
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.stick()
        # the card follows the Omarchy theme, live: `omarchy theme set` restyles it while it's up
        self._css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self._css,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.theme = None
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("key-press-event", self._on_key)
        self.connect("button-press-event", self._on_press)
        self.connect("configure-event", self._on_configure)
        self.connect("realize", lambda *_: self._update_shape())
        self.connect("delete-event", lambda *_: (self.ctrl().dismiss(), True)[1])
        self._build()
        watcher().connect(self._on_theme)
        self._size_window()

    def _on_theme(self, theme):
        self.theme = theme
        self._css.load_from_data(card_css(theme))
        for da in (self.avatar, self.meter_them, self.meter_you):
            da.queue_draw()

    # -- layout ---------------------------------------------------------------------------------
    def _build(self):
        # content = [matches] + card (matches go on the side away from the screen edge)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.content.set_margin_start(SIDE)
        self.content.set_margin_end(SIDE)
        self.content.connect("size-allocate", lambda *_: self._update_shape())
        self.add(self.content)

        card = self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.get_style_context().add_class("card")
        card.set_size_request(WIDTH, -1)

        top = self.top_row = Gtk.Box(spacing=6)
        self.kicker = _label("", "kicker")
        self.kicker.set_max_width_chars(16)     # a long phone name ellipsizes instead of widening the card
        self.chip = _label("", "audio-chip", xalign=0.5)
        self.chip.set_no_show_all(True)
        self.contacts_btn = Gtk.Button.new_from_icon_name("x-office-address-book-symbolic", Gtk.IconSize.MENU)
        self.recent_btn = Gtk.Button.new_from_icon_name("document-open-recent-symbolic", Gtk.IconSize.MENU)
        self.settings_btn = Gtk.Button.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.MENU)
        self.close_btn = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        for b, tip in ((self.recent_btn, "Recent calls (↓ in the empty box)"),
                       (self.contacts_btn, "Contacts"), (self.settings_btn, "Settings"),
                       (self.close_btn, "Hide (the phone keeps ringing)")):
            b.get_style_context().add_class("close")
            b.set_can_focus(False)
            b.set_tooltip_text(tip)
        self.settings_btn.set_no_show_all(True)
        self.contacts_btn.set_no_show_all(True)
        self.recent_btn.set_no_show_all(True)
        self.recent_btn.connect("clicked", self._toggle_recents)
        self.contacts_btn.connect("clicked", lambda *_: self.open_contacts and self.open_contacts())
        self.settings_btn.connect("clicked", lambda *_: self.open_settings and self.open_settings())
        self.close_btn.connect("clicked", lambda *_: self.ctrl().dismiss())
        top.pack_start(self.kicker, True, True, 0)
        top.pack_start(self.chip, False, False, 0)
        top.pack_start(self.recent_btn, False, False, 0)
        top.pack_start(self.contacts_btn, False, False, 0)
        top.pack_start(self.settings_btn, False, False, 0)
        top.pack_start(self.close_btn, False, False, 0)
        card.pack_start(top, False, False, 0)

        who = Gtk.Box(spacing=10)
        self.avatar = Gtk.DrawingArea()
        self.avatar.set_size_request(AVATAR, AVATAR)
        self.avatar.set_valign(Gtk.Align.CENTER)
        self.avatar.set_no_show_all(True)
        self.avatar.connect("draw", self._draw_avatar)
        self._avatar_text, self._avatar_index = "", 0
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.title = _label("", "title")
        self.subtitle = _label("", "dim")
        self.subtitle.set_no_show_all(True)
        # the status (Ringing… / 0:42 / Call ended) cross-fades when it changes kind
        self.status = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                transition_duration=220, hhomogeneous=False, vhomogeneous=True)
        self._status_labels = [_label("", "status"), _label("", "status")]
        for i, lab in enumerate(self._status_labels):
            lab.set_max_width_chars(24)
            self.status.add_named(lab, str(i))
            lab.show()
        self.status.set_no_show_all(True)
        self.title.set_max_width_chars(20)
        self.subtitle.set_max_width_chars(24)
        names.pack_start(self.title, False, False, 0)
        names.pack_start(self.subtitle, False, False, 0)
        names.pack_start(self.status, False, False, 0)

        # live voice levels while the call is on this computer: the caller, then you
        meters = Gtk.Box(spacing=5, margin_top=4)
        self.meter_them = Gtk.DrawingArea()
        self.meter_you = Gtk.DrawingArea()
        self.mic_icon = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic", Gtk.IconSize.MENU)
        speaker = Gtk.Image.new_from_icon_name("audio-speakers-symbolic", Gtk.IconSize.MENU)
        for img in (speaker, self.mic_icon):
            img.set_pixel_size(12)
            img.get_style_context().add_class("meter")
        for i, da in enumerate((self.meter_them, self.meter_you)):
            da.set_size_request(30, 14)
            da.connect("draw", self._draw_meter, i)
        speaker.set_tooltip_text("The caller")
        self.mic_icon.set_tooltip_text("You")
        meters.pack_start(speaker, False, False, 0)
        meters.pack_start(self.meter_them, False, False, 0)
        meters.pack_start(Gtk.Box(margin_start=6), False, False, 0)
        meters.pack_start(self.mic_icon, False, False, 0)
        meters.pack_start(self.meter_you, False, False, 0)
        self.meters = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                                   transition_duration=250)
        self.meters.add(meters)
        names.pack_start(self.meters, False, False, 0)

        who.pack_start(self.avatar, False, False, 0)
        who.pack_start(names, True, True, 0)
        card.pack_start(who, False, False, 0)

        # second call: waiting or on hold
        self.banner = Gtk.Box(spacing=8)
        self.banner.get_style_context().add_class("banner")
        self.banner_text = _label("", wrap=True)
        self.banner_btns = Gtk.Box(spacing=6)
        self.banner.pack_start(self.banner_text, True, True, 0)
        self.banner.pack_start(self.banner_btns, False, False, 0)
        self.banner_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                                       transition_duration=220)
        self.banner_rev.add(self.banner)
        # hidden once folded away, so it doesn't leave the card's spacing behind
        self.banner_rev.set_no_show_all(True)
        self.banner_rev.connect("notify::child-revealed",
                                lambda r, _p: r.get_child_revealed() or r.hide())
        card.pack_start(self.banner_rev, False, False, 0)

        self.hint = _label("", "dim", wrap=True)
        self.hint.set_no_show_all(True)
        card.pack_start(self.hint, False, False, 0)
        self.error = _label("", "error", wrap=True)
        self.error.set_no_show_all(True)
        card.pack_start(self.error, False, False, 0)

        self.stack = Gtk.Stack(vhomogeneous=False, hhomogeneous=False, interpolate_size=True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(220)
        self.stack.add_named(self._build_ringing(), "ringing")
        self.stack.add_named(self._build_call(), "call")
        self.stack.add_named(self._build_idle(), "idle")
        self.stack.add_named(Gtk.Box(), "ended")
        card.pack_start(self.stack, False, False, 0)

        self._build_matches()
        self.content.pack_start(self.match_rev, False, False, 0)
        self.content.pack_start(card, False, False, 0)
        self._apply_anchor()
        self.content.show_all()

    def _build_ringing(self):
        row = Gtk.Box(spacing=10, homogeneous=True, margin_top=2)
        decline = _pill("Decline", "call-stop-symbolic", "hangup")
        answer = _pill("Answer", "call-start-symbolic", "answer")
        decline.connect("clicked", lambda *_: self.ctrl().decline())
        answer.connect("clicked", lambda *_: self.ctrl().answer())
        row.pack_start(decline, True, True, 0)
        row.pack_start(answer, True, True, 0)
        self.answer_btn = answer
        return row

    def _build_call(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.tones = _label("", "tones", xalign=0.5)
        self.tones.set_ellipsize(Pango.EllipsizeMode.START)
        box.pack_start(self.tones, False, False, 0)
        grid = Gtk.Grid(row_spacing=8, column_spacing=8, column_homogeneous=True)
        self.key_btns = {}
        for i, (digit, letters) in enumerate(KEYS):
            b = Gtk.Button()
            b.set_can_focus(False)
            b.get_style_context().add_class("key")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
            d = Gtk.Label(label=digit)
            d.get_style_context().add_class("digit")
            inner.pack_start(d, False, False, 0)
            if letters:
                lt = Gtk.Label(label=letters)
                lt.get_style_context().add_class("letters")
                inner.pack_start(lt, False, False, 0)
            b.add(inner)
            b.connect("clicked", lambda _b, k=digit: self.ctrl().press_key(k))
            grid.attach(b, i % 3, i // 3, 1, 1)
            self.key_btns[digit] = b
        self.keypad = grid
        # the keypad unfolds once the call is up
        self.keypad_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                                       transition_duration=280)
        self.keypad_rev.add(grid)
        box.pack_start(self.keypad_rev, False, False, 0)

        row = Gtk.Box(spacing=6, margin_top=2)   # not homogeneous: "To computer" is the long one
        self.mute_btn = _pill("Mute", "microphone-sensitivity-muted-symbolic")
        self.move_btn = _pill("To phone", "phone-symbolic")
        end = _pill("End", "call-stop-symbolic", "hangup")
        self.mute_btn.connect("clicked", lambda *_: self.ctrl().toggle_mute())
        self.move_btn.connect("clicked", self._on_move)
        end.connect("clicked", lambda *_: self.ctrl().hangup())
        for b in (self.mute_btn, self.move_btn, end):
            row.pack_start(b, True, True, 0)
        box.pack_start(row, False, False, 0)
        return box

    def _build_idle(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.dial_row = Gtk.Box(spacing=8)
        self.entry = Gtk.Entry(placeholder_text="Name or number")
        self.entry.get_style_context().add_class("dial")
        self.entry.connect("activate", self._on_dial)
        self.entry.connect("changed", self._on_search)
        self.entry.connect("key-press-event", self._on_entry_key)
        call = _pill("Call", "call-start-symbolic", "answer")
        call.connect("clicked", self._on_dial)
        self.dial_row.pack_start(self.entry, True, True, 0)
        self.dial_row.pack_start(call, False, False, 0)
        self.dial_row.set_no_show_all(True)
        box.pack_start(self.dial_row, False, False, 0)
        self.connect_btn = _pill("Connect", "bluetooth-active-symbolic")
        self.connect_btn.connect("clicked", lambda *_: self.ctrl().connect_phone())
        self.connect_btn.set_no_show_all(True)
        box.pack_start(self.connect_btn, False, False, 0)
        return box

    def _build_matches(self):
        """Contact matches, or the recent calls, slide out of the card on the side away from the
        screen edge (above a card at the bottom, below one at the top), so the card itself never
        moves while you type. A long list scrolls."""
        inner = Gtk.Box()
        inner.get_style_context().add_class("card")
        inner.get_style_context().add_class("popup")
        self.matches = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        self.matches.get_style_context().add_class("matches")
        self.matches.set_activate_on_single_click(True)
        self.matches.connect("row-activated", lambda _l, row: self._call_match(row))
        self.match_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                               vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        self.match_scroll.set_propagate_natural_height(True)
        self.match_scroll.set_propagate_natural_width(True)
        self.match_scroll.set_max_content_height(LIST_H)
        self.match_scroll.add(self.matches)
        inner.pack_start(self.match_scroll, True, True, 0)
        inner.set_size_request(WIDTH, -1)    # at least the card's width, at most EXTRA more
        self._recents_shown = False          # the list holds the recent calls, not matches
        self.match_rev = Gtk.Revealer(transition_duration=180)
        self.match_rev.add(inner)
        self.match_rev.connect("notify::child-revealed", lambda *_: self._update_shape())

    def matches_visible(self):
        return self.match_rev.get_reveal_child()

    def hide_matches(self):
        self.match_rev.set_reveal_child(False)
        self._recents_shown = False

    # -- recent calls (in the same list as the matches) -----------------------------------------
    def _toggle_recents(self, *_):
        if self._recents_shown and self.matches_visible():
            self.hide_matches()
        else:
            self._show_recents()

    def _show_recents(self, select=False):
        calls = self.ctrl().recent_calls()
        for row in self.matches.get_children():
            row.destroy()
        for e in calls:
            self.matches.add(self._recent_row(e))
        if not calls:
            row = Gtk.ListBoxRow(activatable=False, selectable=False)
            row.number = ""
            row.add(_label("No calls yet. Calls made or received while Desk Phone runs show here.",
                           "none", wrap=True))
            self.matches.add(row)
        self.matches.show_all()
        self.matches.unselect_all()
        self.match_scroll.get_vadjustment().set_value(0)
        self._recents_shown = True
        self.match_rev.set_reveal_child(self.get_visible())
        if select and calls:
            first = next((r for r in self.matches.get_children() if r.get_selectable()), None)
            if first is not None:
                self.matches.select_row(first)
        self._update_shape()

    def _recent_row(self, e):
        """↙ Mom   14:32: which way the call went (red when missed), who, when. Click calls back."""
        row = Gtk.ListBoxRow()
        row.number = e["number"]
        if not e["number"]:                    # a hidden number can't be called back
            row.set_activatable(False)
            row.set_selectable(False)
        tip = describe(e)
        if e["number"] and e.get("named"):
            tip += " · ⁨" + format_number(e["number"]) + "⁩"
        row.set_tooltip_text(tip)
        line = Gtk.Box(spacing=10)
        line.pack_start(_label(KIND_GLYPH.get(e["kind"], "•"), "kind", e["kind"]), False, False, 0)
        name = _label(e["title"])
        name.set_max_width_chars(30)
        line.pack_start(name, True, True, 0)
        when = _label(format_when(e["when"]), "number", xalign=1.0)
        when.set_ellipsize(Pango.EllipsizeMode.NONE)
        line.pack_start(when, False, False, 0)
        row.add(line)
        return row

    # -- window geometry ------------------------------------------------------------------------
    def _apply_anchor(self):
        """Lines the card up against the bottom or the top of the window, matches on the far side,
        and against its right or left side, so wider matches grow away from the screen edge."""
        bottom = self.anchor == "bottom"
        align = Gtk.Align.END if self.hside == "right" else Gtk.Align.START
        for w in (self.content, self.card, self.match_rev):
            w.set_halign(align)
        self.content.set_valign(Gtk.Align.END if bottom else Gtk.Align.START)
        self.content.reorder_child(self.match_rev, 0 if bottom else 1)
        self.match_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP if bottom
                                           else Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._set_slide(0)

    def _set_slide(self, off):
        """Moves the card `off` px toward the screen edge it sits against (for the slide in/out)."""
        off = int(round(off))
        if self.anchor == "bottom":
            self.content.set_margin_top(TOP)
            self.content.set_margin_bottom(max(0, BOTTOM - off))
        else:
            self.content.set_margin_top(max(0, TOP - off))
            self.content.set_margin_bottom(BOTTOM)

    def _win_w(self):
        return WIDTH + EXTRA + 2 * SIDE

    def _size_window(self):
        self.set_size_request(self._win_w(), self._win_h)
        self.resize(self._win_w(), self._win_h)

    def _grow_if_needed(self):
        _min, nat = self.content.get_preferred_height()
        need = nat + TOP + BOTTOM
        if need > self._win_h:
            self._win_h = min(need + 40, self._workarea().height)
            self._size_window()
            self.place()

    def _workarea(self):
        display = Gdk.Display.get_default()
        mon = display.get_primary_monitor() or display.get_monitor(0)
        return mon.get_workarea()

    def _card_edges(self):
        """(card x edge, card y edge, "bottom"|"top", "right"|"left"): where it was moved to, or
        the default corner with the window flush against it."""
        wa = self._workarea()
        if self.corner and len(self.corner) >= 2:
            x, y = self.corner[:2]
            anchor = self.corner[2] if len(self.corner) > 2 else "bottom"
            hside = self.corner[3] if len(self.corner) > 3 else "right"
            if len(self.corner) == 2:          # saved by an older version: the window's corner
                x, y = x - 10, y - 14
            return x, y, anchor, hside
        vert, horiz = (self.default_corner if self.default_corner in CORNERS else CORNERS[0]).split("-")
        x = wa.x + wa.width - SIDE if horiz == "right" else wa.x + SIDE
        y = wa.y + wa.height - BOTTOM if vert == "bottom" else wa.y + TOP
        return x, y, vert, horiz

    def place(self):
        xedge, edge, anchor, hside = self._card_edges()
        if (anchor, hside) != (self.anchor, self.hside):
            self.anchor, self.hside = anchor, hside
            self._apply_anchor()
        w, h = self._win_w(), self._win_h
        wa = self._workarea()
        x = xedge + SIDE - w if hside == "right" else xedge - SIDE
        y = edge + BOTTOM - h if anchor == "bottom" else edge - TOP
        x = min(max(x, wa.x), wa.x + wa.width - w)
        y = min(max(y, wa.y), wa.y + wa.height - h)
        self.move(int(x), int(y))
        return False

    def card_rect(self):
        """The card's visible rectangle in the window (x, y, w, h)."""
        res = self.card.translate_coordinates(self, 0, 0)
        x, y = (res[0], res[1]) if res else (SIDE, 0)
        return x, y, self.card.get_allocated_width(), self.card.get_allocated_height()

    def _update_shape(self):
        """Clicks land only on the card (and the matches); everywhere else goes to what's below."""
        win = self.get_window()
        if win is None:
            return
        region = cairo.Region(cairo.RectangleInt(*self.card_rect()))
        if self.match_rev.get_child_revealed() or self.match_rev.get_reveal_child():
            res = self.match_rev.translate_coordinates(self, 0, 0)
            if res:
                region.union(cairo.RectangleInt(res[0], res[1], self.match_rev.get_allocated_width(),
                                                self.match_rev.get_allocated_height()))
        win.input_shape_combine_region(region, 0, 0)

    # -- rendering ------------------------------------------------------------------------------
    def render(self, m):
        prev = self.model
        self.model = m
        if m is None or m.mode != "idle":
            self.hide_matches()
        if m is None:
            self._animate_out()
            self._update_ticking()
            return
        mode_changed = prev is None or prev.mode != m.mode
        kicker = {"ringing": "INCOMING CALL", "call": "CALL", "ended": "", "idle": "DESK PHONE"}[m.mode]
        if m.phone_name and m.mode in ("ringing", "call"):
            kicker += f" · {m.phone_name.upper()}"
        self.kicker.set_text(kicker)

        chip = {"computer": "● On this computer", "phone": "On the phone",
                "connecting": "Connecting…"}.get(m.audio, "")
        show_chip = m.mode == "call" and bool(chip)
        self.chip.set_text(chip)
        ctx = self.chip.get_style_context()
        (ctx.add_class if m.audio == "computer" else ctx.remove_class)("here")
        self.chip.set_visible(show_chip)
        self.settings_btn.set_visible(m.mode == "idle")
        self.contacts_btn.set_visible(m.mode == "idle")
        self.recent_btn.set_visible(m.mode == "idle" and m.recents)
        if self._recents_shown and not (m.mode == "idle" and m.recents):
            self.hide_matches()
        self.close_btn.set_visible(m.mode != "ended")
        self.top_row.set_visible(m.mode != "ended")
        self.close_btn.set_tooltip_text("Hide (the phone keeps ringing)" if m.mode == "ringing" else "Hide")

        is_idle = m.mode == "idle"
        self.avatar.set_visible(not is_idle)
        if not is_idle:
            self._avatar_text = initials(m.title) if m.subtitle or not m.title[:1] in "+0123456789" else "#"
            self._avatar_index = avatar_index(m.title)
            self.avatar.queue_draw()
        self.title.set_text(m.title if not is_idle else m.status)
        self.subtitle.set_text(m.subtitle)
        self.subtitle.set_visible(bool(m.subtitle))
        self._set_status(m.status if not is_idle else "")

        if m.waiting:
            self._set_banner(f"‎{m.waiting}‎ is calling", [("Decline", "hangup", "decline_waiting"),
                                                                     ("Answer", "answer", "answer_waiting")])
        elif m.held:
            self._set_banner(f"On hold: {m.held}‎", [("Swap", None, "swap")])
        else:
            self.banner_rev.set_reveal_child(False)

        self.hint.set_text(m.hint)
        self.hint.set_visible(bool(m.hint))
        self.error.set_text(m.error)
        self.error.set_visible(bool(m.error))

        ctx = self.answer_btn.get_style_context()
        (ctx.add_class if m.mode == "ringing" else ctx.remove_class)("breathe")
        if m.mode == "ringing" and (prev is None or prev.mode != "ringing"):
            self._ring_start = None
        # a fresh card shows its page at once; a card already up cross-fades to the new one
        self.stack.set_visible_child_full(
            m.mode, Gtk.StackTransitionType.CROSSFADE if self._shown > 0 and self.get_visible()
            else Gtk.StackTransitionType.NONE)
        live = m.mode == "call" and m.audio == "computer"
        self.meters.set_reveal_child(live)
        if m.mode == "call":
            self.tones.set_text(m.tones or " ")
            self.mute_btn.label.set_text("Muted" if m.muted else "Mute")
            self.mute_btn.icon.set_from_icon_name("microphone-sensitivity-muted-symbolic" if m.muted
                                                  else "audio-input-microphone-symbolic", Gtk.IconSize.BUTTON)
            ctx = self.mute_btn.get_style_context()
            (ctx.add_class if m.muted else ctx.remove_class)("toggled")
            ctx = self.mic_icon.get_style_context()
            (ctx.add_class if m.muted else ctx.remove_class)("off")
            self.mic_icon.set_from_icon_name("microphone-sensitivity-muted-symbolic" if m.muted
                                             else "audio-input-microphone-symbolic", Gtk.IconSize.MENU)
            self.mic_icon.set_pixel_size(12)
            self.mute_btn.set_visible(m.audio != "phone")    # muting is for the computer's mic
            self.mute_btn.set_sensitive(m.audio == "computer")
            here = m.audio in ("computer", "connecting")
            self.move_btn.label.set_text("To phone" if here else "To computer")
            self.move_btn.icon.set_from_icon_name("phone-symbolic" if here else "computer-symbolic",
                                                  Gtk.IconSize.BUTTON)
            self.move_btn.set_sensitive(m.audio != "connecting")
            self.keypad.set_sensitive(m.status not in ("Calling…", "Ringing…"))
            if mode_changed:
                # the keypad unfolds after the card has turned into the call
                self.keypad_rev.set_transition_duration(0)
                self.keypad_rev.set_reveal_child(False)
                self.keypad_rev.set_transition_duration(280)
                GLib.timeout_add(120 if self._shown > 0 else 0, self._unfold_keypad)
        if is_idle:
            reveal(self.dial_row, m.can_dial)
            reveal(self.connect_btn, not m.phone_ready)

        # Ringing must not take the keyboard from what you're typing; a call or the dialer may.
        wants_focus = m.mode in ("call", "idle")
        self.set_accept_focus(wants_focus)
        self.set_focus_on_map(m.mode == "idle")
        if m.focus and m.mode == "call":
            # answered here: digits typed now go into the call. Not on Hyprland, where this would
            # take the keyboard from what you're typing; click the card (or Shift + the phone key).
            GLib.idle_add(self.focus_now, False)
        self._grow_if_needed()
        appearing = not self.get_visible() or self._hiding()
        if appearing:
            self._animate_in()
            if m.mode == "idle":
                self.focus_now()
                self.entry.grab_focus()
        self._update_ticking()

    def _unfold_keypad(self):
        if self.model is not None and self.model.mode == "call":
            self.keypad_rev.set_reveal_child(True)
        return False

    def _set_status(self, text):
        self.status.set_visible(bool(text))
        cur = self.status.get_visible_child()
        if cur.get_text() == text:
            return
        if _is_timer(text) and _is_timer(cur.get_text()):
            cur.set_text(text)                # the clock ticking: no fade every second
            return
        other = self._status_labels[1] if cur is self._status_labels[0] else self._status_labels[0]
        other.set_text(text)
        self.status.set_visible_child(other)

    def _set_banner(self, text, buttons):
        self.banner_text.set_text(text)
        for child in self.banner_btns.get_children():
            child.destroy()
        for label, cls, action in buttons:
            b = _pill(label, None, *((cls,) if cls else ()))
            b.connect("clicked", lambda _b, a=action: getattr(self.ctrl(), a)())
            self.banner_btns.pack_start(b, False, False, 0)
        self.banner.show_all()
        self.banner_rev.show()
        self.banner_rev.set_reveal_child(True)

    def focus_now(self, ask_hyprland=True):
        """present() with a fresh X server time: opened by a global key, the window carries no
        user event, and mutter's focus-stealing prevention would otherwise leave it unfocused."""
        try:
            gi.require_version("GdkX11", "3.0")
            from gi.repository import GdkX11
            win = self.get_window()
            if win is not None:
                self.present_with_time(GdkX11.x11_get_server_time(win))
                return
        except (ImportError, ValueError, TypeError):
            pass
        self.present()
        if ask_hyprland and on_hyprland() and not self._asking_hyprland:
            # a window that was only just shown isn't known to Hyprland yet: ask a few times
            tries = [15]

            def ask():
                tries[0] -= 1
                again = (self.get_visible() and not self._hiding() and tries[0] > 0
                         and not focus_own_window(self.get_title()))
                self._asking_hyprland = again
                return again
            self._asking_hyprland = True
            GLib.timeout_add(40, ask)

    # -- appearing and leaving ------------------------------------------------------------------
    def _hiding(self):
        return self._fade is not None and self._fade.out

    def _animate_in(self):
        if self._fade:
            self._fade.cancel()
        if not self.get_visible():
            self._shown = 0.0
            if not self.remember_position:
                self.corner = None             # back to the default corner every time it opens
            self.place()
            self.content.set_opacity(0.0)
            self._set_slide(SLIDE)
            self.show()
            # again once it's mapped: a move made while it was hidden isn't always applied when
            # it's shown again (it's still fully transparent, so nothing visibly jumps)
            self.place()
        start = self._shown

        def step(t):
            self._shown = start + (1 - start) * t
            self.content.set_opacity(self._shown)
            self._set_slide(SLIDE * (1 - self._shown))
        self._fade = Tween(self.content, 240 * (1 - start) + 1, step, done=self._faded)
        self._fade.start()

    def _animate_out(self):
        if not self.get_visible() or self._hiding():
            return
        hand_focus_back()              # (Hyprland: or closing the card closes the scratchpad)
        if self._fade:
            self._fade.cancel()
        start = self._shown

        def step(t):
            self._shown = start * (1 - t)
            self.content.set_opacity(self._shown)
            self._set_slide(SLIDE * 0.6 * (1 - self._shown))

        def done():
            self._faded()
            self.hide()
            self._shown = 0.0
            self.content.set_opacity(1.0)
            self._set_slide(0)
        self._fade = Tween(self.content, 170 * start + 1, step, done=done, out=True)
        self._fade.start()

    def _faded(self):
        self._fade = None

    # -- the avatar, its rings, and the voice meters --------------------------------------------
    def set_levels(self, them: float, you: float):
        """Called by the level meter about 20 times a second while the call is on this computer."""
        self._levels = [them, you]
        self._update_ticking()

    def _needs_ticks(self):
        m = self.model
        if m is None or not self.get_visible():
            return False
        live = m.mode == "call" and m.audio == "computer"
        return m.mode == "ringing" or live or any(v > 0.01 for v in self._shown_levels)

    def _update_ticking(self):
        if self._needs_ticks() and not self._tick_id:
            self._last_tick = None
            self._tick_id = self.avatar.add_tick_callback(self._tick)

    def _tick(self, _w, clock):
        now = clock.get_frame_time() / 1e6
        dt = 0.016 if self._last_tick is None else min(0.1, now - self._last_tick)
        self._last_tick = now
        if self._ring_start is None:
            self._ring_start = now
        self._ring_t = now - self._ring_start
        m = self.model
        muted = m is not None and m.muted
        for i, target in enumerate(self._levels):
            if i == 1 and muted:
                target = 0.0
            cur = self._shown_levels[i]
            # jump up quickly, fall back slowly, like a VU meter
            self._shown_levels[i] = target if target > cur else max(target, cur - 1.6 * dt)
        self._meter_t = now
        self.avatar.queue_draw()
        self.meter_them.queue_draw()
        self.meter_you.queue_draw()
        if not self._needs_ticks():
            self._tick_id = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    _ring_t = 0.0
    _meter_t = 0.0

    def _draw_avatar(self, da, cr):
        """A tile in one of the theme's colors with the caller's initials, outlined like the
        Omarchy shell's controls; while it rings, outlines ripple out from it."""
        w, h = da.get_allocated_width(), da.get_allocated_height()
        cx, cy = w / 2, h / 2
        r = 24.0                                       # half the tile
        t = self.theme
        corner = min(t.radius * 1.4, r)                # as round as the windows, scaled up a bit
        red, green, blue, _a = t.avatar_color(self._avatar_index)
        m = self.model
        if m is not None and m.mode == "ringing":
            if animations_on():
                for i in range(3):                     # outlines ripple out from the tile
                    p = (self._ring_t / 1.8 + i / 3) % 1.0
                    grow = 1 + (w / 2 - r - 2) * p
                    cr.set_source_rgba(red, green, blue, 0.85 * (1 - p) ** 1.4)
                    cr.set_line_width(2.0 - 1.0 * p)
                    rounded_rect(cr, cx - r - grow, cy - r - grow, 2 * (r + grow), 2 * (r + grow),
                                 corner + grow * (corner / r))
                    cr.stroke()
            else:
                cr.set_source_rgba(red, green, blue, 0.4)
                cr.set_line_width(2.0)
                rounded_rect(cr, cx - r - 4, cy - r - 4, 2 * r + 8, 2 * r + 8, corner + 4 * (corner / r))
                cr.stroke()
        elif m is not None and m.mode == "call" and m.audio == "computer":
            lvl = self._shown_levels[0]                # an outline that follows the caller's voice
            if lvl > 0.01:
                grow = 1 + 5 * lvl
                cr.set_source_rgba(red, green, blue, 0.25 + 0.5 * lvl)
                cr.set_line_width(1.5)
                rounded_rect(cr, cx - r - grow, cy - r - grow, 2 * (r + grow), 2 * (r + grow),
                             corner + grow * (corner / r))
                cr.stroke()
        bw = t.border_width
        rounded_rect(cr, cx - r + bw / 2, cy - r + bw / 2, 2 * r - bw, 2 * r - bw, corner)
        cr.set_source_rgba(red, green, blue, 0.16)
        cr.fill_preserve()
        cr.set_source_rgba(red, green, blue, 1.0)
        cr.set_line_width(bw)
        cr.stroke()
        layout = PangoCairo.create_layout(cr)
        font = Pango.FontDescription.from_string("monospace Bold 15")
        layout.set_font_description(font)
        layout.set_text(self._avatar_text, -1)
        _ink, logical = layout.get_pixel_extents()
        cr.set_source_rgba(red, green, blue, 1.0)
        cr.move_to(cx - logical.width / 2 - logical.x, cy - logical.height / 2 - logical.y)
        PangoCairo.show_layout(cr, layout)
        return True

    def _draw_meter(self, da, cr, which):
        w, h = da.get_allocated_width(), da.get_allocated_height()
        n = len(BAR_SHAPE)
        gap = 2.0
        bw = (w - gap * (n - 1)) / n
        lvl = self._shown_levels[which]
        m = self.model
        t = self.theme
        muted = which == 1 and m is not None and m.muted
        for i, shape in enumerate(BAR_SHAPE):
            wobble = 0.85 + 0.15 * math.sin(self._meter_t * 9 + i * 1.7)
            bh = max(2.0, (h - 1) * min(1.0, lvl * shape * wobble * 1.25))
            x, y = i * (bw + gap), (h - bh) / 2
            if muted:
                cr.set_source_rgba(*t.red[:3], 0.6)
            elif lvl > 0.02:
                cr.set_source_rgba(*t.green[:3], 0.55 + 0.45 * min(1, lvl * 1.5))
            else:
                cr.set_source_rgba(*t.fg[:3], 0.22)
            rounded_rect(cr, x, y, bw, bh, bw / 2 if t.radius else 0)
            cr.fill()
        return True

    # -- moving the card ------------------------------------------------------------------------
    def _on_press(self, _w, ev):
        # On Wayland (Hyprland) the window rule places the card and a window can't read its own
        # position, so dragging couldn't be remembered; Super+drag still moves it for the moment.
        if "Wayland" in type(self.get_display()).__name__:
            return False
        if ev.button == 1 and ev.type == Gdk.EventType.BUTTON_PRESS:
            self._moving = True
            self.begin_move_drag(ev.button, int(ev.x_root), int(ev.y_root), ev.time)
            return True
        return False

    def _on_configure(self, _w, ev):
        if self._moving:
            if self._move_timer:
                GLib.source_remove(self._move_timer)
            self._move_timer = GLib.timeout_add(500, self._moved)
        return False

    def _moved(self):
        """After a drag: remember where the card is, and which screen edge it now grows from
        (the bottom, unless it's high up and the window would stick out above the screen)."""
        self._move_timer = 0
        self._moving = False
        wx, wy = self.get_position()
        cx, cy, cw, ch = self.card_rect()
        left, right, top, bottom = wx + cx, wx + cx + cw, wy + cy, wy + cy + ch
        wa = self._workarea()
        fits_bottom = bottom + BOTTOM - self._win_h >= wa.y
        fits_top = top - TOP + self._win_h <= wa.y + wa.height
        upper = (top + bottom) / 2 < wa.y + wa.height / 2
        anchor = "top" if fits_top and (upper or not fits_bottom) else "bottom"
        fits_right = right + SIDE - self._win_w() >= wa.x
        fits_left = left - SIDE + self._win_w() <= wa.x + wa.width
        on_left = (left + right) / 2 < wa.x + wa.width / 2
        hside = "left" if fits_left and (on_left or not fits_right) else "right"
        self.corner = [right if hside == "right" else left, bottom if anchor == "bottom" else top,
                       anchor, hside]
        if (anchor, hside) != (self.anchor, self.hside):
            self.place()
        if self.on_corner_changed:
            self.on_corner_changed(self.corner)
        return False

    # -- input ----------------------------------------------------------------------------------
    def _on_move(self, *_):
        m = self.model
        if m and m.audio in ("computer", "connecting"):
            self.ctrl().to_phone()
        else:
            self.ctrl().to_computer()

    def _on_dial(self, *_):
        row = self.matches.get_selected_row() if self.matches_visible() else None
        if row is not None:
            self._call_match(row)
            return
        text = self.entry.get_text()
        self.entry.set_text("")
        self.ctrl().dial(text)

    def _call_match(self, row):
        if not row.number:
            return
        self.entry.set_text("")
        self.hide_matches()
        self.ctrl().dial(row.number)

    def _on_search(self, *_):
        found = self.ctrl().search_contacts(self.entry.get_text())
        self._recents_shown = False
        if found:
            for row in self.matches.get_children():
                row.destroy()
        for name, number in found:
            row = Gtk.ListBoxRow()
            row.number = number
            line = Gtk.Box(spacing=10)
            name_label = _label(name)
            name_label.set_max_width_chars(36)     # wider than that ellipsizes: the window never grows
            line.pack_start(name_label, True, True, 0)
            num = _label(format_number(number), "number", xalign=1.0)
            num.set_ellipsize(Pango.EllipsizeMode.NONE)
            line.pack_start(num, False, False, 0)
            row.add(line)
            self.matches.add(row)
        self.matches.show_all()
        # (the old rows stay while the list slides away, so it doesn't collapse empty)
        self.match_rev.set_reveal_child(bool(found) and self.get_visible())
        self._update_shape()
        # Enter takes the top match for a name; for digits only if it's the very number typed,
        # so a full number that isn't saved still dials as typed.
        text = self.entry.get_text()
        if found and (any(ch.isalpha() for ch in text)
                      or match_key(found[0][1]) == match_key(text)):
            self.matches.select_row(self.matches.get_row_at_index(0))
        else:
            self.matches.unselect_all()

    def _on_entry_key(self, _w, ev):
        """Up/Down pick among the matches while typing stays in the box. Down in the empty box
        opens the recent calls."""
        if ev.keyval not in (Gdk.KEY_Up, Gdk.KEY_Down):
            return False
        if not self.matches_visible():
            if (ev.keyval == Gdk.KEY_Down and not self.entry.get_text()
                    and self.model is not None and self.model.recents):
                self._show_recents(select=True)
                return True
            return False
        rows = [r for r in self.matches.get_children() if r.get_selectable()]
        if not rows:
            return True
        cur = self.matches.get_selected_row()
        i = rows.index(cur) if cur in rows else -1
        i = max(0, min(len(rows) - 1, i + (1 if ev.keyval == Gdk.KEY_Down else -1)))
        self.matches.select_row(rows[i])
        alloc = rows[i].get_allocation()          # keep it in view when the list scrolls
        self.match_scroll.get_vadjustment().clamp_page(alloc.y, alloc.y + alloc.height)
        return True

    def flash_key(self, key):
        """A typed digit lights up its key on the keypad, as if it were pressed."""
        b = self.key_btns.get(key)
        if b is None:
            return
        b.get_style_context().add_class("flash")
        if key in self._flash_timers:
            GLib.source_remove(self._flash_timers[key])

        def off():
            b.get_style_context().remove_class("flash")
            self._flash_timers.pop(key, None)
            return False
        self._flash_timers[key] = GLib.timeout_add(130, off)

    def _on_key(self, _w, ev):
        m = self.model
        if m is None:
            return False
        if ev.keyval == Gdk.KEY_Escape:
            self.ctrl().dismiss()
            return True
        if m.mode != "call":
            return False
        ch = chr(Gdk.keyval_to_unicode(ev.keyval) or 0)
        if ev.keyval == Gdk.KEY_KP_Multiply:
            ch = "*"
        if ch and ch in "0123456789*#":
            self.flash_key(ch)
            self.ctrl().press_key(ch)
            return True
        return False
