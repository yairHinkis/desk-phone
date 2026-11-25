"""`desk-phone --check`: is everything in place for calls? `desk-phone --connect`: connect the phone."""

import re
import shutil
import subprocess
from pathlib import Path

from gi.repository import Gio, GLib

from .bluez import HFP_AG, HFP_HF, PBAP_PSE, Bluez
from .hostsetup import dropin_path, install_hint
from .telephony import AG_IFACE, error_text, MANAGER_PATH, OM_IFACE, SERVICE, TRANSPORT_IFACE


def _ok(cond, text, fix=""):
    print(("  ✓ " if cond else "  ✗ ") + text)
    if not cond and fix:
        for line in fix.split("\n"):
            print("      " + line)
    return cond


def pipewire_version():
    try:
        out = subprocess.run(["pipewire", "--version"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"libpipewire (\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else None


def gateways():
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        r = bus.call_sync(SERVICE, MANAGER_PATH, OM_IFACE, "GetManagedObjects", None, None,
                          Gio.DBusCallFlags.NONE, 3000, None)
    except GLib.Error as e:
        return None, error_text(e)
    objs = r.unpack()[0]
    return {p: i for p, i in objs.items() if AG_IFACE in i}, ""


def main() -> int:
    good = True
    print("Desk Phone check\n\nThis computer")
    ver = pipewire_version()
    good &= _ok(ver is not None and ver >= (1, 4, 0),
                f"PipeWire {'.'.join(map(str, ver)) if ver else 'not found'} (needs 1.4 or newer)",
                "Update the system's pipewire and wireplumber packages.")
    tools = {t: shutil.which(t) for t in ("pw-dump", "pw-metadata", "wpctl")}
    good &= _ok(all(tools.values()), "PipeWire tools (pw-dump, pw-metadata, wpctl)",
                install_hint("pipewire-utils wireplumber", "pipewire wireplumber"))
    ec = any(Path(d, "libpipewire-module-echo-cancel.so").exists()
             for d in ("/usr/lib64/pipewire-0.3", "/usr/lib/pipewire-0.3", "/usr/lib/x86_64-linux-gnu/pipewire-0.3"))
    _ok(ec, "Echo canceller module", "Calls still work; the caller may hear an echo on speakers.")
    _ok(dropin_path().exists(), "Calls stay on the phone unless you take them here (WirePlumber setting)",
        "Run: desk-phone --setup   (without it, Desk Phone sets this each time the phone connects)")

    gws, err = gateways()
    good &= _ok(gws is not None, "PipeWire's call service (org.pipewire.Telephony)",
                f"{err}\nIs WirePlumber running?  systemctl --user status wireplumber")

    print("\nBluetooth")
    bz = Bluez()
    adapters = bz.adapters()
    powered = [a for a in adapters if a.get("Powered")]
    good &= _ok(bool(powered), f"Adapter on ({', '.join(a.get('Alias', '?') for a in adapters) or 'none found'})",
                "Turn Bluetooth on in Settings → Bluetooth.")
    if powered:
        good &= _ok(any(HFP_HF in (a.get("UUIDs") or []) for a in powered),
                    "The computer offers itself as a hands-free kit",
                    "PipeWire's hfp_hf role is off. Check ~/.config/wireplumber for bluez5.roles.")
    paired = [d for d in bz.devices() if d.get("Paired")]
    phones = [d for d in paired if HFP_AG in (d.get("UUIDs") or [])]
    good &= _ok(bool(phones), "A paired phone that can send calls: " +
                (", ".join(f"{d.get('Alias')} ({d.get('Address')})" for d in phones) or "none"),
                "Pair the phone in Settings → Bluetooth.")
    for d in phones:
        name = d.get("Alias") or d.get("Address")
        ag = next((i for i in (gws or {}).values()
                   if i[AG_IFACE].get("Address", "").upper() == d.get("Address", "").upper()), None)
        good &= _ok(ag is not None, f"{name}: connected for calls" + (
            f" (call audio link: {ag[TRANSPORT_IFACE].get('State')}, "
            f"stays on the phone: {'yes' if ag[TRANSPORT_IFACE].get('RejectSCO') else 'no'})" if ag else ""),
            f"On the phone: Settings → Bluetooth → ⚙ next to this computer → turn on “Phone calls”.\n"
            f"Then run:  desk-phone --connect\n"
            f"Still nothing? Forget the device on both sides and pair again from the phone.")
        _ok(PBAP_PSE in (d.get("UUIDs") or []), f"{name}: can share contacts (for caller names)",
            "Names are optional; numbers show without them.")
    print("\nAll set." if good else "\nSomething above needs fixing (✗).")
    return 0 if good else 1


def connect_main() -> int:
    loop = GLib.MainLoop()
    result = {}

    def done(message):
        result["m"] = message
        loop.quit()
    Bluez().connect_phones(done)
    if "m" not in result:
        loop.run()
    print(result["m"])
    return 0 if result["m"].startswith("Connected") else 1
