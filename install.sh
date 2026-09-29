#!/usr/bin/env bash
# Open Player for Sonos — installer.
#
# Installs for the current user only (~/.local, ~/.config). Nothing needs root,
# except the optional firewall rule, which is only added if you say yes.
#
#   ./install.sh                  app + "send laptop sound to a room"
#   ./install.sh --no-laptop      app only (no PipeWire/swyh-rs changes)
#   ./install.sh --with-monitor   also run the network monitor in the background
#   ./install.sh --no-firewall    never touch the firewall
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
APP_DIR="$DATA/openplayer"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$CONF/systemd/user"

# swyh-rs streams the laptop's sound to the speakers (MIT, github.com/dheijl/swyh-rs).
SWYH_VERSION="1.21.0"
SWYH_URL="https://github.com/dheijl/swyh-rs/releases/download/$SWYH_VERSION/swyh-rs-cli-x86_64.AppImage"
SWYH_SHA256="619b5779c52477620c72b8abdbc6a7a185addc371b267c2968f2da0554c07f46"
STREAM_PORT=5901

LAPTOP=1 MONITOR=0 FIREWALL=1
for arg in "$@"; do
  case "$arg" in
    --no-laptop) LAPTOP=0 ;;
    --with-monitor) MONITOR=1 ;;
    --no-firewall) FIREWALL=0 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*" >&2; }
need() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- checks
need python3 || { warn "python3 is required."; exit 1; }
python3 - <<'EOF' || { warn "Python 3.11 or newer is required."; exit 1; }
import sys; sys.exit(sys.version_info < (3, 11))
EOF
if [ "$LAPTOP" = 1 ]; then
  for t in pactl pw-record pw-link; do
    need "$t" || { warn "'$t' not found — laptop sound needs PipeWire. Installing without it."; LAPTOP=0; break; }
  done
fi

# ---------------------------------------------------------------- the app
say "Installing the app into $APP_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet "$SRC"
ln -sf "$APP_DIR/venv/bin/openplayer" "$BIN_DIR/openplayer"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR is not on your PATH; add it to run 'openplayer'." ;; esac

say "Adding the launcher entry"
mkdir -p "$DATA/applications"
if need xdg-terminal-exec; then
  EXEC="xdg-terminal-exec --app-id=TUI.tile -e $BIN_DIR/openplayer"; TERMINAL=false
else
  EXEC="$BIN_DIR/openplayer"; TERMINAL=true
fi
cat > "$DATA/applications/open-player.desktop" <<EOF
[Desktop Entry]
Version=1.0
Name=Open Player for Sonos
Comment=Mixer, player and music visualizer for Sonos speakers
Exec=$EXEC
Terminal=$TERMINAL
Type=Application
Icon=audio-speakers
Categories=AudioVideo;Audio;Player;
StartupNotify=true
EOF

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/openplayer-watch.service" <<EOF
[Unit]
Description=Open Player for Sonos: watch the network for dropouts
After=network-online.target

[Service]
ExecStart=$BIN_DIR/openplayer watch
Restart=always
RestartSec=30
Nice=10

[Install]
WantedBy=default.target
EOF

# ---------------------------------------------------------------- laptop sound
if [ "$LAPTOP" = 1 ]; then
  say "Setting up 'send laptop sound to a room'"
  mkdir -p "$CONF/pipewire/pipewire.conf.d"
  cp "$SRC/setup/60-openplayer.conf" "$CONF/pipewire/pipewire.conf.d/"

  # Our ALSA device lives between markers so the rest of ~/.asoundrc is untouched.
  touch "$HOME/.asoundrc"
  sed -i '/^# >>> openplayer >>>$/,/^# <<< openplayer <<<$/d' "$HOME/.asoundrc"
  { echo "# >>> openplayer >>>"; cat "$SRC/setup/asoundrc"; echo "# <<< openplayer <<<"; } >> "$HOME/.asoundrc"

  APPIMAGE="$APP_DIR/swyh-rs-cli.AppImage"
  SWYH="$APP_DIR/swyh-rs-cli"
  if ! echo "$SWYH_SHA256  $APPIMAGE" | sha256sum --check --status 2>/dev/null; then
    say "Downloading swyh-rs $SWYH_VERSION"
    curl -fsSL -o "$APPIMAGE.part" "$SWYH_URL"
    echo "$SWYH_SHA256  $APPIMAGE.part" | sha256sum --check --status \
      || { rm -f "$APPIMAGE.part"; warn "swyh-rs download failed its checksum — not installed."; exit 1; }
    mv "$APPIMAGE.part" "$APPIMAGE"
    chmod +x "$APPIMAGE"
  fi
  # Unpack the single program inside the AppImage and run it directly: no FUSE
  # needed, and no crash on shutdown when the AppImage's mount goes away first.
  TMP=$(mktemp -d)
  (cd "$TMP" && "$APPIMAGE" --appimage-extract usr/bin/swyh-rs-cli >/dev/null)
  install -m 755 "$TMP/squashfs-root/usr/bin/swyh-rs-cli" "$SWYH"
  rm -rf "$TMP"

  cat > "$UNIT_DIR/openplayer-stream.service" <<EOF
[Unit]
Description=Open Player for Sonos: serve the "Sonos" virtual output to the speakers (swyh-rs)
After=pipewire.service wireplumber.service

[Service]
# Serve-only: the app tells the speakers where to fetch the stream.
# Input "openplayer-openplayer" is defined in ~/.asoundrc (never the microphone).
ExecStart=$SWYH -x true -P false -s openplayer-openplayer -f wav -b 16 -R 48000 -p $STREAM_PORT
Restart=on-failure
EOF

  say "Restarting PipeWire to add the 'Sonos' output (sound blips for a second)"
  systemctl --user restart pipewire pipewire-pulse wireplumber 2>/dev/null || warn "Couldn't restart PipeWire; log out and back in."

  # The speakers connect back to the laptop to fetch the stream.
  if [ "$FIREWALL" = 1 ] && need ufw && systemctl is-active --quiet ufw 2>/dev/null; then
    IFACE=$(ip route show default | awk '{print $5; exit}')
    NET=$(ip -o -4 route show dev "$IFACE" scope link proto kernel 2>/dev/null | awk '{print $1; exit}')
    if [ -n "$NET" ]; then
      echo
      echo "Your firewall (ufw) is on. For laptop sound, speakers on your network ($NET)"
      echo "must be able to reach port $STREAM_PORT on this computer. Only that network is allowed."
      read -r -p "Add the rule now with sudo? [y/N] " ok
      if [[ "$ok" =~ ^[Yy]$ ]]; then
        sudo ufw allow from "$NET" to any port "$STREAM_PORT" proto tcp comment 'open-player-for-sonos'
      else
        echo "Skipped. Later: sudo ufw allow from $NET to any port $STREAM_PORT proto tcp"
      fi
    fi
  fi
fi

systemctl --user daemon-reload 2>/dev/null || true
if [ "$MONITOR" = 1 ]; then
  systemctl --user enable --now openplayer-watch.service
  say "Network monitor is on (openplayer monitor off to stop it)."
fi

echo
say "Done. Open 'Open Player for Sonos' from your launcher, or run: openplayer"
