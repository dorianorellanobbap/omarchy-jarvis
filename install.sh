#!/usr/bin/env bash
# Install the Jarvis daemon that backs the dorian.voice bar widget.
# Safe to re-run: every step is idempotent.
set -euo pipefail

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
JARVIS_DIR="$HOME/.local/share/jarvis"
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/dorian.voice"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/jarvis"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
VOICE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    warning: %s\033[0m\n' "$*" >&2; }

# --- preflight -------------------------------------------------------------
missing=()
for cmd in python3 curl jq pw-record pw-play; do
  command -v "$cmd" >/dev/null || missing+=("$cmd")
done
if ((${#missing[@]})); then
  echo "missing required commands: ${missing[*]}" >&2
  echo "on Omarchy: sudo pacman -S --needed python curl jq pipewire-audio" >&2
  exit 1
fi

python3 - <<'PY' || { echo "Jarvis needs Python 3.11+ (tomllib)." >&2; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

command -v voxtype >/dev/null || warn "'voxtype' not on PATH -- Jarvis needs it to transcribe. It ships with Omarchy."

# --- bar widget ------------------------------------------------------------
# Already in place when installed via `omarchy plugin add`; copied here for a
# plain git clone.
if [[ $SRC != "$PLUGIN_DIR" ]]; then
  say "Installing the bar widget"
  mkdir -p "$PLUGIN_DIR"
  cp "$SRC/manifest.json" "$SRC/BarWidget.qml" "$PLUGIN_DIR/"
  echo "  -> $PLUGIN_DIR"
fi

# --- daemon ----------------------------------------------------------------
say "Installing the daemon"
mkdir -p "$JARVIS_DIR/bin" "$JARVIS_DIR/voices"
cp "$SRC/daemon/jarvis-listen.py" "$JARVIS_DIR/"
cp "$SRC/daemon/jarvis-open"      "$JARVIS_DIR/bin/"
cp "$SRC/daemon/en_US-amy-medium.onnx.json" "$JARVIS_DIR/voices/"
chmod +x "$JARVIS_DIR/bin/jarvis-open" "$JARVIS_DIR/jarvis-listen.py"

# --- python venv -----------------------------------------------------------
say "Building the venv (onnxruntime + scipy, this takes a minute)"
[[ -x "$JARVIS_DIR/venv/bin/python" ]] || python3 -m venv "$JARVIS_DIR/venv"
"$JARVIS_DIR/venv/bin/pip" install --quiet --upgrade pip
"$JARVIS_DIR/venv/bin/pip" install --quiet -r "$SRC/daemon/requirements.txt"

# openWakeWord 0.4.0 bundles the wake-word models and the feature
# extractors in the wheel, so there is nothing to download -- just
# confirm they landed.
"$JARVIS_DIR/venv/bin/python" - <<'WAKEWORDS'
import openwakeword, os
d = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
need = ["melspectrogram.onnx", "embedding_model.onnx",
        "hey_jarvis_v0.1.onnx", "alexa_v0.1.onnx",
        "hey_mycroft_v0.1.onnx", "hey_marvin_v0.1.onnx"]
missing = [m for m in need if not os.path.exists(os.path.join(d, m))]
if missing:
    raise SystemExit(f"openWakeWord models missing: {missing}")
print("  -> wake-word models ready")
WAKEWORDS

# --- piper voice (63MB, not in the repo) -----------------------------------
say "Fetching the piper voice"
VOICE="$JARVIS_DIR/voices/en_US-amy-medium.onnx"
[[ -f $VOICE ]] || curl -fL --progress-bar -o "$VOICE" "$VOICE_URL"
( cd "$JARVIS_DIR/voices" && sha256sum -c "$SRC/daemon/voice-model.sha256" >/dev/null ) \
  || { echo "voice checksum mismatch -- delete $VOICE and re-run" >&2; exit 1; }
echo "  -> $VOICE"

# --- config ----------------------------------------------------------------
say "Configuration"
mkdir -p "$CONFIG_DIR"
if [[ -f $CONFIG_DIR/config.toml ]]; then
  echo "  -> keeping your existing $CONFIG_DIR/config.toml"
else
  cp "$SRC/config/config.toml.example" "$CONFIG_DIR/config.toml"
  echo "  -> wrote $CONFIG_DIR/config.toml (defaults to the claude agent)"
fi
cp "$SRC/config/config.toml.example" "$CONFIG_DIR/config.toml.example"

# --- systemd user unit -----------------------------------------------------
say "Installing the systemd user unit"
mkdir -p "$UNIT_DIR"
cp "$SRC/daemon/jarvis.service" "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable jarvis.service >/dev/null
echo "  -> enabled (not started; the listener is off until you arm it)"

# --- verify ----------------------------------------------------------------
say "Checking the install"
"$JARVIS_DIR/venv/bin/python" "$JARVIS_DIR/jarvis-listen.py" --check || {
  echo
  echo "Something above is missing. Fix it, then re-run this script." >&2
  exit 1
}

cat <<DONE

Done. Next:

  1. Add the "Voice Assistant" widget to your bar (Omarchy menu ->
     Setup -> Bar), then log out and back in.
  2. Arm it from the widget, or: systemctl --user start jarvis
  3. Say "hey jarvis", then ask something.

  Pick a different agent or wake word in $CONFIG_DIR/config.toml
  See what's configured:  $JARVIS_DIR/venv/bin/python $JARVIS_DIR/jarvis-listen.py --agents
  Test without the mic:   $JARVIS_DIR/venv/bin/python $JARVIS_DIR/jarvis-listen.py --ask "hello"
DONE
