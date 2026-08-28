#!/usr/bin/env bash
# Install the Jarvis daemon that backs the dorian.voice bar widget.
# Safe to re-run: every step is idempotent.
set -euo pipefail

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
JARVIS_DIR="$HOME/.local/share/jarvis"
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/dorian.voice"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/jarvis"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    warning: %s\033[0m\n' "$*" >&2; }

# put <mode> <src> <dest>: copy a file into place the way the daemon writes
# files -- to an unpredictably named private temp file (mktemp, 0600) in the
# destination directory, then an atomic rename over the target. Re-running
# this script rewrites every one of these paths, and a plain `cp` onto an
# existing name opens and follows whatever is there: a planted symlink would
# have the write redirected through it. rename() replaces the name itself
# and follows nothing.
put() {
  local mode=$1 src=$2 dest=$3 tmp
  tmp=$(mktemp -p "$(dirname "$dest")" '.jarvis.XXXXXXXX') || exit 1
  cp "$src" "$tmp" && chmod "$mode" "$tmp" && mv -f "$tmp" "$dest" \
    || { rm -f "$tmp"; exit 1; }
}

# --- preflight -------------------------------------------------------------
missing=()
for cmd in python3 pw-record pw-play; do
  command -v "$cmd" >/dev/null || missing+=("$cmd")
done
if ((${#missing[@]})); then
  echo "missing required commands: ${missing[*]}" >&2
  echo "on Omarchy: sudo pacman -S --needed python pipewire-audio" >&2
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
  put 644 "$SRC/manifest.json" "$PLUGIN_DIR/manifest.json"
  put 644 "$SRC/BarWidget.qml" "$PLUGIN_DIR/BarWidget.qml"
  put 644 "$SRC/Panel.qml"     "$PLUGIN_DIR/Panel.qml"
  echo "  -> $PLUGIN_DIR"
fi

# --- daemon ----------------------------------------------------------------
say "Installing the daemon"
mkdir -p "$JARVIS_DIR/bin" "$JARVIS_DIR/voices"
put 755 "$SRC/daemon/jarvis-listen.py" "$JARVIS_DIR/jarvis-listen.py"
put 755 "$SRC/daemon/jarvis-open"      "$JARVIS_DIR/bin/jarvis-open"
put 755 "$SRC/daemon/jarvis-config"    "$JARVIS_DIR/jarvis-config"
# The safe-file helpers are imported by all three, and each imports it from
# its own directory, so it lands in both.
put 644 "$SRC/daemon/safefile.py"      "$JARVIS_DIR/safefile.py"
put 644 "$SRC/daemon/safefile.py"      "$JARVIS_DIR/bin/safefile.py"
# Checksums for every voice the settings panel can download. Without it,
# jarvis-config refuses to install a voice rather than installing an
# unverified one.
put 644 "$SRC/daemon/voices.sha256"    "$JARVIS_DIR/voices.sha256"
put 644 "$SRC/daemon/en_US-amy-medium.onnx.json" "$JARVIS_DIR/voices/en_US-amy-medium.onnx.json"

# The settings panel shells out to jarvis-config, which needs the venv's
# interpreter. Wrap it so neither the panel nor a person has to know that.
# Same private-temp-then-rename discipline as put().
wrapper_tmp=$(mktemp -p "$JARVIS_DIR/bin" '.jarvis.XXXXXXXX')
cat > "$wrapper_tmp" <<'WRAPPER'
#!/usr/bin/env bash
JARVIS_DIR="${JARVIS_DIR:-$HOME/.local/share/jarvis}"
exec "$JARVIS_DIR/venv/bin/python" "$JARVIS_DIR/jarvis-config" "$@"
WRAPPER
chmod 755 "$wrapper_tmp"
mv -f "$wrapper_tmp" "$JARVIS_DIR/bin/jarvis-config"

# --- python venv -----------------------------------------------------------
say "Building the venv (onnxruntime + scipy, this takes a minute)"
[[ -x "$JARVIS_DIR/venv/bin/python" ]] || python3 -m venv "$JARVIS_DIR/venv"

# Every artifact is verified against a sha256 committed to this repo, pip
# itself included. --require-hashes also makes pip refuse to install anything
# not listed, so a new transitive dependency cannot slip in unreviewed.
# Deliberately no `--upgrade pip` beforehand: that would pull an unconstrained
# release from the live index to install a hash-locked tree with.
"$JARVIS_DIR/venv/bin/pip" install --quiet --require-hashes -r "$SRC/daemon/requirements.lock"

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
# jarvis-config install-voice is the same hardened path the settings panel
# uses: the transfer goes to an unpredictably named private temp file with a
# byte ceiling, is verified against the committed sha256 lock, and only then
# renamed onto the final pathname. curl straight onto that predictable name
# would follow a planted symlink and write unbounded, unverified bytes
# through it before any checksum ran. Skips files already present.
"$JARVIS_DIR/bin/jarvis-config" install-voice en_US-amy-medium
# Independent recheck of whatever is on disk, downloaded now or before.
( cd "$JARVIS_DIR/voices" && sha256sum -c "$SRC/daemon/voice-model.sha256" >/dev/null ) \
  || { echo "voice checksum mismatch -- delete $VOICE and re-run" >&2; exit 1; }
echo "  -> $VOICE"

# --- config ----------------------------------------------------------------
say "Configuration"
mkdir -p "$CONFIG_DIR"
if [[ -f $CONFIG_DIR/config.toml ]]; then
  echo "  -> keeping your existing $CONFIG_DIR/config.toml"
else
  put 644 "$SRC/config/config.toml.example" "$CONFIG_DIR/config.toml"
  echo "  -> wrote $CONFIG_DIR/config.toml (defaults to the claude agent)"
fi
put 644 "$SRC/config/config.toml.example" "$CONFIG_DIR/config.toml.example"

# --- systemd user unit -----------------------------------------------------
say "Installing the systemd user unit"
mkdir -p "$UNIT_DIR"
put 644 "$SRC/daemon/jarvis.service" "$UNIT_DIR/jarvis.service"
systemctl --user daemon-reload
# Deliberately not `enable`d. Enabling would start the listener at every
# login, which for an always-on microphone is not a default anyone should
# get without asking for it. The widget starts and stops it on demand.
echo "  -> installed, not enabled: the mic stays off until you arm it"
echo "     (want it armed at login? systemctl --user enable jarvis)"

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

  Most settings are in the widget's panel: click it in the bar.
  Or edit $CONFIG_DIR/config.toml directly
  See what's configured:  $JARVIS_DIR/venv/bin/python $JARVIS_DIR/jarvis-listen.py --agents
  Test without the mic:   $JARVIS_DIR/venv/bin/python $JARVIS_DIR/jarvis-listen.py --ask "hello"
DONE
