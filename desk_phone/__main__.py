"""Entry point: `python -m desk_phone [--background|--settings|--quit|--answer|--decline|--mute|
--demo|--check|--connect|--setup|--uninstall|--install-shortcuts|--remove-shortcuts|--version]`."""

import logging
import logging.handlers
import os
import signal
import sys

from . import APP_ID, APP_NAME, __version__
from .config import state_dir


class PrivateLog(logging.handlers.RotatingFileHandler):
    """The log names who called, so like the cached contacts it's readable by its owner alone."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        os.chmod(self.baseFilename, 0o600)
        return super()._open()


def setup_logging(debug: bool):
    handlers = [logging.StreamHandler()]
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        handlers.append(PrivateLog(d / "desk-phone.log", maxBytes=1_000_000, backupCount=2,
                                   encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--version" in argv:
        print(f"{APP_NAME} {__version__}")
        return 0
    if "--quit-if-running" in argv:        # (--quit alone would first start one, to tell it to quit)
        from .hostsetup import quit_running
        quit_running()
        return 0
    if "--setup" in argv or "--uninstall" in argv:
        from .hostsetup import main as hostsetup_main
        return hostsetup_main(argv)
    if "--check" in argv:
        from .check import main as check_main
        return check_main()
    if "--install-shortcuts" in argv or "--remove-shortcuts" in argv:
        from .shortcuts import main as shortcuts_main
        return shortcuts_main(argv)
    if "--connect" in argv:
        from .check import connect_main
        return connect_main()
    setup_logging("--debug" in argv)
    demo = "--demo" in argv
    if demo:
        argv.remove("--demo")

    # GTK3 on Wayland can't keep a window above others or place it; under XWayland it can.
    # Hyprland does both with a window rule (shortcuts.hypr_block), so there it runs natively on
    # Wayland, which also follows a fractional screen scale (XWayland apps stay unscaled, so small).
    hyprland = bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))
    if not hyprland:
        os.environ["GDK_BACKEND"] = "x11"
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import GLib
    GLib.set_prgname(APP_ID)
    GLib.set_application_name(APP_NAME)
    from gi.repository import Gdk, Gtk  # noqa: F401
    if not hyprland:
        os.environ.pop("GDK_BACKEND", None)
    if Gdk.Display.get_default() is None:
        print(f"{APP_NAME}: can't open a display. Outside Hyprland it needs XWayland "
              "(xorg-x11-server-Xwayland on Fedora, xorg-xwayland on Arch).", file=sys.stderr)
        return 1
    Gdk.set_program_class(APP_ID)

    from .app import DeskPhoneApp
    app = DeskPhoneApp(demo=demo)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: (app.quit_app(), False)[1])
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: (app.quit_app(), False)[1])
    return app.run(argv)


if __name__ == "__main__":
    sys.exit(main())
