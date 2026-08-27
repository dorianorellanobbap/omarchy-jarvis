#!/usr/bin/env python3
"""Wake-word voice assistant: say the wake word -> ask an agent -> speak the answer.

Runs as a systemd user service, toggled from the Omarchy bar widget
(dorian.voice). Everything except the agent call is local: openWakeWord
listens on a continuous 16kHz mic stream (~3% of one core), voxtype's whisper
model transcribes, piper speaks the reply.

Which agent answers is configuration, not code -- see config.toml.example.
Point it at Claude Code, Codex, a local ollama model, or anything else with a
non-interactive CLI.

Pipeline state is written to $XDG_RUNTIME_DIR/jarvis/state so the bar widget
can show what it is doing without talking to this process.
"""

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import wave

import numpy as np

HOME = os.path.expanduser("~")
JARVIS_DIR = os.path.join(HOME, ".local", "share", "jarvis")
VOICES_DIR = os.path.join(JARVIS_DIR, "voices")
VENV_PY = os.path.join(JARVIS_DIR, "venv", "bin", "python")
JARVIS_BIN = os.path.join(JARVIS_DIR, "bin")

CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
CONFIG_PATH = os.path.join(CONFIG_HOME, "jarvis", "config.toml")

RATE = 16000
CHUNK_SAMPLES = 1280           # openWakeWord wants 80ms frames
CHUNK_BYTES = CHUNK_SAMPLES * 2

# openWakeWord ships these four wake-word models. The other .onnx files in
# its resources dir are feature extractors and intent classifiers, not wake
# words -- naming one still works, but these are the supported set.
WAKE_WORDS = ("hey_jarvis", "alexa", "hey_mycroft", "hey_marvin")

DEFAULTS = {
    "agent": "claude",
    "wake_word": "hey_jarvis",
    "voice": "en_US-amy-medium.onnx",
    "listen": {
        "wake_threshold": 0.5,
        "silence_tail": 1.2,
        "min_speech": 0.4,
        "max_command": 15.0,
        "cooldown": 1.0,
    },
    "agents": {
        "claude": {
            "command": ["claude", "-p", "{prompt}",
                        "--append-system-prompt", "{system}",
                        "--allowedTools", "Bash(jarvis-open:*)"],
            "actions": True,
        },
    },
}

# The voice-style half of the system prompt. Always sent.
STYLE_PROMPT = (
    "You are a voice assistant. Your reply will be read aloud by a "
    "text-to-speech engine, so answer in at most three short sentences of "
    "plain spoken English. No markdown, no lists, no code blocks, no URLs."
)

# The actions half. Only sent to agents configured with actions = true.
ACTIONS_PROMPT = (
    "You can act on the machine, not just answer. To open an installed app "
    "run `jarvis-open app <name>` (e.g. jarvis-open app chromium). To open a "
    "web page in the browser run `jarvis-open url <https url>`. "
    "`jarvis-open list` prints every installed app name. That command is "
    "pre-approved, so run it directly and never ask for permission or say "
    "you are waiting on approval. If something else is asked of you that "
    "jarvis-open cannot do, just say so out loud."
)

runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
STATE_DIR = os.path.join(runtime, "jarvis")
STATE_FILE = os.path.join(STATE_DIR, "state")

_running = True


def log(msg):
    # stderr, not stdout: jarvis-config prints JSON on stdout and the bar
    # widget parses it. journald captures both streams either way.
    print(f"[jarvis] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def merge(base, override):
    """Recursive dict merge; override wins. Used to layer config over DEFAULTS."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


class Agent:
    """One configured agent CLI: how to invoke it and what it's allowed to do."""

    def __init__(self, name, spec):
        command = spec.get("command")
        if not isinstance(command, list) or not command:
            raise ValueError(f"agent '{name}': 'command' must be a non-empty array")
        if not all(isinstance(part, str) for part in command):
            raise ValueError(f"agent '{name}': every 'command' entry must be a string")

        self.name = name
        self.command = command
        self.actions = bool(spec.get("actions", False))
        self.strip_prefixes = tuple(spec.get("strip_prefixes", []))
        self.timeout = float(spec.get("timeout", 180))
        # A {outfile} anywhere in argv means the reply is written to a file
        # rather than printed -- the escape hatch for CLIs whose stdout is a
        # progress log.
        self.uses_outfile = any("{outfile}" in part for part in command)

    @property
    def system_prompt(self):
        if self.actions:
            return STYLE_PROMPT + "\n\n" + ACTIONS_PROMPT
        return STYLE_PROMPT

    @property
    def executable(self):
        return self.command[0]

    def build_argv(self, prompt, outfile):
        fields = {
            "{prompt}": prompt,
            "{system}": self.system_prompt,
            "{outfile}": outfile or "",
        }
        argv = []
        for part in self.command:
            for token, value in fields.items():
                part = part.replace(token, value)
            argv.append(part)
        return argv


def load_config(path=CONFIG_PATH):
    """DEFAULTS, with ~/.config/jarvis/config.toml layered on top if present."""
    cfg = DEFAULTS
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                cfg = merge(DEFAULTS, tomllib.load(fh))
            log(f"config: {path}")
        except (tomllib.TOMLDecodeError, OSError) as exc:
            log(f"config unreadable ({exc}), using defaults")
    else:
        log("no config file, using defaults")
    return cfg


def select_agent(cfg):
    """Resolve cfg['agent'] to an Agent, failing loudly on a bad name."""
    name = cfg.get("agent", "claude")
    specs = cfg.get("agents", {})
    if name not in specs:
        known = ", ".join(sorted(specs)) or "none"
        raise SystemExit(f"[jarvis] unknown agent '{name}'. Configured: {known}")
    agent = Agent(name, specs[name])
    if shutil.which(agent.executable) is None:
        log(f"warning: '{agent.executable}' is not on PATH -- replies will fail")
    log(f"agent: {agent.name} ({'can act' if agent.actions else 'answer-only'})")
    return agent


def resolve_voice(cfg):
    voice = cfg.get("voice", DEFAULTS["voice"])
    return voice if os.path.isabs(voice) else os.path.join(VOICES_DIR, voice)


def resolve_wake_model(cfg):
    """Map a wake-word name to the onnx file openWakeWord ships.

    Returns (path, score_key). The score key is the file stem, which is what
    Model.predict() uses to label its scores.
    """
    import openwakeword

    name = cfg.get("wake_word", DEFAULTS["wake_word"])
    models_dir = os.path.join(os.path.dirname(openwakeword.__file__),
                              "resources", "models")
    for stem in sorted(os.path.splitext(f)[0] for f in os.listdir(models_dir)
                       if f.endswith(".onnx")):
        # "hey_jarvis" should match the shipped "hey_jarvis_v0.1".
        if stem == name or stem.rsplit("_v", 1)[0] == name:
            return os.path.join(models_dir, stem + ".onnx"), stem
    raise SystemExit(f"[jarvis] unknown wake_word '{name}'. "
                     f"Available: {', '.join(WAKE_WORDS)}")


# --------------------------------------------------------------------------
# Pipeline state, shared with the bar widget
# --------------------------------------------------------------------------

def set_state(state):
    """Publish pipeline state for the bar widget (idle/listening/thinking/speaking)."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(state)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def on_signal(_signum, _frame):
    global _running
    _running = False


# --------------------------------------------------------------------------
# Audio in
# --------------------------------------------------------------------------

def open_mic():
    """Continuous raw 16kHz mono s16 stream from PipeWire on stdout.

    pw-record exits early if its stderr is subprocess.DEVNULL, so give it a
    real file to write to.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    err = open(os.path.join(STATE_DIR, "pw-record.log"), "w")
    return subprocess.Popen(
        ["pw-record", "--rate=16000", "--channels=1", "--format=s16",
         "--latency=40ms", "-"],
        stdout=subprocess.PIPE,
        stderr=err,
    )


def read_chunk(mic):
    """Read one full frame. Short reads are normal while the stream spins up,
    so only a dead pw-record counts as the end of the stream."""
    buf = b""
    while len(buf) < CHUNK_BYTES:
        part = mic.stdout.read(CHUNK_BYTES - len(buf))
        if not part:
            if mic.poll() is not None:
                return None
            time.sleep(0.01)
            continue
        buf += part
    return np.frombuffer(buf, dtype=np.int16)


def rms(samples):
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


def tone(freq, ms=120):
    n = int(RATE * ms / 1000)
    t = np.arange(n) / RATE
    envelope = np.minimum(1.0, np.minimum(t * 40, (n / RATE - t) * 40))
    wave_data = 0.25 * np.sin(2 * np.pi * freq * t) * envelope
    return (wave_data * 32767).astype(np.int16).tobytes()


def chime(kind):
    """Short feedback tone so you know it heard you, without a notification."""
    freq = 880 if kind == "start" else 440
    subprocess.run(
        ["pw-play", "--rate=16000", "--channels=1", "--format=s16", "-"],
        input=tone(freq), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False,
    )


def capture_command(mic, ambient, listen):
    """Record until the speaker stops. Returns int16 samples, or None."""
    threshold = max(ambient * 3.0, 300.0)
    frames = []
    speech_time = 0.0
    silence_time = 0.0
    elapsed = 0.0
    frame_secs = CHUNK_SAMPLES / RATE

    while _running and elapsed < listen["max_command"]:
        samples = read_chunk(mic)
        if samples is None:
            return None
        frames.append(samples)
        elapsed += frame_secs

        if rms(samples) > threshold:
            speech_time += frame_secs
            silence_time = 0.0
        else:
            silence_time += frame_secs
            if speech_time >= listen["min_speech"] and silence_time >= listen["silence_tail"]:
                break

    if speech_time < listen["min_speech"]:
        return None
    return np.concatenate(frames)


def write_wav(samples, path):
    with wave.open(path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(RATE)
        fh.writeframes(samples.tobytes())


# --------------------------------------------------------------------------
# Transcribe -> agent -> speak
# --------------------------------------------------------------------------

ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# voxtype logs to stdout alongside the transcript.
VOXTYPE_NOISE = ("Loading ", "Audio format:", "Processing ", "whisper_")


def transcribe(path):
    """Run voxtype's local whisper model. It logs to stdout, so take the tail."""
    proc = subprocess.run(
        ["voxtype", "transcribe", path],
        capture_output=True, text=True, timeout=120, check=False,
    )
    lines = []
    for raw in proc.stdout.splitlines():
        line = ANSI.sub("", raw).strip()
        if not line or " INFO " in line or " WARN " in line:
            continue
        if line.startswith(VOXTYPE_NOISE):
            continue
        lines.append(line)
    return lines[-1] if lines else ""


def ask_agent(agent, prompt):
    """Run the configured agent CLI and return its spoken reply, or ''."""
    outfile = None
    if agent.uses_outfile:
        fd, outfile = tempfile.mkstemp(suffix=".txt", prefix="jarvis-reply-")
        os.close(fd)

    argv = agent.build_argv(prompt, outfile)

    env = dict(os.environ)
    # jarvis-open has to be findable by name for the allowlist to match.
    env["PATH"] = JARVIS_BIN + os.pathsep + env.get("PATH", "")

    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=agent.timeout, check=False, cwd=HOME, env=env,
        )
    except FileNotFoundError:
        log(f"agent '{agent.name}': '{agent.executable}' not found on PATH")
        return ""
    except subprocess.TimeoutExpired:
        log(f"agent '{agent.name}' timed out after {agent.timeout:.0f}s")
        return ""

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:200]
        log(f"agent '{agent.name}' failed (exit {proc.returncode}): {detail}")
        return ""

    if outfile:
        try:
            with open(outfile) as fh:
                return fh.read().strip()
        except OSError:
            log(f"agent '{agent.name}' wrote no reply file")
            return ""
        finally:
            try:
                os.unlink(outfile)
            except OSError:
                pass

    return clean_reply(proc.stdout, agent.strip_prefixes)


def clean_reply(text, strip_prefixes):
    """Drop ANSI codes and any configured progress-log lines."""
    lines = []
    for raw in text.splitlines():
        line = ANSI.sub("", raw).rstrip()
        if strip_prefixes and line.strip().startswith(strip_prefixes):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def speak(text, voice):
    if not text:
        return
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        out = tmp.name
    try:
        subprocess.run(
            [VENV_PY, "-m", "piper", "-m", voice, "-f", out],
            input=text, text=True, capture_output=True, timeout=180, check=False,
        )
        # A piper failure still leaves a bare 44-byte wav header behind.
        if os.path.getsize(out) > 44:
            subprocess.run(["pw-play", out], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def respond(agent, voice, text):
    """Shared tail of the pipeline: ask, then say the answer out loud."""
    log(f"heard: {text}")
    answer = ask_agent(agent, text) or "Sorry, I could not get an answer."
    log(f"reply: {answer[:120]}")
    set_state("speaking")
    speak(answer, voice)
    return answer


def handle_command(mic, ambient, agent, voice, listen):
    chime("start")
    set_state("listening")
    samples = capture_command(mic, ambient, listen)
    if samples is None:
        log("nothing said")
        return

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = tmp.name
    try:
        write_wav(samples, path)
        set_state("thinking")
        chime("stop")
        text = transcribe(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if not text:
        log("empty transcription")
        return
    respond(agent, voice, text)


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def listen_forever(agent, voice, wake_path, wake_key, listen):
    from openwakeword.model import Model

    model = Model(wakeword_model_paths=[wake_path])
    log(f"model loaded, listening for '{wake_key.rsplit('_v', 1)[0].replace('_', ' ')}'")

    mic = open_mic()
    set_state("idle")

    ambient = 200.0
    last_fire = 0.0

    try:
        while _running:
            samples = read_chunk(mic)
            if samples is None:
                log("mic stream ended, restarting")
                mic.kill()
                time.sleep(1)
                mic = open_mic()
                model.reset()
                continue

            level = rms(samples)
            # Slow rolling floor so the VAD adapts to the room.
            if level < ambient * 2:
                ambient = ambient * 0.995 + level * 0.005

            scores = model.predict(samples)
            score = float(scores.get(wake_key, 0.0))

            if score > listen["wake_threshold"] and time.time() - last_fire > listen["cooldown"]:
                log(f"wake word detected ({score:.2f})")
                handle_command(mic, ambient, agent, voice, listen)
                # Nothing drained the mic while we were thinking and speaking,
                # so the pipe holds seconds of stale audio (including our own
                # reply). Start a fresh stream rather than replay it.
                mic.kill()
                mic = open_mic()
                model.reset()
                last_fire = time.time()
                set_state("idle")
    finally:
        set_state("off")
        mic.kill()
        log("stopped")


def main():
    parser = argparse.ArgumentParser(
        prog="jarvis-listen",
        description="Wake-word voice assistant. With no arguments, listens forever.",
    )
    parser.add_argument("--config", default=CONFIG_PATH,
                        help=f"config file (default: {CONFIG_PATH})")
    parser.add_argument("--ask", metavar="TEXT",
                        help="skip the mic: send TEXT to the agent and speak the reply")
    parser.add_argument("--agents", action="store_true",
                        help="list configured agents and exit")
    parser.add_argument("--check", action="store_true",
                        help="verify config and runtime dependencies, then exit")
    args = parser.parse_args()

    cfg = load_config(args.config)
    listen = merge(DEFAULTS["listen"], cfg.get("listen", {}))

    if args.agents:
        for name in sorted(cfg.get("agents", {})):
            try:
                agent = Agent(name, cfg["agents"][name])
            except ValueError as exc:
                print(f"  {name:12} INVALID -- {exc}")
                continue
            found = "ok" if shutil.which(agent.executable) else "not installed"
            mark = "*" if name == cfg.get("agent") else " "
            kind = "can act" if agent.actions else "answer-only"
            print(f"{mark} {name:12} {found:15} {kind}")
        return 0

    agent = select_agent(cfg)
    voice = resolve_voice(cfg)
    wake_path, wake_key = resolve_wake_model(cfg)

    if args.check:
        ok = True
        for label, path in (("voice", voice), ("wake model", wake_path)):
            exists = os.path.exists(path)
            ok &= exists
            print(f"{'ok ' if exists else 'MISSING'}  {label}: {path}")
        for cmd in ("pw-record", "pw-play", "voxtype", agent.executable):
            found = shutil.which(cmd)
            ok &= bool(found)
            print(f"{'ok ' if found else 'MISSING'}  {cmd}: {found or '-'}")
        return 0 if ok else 1

    if args.ask:
        respond(agent, voice, args.ask)
        return 0

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    listen_forever(agent, voice, wake_path, wake_key, listen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
