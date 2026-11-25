#!/bin/bash
# Installs or updates Desk Phone for the current user. No root is needed for Desk Phone itself;
# it only asks before installing missing system packages.
#
#   ./install.sh            copy the app to ~/.local/share/desk-phone and set it up
#   ./install.sh --link     run it from this folder instead of a copy (for working on the code)
#   ./install.sh --no-deps  don't check the system packages
#   ./install.sh --yes      don't ask before installing missing packages
#
# Run it again to update. Remove everything with `desk-phone --uninstall` (or ./uninstall.sh).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN="$HOME/.local/bin"
DEST="$DATA/desk-phone"
PY=/usr/bin/python3            # the system Python: PyGObject comes from the distribution
LINK=0 DEPS=1 YES=0

for arg in "$@"; do
    case "$arg" in
        --link)    LINK=1 ;;
        --no-deps) DEPS=0 ;;
        --yes|-y)  YES=1 ;;
        -h|--help) sed -n '2,10s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "install.sh: unknown option $arg (see --help)" >&2; exit 2 ;;
    esac
done

say()  { printf '\033[1;34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || die "run install.sh as yourself, not as root: Desk Phone is set up per user."
[ -d "$APP_DIR/desk_phone" ] || die "desk_phone/ isn't next to install.sh; run it from the Desk Phone folder."

# 1. System packages (normally all there already on Fedora Workstation and on Omarchy).
install_missing() {     # install_missing <command to show and run> <package>...
    local cmd="$1"; shift
    say "Missing packages: $*"
    if [ "$YES" != 1 ]; then
        [ -t 0 ] || die "install them and run install.sh again:  $cmd $*"
        read -rp "Install them now with '$cmd'? [Y/n] " answer
        case "$answer" in ""|y|Y|yes|Yes) ;; *) die "install them and run install.sh again:  $cmd $*" ;; esac
    fi
    $cmd "$@"
}
if [ "$DEPS" = 1 ]; then
    if command -v pacman >/dev/null 2>&1; then
        say "Checking Arch packages"
        pkgs=(python python-gobject gtk3 glib2 gstreamer gst-plugins-base gst-plugins-good
              pipewire gst-plugin-pipewire wireplumber bluez)
        # Outside Hyprland the card runs under XWayland, the only way it can stay on top in a corner.
        [ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ] || pkgs+=(xorg-xwayland)
        mapfile -t missing < <(pacman -T "${pkgs[@]}" || true)
        ((${#missing[@]} == 0)) || install_missing "sudo pacman -S --needed$([ "$YES" != 1 ] || echo ' --noconfirm')" "${missing[@]}"
        pacman -T bluez-obex >/dev/null 2>&1 \
            || warn "bluez-obex isn't installed: calls work, but without caller names (sudo pacman -S bluez-obex)."
    elif command -v rpm >/dev/null 2>&1 && command -v dnf >/dev/null 2>&1; then
        say "Checking Fedora packages"
        pkgs=(python3 python3-gobject gtk3 glib2 gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good
              pipewire pipewire-utils pipewire-gstreamer wireplumber bluez xorg-x11-server-Xwayland)
        missing=()
        for p in "${pkgs[@]}"; do rpm -q "$p" >/dev/null 2>&1 || missing+=("$p"); done
        ((${#missing[@]} == 0)) || install_missing "sudo dnf install$([ "$YES" != 1 ] || echo ' -y')" "${missing[@]}"
        rpm -q bluez-obexd >/dev/null 2>&1 \
            || warn "bluez-obexd isn't installed: calls work, but without caller names (sudo dnf install bluez-obexd)."
    else
        warn "this isn't Fedora or Arch, so the packages aren't checked. Desk Phone needs: Python 3.11+"
        warn "with PyGObject, GTK 3, GStreamer with its PipeWire plugin, PipeWire 1.4+ with its tools"
        warn "(pw-dump, pw-metadata), WirePlumber, BlueZ (with obexd for caller names) and XWayland."
    fi
fi
[ -x "$PY" ] || die "$PY isn't there; Desk Phone runs on the system's Python 3."
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || die "Python 3.11 or newer is needed; $PY is $("$PY" -V 2>&1)."
"$PY" -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk" \
    || die "PyGObject with GTK 3 doesn't load in $PY; see the error above."

# 2. The app: a copy that doesn't depend on this folder (or, with --link, this folder itself).
#    An update must not leave the old version running on the new files, so a running one quits
#    first (it hands a call in progress back to the phone).
PYTHONPATH="$APP_DIR" "$PY" -P -m desk_phone --quit-if-running >/dev/null 2>&1 || true
mkdir -p "$BIN" "$DATA"
if [ "$LINK" = 1 ]; then
    CODE="$APP_DIR"
    say "Running Desk Phone from $APP_DIR (--link)"
    rm -rf "$DEST"
else
    CODE="$DEST"
    say "Copying Desk Phone to $DEST"
    rm -rf "$DEST.new"
    mkdir -p "$DEST.new"
    cp -r "$APP_DIR/desk_phone" "$DEST.new/"
    find "$DEST.new" -name __pycache__ -type d -prune -exec rm -rf {} +
    for f in LICENSE README.md; do [ ! -f "$APP_DIR/$f" ] || cp "$APP_DIR/$f" "$DEST.new/"; done
    rm -rf "$DEST"
    mv "$DEST.new" "$DEST"
fi
cat > "$BIN/desk-phone" <<LAUNCHER
#!/bin/bash
# Desk Phone launcher, written by its install.sh. Remove Desk Phone with: desk-phone --uninstall
APP_DIR="$CODE"
if [ ! -d "\$APP_DIR/desk_phone" ]; then
    msg="its files in \$APP_DIR are gone. Run Desk Phone's install.sh again."
    echo "Desk Phone: \$msg" >&2
    command -v notify-send >/dev/null && notify-send -i dialog-error "Desk Phone" "\$msg"
    exit 1
fi
export PYTHONPATH="\$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
# -P: never a desk_phone folder that happens to be in the current directory
exec $PY -P -m desk_phone "\$@"
LAUNCHER
chmod +x "$BIN/desk-phone"

# 3. Everything per user: app entry, the PipeWire setting, the keys, start at login.
say "Setting up"
"$BIN/desk-phone" --setup

# 4. Start it now.
say "Starting Desk Phone"
setsid "$BIN/desk-phone" --background >/dev/null 2>&1 < /dev/null &

case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN isn't in your PATH; use $BIN/desk-phone." ;; esac
echo
say "Done. Checking the setup:"
echo
"$BIN/desk-phone" --check || true
echo
echo "  • The keys:  key = answer / hang up, or open the dialer    Shift+key = decline"
echo "               Ctrl+key = mute the call, or silence the computer's ringtone"
echo "  • Try the card without a phone:  desk-phone --demo"
echo "  • Update: run install.sh again.  Remove: desk-phone --uninstall"
echo "  • Log: ~/.local/state/desk-phone/desk-phone.log"
