#!/usr/bin/env bash
# Open Player for Sonos — uninstaller. Your settings and monitor history are
# kept unless you pass --purge.
set -euo pipefail

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}"

systemctl --user disable --now openplayer-watch.service openplayer-stream.service 2>/dev/null || true
rm -f "$CONF/systemd/user/openplayer-watch.service" "$CONF/systemd/user/openplayer-stream.service"
systemctl --user daemon-reload 2>/dev/null || true

rm -f "$HOME/.local/bin/openplayer" "$DATA/applications/open-player.desktop"
rm -rf "$DATA/openplayer"

if [ -f "$CONF/pipewire/pipewire.conf.d/60-openplayer.conf" ]; then
  rm -f "$CONF/pipewire/pipewire.conf.d/60-openplayer.conf"
  systemctl --user restart pipewire pipewire-pulse wireplumber 2>/dev/null || true
fi
[ -f "$HOME/.asoundrc" ] && sed -i '/^# >>> openplayer >>>$/,/^# <<< openplayer <<<$/d' "$HOME/.asoundrc"

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$CONF/openplayer" "$STATE/openplayer" "$CACHE/openplayer"
fi

echo "Open Player for Sonos removed."
if command -v ufw >/dev/null && systemctl is-active --quiet ufw 2>/dev/null; then
  echo "If you added the firewall rule, remove it with: sudo ufw status numbered, then sudo ufw delete <number>"
fi
