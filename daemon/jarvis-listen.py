#!/usr/bin/env python3
"""Wake-word voice assistant: say the wake word -> ask an agent -> speak the answer.

Runs as a systemd user service, toggled from the Omarchy bar widget
(dorian.voice). Everything except the agent call is local: openWakeWord
listens on a continuous 16kHz mic stream (~3% of one core), voxtype's whisper
model transcribes, piper speaks the reply.

Which agent answers is configuration, not code -- see config.toml.example.
Point it at Claude Code, Codex, a local ollama model, or anything else with a
non-interactive CLI.

Pipeline state is written to $XDG_RUNTIME_DIR/jarvis/state (falling back to
$XDG_STATE_HOME, never to a world-writable /tmp) so the bar widget can show
what it is doing without talking to this process.
"""

import argparse
import collections
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.parse
import wave

import numpy as np

# Next to this file, in the repo and in ~/.local/share/jarvis alike.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safefile

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
    # What you say near an open microphone can carry secrets, and journald
    # persists what we print. Off means the journal records sizes and
    # outcomes, never the words.
    "log_transcripts": False,
    "listen": {
        "wake_threshold": 0.5,
        "silence_tail": 1.2,
        "min_speech": 0.4,
        "max_command": 15.0,
        "cooldown": 1.0,
    },
    "agents": {
        "claude": {
            # No {prompt} in argv: the transcript is fed to `claude -p` on
            # stdin, where it is not readable out of the process list. And no
            # tool flags: the agent CLI is invoked answer-only in *both*
            # modes. With actions = true the daemon parses a strictly
            # validated <<jarvis:open-...>> directive out of the reply text
            # and execs the jarvis-open broker itself, so there is never a
            # tool grant for a spoken or injected request to aim at.
            "command": ["claude", "-p",
                        "--append-system-prompt", "{system}"],
            # Off unless the config says otherwise. Letting a sentence spoken
            # near the mic open apps and URLs is a decision the person
            # installing this should make on purpose, not one they inherit
            # from a default. It also means these DEFAULTS stay safe as a
            # fallback: an unreadable config drops back to here, and dropping
            # back should never quietly grant more than was granted before.
            "actions": False,
        },
    },
}

# The voice-style half of the system prompt. Always sent.
STYLE_PROMPT = (
    "You are a voice assistant. Your reply will be read aloud by a "
    "text-to-speech engine, so answer in at most three short sentences of "
    "plain spoken English. No markdown, no lists, no code blocks, no URLs."
)

# The actions half. Only sent to agents configured with actions = true. The
# agent is never given a tool or a shell: it asks for an action by ending its
# reply with one directive line, and the daemon decides whether anything
# happens. See extract_directive/run_directive below.
ACTIONS_PROMPT = (
    "You cannot run commands, but you can ask Jarvis to open things. To open "
    "an installed app, add a line at the end of your reply of exactly this "
    "form: <<jarvis:open-app NAME>>. To open a web page in the browser: "
    "<<jarvis:open-url URL>> (http or https only). At most one such line per "
    "reply. The line is stripped before your reply is spoken, so also say in "
    "your reply what you are opening. If asked to do anything else to the "
    "machine, say out loud that you cannot."
)

# The web half. Only sent to agents configured with a web_command. The first
# call still runs with no tools; asking to search hands the exchange to a
# second, search-capable invocation whose reply is treated as tainted -- see
# run_search below.
WEB_PROMPT = (
    "If answering needs current information from the web, reply with only "
    "this line and nothing else: <<jarvis:search WHAT TO LOOK UP>>. Jarvis "
    "will run one web-enabled round and speak its answer. Do not search for "
    "things you already know, and never combine a search line with an open "
    "line."
)

# System prompt for the web-enabled second call. Deliberately excludes
# ACTIONS_PROMPT and WEB_PROMPT: this call can read the open web, so it gets
# no way to ask for anything -- no opens, no further searches.
WEB_TURN_PROMPT = (
    "Use your web search tool to find what the question needs, then answer "
    "from what you found. Say plainly if the search settles nothing. Do not "
    "read URLs aloud."
)

def _state_root():
    """Where the pipeline-state file lives.

    XDG_RUNTIME_DIR is per-user and mode 0700, so it is the right home. The
    old fallback was tempfile.gettempdir() -- i.e. a predictable path inside a
    world-writable /tmp, where another local user could pre-plant `state` as a
    FIFO (blocking the bar widget's reader, which polls every second) or
    `state.tmp` as a symlink (redirecting our write onto one of this user's
    own files). Fall back to a directory only this user can write instead.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return runtime
    return os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state")


STATE_DIR = os.path.join(_state_root(), "jarvis")
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

        # A TOML string here would iterate as characters, and an empty prefix
        # matches every line -- either way clean_reply would quietly eat the
        # whole reply. A non-string would TypeError mid-exchange instead of
        # at startup. Refuse all of it here, loudly.
        prefixes = spec.get("strip_prefixes", [])
        if isinstance(prefixes, str) or not isinstance(prefixes, list) \
                or not all(isinstance(p, str) and p for p in prefixes):
            raise ValueError(f"agent '{name}': 'strip_prefixes' must be an "
                             "array of non-empty strings")
        self.strip_prefixes = tuple(prefixes)

        try:
            self.timeout = float(spec.get("timeout", 180))
        except (TypeError, ValueError):
            raise ValueError(f"agent '{name}': 'timeout' must be a number")
        if not 0 < self.timeout <= 3600:
            raise ValueError(f"agent '{name}': 'timeout' must be between "
                             "0 and 3600 seconds")

        # A second argv for the web-enabled round of a search exchange --
        # the one place a (CLI-enforced, read-only) search tool grant
        # belongs. Its presence is what enables search for this agent.
        web_command = spec.get("web_command")
        if web_command is not None:
            if not isinstance(web_command, list) or not web_command \
                    or not all(isinstance(p, str) for p in web_command):
                raise ValueError(f"agent '{name}': 'web_command' must be a "
                                 "non-empty array of strings")
        self.web_command = web_command
        self.web = web_command is not None
        self.web_uses_outfile = any("{outfile}" in part
                                    for part in web_command or [])
        # A {outfile} anywhere in argv means the reply is written to a file
        # rather than printed -- the escape hatch for CLIs whose stdout is a
        # progress log.
        self.uses_outfile = any("{outfile}" in part for part in command)

    @property
    def system_prompt(self):
        parts = [STYLE_PROMPT]
        if self.actions:
            parts.append(ACTIONS_PROMPT)
        if self.web:
            parts.append(WEB_PROMPT)
        return "\n\n".join(parts)

    @property
    def executable(self):
        return self.command[0]

    def build_invocation(self, prompt, outfile, system_extra="", web=False):
        """(argv, stdin_payload) for one question.

        The transcript only lands in argv if the command template asks for it
        with {prompt} -- argv is readable by every process on the machine, so
        the presets don't. Without {prompt}, the transcript is fed on stdin;
        a template that names neither {prompt} nor {system} gets both there,
        system prompt first, for CLIs with no system-prompt flag.

        web=True builds the search-capable second call: web_command's argv,
        and a system prompt that offers no directives of any kind.
        """
        if web:
            command = self.web_command
            system = STYLE_PROMPT + "\n\n" + WEB_TURN_PROMPT
        else:
            command = self.command
            system = self.system_prompt
        if system_extra:
            system += "\n\n" + system_extra
        fields = {
            "{prompt}": prompt,
            "{system}": system,
            "{outfile}": outfile or "",
        }
        used = set()
        argv = []
        for part in command:
            for token, value in fields.items():
                if token in part:
                    used.add(token)
                    part = part.replace(token, value)
            argv.append(part)
        if "{prompt}" in used:
            return argv, None
        if "{system}" in used:
            return argv, prompt
        return argv, system + "\n\n" + prompt


def load_config(path=CONFIG_PATH):
    """DEFAULTS, with ~/.config/jarvis/config.toml layered on top if present."""
    cfg = DEFAULTS
    try:
        # Descriptor-first and bounded: a symlink, FIFO or oversized file at
        # this predictable path is refused, not followed, waited on, or slurped.
        raw = safefile.read_bytes(path, safefile.MAX_CONFIG_BYTES)
    except FileNotFoundError:
        log("no config file, using defaults")
        return cfg
    except OSError as exc:
        log(f"config unreadable ({exc}), using defaults")
        return cfg
    try:
        cfg = merge(DEFAULTS, tomllib.loads(raw.decode("utf-8")))
        log(f"config: {path}")
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        log(f"config unreadable ({exc}), using defaults")
    return cfg


def select_agent(cfg):
    """Resolve cfg['agent'] to an Agent, failing loudly on a bad name."""
    name = cfg.get("agent", "claude")
    specs = cfg.get("agents", {})
    if name not in specs:
        known = ", ".join(sorted(specs)) or "none"
        raise SystemExit(f"[jarvis] unknown agent '{name}'. Configured: {known}")
    try:
        agent = Agent(name, specs[name])
    except ValueError as exc:
        # A clean message, not a traceback, for systemd's restart loop to log.
        raise SystemExit(f"[jarvis] {exc}")
    if shutil.which(agent.executable) is None:
        log(f"warning: '{agent.executable}' is not on PATH -- replies will fail")
    if any("{prompt}" in part for part in agent.command):
        log(f"warning: agent '{agent.name}' puts the transcript in argv, where "
            "every local process can read it; drop {prompt} from `command` to "
            "send it on stdin instead")
    # Actions are brokered by this daemon, never by a tool grant to the CLI.
    # A command that hands the agent tools anyway isn't something we can
    # police -- it's the user's argv -- but it deserves a loud note.
    if any("--allowedTools" in part or "--dangerously" in part
           for part in agent.command):
        log(f"warning: agent '{agent.name}' grants the CLI tools in `command`. "
            "Jarvis never needs that: actions go through the jarvis-open "
            "broker, and a search grant belongs in `web_command`. Remove the "
            "tool flags unless you accept the risk.")
    # The web invocation reads the open internet, so what it may hold matters
    # more, not less: WebFetch or a shell there hands a hostile page an
    # exfiltration channel. WebSearch alone is the sanctioned grant.
    if any("WebFetch" in part or "Bash" in part or "--dangerously" in part
           for part in agent.web_command or []):
        log(f"warning: agent '{agent.name}' grants `web_command` more than "
            "web search. A fetch tool or a shell in the web-enabled call "
            "lets a hostile page exfiltrate or act; grant WebSearch only.")
    caps = "can act" if agent.actions else "answer-only"
    if agent.web:
        caps += ", web search"
    log(f"agent: {agent.name} ({caps})")
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
    """Publish pipeline state for the bar widget (idle/listening/thinking/speaking).

    safefile.write_atomic writes an unpredictably named 0600 temp file inside
    the 0700 state dir and renames it over the target, so there is no
    guessable `state.tmp` to pre-plant and the widget's once-a-second reader
    only ever sees a complete value.
    """
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        safefile.write_atomic(STATE_FILE, state)
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
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    err = safefile.open_w_nofollow(os.path.join(STATE_DIR, "pw-record.log"))
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
    """Short feedback tone so you know it heard you, without a notification.

    The timeout matters more than the tone: pw-play blocking on a wedged
    audio server would otherwise hang the listener, not just skip a beep.
    """
    freq = 880 if kind == "start" else 440
    try:
        subprocess.run(
            ["pw-play", "--rate=16000", "--channels=1", "--format=s16", "-"],
            input=tone(freq), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=10,
        )
    except subprocess.TimeoutExpired:
        log("chime timed out; is the audio server healthy?")


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
# Bounded subprocess execution
# --------------------------------------------------------------------------

# Ceilings on what a child process can make this always-on service hold.
MAX_CAPTURE_BYTES = 1 << 20   # any child's stdout
MAX_ERR_BYTES = 64 << 10      # stderr is only ever quoted in error messages
MAX_SPOKEN_CHARS = 1200       # the reply is three short sentences; this is slack

BoundedRun = collections.namedtuple(
    "BoundedRun", "returncode stdout stderr overflowed")


def run_bounded(argv, *, timeout, stdout_limit=MAX_CAPTURE_BYTES,
                stderr_limit=MAX_ERR_BYTES, input_text=None, cwd=None):
    """subprocess.run(capture_output=True) minus the unbounded buffering.

    capture_output accumulates everything the child ever prints before any
    caller-side truncation can happen, so one runaway or compromised
    executable could grow this service without limit. Here each stream is
    drained into a capped buffer as it is produced; the moment either stream
    passes its ceiling the child is killed and the result comes back marked
    `overflowed` -- callers treat that as a failure, never as a long answer.
    A timeout kills the child and re-raises subprocess.TimeoutExpired, same
    as subprocess.run. `input_text` is fed to the child's stdin from a
    thread, so a child that never reads it cannot deadlock us; with no
    input_text, stdin is /dev/null rather than our own.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
    )
    out_buf, err_buf = bytearray(), bytearray()
    overflowed = threading.Event()

    def drain(stream, buf, limit):
        try:
            while True:
                chunk = stream.read(1 << 16)
                if not chunk:
                    return
                if len(buf) + len(chunk) > limit:
                    buf += chunk[:limit - len(buf)]
                    overflowed.set()
                    proc.kill()
                    # Keep the pipe moving until EOF so the dying child is
                    # never blocked writing to it.
                    while stream.read(1 << 16):
                        pass
                    return
                buf += chunk
        except (OSError, ValueError):
            pass

    def feed():
        try:
            proc.stdin.write(input_text.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    threads = [
        threading.Thread(target=drain, args=(proc.stdout, out_buf, stdout_limit)),
        threading.Thread(target=drain, args=(proc.stderr, err_buf, stderr_limit)),
    ]
    if input_text is not None:
        threads.append(threading.Thread(target=feed))
    for t in threads:
        t.daemon = True
        t.start()

    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        for t in threads:
            t.join(timeout=5)
        raise
    # A grandchild holding the pipe open could stall a reader past the
    # child's own exit; the join timeout (plus daemon threads) means it
    # stalls the reader, not the listener.
    for t in threads:
        t.join(timeout=5)

    return BoundedRun(
        returncode=returncode,
        stdout=out_buf.decode("utf-8", "replace"),
        stderr=err_buf.decode("utf-8", "replace"),
        overflowed=overflowed.is_set(),
    )


# --------------------------------------------------------------------------
# Transcribe -> agent -> speak
# --------------------------------------------------------------------------

ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# voxtype logs to stdout alongside the transcript.
VOXTYPE_NOISE = ("Loading ", "Audio format:", "Processing ", "whisper_")


def transcribe(path):
    """Run voxtype's local whisper model. It logs to stdout, so take the tail."""
    proc = run_bounded(["voxtype", "transcribe", path], timeout=120)
    if proc.overflowed:
        log("voxtype exceeded its output ceiling; transcription discarded")
        return ""
    lines = []
    for raw in proc.stdout.splitlines():
        line = ANSI.sub("", raw).strip()
        if not line or " INFO " in line or " WARN " in line:
            continue
        if line.startswith(VOXTYPE_NOISE):
            continue
        lines.append(line)
    return lines[-1] if lines else ""


def ask_agent(agent, prompt, web=False):
    """Run the configured agent CLI and return its spoken reply, or ''.

    web=True runs the agent's web_command instead -- the search-capable
    second half of a search exchange. The caller treats that reply as
    tainted: no directive from it is ever executed.
    """
    outfile = None
    uses_outfile = agent.web_uses_outfile if web else agent.uses_outfile
    if uses_outfile:
        fd, outfile = tempfile.mkstemp(suffix=".txt", prefix="jarvis-reply-")
        os.close(fd)

    system_extra = ""
    if agent.actions and not web:
        apps = installed_apps()
        if apps:
            system_extra = "Installed apps: " + ", ".join(apps) + "."

    argv, stdin_payload = agent.build_invocation(prompt, outfile, system_extra,
                                                 web=web)

    try:
        try:
            proc = run_bounded(argv, timeout=agent.timeout,
                               input_text=stdin_payload, cwd=HOME)
        except FileNotFoundError:
            log(f"agent '{agent.name}': '{agent.executable}' not found on PATH")
            return ""
        except subprocess.TimeoutExpired:
            log(f"agent '{agent.name}' timed out after {agent.timeout:.0f}s")
            return ""

        if proc.overflowed:
            log(f"agent '{agent.name}' exceeded its output ceiling; reply discarded")
            return ""
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:200]
            log(f"agent '{agent.name}' failed (exit {proc.returncode}): {detail}")
            return ""

        if outfile:
            try:
                # mkstemp made this one, but it lives in a world-writable /tmp
                # for the lifetime of the agent call: read it back the same
                # careful way as anything else, and cap what a runaway agent
                # can make us hold in memory.
                return safefile.read_text(outfile, safefile.MAX_TEXT_BYTES).strip()
            except OSError:
                log(f"agent '{agent.name}' wrote no reply file")
                return ""

        return clean_reply(proc.stdout, agent.strip_prefixes)
    finally:
        if outfile:
            try:
                os.unlink(outfile)
            except OSError:
                pass


def clean_reply(text, strip_prefixes):
    """Drop ANSI codes and any configured progress-log lines."""
    lines = []
    for raw in text.splitlines():
        line = ANSI.sub("", raw).rstrip()
        if strip_prefixes and line.strip().startswith(strip_prefixes):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Actions: a structured directive, brokered outside the agent
#
# The agent CLI never gets a shell or a tool grant. When actions are on, the
# agent asks for an action by ending its reply with one directive line; the
# daemon parses it against a strict pattern, validates the argument again
# here, and execs the jarvis-open broker directly -- one argv, no shell --
# which validates it a third time and can launch an installed .desktop entry
# or open an http(s) URL, nothing else. A prompt-level instruction plus a
# shell allowlist is not an authorization boundary; this is enforced where
# the agent cannot reach it.
#
# The optional search hand-off (run_search) rides the same rails: the
# no-tools first call may request one web-enabled round, and the reply of
# that round -- the only place web content can enter -- has every directive
# stripped and ignored, so what came off the web can never act here.
# --------------------------------------------------------------------------

DIRECTIVE_RE = re.compile(
    r"^\s*<<jarvis:(open-app|open-url|search)\s+([^<>\n]{1,2048}?)\s*>>\s*$")
_DIRECTIVE_KINDS = {"open-app": "app", "open-url": "url", "search": "search"}
# What we will pass the broker as an app query: printable, no leading dash,
# short. The broker only fuzzy-matches it against installed .desktop names.
APP_QUERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,79}$")
# Same shape jarvis-open itself enforces before handing a URL to xdg-open.
URL_RE = re.compile(r"^https?://[^\s\"'\\<>]+$")


def jarvis_open_path():
    """The broker binary: installed under ~/.local/share/jarvis/bin, or next
    to this file when running from a checkout."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(JARVIS_BIN, "jarvis-open"),
                      os.path.join(here, "jarvis-open")):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


# The list behind the actions prompt costs a broker spawn plus a full
# .desktop scan. Fine once, needless on every question of a conversation --
# but the daemon runs for days, so a process-lifetime cache would hide a
# newly installed app until restart. A short TTL gets both, and failures are
# not cached, so a transient broker problem is retried on the next exchange.
_APPS_TTL_SECONDS = 60.0
_apps_cache = {"at": 0.0, "names": []}


def installed_apps():
    """App names for the actions system prompt, from the broker's `list`.

    The agent has no way to run `jarvis-open list` itself any more, so tell
    it what is installed up front. Bounded like every other child, capped
    well below any prompt-size trouble, and cached briefly (see above).
    """
    now = time.monotonic()
    if _apps_cache["names"] and now - _apps_cache["at"] < _APPS_TTL_SECONDS:
        return _apps_cache["names"]
    broker = jarvis_open_path()
    if broker is None:
        return []
    try:
        proc = run_bounded([broker, "list"], timeout=10,
                           stdout_limit=256 << 10, stderr_limit=16 << 10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0 or proc.overflowed:
        return []
    names, total = [], 0
    for line in proc.stdout.splitlines():
        name = line.strip()
        if not name:
            continue
        total += len(name) + 2
        if total > 4000:
            break
        names.append(name)
    _apps_cache["at"] = now
    _apps_cache["names"] = names
    return names


def extract_directive(reply):
    """Split a reply into (spoken_text, directive-or-None).

    Directive lines are stripped from the spoken text whether or not actions
    are enabled -- an ignored directive should not be read aloud either --
    and only the first one counts.
    """
    directive = None
    kept = []
    for line in reply.splitlines():
        match = DIRECTIVE_RE.match(line)
        if match:
            if directive is None:
                directive = (_DIRECTIVE_KINDS[match.group(1)],
                             match.group(2).strip())
            continue
        kept.append(line)
    return "\n".join(kept).strip(), directive


def run_directive(directive):
    """Validate one directive and exec the broker for it. True on success."""
    kind, value = directive
    if kind == "app" and not APP_QUERY_RE.match(value):
        log("directive refused: app name failed validation")
        return False
    if kind == "url" and not URL_RE.match(value):
        log("directive refused: not a plain http(s) url")
        return False
    broker = jarvis_open_path()
    if broker is None:
        log("directive refused: jarvis-open broker not found")
        return False
    try:
        proc = run_bounded([broker, kind, value], timeout=15,
                           stdout_limit=64 << 10, stderr_limit=16 << 10)
    except (OSError, subprocess.TimeoutExpired):
        log("jarvis-open did not run")
        return False
    if proc.returncode != 0 or proc.overflowed:
        log(f"jarvis-open refused: {(proc.stderr or proc.stdout).strip()[:200]}")
        return False
    if kind == "url":
        # A search URL carries the spoken question verbatim in its query
        # string, and the journal must not learn the transcript through a
        # side door. Audit the destination's origin, never the full URL.
        origin = urllib.parse.urlsplit(value)
        log(f"jarvis-open: opened {origin.scheme}://{origin.netloc} "
            "(full url not journaled)")
    else:
        log(f"jarvis-open: {proc.stdout.strip()[:200]}")
    return True


MAX_SEARCH_QUERY_CHARS = 400


def run_search(agent, query, log_text=False):
    """The web-enabled second half of a search exchange. Returns spoken text.

    The gating here is by construction, not by trust. The first call ran
    with no tools at all, so nothing from the open web can have entered it:
    a directive it emits traces back to the speaker, and is executed. This
    call reads the web, so nothing it emits is trusted: every directive in
    its reply -- an open, another search -- is stripped and ignored, which
    is what makes granting the search tool safe at all, and why there is
    exactly one hop.
    """
    if not agent.web:
        log("agent asked to search but has no web_command; refused")
        return "Sorry, I cannot search the web."
    query = " ".join(query.split())
    if not 0 < len(query) <= MAX_SEARCH_QUERY_CHARS:
        log("search query failed validation; refused")
        return "Sorry, I could not run that search."
    # The query is derived from what was spoken: journal its size, not it.
    log(f"searching: {query}" if log_text else
        f"searching ({len(query)} characters)")
    reply, stray = extract_directive(ask_agent(agent, query, web=True))
    if stray:
        log(f"directive in a web-tainted reply ignored ({stray[0]})")
    return (reply[:MAX_SPOKEN_CHARS]
            or "Sorry, the search did not come back with an answer.")


def speak(text, voice):
    # Belt-and-braces: respond() caps the reply too, but nothing longer than
    # this ever reaches the synthesiser regardless of the path in.
    text = text[:MAX_SPOKEN_CHARS]
    if not text:
        return
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        out = tmp.name
    try:
        try:
            run_bounded(
                [VENV_PY, "-m", "piper", "-m", voice, "-f", out],
                input_text=text, timeout=180,
                stdout_limit=64 << 10, stderr_limit=64 << 10,
            )
        except subprocess.TimeoutExpired:
            log("speech synthesis timed out")
            return
        # A piper failure still leaves a bare 44-byte wav header behind.
        # The capped reply synthesises to at most a couple of minutes of
        # audio, so a playback still running at five is a wedged audio
        # server holding the listener hostage, not a long answer.
        if os.path.getsize(out) > 44:
            try:
                subprocess.run(["pw-play", out], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False,
                               timeout=300)
            except subprocess.TimeoutExpired:
                log("playback timed out; is the audio server healthy?")
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def respond(agent, voice, text, log_text=False):
    """Shared tail of the pipeline: ask, then say the answer out loud.

    The words themselves only reach the journal when log_transcripts opted
    in; by default the journal records that an exchange happened and how big
    it was, because spoken content can carry secrets and journald persists.
    """
    log(f"heard: {text}" if log_text else f"heard {len(text)} characters")
    answer, directive = extract_directive(ask_agent(agent, text))
    answer = answer[:MAX_SPOKEN_CHARS]
    if directive and directive[0] == "search":
        answer = run_search(agent, directive[1], log_text)
        directive = None
    if directive:
        if agent.actions:
            ok = run_directive(directive)
            if ok and not answer:
                answer = "Opening it now."
            elif not ok:
                answer = (answer + " Sorry, that did not open.").strip()
        else:
            log("agent sent an open directive but actions are off; ignored")
            if not answer:
                answer = "Sorry, opening things is turned off."
    # The generic fallback comes last, after directive handling: a reply that
    # was nothing but a directive line strips to empty, and silence is the
    # one answer a voice assistant must never give.
    if not answer:
        answer = "Sorry, I could not get an answer."
    log(f"reply: {answer[:120]}" if log_text else f"reply: {len(answer)} characters")
    set_state("speaking")
    speak(answer, voice)
    return answer


def handle_command(mic, ambient, agent, voice, listen, log_text=False):
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
    except subprocess.TimeoutExpired:
        # One slow transcription should cost you one question, not the
        # listener. Letting this escape kills the daemon, and systemd's
        # Restart=on-failure then brings the microphone back up on its own,
        # which is a strange way for an armed mic to behave.
        log("transcription timed out")
        return
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if not text:
        log("empty transcription")
        return
    respond(agent, voice, text, log_text)


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def listen_forever(agent, voice, wake_path, wake_key, listen, log_text=False):
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
                handle_command(mic, ambient, agent, voice, listen, log_text)
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
            if agent.web:
                kind += " +web"
            print(f"{mark} {name:12} {found:15} {kind}")
        return 0

    agent = select_agent(cfg)
    voice = resolve_voice(cfg)
    wake_path, wake_key = resolve_wake_model(cfg)
    log_text = bool(cfg.get("log_transcripts", False))

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
        respond(agent, voice, args.ask, log_text)
        return 0

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    listen_forever(agent, voice, wake_path, wake_key, listen, log_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
