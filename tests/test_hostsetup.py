"""Setting up and removing Desk Phone for a user, in a pretend home folder."""
import os
import stat

import pytest

from desk_phone import APP_ID, hostsetup, shortcuts
from desk_phone.shortcuts import HYPR_BEGIN, hypr_key


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A pretend home on a pretend Hyprland, with every outside command recorded, not run."""
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "test")
    ran = []
    monkeypatch.setattr(hostsetup, "_run", lambda argv, **kw: ran.append(argv) or True)
    monkeypatch.setattr(hostsetup, "running", lambda *a: False)
    monkeypatch.setattr(hostsetup, "forget_tel_links", lambda: None)
    monkeypatch.setattr(hostsetup, "SYSTEM_DATA_DIRS", (str(tmp_path / "usr-share"),))
    monkeypatch.setattr(shortcuts.subprocess, "run", lambda *a, **k: None)      # no hyprctl reload
    monkeypatch.setattr(shortcuts, "HYPR_BINDINGS", tmp_path / ".config/hypr/bindings.lua")
    monkeypatch.setattr(shortcuts, "on_omarchy", lambda: True)
    monkeypatch.setattr(shortcuts, "remove", lambda: None)                       # GNOME's: not here
    monkeypatch.setattr(shortcuts, "_settings", lambda: (_ for _ in ()).throw(RuntimeError("no GNOME")))
    exe = tmp_path / ".local/bin/desk-phone"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/bash\n# Desk Phone launcher, written by its install.sh.\n")
    return Home(tmp_path, ran)


class Home:
    """The pretend home: `home / "x"` is a path in it, `home.ran` the commands that were asked for."""

    def __init__(self, path, ran):
        self.path, self.ran = path, ran

    def __truediv__(self, other):
        return self.path / other

    def __str__(self):
        return str(self.path)

    def files(self):
        return {p: p.read_bytes() for p in self.path.rglob("*") if p.is_file()}


def test_setup_writes_everything_for_the_user(home, monkeypatch, capsys):
    assert hostsetup.setup() == 0
    entry = (home / f".local/share/applications/{APP_ID}.desktop").read_text()
    assert f"Exec={home}/.local/bin/desk-phone %u" in entry and "@LAUNCHER@" not in entry
    assert (home / f".local/share/icons/hicolor/scalable/apps/{APP_ID}.svg").exists()
    dropin = home / ".config/wireplumber/wireplumber.conf.d/60-desk-phone.conf"
    assert "bluez5.telephony.default-reject-sco = true" in dropin.read_text()
    assert ["systemctl", "--user", "restart", "wireplumber"] in home.ran
    assert HYPR_BEGIN in (home / ".config/hypr/bindings.lua").read_text()
    auto = (home / f".config/autostart/{APP_ID}.desktop").read_text()
    assert f"Exec={home}/.local/bin/desk-phone --background" in auto
    assert "Starts at login" in capsys.readouterr().out


def test_setup_again_changes_nothing_and_leaves_wireplumber_alone(home, monkeypatch):
    hostsetup.setup()
    files = home.files()
    home.ran.clear()
    hostsetup.setup()
    assert home.files() == files
    assert ["systemctl", "--user", "restart", "wireplumber"] not in home.ran


def test_setup_clears_what_the_earlier_id_left(home, capsys):
    old = hostsetup.OLD_APP_IDS[0]
    left = [home / f".local/share/applications/{old}.desktop", home / f".config/autostart/{old}.desktop",
            home / f".local/share/icons/hicolor/scalable/apps/{old}.svg"]
    other = home / ".config/autostart/someone-else.desktop"
    for path in (*left, other):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    hostsetup.setup()
    assert not any(p.exists() for p in left) and other.exists()
    assert (home / f".config/autostart/{APP_ID}.desktop").exists()
    assert "earlier version" in capsys.readouterr().out
    hostsetup.setup()
    assert "earlier version" not in capsys.readouterr().out


def test_setup_keeps_a_key_the_user_chose(home, monkeypatch):
    hostsetup.setup(key="XF86Display")
    assert hypr_key(shortcuts.HYPR_BINDINGS.read_text()) == "XF86Display"
    hostsetup.setup()                                      # an update: no key named
    assert hypr_key(shortcuts.HYPR_BINDINGS.read_text()) == "XF86Display"
    assert shortcuts.current_key() == "XF86Display"


def test_setup_respects_start_at_login_off(home, monkeypatch):
    cfg = home / ".config/desk-phone/config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('{"autostart": false}')
    hostsetup.setup()
    assert not (home / f".config/autostart/{APP_ID}.desktop").exists()


def test_setup_without_a_known_desktop_says_how_to_add_the_keys(home, monkeypatch, capsys):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE")
    assert hostsetup.setup() == 0                          # no Hyprland, no GNOME: still fine
    out = capsys.readouterr().out
    assert "Keys: not added" in out and "org.gtk.Actions.Activate answer-or-hangup" in out
    assert not (home / ".config/hypr/bindings.lua").exists()


def test_hyprland_that_is_not_omarchy_gets_no_block(home, monkeypatch, capsys):
    monkeypatch.setattr(shortcuts, "on_omarchy", lambda: False)
    assert hostsetup.setup() == 0
    assert "Keys: not added (this Hyprland isn't Omarchy's" in capsys.readouterr().out
    assert not (home / ".config/hypr/bindings.lua").exists()


def test_a_packaged_app_keeps_its_system_entry_and_launcher(home, monkeypatch):
    system = home / "usr-share/applications"
    system.mkdir(parents=True)
    (system / f"{APP_ID}.desktop").write_text("[Desktop Entry]\n")
    (home / ".local/bin/desk-phone").unlink()
    pkg_bin = home / "usr-bin"
    pkg_bin.mkdir()
    (pkg_bin / "desk-phone").write_text("#!/bin/sh\n")
    (pkg_bin / "desk-phone").chmod(0o755)
    monkeypatch.setenv("PATH", f"{pkg_bin}:{os.environ['PATH']}")
    hostsetup.setup()
    assert not (home / f".local/share/applications/{APP_ID}.desktop").exists()
    assert f"Exec={pkg_bin}/desk-phone --background" in (home / f".config/autostart/{APP_ID}.desktop").read_text()
    assert f'|| exec \\"{pkg_bin}/desk-phone\\"' in shortcuts.HYPR_BINDINGS.read_text()


def test_uninstall_removes_what_setup_made_and_the_data(home, monkeypatch, capsys):
    bindings = home / ".config/hypr/bindings.lua"
    bindings.parent.mkdir(parents=True)
    bindings.write_text('-- mine\no.bind("SUPER + F", "Full screen", "x")\n')
    other = home / ".config/wireplumber/wireplumber.conf.d/50-mine.conf"
    other.parent.mkdir(parents=True)
    other.write_text("# mine\n")
    hostsetup.setup()
    for d in (".config/desk-phone", ".cache/desk-phone", ".local/state/desk-phone",
              ".local/share/desk-phone/desk_phone"):
        (home / d).mkdir(parents=True, exist_ok=True)
        (home / d / "file").write_text("x")
    home.ran.clear()
    assert hostsetup.uninstall(assume_yes=True) == 0
    left = sorted(str(p.relative_to(home.path)) for p in home.files())
    assert left == [".config/hypr/bindings.lua", ".config/wireplumber/wireplumber.conf.d/50-mine.conf"]
    assert bindings.read_text() == '-- mine\no.bind("SUPER + F", "Full screen", "x")\n'
    assert ["systemctl", "--user", "restart", "wireplumber"] in home.ran
    assert "Desk Phone is removed." in capsys.readouterr().out


def test_uninstall_can_keep_the_data_and_never_removes_a_foreign_launcher(home, monkeypatch):
    hostsetup.setup()
    (home / ".config/desk-phone").mkdir(parents=True, exist_ok=True)
    (home / ".config/desk-phone/config.json").write_text("{}")
    exe = home / ".local/bin/desk-phone"
    exe.write_text("#!/bin/sh\n# somebody else's script with the same name\n")
    hostsetup.uninstall(assume_yes=True, keep_data=True)
    assert (home / ".config/desk-phone/config.json").exists() and exe.exists()
    assert not (home / f".config/autostart/{APP_ID}.desktop").exists()


def test_uninstall_asks_first(home, monkeypatch, capsys):
    hostsetup.setup()
    monkeypatch.setattr("sys.stdin", type("NoTty", (), {"isatty": lambda self: False})())
    assert hostsetup.uninstall() == 1                      # nobody to ask: nothing is removed
    assert (home / f".config/autostart/{APP_ID}.desktop").exists()
    monkeypatch.setattr("sys.stdin", type("Tty", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert hostsetup.uninstall() == 1
    assert (home / f".config/autostart/{APP_ID}.desktop").exists()
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert hostsetup.uninstall() == 0
    assert not (home / f".config/autostart/{APP_ID}.desktop").exists()


def test_autostart_switch(home):
    hostsetup.sync_autostart(True)
    path = home / f".config/autostart/{APP_ID}.desktop"
    assert path.exists()
    hostsetup.sync_autostart(False)
    assert not path.exists()
    hostsetup.sync_autostart(False)                        # already gone: fine


def test_launcher_path_with_a_space_is_quoted(home):
    odd = home / "my bin" / "desk-phone"
    hostsetup.sync_autostart(True, odd)
    assert f'Exec="{odd}" --background' in (home / f".config/autostart/{APP_ID}.desktop").read_text()


def test_launcher_ignores_the_current_folder(tmp_path):
    """`python -m` looks in the current folder first; the launcher must run the installed copy
    even when it's started in a folder that has a desk_phone of its own (a clone, say)."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(hostsetup.__file__).resolve().parent.parent
    for script in ("install.sh", "uninstall.sh"):
        assert "-P -m desk_phone" in (root / script).read_text()
        assert " -m desk_phone" not in (root / script).read_text().replace("-P -m desk_phone", "")
    decoy = tmp_path / "desk_phone"
    decoy.mkdir()
    (decoy / "__init__.py").write_text("")
    (decoy / "__main__.py").write_text("print('the wrong one')")
    out = subprocess.run([sys.executable, "-P", "-m", "desk_phone", "--version"], cwd=tmp_path,
                         env=dict(os.environ, PYTHONPATH=str(root)), capture_output=True, text=True)
    assert out.stdout.startswith("Desk Phone ")


def test_package_data_is_inside_the_package():
    for name in ("echo-cancel.conf", "60-desk-phone.conf", f"{APP_ID}.desktop.in", f"{APP_ID}.svg"):
        assert (hostsetup.PKG_DATA / name).is_file(), name
    from desk_phone.audio import EC_CONF
    assert EC_CONF.is_file() and EC_CONF.parent == hostsetup.PKG_DATA


def test_private_log_file(tmp_path):
    from desk_phone.__main__ import PrivateLog
    path = tmp_path / "desk-phone.log"
    path.write_text("old\n")
    path.chmod(0o644)
    handler = PrivateLog(path)
    handler.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
