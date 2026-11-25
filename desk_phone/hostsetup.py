"""What Desk Phone sets up on the computer for one user, and how it takes it away again.

  desk-phone --setup       the app entry and icon, the WirePlumber setting, the keys, autostart
  desk-phone --uninstall   removes all of that, plus the settings, cached contacts, recent calls,
                           log, and the copy of the app that install.sh made

install.sh only copies the app to ~/.local/share/desk-phone, writes the launcher and runs --setup.
A distribution package installs the app for everyone, and each user runs --setup once. Everything
here is per user and needs no root.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import APP_ID, APP_NAME, OLD_APP_IDS, shortcuts
from .config import Config, cache_dir, config_dir, state_dir

PKG_DATA = Path(__file__).resolve().parent / "data"
LAUNCHER_MARK = "Desk Phone launcher"
SYSTEM_DATA_DIRS = ("/usr/share", "/usr/local/share")
TEL_SCHEMES = ("x-scheme-handler/tel", "x-scheme-handler/callto")

AUTOSTART = """[Desktop Entry]
Type=Application
Name=Desk Phone
Comment=Waits in the background for your phone's calls
Exec={launcher} --background
Icon={icon}
NoDisplay=true
X-GNOME-Autostart-enabled=true
"""


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def data_home() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def user_launcher() -> Path:
    return Path.home() / ".local" / "bin" / "desk-phone"


def launcher() -> Path:
    """The `desk-phone` command: the one install.sh wrote, else the one on the PATH (a package's)."""
    mine = user_launcher()
    if mine.exists():
        return mine
    found = shutil.which("desk-phone")
    return Path(found) if found else mine


def app_copy() -> Path:
    """Where install.sh puts its copy of the app."""
    return data_home() / "desk-phone"


def desktop_entry() -> Path:
    return data_home() / "applications" / f"{APP_ID}.desktop"


def icon_file() -> Path:
    return data_home() / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg"


def autostart_path() -> Path:
    return config_home() / "autostart" / f"{APP_ID}.desktop"


def dropin_path() -> Path:
    return config_home() / "wireplumber" / "wireplumber.conf.d" / "60-desk-phone.conf"


def packaged() -> bool:
    """A distribution package already put the app entry and icon in the system folders."""
    return any(Path(d, "applications", f"{APP_ID}.desktop").exists() for d in SYSTEM_DATA_DIRS)


def install_hint(fedora: str, arch: str | None = None) -> str:
    """The command that installs a package here: `sudo pacman -S …` on Arch, `sudo dnf install …`
    on Fedora, and the package's name alone elsewhere."""
    if shutil.which("pacman"):
        return f"sudo pacman -S {arch or fedora}"
    if shutil.which("dnf"):
        return f"sudo dnf install {fedora}"
    return f"install {fedora} with your package manager"


def _say(text: str):
    print(f"  • {text}")


def _short(path: Path) -> str:
    """~/… for a path in the home folder."""
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


def _run(argv, **kw):
    """Runs a helper command; a missing or failing one is never fatal here."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False, **kw).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _quote(path: Path) -> str:
    s = str(path)
    return s if not any(ch in s for ch in ' \t"`$\\') else '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# -- the pieces -----------------------------------------------------------------------------------
def install_app_entry(exe: Path | None = None) -> bool:
    """The entry in the app menu and the icon, for this user. Skipped when a package has them."""
    if packaged():
        return False
    exe = exe or launcher()
    entry, icon = desktop_entry(), icon_file()
    entry.parent.mkdir(parents=True, exist_ok=True)
    icon.parent.mkdir(parents=True, exist_ok=True)
    template = (PKG_DATA / f"{APP_ID}.desktop.in").read_text()
    entry.write_text(template.replace("@LAUNCHER@", _quote(exe)))
    shutil.copyfile(PKG_DATA / f"{APP_ID}.svg", icon)
    _run(["update-desktop-database", "-q", str(entry.parent)])
    _run(["gtk-update-icon-cache", "-q", "-t", "-f", str(data_home() / "icons" / "hicolor")])
    return True


def install_dropin() -> bool:
    """Calls stay on the phone unless Desk Phone takes them, also while it isn't running.
    Returns True when the file changed (WirePlumber then needs a restart)."""
    src, dst = PKG_DATA / "60-desk-phone.conf", dropin_path()
    try:
        if dst.read_bytes() == src.read_bytes():
            return False
    except OSError:
        pass
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return True


def remove_dropin() -> bool:
    try:
        dropin_path().unlink()
        return True
    except OSError:
        return False


def restart_wireplumber() -> bool:
    return _run(["systemctl", "--user", "restart", "wireplumber"])


def sync_autostart(enabled: bool, exe: Path | None = None):
    """Writes or removes the start-at-login entry (the Settings switch calls this too)."""
    path = autostart_path()
    exe = exe or launcher()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(AUTOSTART.format(launcher=_quote(exe), icon=APP_ID))
    else:
        path.unlink(missing_ok=True)


def _ask_bus(method: str, name: str):
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                          method, GLib.Variant("(s)", (name,)), None, Gio.DBusCallFlags.NONE, 2000, None)
    return reply.unpack()[0]


def running(app_id: str = APP_ID) -> bool:
    """Is a Desk Phone running in this session?"""
    try:
        return bool(_ask_bus("NameHasOwner", app_id))
    except Exception:  # noqa: BLE001 - no session bus: nothing is running that we could reach
        return False


def quit_running(wait_s: float = 4.0):
    """Asks the running Desk Phone to quit (it hands calls back to the phone first)."""
    if running():
        _run([sys.executable, "-P", "-m", "desk_phone", "--quit"],
             env=dict(os.environ, PYTHONPATH=str(PKG_DATA.parent.parent)), timeout=10)
    for old in OLD_APP_IDS:               # a copy from before the ID changed: SIGTERM is "quit" to it
        try:
            if running(old):
                os.kill(_ask_bus("GetConnectionUnixProcessID", old), signal.SIGTERM)
        except Exception:  # noqa: BLE001
            pass
    end = time.monotonic() + wait_s
    while any(map(running, (APP_ID, *OLD_APP_IDS))) and time.monotonic() < end:
        time.sleep(0.1)


def forget_tel_links():
    """If tel: links were given to Desk Phone (the GNOME Contacts setting), give them back."""
    try:
        ids = [f"{i}.desktop" for i in (APP_ID, *OLD_APP_IDS)]
        listed = (config_home() / "mimeapps.list").read_text()
        if not any(i in listed for i in ids):
            return
        from gi.repository import Gio
        for scheme in TEL_SCHEMES:
            app = Gio.AppInfo.get_default_for_type(scheme, False)
            if app is not None and app.get_id() in ids:
                Gio.AppInfo.reset_type_associations(scheme)
    except Exception:  # noqa: BLE001
        pass


def forget_old_ids() -> bool:
    """Removes the app entry, icon and start at login that an install with the earlier ID left."""
    gone = False
    for old in OLD_APP_IDS:
        for path in (desktop_entry(), icon_file(), autostart_path()):
            try:
                path.with_name(path.name.replace(APP_ID, old)).unlink()
                gone = True
            except OSError:
                pass
    return gone


# -- setup ----------------------------------------------------------------------------------------
def setup(key: str | None = None) -> int:
    print(f"Setting up {APP_NAME} for {Path.home().name}")
    exe = launcher()
    if forget_old_ids():
        _say("Removed the app entry of an earlier version (its ID was different)")
    if install_app_entry(exe):
        _say(f"App entry and icon: {_short(desktop_entry().parent)}")
    else:
        _say("App entry and icon: already installed by the system package")

    if install_dropin():
        _say(f"Calls stay on the phone unless you take them here: {_short(dropin_path())}")
        print("    Restarting WirePlumber (sound stops for a second; Bluetooth devices reconnect)")
        if not restart_wireplumber():
            print("    Couldn't restart WirePlumber; log out and back in for this to take effect.")
    else:
        _say("Calls stay on the phone unless you take them here: already set")

    try:
        key = key or shortcuts.current_key() or shortcuts.default_key()
        where = shortcuts.install_for_desktop(key)
        _say(f"Keys on {shortcuts.key_name(key)}: {where}")
    except Exception as e:  # noqa: BLE001 - the keys are optional; say how to add them by hand
        _say(f"Keys: not added ({e}). To add them yourself, bind three keys to:")
        for _key, name, _mods, action in shortcuts.SHORTCUTS:
            print(f"      {name}:  {shortcuts.command(action)}")

    autostart = bool(Config()["autostart"])
    sync_autostart(autostart, exe)
    _say("Starts at login" if autostart else "Start at login is off (Settings)")
    return 0


# -- uninstall ------------------------------------------------------------------------------------
def _remove(path: Path) -> bool:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        print(f"    Couldn't remove {path}: {e}")
        return False


def _our_launcher(path: Path) -> bool:
    try:
        return LAUNCHER_MARK in path.read_text(errors="replace")[:400]
    except OSError:
        return False


def uninstall(assume_yes: bool = False, keep_data: bool = False) -> int:
    data = [("settings", config_dir()), ("cached contacts and sounds", cache_dir()),
            ("recent calls and log", state_dir())]
    print(f"This removes {APP_NAME} for {Path.home().name}: its keys, start at login, the WirePlumber "
          "setting, the app entry" + ("." if keep_data else
                                      ",\nand its settings, cached contacts, recent calls and log."))
    if not assume_yes:
        if not sys.stdin.isatty():
            print("Not removing anything without a terminal to ask in; add --yes to go ahead.")
            return 1
        if input("Remove it? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing was removed.")
            return 1

    quit_running()
    try:
        if shortcuts.remove_everywhere():
            _say("Keys removed")
    except Exception as e:  # noqa: BLE001
        _say(f"Keys: couldn't remove them ({e})")
    if _remove(autostart_path()):
        _say("Start at login removed")
    if remove_dropin():
        _say("WirePlumber setting removed; restarting WirePlumber (sound stops for a second)")
        restart_wireplumber()
    forget_tel_links()
    removed = [_remove(desktop_entry()), _remove(icon_file()), forget_old_ids()]
    if any(removed):
        _run(["update-desktop-database", "-q", str(desktop_entry().parent)])
        _run(["gtk-update-icon-cache", "-q", "-t", "-f", str(data_home() / "icons" / "hicolor")])
        _say("App entry and icon removed")
    if not keep_data:
        for what, path in data:
            if _remove(path):
                _say(f"Removed the {what}: {_short(path)}")
    exe = user_launcher()
    if _our_launcher(exe) and _remove(exe):
        _say(f"Removed {_short(exe)}")
    if _remove(app_copy()):
        _say(f"Removed {_short(app_copy())}")
    print(f"\n{APP_NAME} is removed."
          + ("\nIt was installed as a system package too; remove that with your package manager."
             if packaged() else "")
          + "\nA connected phone now sends every call's audio to this computer again; turn off"
            "\n“Phone calls” for this computer in the phone's Bluetooth settings if you don't want that.")
    return 0


def main(argv) -> int:
    if "--uninstall" in argv:
        return uninstall(assume_yes="--yes" in argv or "-y" in argv, keep_data="--keep-data" in argv)
    i = argv.index("--setup")
    key = None
    if "--key" in argv[i:]:
        j = argv.index("--key", i)
        key = argv[j + 1] if len(argv) > j + 1 else None
    return setup(key)
