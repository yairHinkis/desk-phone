"""The Omarchy theme, read live: colors, the window border, corner rounding and the font.

Omarchy stages the current theme in ~/.local/state/omarchy/current/theme (colors.toml, and the
shell.toml its own bar, notifications and menus are drawn from) and rewrites theme.generation on
every switch. Theme.watch() follows that, so the card changes with `omarchy theme set` while it
runs. Hyprland's rounding and border width are read from hyprctl, so the card's corners and
border match the windows'. Off Omarchy everything falls back to Omarchy's default look.
"""

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

from gi.repository import Gdk, Gio, GLib, Gtk

STATE = Path.home() / ".local/state/omarchy/current"

# Omarchy's default theme (Tokyo Night), for when there's no Omarchy theme to read
DEFAULTS = {
    "mode": "dark",
    "background": "#1a1b26", "foreground": "#a9b1d6", "accent": "#7aa2f7",
    "red": "#f7768e", "green": "#9ece6a", "yellow": "#e0af68", "orange": "#ff9e64",
    "blue": "#7aa2f7", "magenta": "#bb9af7", "cyan": "#7dcfff",
}
CONTROLS = {
    "normal-fill-alpha": 0.04, "normal-border-alpha": 0.4,
    "hover-cursor-fill-alpha": 0.08, "hover-cursor-border-alpha": 0.25,
    "selected-fill-alpha": 0.18, "pressed-fill-alpha": 0.22, "selection-fill-alpha": 0.35,
}
_COLOR = re.compile(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?|rgba?\(\s*[0-9a-fA-F]{6,8}\s*\)")


def parse_color(text):
    """'#rrggbb', '#rrggbbaa', or Hyprland's 'rgb(rrggbb)' / 'rgba(rrggbbaa)' → (r, g, b, a) in 0..1."""
    m = _COLOR.search(text or "")
    if not m:
        return None
    hexes = re.sub(r"[^0-9a-fA-F]", "", m.group(0).replace("rgba", "").replace("rgb", ""))
    vals = [int(hexes[i:i + 2], 16) / 255 for i in range(0, len(hexes), 2)]
    return tuple(vals + [1.0] * (4 - len(vals)))


def parse_gradient(text):
    """A solid color or a Hyprland gradient ('rgba(..) rgba(..) 45deg') → ([colors], angle)."""
    colors = [parse_color(m.group(0)) for m in _COLOR.finditer(text or "")]
    angle = re.search(r"(-?\d+)\s*deg", text or "")
    return colors, int(angle.group(1)) if angle else 0


def css_rgba(c, alpha=1.0):
    r, g, b, a = c
    return f"rgba({round(r * 255)},{round(g * 255)},{round(b * 255)},{a * alpha:.3f})"


def _read_toml(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _hypr_option(name):
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return None
    try:
        out = subprocess.run(["hyprctl", "-j", "getoption", name], capture_output=True,
                             text=True, timeout=1).stdout
        return json.loads(out).get("int")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


class Theme:
    """One snapshot of the theme. Colors are (r, g, b, a) tuples in 0..1."""

    def __init__(self, colors=None, shell=None, rounding=None, border_size=None):
        colors = {**DEFAULTS, **(colors or {})}
        shell = shell or {}
        self.dark = colors.get("mode", "dark") != "light"
        pick = lambda k: parse_color(str(colors.get(k))) or parse_color(DEFAULTS.get(k, "#808080"))
        self.bg, self.fg, self.accent = pick("background"), pick("foreground"), pick("accent")
        self.red, self.green, self.yellow = pick("red"), pick("green"), pick("yellow")
        self.orange, self.blue = pick("orange"), pick("blue")
        self.magenta, self.cyan = pick("magenta"), pick("cyan")
        self.palette = [self.blue, self.cyan, self.green, self.yellow, self.orange, self.red,
                        self.magenta]

        hypr = shell.get("hyprland", {})
        notif = shell.get("notifications", {})
        border = notif.get("border", "hyprland.active-border")
        if border.startswith("hyprland."):
            border = hypr.get(border.split(".", 1)[1], "")
        self.border, self.border_angle = parse_gradient(border)
        if not self.border:
            self.border = [self.accent]
        self.surface_alpha = float(notif.get("background-alpha", 1.0))
        bg = parse_color(notif.get("background", ""))
        if bg:
            self.bg = bg
        controls = shell.get("controls", {})
        self.controls = {k: float(controls.get(k, v)) for k, v in CONTROLS.items()}

        self.radius = max(0, min(int(rounding if rounding is not None else 0), 18))
        self.border_width = max(1, min(int(border_size if border_size is not None else 2), 3))

    @classmethod
    def load(cls):
        folder = STATE / "theme"
        return cls(_read_toml(folder / "colors.toml"), _read_toml(folder / "shell.toml"),
                   _hypr_option("decoration:rounding"), _hypr_option("general:border_size"))

    def avatar_color(self, index):
        return self.palette[index % len(self.palette)]

    def border_css(self, width=None):
        """CSS for the card's border: the Hyprland active border, a gradient one included."""
        width = self.border_width if width is None else width
        first = self.border[0]
        if len(self.border) > 1 and self.radius == 0:      # (border-image can't round corners)
            stops = ", ".join(css_rgba(c) for c in self.border)
            return (f"border: {width}px solid {css_rgba(first)}; "
                    f"border-image: linear-gradient({self.border_angle + 90}deg, {stops}) 1;")
        return f"border: {width}px solid {css_rgba(first)};"


class ThemeWatcher:
    """Keeps a Theme current and tells listeners when `omarchy theme set` switched it."""

    def __init__(self):
        self.theme = Theme.load()
        self._listeners = []
        self._timer = 0
        self._monitor = None
        try:
            self._monitor = Gio.File.new_for_path(str(STATE)).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._monitor.connect("changed", self._on_changed)
        except GLib.Error:
            pass

    def connect(self, callback):
        self._listeners.append(callback)
        callback(self.theme)

    def _on_changed(self, _mon, file, _other, _event):
        if file.get_basename() not in ("theme.generation", "theme.name", "theme"):
            return
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(300, self._reload)   # the switch writes several files

    def _reload(self):
        self._timer = 0
        self.theme = Theme.load()
        for callback in self._listeners:
            callback(self.theme)
        return False


_watcher = None


def watcher():
    global _watcher
    if _watcher is None:
        _watcher = ThemeWatcher()
    return _watcher


def windows_css(t):
    """The settings and contacts windows (and their dialogs, menus and tooltips) in the theme."""
    fg, bg, ac = t.fg, t.bg, t.accent
    c = t.controls
    r = f"{t.radius}px"
    return f"""
window.background:not(.deskphone), dialog.background, messagedialog.background {{
  background-color: {css_rgba(bg)}; color: {css_rgba(fg)}; font-family: monospace;
}}
window:not(.deskphone) label, messagedialog label {{ color: {css_rgba(fg)}; }}
window:not(.deskphone) label.dim-label {{ color: {css_rgba(fg, 0.6)}; }}
headerbar, headerbar:backdrop {{
  background-image: none; background-color: {css_rgba(bg)}; color: {css_rgba(fg)};
  border: none; box-shadow: inset 0 -1px {css_rgba(fg, 0.15)};
}}
headerbar .title {{ font-weight: bold; }}
headerbar .subtitle {{ color: {css_rgba(fg, 0.6)}; }}
window:not(.deskphone) button, messagedialog button, headerbar button {{
  background-image: none; background-color: {css_rgba(fg, c['normal-fill-alpha'])};
  color: {css_rgba(fg)}; border: 1px solid {css_rgba(fg, c['normal-border-alpha'])};
  border-radius: {r}; box-shadow: none; text-shadow: none; -gtk-icon-shadow: none;
}}
window:not(.deskphone) button:hover, messagedialog button:hover, headerbar button:hover {{
  background-color: {css_rgba(fg, c['hover-cursor-fill-alpha'])};
}}
window:not(.deskphone) button:active, messagedialog button:active {{
  background-color: {css_rgba(fg, c['pressed-fill-alpha'])};
}}
window:not(.deskphone) button:disabled {{ color: {css_rgba(fg, 0.4)}; border-color: {css_rgba(fg, 0.15)}; }}
window:not(.deskphone) button.flat, window:not(.deskphone) list button {{
  background-color: transparent; border-color: transparent;
}}
window:not(.deskphone) list button:hover {{
  background-color: {css_rgba(fg, c['hover-cursor-fill-alpha'])}; color: {css_rgba(ac)};
  border-color: {css_rgba(fg, c['hover-cursor-border-alpha'])};
}}
headerbar button.titlebutton {{ background-color: transparent; border-color: transparent; }}
window:not(.deskphone) entry {{
  background-image: none; background-color: {css_rgba(fg, c['normal-fill-alpha'])};
  color: {css_rgba(fg)}; border: 1px solid {css_rgba(fg, c['normal-border-alpha'])};
  border-radius: {r}; box-shadow: none; caret-color: {css_rgba(ac)};
}}
window:not(.deskphone) entry:focus {{ border-color: {css_rgba(ac)}; }}
window:not(.deskphone) entry selection, window:not(.deskphone) label selection {{
  background-color: {css_rgba(ac, c['selection-fill-alpha'])}; color: {css_rgba(fg)};
}}
window:not(.deskphone) list, window:not(.deskphone) scrolledwindow {{ background-color: transparent; }}
window:not(.deskphone) list row {{ border-radius: {r}; }}
window:not(.deskphone) list row:hover {{ background-color: {css_rgba(fg, 0.04)}; }}
window:not(.deskphone) switch {{
  background-image: none; background-color: {css_rgba(fg, c['normal-fill-alpha'])};
  border: 1px solid {css_rgba(fg, c['normal-border-alpha'])}; border-radius: {r}; box-shadow: none;
  color: {css_rgba(fg)};
}}
window:not(.deskphone) switch:checked {{
  background-color: {css_rgba(ac, 0.25)}; border-color: {css_rgba(ac)}; color: {css_rgba(fg)};
}}
window:not(.deskphone) switch slider {{
  background-image: none; background-color: {css_rgba(fg, 0.7)}; border: none;
  border-radius: {r}; box-shadow: none; min-width: 22px; min-height: 22px;
}}
window:not(.deskphone) switch:checked slider {{ background-color: {css_rgba(ac)}; }}
window:not(.deskphone) switch:disabled {{ opacity: 0.45; }}
menu, .menu, .context-menu, popover.background {{
  background-color: {css_rgba(bg)}; color: {css_rgba(fg)}; font-family: monospace;
  border: 1px solid {css_rgba(fg, 0.25)}; border-radius: {r}; box-shadow: none;
}}
menu menuitem:hover, .menu menuitem:hover {{
  background-color: {css_rgba(fg, c['hover-cursor-fill-alpha'])}; color: {css_rgba(ac)};
}}
tooltip, tooltip.background {{
  background-color: {css_rgba(bg, 0.97)}; color: {css_rgba(fg)}; font-family: monospace;
  border: 1px solid {css_rgba(fg)}; border-radius: {r}; box-shadow: none;
}}
tooltip label {{ color: {css_rgba(fg)}; }}
scrollbar slider {{ background-color: {css_rgba(fg, 0.3)}; border-radius: {r}; }}
""".encode()


def install_window_css():
    """Styles every non-card window of this app from the theme, and keeps it current."""
    provider = Gtk.CssProvider()
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def apply(t):
        provider.load_from_data(windows_css(t))
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", t.dark)
    watcher().connect(apply)
    return provider
