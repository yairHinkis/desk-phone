from desk_phone import shortcuts
from desk_phone.shortcuts import (HYPR_BEGIN, HYPR_END, hypr_block, hypr_install, hypr_key, hypr_remove,
                                  hypr_with_block, without_hypr_block)

USER = """-- my bindings
o.bind("SUPER + F", "Full screen", "x")

-- WhatsApp
o.rebind("SUPER + W", "Close", "y")
"""


def test_block_has_three_binds_with_modifiers():
    block = hypr_block("XF86LinkPhone")
    assert block.startswith(HYPR_BEGIN) and block.rstrip().endswith(HYPR_END)
    binds = [line for line in block.splitlines() if line.startswith("o.bind(")]
    assert [b.split('"')[1] for b in binds] == ["XF86LinkPhone", "SHIFT + XF86LinkPhone",
                                               "CTRL + XF86LinkPhone"]


def test_lua_escaping(monkeypatch, tmp_path):
    # The answer command has double quotes and $HOME in it; they must come out escaped.
    monkeypatch.setenv("HOME", str(tmp_path))              # (no package's desk-phone in the way)
    monkeypatch.setenv("PATH", "/nonexistent")
    line = next(line for line in hypr_block("K").splitlines() if "answer" in line)
    assert '\\"$HOME/.local/bin/desk-phone\\"' in line


def test_add_then_remove_is_byte_identical():
    for original in (USER, USER.rstrip("\n"), "", "\n"):
        added = hypr_with_block(original, "XF86LinkPhone")
        assert added.count(HYPR_BEGIN) == 1
        removed = without_hypr_block(added)
        assert removed == (original if original.endswith("\n") or not original else original + "\n")


def test_rerun_replaces_the_block():
    once = hypr_with_block(USER, "XF86LinkPhone")
    twice = hypr_with_block(once, "XF86Display")
    assert twice.count(HYPR_BEGIN) == 1 and twice.count(HYPR_END) == 1
    assert "XF86LinkPhone" not in twice and "SHIFT + XF86Display" in twice
    assert hypr_with_block(once, "XF86LinkPhone") == once      # same key: no change at all


def test_without_block_leaves_other_text_alone():
    assert without_hypr_block(USER) == USER
    broken = USER + HYPR_BEGIN + "\n-- no end marker\n"       # never cut without both markers
    assert without_hypr_block(broken) == broken


def test_install_and_remove_on_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcuts.subprocess, "run", lambda *a, **k: None)   # no hyprctl reload
    path = tmp_path / "hypr" / "bindings.lua"
    path.parent.mkdir()
    path.write_text(USER)
    hypr_install("XF86LinkPhone", path)
    hypr_install("XF86LinkPhone", path)
    assert path.read_text().count(HYPR_BEGIN) == 1
    hypr_remove(path)
    assert path.read_text() == USER
    hypr_remove(path)                                          # nothing to remove: still the same
    assert path.read_text() == USER


def test_key_is_read_back_from_the_block():
    assert hypr_key(USER) is None
    assert hypr_key(hypr_with_block(USER, "XF86LinkPhone")) == "XF86LinkPhone"
    assert hypr_key(hypr_with_block(USER, "SUPER + F9")) == "SUPER + F9"
    assert hypr_key(hypr_with_block(USER, "F9")) == "F9"


def test_remove_everywhere_takes_the_block_out_even_off_hyprland(tmp_path, monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.setattr(shortcuts.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(shortcuts, "_settings", lambda: (_ for _ in ()).throw(RuntimeError("no GNOME")))
    path = tmp_path / "bindings.lua"
    path.write_text(hypr_with_block(USER, "XF86LinkPhone"))
    monkeypatch.setattr(shortcuts, "HYPR_BINDINGS", path)
    assert shortcuts.remove_everywhere() is True
    assert path.read_text() == USER
    assert shortcuts.remove_everywhere() is False


def test_rerun_keeps_the_block_where_it_is():
    moved = hypr_with_block(USER, "XF86LinkPhone") + '\n-- added later\no.bind("SUPER + G", "More", "z")\n'
    assert hypr_with_block(moved, "XF86LinkPhone") == moved            # an update changes nothing
    other = hypr_with_block(moved, "F9")
    assert other.index(HYPR_BEGIN) == moved.index(HYPR_BEGIN) and other.endswith('"z")\n')
    assert without_hypr_block(other) == USER + '\n-- added later\no.bind("SUPER + G", "More", "z")\n'
