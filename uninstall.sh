#!/usr/bin/env bash
# Remove Jarvis. Leaves your config unless you pass --purge.
set -euo pipefail

JARVIS_DIR="$HOME/.local/share/jarvis"
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/dorian.voice"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/jarvis"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/jarvis.service"

purge=false
[[ ${1:-} == --purge ]] && purge=true

echo "This will remove:"
echo "  $JARVIS_DIR  (daemon, venv, voice model)"
echo "  $PLUGIN_DIR  (bar widget)"
echo "  $UNIT"
$purge && echo "  $CONFIG_DIR  (your config)"
read -rp "Continue? [y/N] " reply
[[ ${reply,,} == y* ]] || { echo "aborted"; exit 0; }

systemctl --user stop jarvis.service 2>/dev/null || true
systemctl --user disable jarvis.service 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload 2>/dev/null || true

rm -rf "$JARVIS_DIR" "$PLUGIN_DIR"
$purge && rm -rf "$CONFIG_DIR"

echo
echo "Removed. Take the Voice Assistant widget out of your bar layout"
echo "(Omarchy menu -> Setup -> Bar) if it is still there."
$purge || echo "Your config is still at $CONFIG_DIR -- delete it or re-run with --purge."
