"""Desk Phone's global keys: GNOME custom shortcuts (Settings → Keyboard → Custom Shortcuts), or on
Hyprland (Omarchy) a marked block of o.bind lines in ~/.config/hypr/bindings.lua.

Each one asks the running Desk Phone over D-Bus (org.gtk.Actions) to do its action, which is
instant; nothing happens when Desk Phone isn't running.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import APP_ID, APP_NAME

MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM = MEDIA_KEYS + ".custom-keybinding"
BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
OBJECT_PATH = "/" + APP_ID.replace(".", "/")

# One key with modifiers. The laptop's "phone link" key (F11 without Fn, KEY_LINK_PHONE) when the
# keyboard data knows it; otherwise the "switch display" key (F7 without Fn, unused with one screen).
PHONE_KEY = "XF86LinkPhone"
FALLBACK_KEY = "XF86Display"
XKB_INET = Path("/usr/share/X11/xkb/symbols/inet")

SHORTCUTS = [   # (dconf key, name, modifiers, action)
    ("desk-phone-answer", "Desk Phone: answer / hang up / dialer", "", "answer-or-hangup"),
    ("desk-phone-reject", "Desk Phone: decline", "<Shift>", "decline-or-show"),
    ("desk-phone-mute", "Desk Phone: mute call / silence ring", "<Primary>", "mute"),
]
RETIRED = ["desk-phone-decline", "desk-phone-call", "desk-phone-decline-or-dial"]   # earlier versions'


def default_key() -> str:
    try:
        return PHONE_KEY if PHONE_KEY in XKB_INET.read_text() else FALLBACK_KEY
    except OSError:
        return FALLBACK_KEY


def bindings(key: str) -> dict[str, str]:
    return {name: mods + key for name, _label, mods, _action in SHORTCUTS}


def key_name(key: str) -> str:
    return {PHONE_KEY: "the phone key (XF86LinkPhone)",
            FALLBACK_KEY: "the screen key (XF86Display)"}.get(key, key)


def _launcher_expr() -> str:
    """The launcher as the key's shell command names it: the one in the user's ~/.local/bin, or a
    package's (/usr/bin/desk-phone)."""
    from .hostsetup import launcher, user_launcher
    exe = launcher()
    return '"$HOME/.local/bin/desk-phone"' if exe == user_launcher() else f'"{exe}"'


def command(action: str) -> str:
    cmd = (f"gdbus call --session --dest {APP_ID} --object-path {OBJECT_PATH} "
           f"--method org.gtk.Actions.Activate {action} [] {{}}")
    if action == "answer-or-hangup":
        # Opening the dialer also starts Desk Phone when it isn't running.
        return f"sh -c '{cmd} || exec {_launcher_expr()}'"
    return cmd


def merged(current: list[str], add: bool) -> list[str]:
    ours = [BASE + key + "/" for key, *_ in SHORTCUTS]
    retired = [BASE + key + "/" for key in RETIRED]
    rest = [p for p in current if p not in ours and p not in retired]
    return rest + ours if add else rest


def _settings():
    from gi.repository import Gio
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup(CUSTOM, True) is None:
        raise RuntimeError("GNOME's shortcut settings aren't available here")
    return Gio.Settings.new(MEDIA_KEYS), lambda path: Gio.Settings.new_with_path(CUSTOM, path)


def install(base_key: str):
    keys, custom = _settings()
    for key, name, mods, action in SHORTCUTS:
        s = custom(BASE + key + "/")
        s.set_string("name", name)
        s.set_string("command", command(action))
        s.set_string("binding", mods + base_key)
    _reset(custom, RETIRED)
    keys.set_strv("custom-keybindings", merged(keys.get_strv("custom-keybindings"), True))
    from gi.repository import Gio
    Gio.Settings.sync()


def _reset(custom, keys_):
    for key in keys_:
        s = custom(BASE + key + "/")
        for k in ("name", "command", "binding"):
            s.reset(k)


def remove():
    keys, custom = _settings()
    _reset(custom, RETIRED)
    for key, *_ in SHORTCUTS:
        s = custom(BASE + key + "/")
        for k in ("name", "command", "binding"):
            s.reset(k)
    keys.set_strv("custom-keybindings", merged(keys.get_strv("custom-keybindings"), False))
    from gi.repository import Gio
    Gio.Settings.sync()


# Hyprland (Omarchy): the keys are a marked block in the user's bindings file, loaded after Omarchy's
# defaults. Rerunning the installer replaces the block; removing takes out only the block.
HYPR_BINDINGS = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "hypr" / "bindings.lua"
HYPR_BEGIN = "-- >>> desk-phone >>>"
HYPR_END = "-- <<< desk-phone <<<"
HYPR_MODS = {"": "", "<Shift>": "SHIFT + ", "<Primary>": "CTRL + "}


def on_hyprland() -> bool:
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def on_omarchy() -> bool:
    """The block below is written for Omarchy's Lua config (its o.bind and o.window helpers)."""
    return (shutil.which("omarchy") is not None or Path("/usr/share/omarchy").is_dir()
            or (Path.home() / ".local/share/omarchy").is_dir())


def _lua(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def hypr_block(key: str) -> str:
    lines = [HYPR_BEGIN,
             "-- Desk Phone's keys and windows, written by its install.sh (desk-phone --remove-shortcuts removes them).",
             *(f"o.bind({_lua(HYPR_MODS[mods] + key)}, {_lua(name)}, {_lua(command(action))})"
               for _key, name, mods, action in SHORTCUTS),
             *HYPR_RULES,
             HYPR_END]
    return "\n".join(lines) + "\n"


# The card is a transparent window with the card drawn flush in its bottom-right corner (the margin
# is room for the shadow): float it there on every workspace, without Hyprland's border, shadow,
# blur, rounding or animation (it draws and animates itself), and without taking the focus when it
# rings. Settings and Contacts float in the middle, like on GNOME.
_CLASS = "^" + APP_ID.replace(".", r"\\.") + r"(\\.Demo)?$"
HYPR_RULES = [
    f'o.window({{ class = "{_CLASS}", title = "^{APP_NAME}$" }}, {{',
    '  float = true, pin = true, move = { "(monitor_w-window_w)", "(monitor_h-window_h)" },',
    '  border_size = 0, no_shadow = true, no_blur = true, rounding = 0, no_anim = true, no_initial_focus = true,',
    "})",
    f'o.window({{ class = "{_CLASS}", title = "^({APP_NAME} Settings|Contacts — {APP_NAME})$" }}, '
    "{ float = true, center = true })",
]


def without_hypr_block(text: str) -> str:
    """The file without our block, and without the blank line hypr_with_block put before it."""
    start = text.find(HYPR_BEGIN)
    end = text.find(HYPR_END, start) if start >= 0 else -1
    if end < 0:
        return text
    end += len(HYPR_END)
    if text[end:end + 1] == "\n":
        end += 1
    before = text[:start]
    if before.endswith("\n\n"):
        before = before[:-1]
    return before + text[end:]


def hypr_with_block(text: str, key: str) -> str:
    """The file with our block for that key: replaced where it is, or added at the end."""
    start = text.find(HYPR_BEGIN)
    end = text.find(HYPR_END, start) if start >= 0 else -1
    if end >= 0:                         # an update: the block stays where the user has it
        end += len(HYPR_END)
        if text[end:end + 1] == "\n":
            end += 1
        return text[:start] + hypr_block(key) + text[end:]
    if text and not text.endswith("\n"):
        text += "\n"
    return text + ("\n" if text else "") + hypr_block(key)


def _hypr_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    # Hyprland also reloads by itself when the file is saved; this makes it immediate.
    try:
        subprocess.run(["hyprctl", "reload"], capture_output=True, check=False)
    except OSError:
        pass


def hypr_key(text: str) -> str | None:
    """The key our block in a bindings file is on, or None when there's no block."""
    start = text.find(HYPR_BEGIN)
    end = text.find(HYPR_END, start) if start >= 0 else -1
    if end < 0:
        return None
    m = re.search(r'^o\.bind\("([^"]+)"', text[start:end], re.MULTILINE)   # the first: no modifier added
    return m.group(1).strip() if m else None


def hypr_install(key: str, path: Path | None = None):
    path = path or HYPR_BINDINGS
    _hypr_write(path, hypr_with_block(path.read_text() if path.exists() else "", key))


def hypr_remove(path: Path | None = None):
    path = path or HYPR_BINDINGS
    if path.exists():
        text = path.read_text()
        if HYPR_BEGIN in text:
            _hypr_write(path, without_hypr_block(text))


def current_key() -> str | None:
    """The key the shortcuts are on now, so setting up again (an update) keeps a key you chose."""
    try:
        if on_hyprland():
            return hypr_key(HYPR_BINDINGS.read_text()) if HYPR_BINDINGS.exists() else None
        _keys, custom = _settings()
        binding = custom(BASE + SHORTCUTS[0][0] + "/").get_string("binding")
        return binding or None
    except (OSError, RuntimeError):
        return None


def install_for_desktop(key: str) -> str:
    """Adds the keys the way this desktop takes them; returns where they are now."""
    if on_hyprland():
        if not on_omarchy():
            raise RuntimeError("this Hyprland isn't Omarchy's, whose config format the keys and "
                               "the card's window rules are written for; see the README, "
                               "“Other systems”")
        hypr_install(key)
        return "a marked block in ~/.config/hypr/bindings.lua"
    install(key)
    return "GNOME Settings → Keyboard → Custom Shortcuts"


def remove_everywhere() -> bool:
    """Removes the keys from Hyprland's bindings file and from GNOME, wherever they are."""
    done = False
    if HYPR_BINDINGS.exists() and HYPR_BEGIN in HYPR_BINDINGS.read_text():
        hypr_remove()
        done = True
    try:
        keys, _custom = _settings()
    except RuntimeError:
        return done
    ours = {BASE + key + "/" for key in [k for k, *_ in SHORTCUTS] + RETIRED}
    if ours & set(keys.get_strv("custom-keybindings")):
        remove()
        done = True
    return done


def main(argv) -> int:
    """--install-shortcuts [KEY] (default: the key they're on now, else the phone key if the
    keyboard data knows it, else XF86Display), --remove-shortcuts."""
    try:
        if "--remove-shortcuts" in argv:
            remove_everywhere()
            return 0
        i = argv.index("--install-shortcuts")
        key = (argv[i + 1] if len(argv) > i + 1 and not argv[i + 1].startswith("-")
               else current_key() or default_key())
        install_for_desktop(key)
        print(key)
    except (RuntimeError, Exception) as e:  # noqa: BLE001 - report anything to the installer
        print(f"Desk Phone shortcuts: {e}", file=sys.stderr)
        return 1
    return 0
