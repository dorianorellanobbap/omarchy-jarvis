#!/usr/bin/env python3
"""Wake-word voice assistant: say the wake word -> ask an agent -> speak the answer.

Runs as a systemd user service, toggled from the Omarchy bar widget
(dorian.voice). Everything except the agent call is local: openWakeWord
listens on a continuous 16kHz mic stream (~3% of one core), voxtype's whisper
model transcribes, piper speaks the reply.

Which agent answers is configuration, not code -- see config.toml.example.
Point it at Claude Code, a local ollama model, or anything else with a
non-interactive CLI, bearing in mind that only an invocation which actually
denies the CLI its tools is answer-only; that file explains how to check.

Pipeline state is written to $XDG_RUNTIME_DIR/jarvis/state (falling back to
$XDG_STATE_HOME, never to a world-writable /tmp) so the bar widget can show
what it is doing without talking to this process.
"""

import argparse
import collections
import os
import re
import resource
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
            # stdin, where it is not readable out of the process list.
            #
            # The tool flags are the deny boundary, and both are load-bearing.
            # Passing no tool flags at all is *not* answer-only: `claude -p`
            # still exposes its built-in read tools, and still loads whatever
            # MCP servers the user's own configuration defines, so a sentence
            # spoken near the mic could read local files through an agent we
            # meant to be a text box. `--tools ""` is the CLI's empty built-in
            # allowlist, and `--strict-mcp-config` with no accompanying
            # --mcp-config loads no MCP servers, so an inherited user or
            # project config cannot put tools back. With actions = true the
            # daemon parses a strictly validated <<jarvis:open-...>> directive
            # out of the reply text and execs the jarvis-open broker itself,
            # so there is still never a tool grant to aim at.
            "command": ["claude", "-p",
                        "--tools", "", "--strict-mcp-config",
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

# Sent whenever the invocation grants the CLI no tools, which is what the
# shipped presets do. Without it the model does not know its tools are gone:
# it answers a "read this file" with tool-call syntax, which is then read
# aloud as punctuation soup. Telling it plainly gets a plain refusal instead.
# Left off an invocation the user has given tools to, where it would be false.
NO_TOOLS_PROMPT = (
    "You have no tools in this conversation. You cannot read or write files, "
    "run commands, or browse. If answering would need one, say so in a short "
    "spoken sentence. Never write out a tool call or any other markup."
)

# The actions half. Only sent to agents configured with actions = true. The
# agent is never given a tool or a shell: it asks for an action by ending its
# reply with one directive line, and the daemon decides whether anything
# happens. See extract_directives/run_directives below.
ACTIONS_PROMPT = (
    "You cannot run commands, but you can ask Jarvis to open things. To open "
    "an installed app, add a line at the end of your reply of exactly this "
    "form: <<jarvis:open-app NAME>>. To open a web page in the browser: "
    "<<jarvis:open-url URL>> (http or https only). To open one of the "
    "speaker's Chromium bookmarks, name it: <<jarvis:open-bookmark NAME>>, "
    "where NAME is roughly what they called it. You cannot see their "
    "bookmarks and must never guess a URL for one; ask for it by name and "
    "Jarvis will match it, or say you could not find it. To open several "
    "things at "
    "once, write one line for each, up to three, in the order you want them "
    "opened. Never refuse a request just because it asks for more than one "
    "thing. The lines are stripped before your reply is spoken, so also say "
    "in your reply what you are opening. If asked to do anything else to the "
    "machine, say out loud that you cannot."
)

# The desktop half. Only sent to agents configured with desktop = true. Same
# shape as the actions directive, and deliberately the same non-grant: the
# agent ends its reply with a line, and a broker decides whether anything
# happens. Every one of these is reversible by saying the opposite, none
# touches a file, installs anything, or reaches the network.
DESKTOP_PROMPT = (
    "You can change a few things about the desktop, each by adding a line at "
    "the end of your reply of exactly this form:\n"
    "<<jarvis:workspace N>> to switch to workspace N, 1 to 10.\n"
    "The speaker's words reach you through speech-to-text, which writes "
    "spoken numbers as the words that sound like them: 'to' and 'too' are 2, "
    "'for' and 'fore' are 4, 'won' is 1, 'ate' is 8, 'tree' is 3. Read the "
    "number they meant and act on it. Never ask which number they wanted "
    "when the sentence already contains one in any form.\n"
    "<<jarvis:volume V>> where V is up, down, mute, or a number 0 to 100.\n"
    "<<jarvis:brightness V>> where V is up, down, or a number 0 to 100.\n"
    "<<jarvis:theme NAME>> to change the colour theme.{themes}\n"
    "<<jarvis:media V>> where V is play, pause, playpause, next or previous, "
    "for whatever is playing music or video. Use playpause when they just "
    "say to pause or resume without naming which.\n"
    "Say briefly in your reply what you changed. If asked for anything else "
    "about the machine, say out loud that you cannot."
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

# The agent CLI's working directory. Deliberately NOT $HOME: a relative path
# an agent tries to touch should land in an empty private directory rather
# than among the user's files. The shipped Claude preset has no tools and so
# cannot use it either way, but a user-written preset may, and cwd is a free
# amplifier to give away. 0700 and under XDG_RUNTIME_DIR where available.
AGENT_CWD = os.path.join(STATE_DIR, "agent-cwd")

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
        # Separate from actions on purpose. Changing the volume is a different
        # decision from launching applications, and someone should be able to
        # grant one without the other. Off unless the config says otherwise,
        # like every other grant here.
        self.desktop = bool(spec.get("desktop", False))

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
        if not grants_tools(self.command):
            parts.append(NO_TOOLS_PROMPT)
        if self.actions:
            parts.append(ACTIONS_PROMPT)
        if self.desktop:
            themes = installed_themes()
            parts.append(DESKTOP_PROMPT.format(
                themes=(" Installed themes: " + ", ".join(themes) + "."
                        if themes else "")))
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


def agent_cwd():
    """An empty private directory to run the agent CLI in, falling back to /.

    Never $HOME. If the directory cannot be made, / is still a better cwd than
    the user's files, and the call proceeds rather than failing the reply.
    """
    try:
        os.makedirs(AGENT_CWD, mode=0o700, exist_ok=True)
        return AGENT_CWD
    except OSError as exc:
        log(f"could not create {AGENT_CWD} ({exc}); running the agent in /")
        return "/"


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


def grants_tools(argv):
    """True if this argv hands the agent CLI tools of its own.

    `--tools ""` is how the shipped invocation *removes* the built-in set, so
    a --tools whose value is empty is a denial, not a grant; anything else
    after it names tools to keep. --allowedTools adds to whatever is already
    there, so it is always a grant.
    """
    for i, part in enumerate(argv):
        if "--dangerously" in part or "--allowedTools" in part \
                or "--allowed-tools" in part:
            return True
        if part == "--tools":
            return bool(argv[i + 1].strip()) if i + 1 < len(argv) else False
        if part.startswith("--tools="):
            return bool(part.split("=", 1)[1].strip())
    return False


# Agent CLIs whose flag vocabulary this daemon actually knows. Recognising a
# *denial* is not the same problem as recognising a grant: to say an
# invocation is tool-free we have to know which flag removes the tools and
# what its absence implies, and that is per-CLI knowledge. We have it for
# Claude Code and nothing else.
KNOWN_CLIS = ("claude",)

TOOLS_DENIED = "denied"      # verified tool-free
TOOLS_GRANTED = "granted"    # verified to hand the CLI tools
TOOLS_UNKNOWN = "unknown"    # we cannot tell, so we must not claim


def tool_posture(executable, argv):
    """What we can honestly say about the tools this invocation exposes.

    The trap this exists to close: `actions` says whether *Jarvis* will act on
    a <<jarvis:...>> directive. It says nothing about whether the agent CLI
    has tools of its own. Reporting "answer-only" off `actions` alone once let
    a `codex exec -s read-only` preset -- a live shell over $HOME -- describe
    itself as answer-only in the panel, the journal and --check. An unknown
    CLI is not a safe CLI, it is an unaudited one, and the label has to say so.
    """
    if grants_tools(argv):
        return TOOLS_GRANTED
    if os.path.basename(executable) not in KNOWN_CLIS:
        return TOOLS_UNKNOWN
    # Claude Code: tool-free requires *both* the empty built-in allowlist and
    # a strict MCP config with nothing to load, or the user's own MCP servers
    # come back. Bare `claude -p` is not answer-only.
    empty_tools = any(
        (part == "--tools" and i + 1 < len(argv) and not argv[i + 1].strip())
        or (part.startswith("--tools=") and not part.split("=", 1)[1].strip())
        for i, part in enumerate(argv)
    )
    strict_mcp = "--strict-mcp-config" in argv and not any(
        part == "--mcp-config" or part.startswith("--mcp-config=")
        for part in argv
    )
    return TOOLS_DENIED if (empty_tools and strict_mcp) else TOOLS_UNKNOWN


def capability_label(agent):
    """One phrase for the panel, the journal and --check, and never a lie."""
    posture = tool_posture(agent.executable, agent.command)
    if posture == TOOLS_GRANTED:
        return "CLI tools granted"
    if posture == TOOLS_UNKNOWN:
        return "tools not verified"
    # Both grants get named, and "answer-only" survives only when neither is
    # on. A label that quietly omits a grant is the same defect as one that
    # claims a safety property the argv does not have.
    granted = []
    if agent.actions:
        granted.append("can act")
    if agent.desktop:
        granted.append("desktop controls")
    return ", ".join(granted) if granted else "answer-only"


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
    # Both argv templates, not just the first: web_command carries a search
    # query derived from the same transcript, and argv is argv.
    for label, template in (("command", agent.command),
                            ("web_command", agent.web_command or [])):
        if any("{prompt}" in part for part in template):
            log(f"warning: agent '{agent.name}' puts the transcript in argv "
                f"via `{label}`, where every local process can read it; drop "
                f"{{prompt}} from `{label}` to send it on stdin instead")
    # Actions are brokered by this daemon, never by a tool grant to the CLI.
    # A command that hands the agent tools anyway isn't something we can
    # police -- it's the user's argv -- but it deserves a loud note. An
    # *empty* --tools is the opposite of a grant, so it doesn't count.
    if grants_tools(agent.command):
        log(f"warning: agent '{agent.name}' grants the CLI tools in `command`. "
            "Jarvis never needs that: actions go through the jarvis-open "
            "broker, and a search grant belongs in `web_command`. Remove the "
            "tool flags unless you accept the risk.")
    elif tool_posture(agent.executable, agent.command) == TOOLS_UNKNOWN:
        # Not an accusation, an admission: we do not know this CLI's flags, so
        # we cannot tell a text box from a shell. Saying nothing here is what
        # let a read-only Codex sandbox pass itself off as answer-only.
        log(f"warning: agent '{agent.name}' runs '{agent.executable}', whose "
            "tool flags Jarvis does not know, so it CANNOT confirm this "
            "invocation is answer-only. A sandbox flag is not a tool denial: "
            "some CLIs still read every file you can. Verify it yourself -- "
            "put a known string in a file, then run `jarvis-listen --ask "
            "\"read <that file> and tell me what it says\"`. If the string "
            "comes back, this agent can read your home directory.")
    # The web invocation reads the open internet, so what it may hold matters
    # more, not less: WebFetch or a shell there hands a hostile page an
    # exfiltration channel. WebSearch alone is the sanctioned grant.
    if any("WebFetch" in part or "Bash" in part or "--dangerously" in part
           for part in agent.web_command or []):
        log(f"warning: agent '{agent.name}' grants `web_command` more than "
            "web search. A fetch tool or a shell in the web-enabled call "
            "lets a hostile page exfiltrate or act; grant WebSearch only.")
    caps = capability_label(agent)
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
    """Loudness of one frame, with any DC offset removed first.

    Some microphones do not sit centred on zero. The ASUS ProArt PX13's
    internal mic rests around -2800, and a plain RMS of that reads ~2800 in
    a silent room, which is louder than most speech ever registers. Every
    frame then looks like talking: the ambient floor never falls, nothing
    is ever quiet, and the recorder runs until max_command on every single
    question instead of stopping when you stop.

    Subtracting the frame mean removes the offset. It is a high pass at
    about 12Hz for an 80ms frame, well below anything in a voice, so it
    costs nothing on a microphone that was already centred.
    """
    centred = samples.astype(np.float32)
    centred -= centred.mean()
    return float(np.sqrt(np.mean(centred ** 2)))


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
    """Record until the speaker stops. Returns int16 samples, or None.

    Two thresholds, not one. Speech is not uniformly loud: unvoiced
    consonants and the tail end of a word routinely fall below the level
    that started the sentence, and a single comparison scores those the
    same as an empty room, which is how a listener ends up thinking while
    you are still talking. Starting to speak has to clear `onset`, but
    staying in the sentence only has to clear `sustain`, so the quiet parts
    of a question stay inside it.

    The floor also keeps adapting while we record. It used to be frozen at
    whatever the room measured when the wake word fired, so one noisy
    instant just before you spoke set the bar too high for everything after
    it. Quiet frames pull it down quickly and push it up slowly, so a long
    question cannot drag the bar up behind itself and cut off its own end.
    """
    floor = max(ambient, 1.0)
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

        level = rms(samples)
        if level < floor:
            # Downward on every frame, including ones we are calling speech.
            # A stale room level, left over from a noisy moment before the
            # wake word, otherwise holds the bar above the speaker for the
            # whole question and nothing ever counts as talking.
            floor = floor * 0.9 + level * 0.1
        onset, sustain = thresholds(floor)

        if level > (sustain if speech_time else onset):
            speech_time += frame_secs
            silence_time = 0.0
            continue

        silence_time += frame_secs
        floor = floor * 0.995 + level * 0.005
        if speech_time >= listen["min_speech"] and silence_time >= listen["silence_tail"]:
            break

    if speech_time < listen["min_speech"]:
        return None
    if elapsed >= listen["max_command"] and silence_time < listen["silence_tail"]:
        # Truncated mid-sentence. Worth a line, because the symptom reaching
        # the user is a half-question answered oddly, with nothing to explain
        # it, and max_command is a setting they can raise.
        log(f"hit the {listen['max_command']:.0f}s ceiling while you were "
            f"still talking; raise max_command if questions get cut off")
    return np.concatenate(frames)


def thresholds(floor):
    """Levels that separate talking from not, for a room measured at `floor`.

    Both are multiples of the room, never fixed levels, because the numbers a
    microphone reports are not a physical unit: they scale with input gain,
    with the hardware, and with the driver. The same voice in the same room
    measured 326 at 50% input volume and about three times that at 70%. A
    fixed bar of 300 is six times the room on one setting and sixteen on
    another, and on a quieter mic it is simply unreachable, so someone speaks
    normally and is never heard while their signal is perfectly good.

    Onset is deliberately well above sustain. Starting a sentence should take
    a clear voice; staying in one should survive the quiet parts of a word.
    The small absolute guards only matter when the floor is near zero, where
    a pure multiple would make every rustle count as speech and the question
    would never end.
    """
    return max(floor * 5.0, 40.0), max(floor * 2.0, 20.0)


def sample_mic(seconds):
    """Collect `seconds` of frames from the mic. Returns a list of frames.

    Opens its own stream and closes it again, so it is safe to run while
    nothing else holds the microphone. Returns whatever it got if the stream
    dies early, and an empty list if it never produced anything.
    """
    mic = open_mic()
    frames = []
    want = int(seconds * RATE / CHUNK_SAMPLES)
    try:
        # A stream that just opened has not settled: the first frames can come
        # back constant, which reads as a silent mic and skews both the offset
        # and the floor. The daemon never sees this because it holds one
        # stream open for as long as it is armed.
        for _ in range(int(0.5 * RATE / CHUNK_SAMPLES)):
            if read_chunk(mic) is None:
                break
        for _ in range(want):
            samples = read_chunk(mic)
            if samples is None:
                break
            frames.append(samples)
    finally:
        mic.kill()
    return frames


def describe_mic(frames):
    """Turn raw frames into the numbers that decide whether speech is heard."""
    if not frames:
        return None
    block = np.concatenate(frames).astype(np.float32)
    levels = np.array([rms(f) for f in frames])
    return {
        "seconds": len(block) / RATE,
        "offset": float(block.mean()),
        "peak": float(np.abs(block).max()),
        "floor": float(np.median(levels)),
        "quietest": float(levels.min()),
        "loudest": float(levels.max()),
        "clipped": float(np.mean(np.abs(block) > 32000)),
    }


def report_mic(quiet, spoken=None):
    """Print a verdict on the microphone. Returns True if it looks usable.

    Everything here is about one question: does this person's voice clear the
    bar that the wake word and the endpointer both work from? A mic can be
    present, unmuted and still fail that, and the failure is invisible from
    the outside: the daemon simply never hears anyone.
    """
    if quiet is None:
        print("FAIL     the microphone produced no audio at all")
        print("         check that pw-record works and a source is selected:")
        print("         pactl info | grep 'Default Source'")
        return False

    onset, sustain = thresholds(quiet["floor"])
    print(f"ok       captured {quiet['seconds']:.1f}s, "
          f"room level {quiet['floor']:.0f}, speech needs about {onset:.0f}")

    ok = True
    # A mic resting off zero reads loud in a silent room to anything that does
    # not centre it first. Jarvis does, so this is a note and not a failure.
    if abs(quiet["offset"]) > 500:
        print(f"note     this mic rests at a DC offset of {quiet['offset']:.0f} "
              f"rather than 0, which is handled")
    # A working mic in a treated room still reads tens of units of noise. Only
    # digital silence, every sample identical, means nothing is arriving.
    if quiet["loudest"] < 2.0:
        print("FAIL     the microphone is producing silence, not quiet room tone")
        print("         it is probably muted or its input volume is at zero")
        ok = False
    if quiet["clipped"] > 0.01:
        print("warn     the input is clipping with nobody talking; turn the gain down")

    if spoken is None:
        return ok

    if spoken["loudest"] < onset:
        print(f"FAIL     while talking it only reached {spoken['loudest']:.0f}, "
              f"under the {onset:.0f} needed")
        print("         turn the input volume up, or move closer to the mic")
        return False

    # How far the voice sits above the room is the whole verdict, and it is the
    # only part of this that means anything on hardware we have never seen. A
    # loud room with a proportionally loud voice is fine; a quiet room with a
    # voice barely above it is not, at any absolute level. Staying inside a
    # sentence takes 2x the room, so a voice peaking at only a few times the
    # room leaves the gaps between words above that line and the question runs
    # to max_command instead of ending. Clearing onset already takes 5x, so
    # only the band between 5x and 8x can reach this warning.
    margin = spoken["loudest"] / max(quiet["floor"], 1.0)
    print(f"ok       while talking it reached {spoken['loudest']:.0f}, "
          f"{margin:.0f}x the room")
    if margin < 8.0:
        print("warn     that is a narrow margin over the room noise, so it may "
              "not notice when you stop talking")
        print("         a headset mic, or more input volume, widens it")
    if spoken["clipped"] > 0.02:
        print("warn     your voice is clipping the input; turn the gain down")
    return ok


# Recording starts the instant the wake word lands, so the first word begins
# at sample zero with no run-up. Whisper transcribes that badly: it routinely
# drops or mangles a word with no silence in front of it, which is why "switch
# to workspace two" came back as "to workspace too". A short lead-in of
# silence costs a quarter second of file and gives the model somewhere to
# start.
LEAD_IN_SECONDS = 0.25


def write_wav(samples, path):
    # Centre it for the same reason rms() does. A mic resting at -2800 spends
    # 9% of its headroom on an offset the transcriber has no use for.
    samples = samples.astype(np.float32)
    samples -= samples.mean()
    samples = np.clip(samples, -32768, 32767).astype(np.int16)
    samples = np.concatenate(
        [np.zeros(int(LEAD_IN_SECONDS * RATE), dtype=np.int16), samples])
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
# Pipes are drained and capped below, but a child writing to a *file* -- the
# agent's reply file, piper's wav -- is writing past us, and a runaway one
# would keep going until its timeout or until the disk filled. RLIMIT_FSIZE
# is the ceiling the kernel enforces on our behalf: the child dies on SIGXFSZ
# instead. Both values are deliberately generous, because that rlimit applies
# to every file the child writes and not only the one we asked for -- an agent
# CLI also writes its own session and cache files, and killing it over one of
# those would be a bug we shipped for no gain. Read them as "cannot fill the
# disk", not as a tight fit: the reply we actually keep is capped again at
# read-back by safefile, and a capped reply synthesises to about two minutes
# of audio.
MAX_REPLY_FILE_BYTES = 64 << 20   # agent {outfile}: text, read back capped
MAX_WAV_BYTES = 64 << 20          # piper -f: ~2 min of 22 kHz 16-bit mono

BoundedRun = collections.namedtuple(
    "BoundedRun", "returncode stdout stderr overflowed")


def _fsize_limiter(limit):
    """A preexec_fn that caps what the child may write to any single file.

    Deliberately one syscall and nothing else: this runs between fork and
    exec in a process that inherited our threads' locks, so anything that
    could allocate or take a lock would risk wedging the child there.
    """
    def apply():
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    return apply


def run_bounded(argv, *, timeout, stdout_limit=MAX_CAPTURE_BYTES,
                stderr_limit=MAX_ERR_BYTES, input_text=None, cwd=None,
                file_limit=None):
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

    `file_limit` caps, via RLIMIT_FSIZE, what the child may write to any file
    it opens -- the stream ceilings above say nothing about those. A child
    that exceeds it dies on SIGXFSZ, which arrives here as a non-zero
    returncode.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        preexec_fn=_fsize_limiter(file_limit) if file_limit else None,
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
                               input_text=stdin_payload, cwd=agent_cwd(),
                               file_limit=MAX_REPLY_FILE_BYTES)
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
# agent asks for an action by ending its reply with directive lines; the
# daemon parses each against a strict pattern, validates the argument again
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
    r"^\s*<<jarvis:(open-app|open-url|open-bookmark|search"
    r"|workspace|theme|volume|brightness|media)\s+"
    r"([^<>\n]{1,2048}?)\s*>>\s*$")
_DIRECTIVE_KINDS = {"open-app": "app", "open-url": "url",
                    "open-bookmark": "bookmark", "search": "search",
                    "workspace": "workspace", "theme": "theme",
                    "volume": "volume", "brightness": "brightness",
                    "media": "media"}
# The ones the broker treats as desktop controls rather than as opening
# something. Gated by `desktop`, not by `actions`.
DESKTOP_KINDS = ("workspace", "theme", "volume", "brightness", "media")
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


# Asking for music and a browser at once is one request, and answering it with
# "I can only do one thing at a time, ask me again" is a worse assistant than
# the machinery requires. Several directives are no more authority than one:
# each is validated on its own and brokered through the same argv, so this is
# a repeat of an allowed action, not a wider one. The cap keeps a confused or
# hostile reply from turning one sentence into an unbounded run of launches,
# and duplicates collapse so "open the browser" twice is one window.
MAX_DIRECTIVES = 3


_THEMES_TTL_SECONDS = 60.0
_themes_cache = {"at": 0.0, "names": []}


def installed_themes():
    """Theme names for the desktop prompt, from the broker's `themes`.

    Unlike the bookmark list, this is safe to put in a prompt: theme names are
    published by Omarchy, not a record of anything the speaker did. Naming
    them is what makes "make it look like gruvbox" land on the right one.
    """
    now = time.monotonic()
    if _themes_cache["names"] and now - _themes_cache["at"] < _THEMES_TTL_SECONDS:
        return _themes_cache["names"]
    broker = jarvis_open_path()
    if broker is None:
        return []
    try:
        proc = run_bounded([broker, "themes"], timeout=10,
                           stdout_limit=64 << 10, stderr_limit=16 << 10)
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
        if total > 2000:
            break
        names.append(name)
    _themes_cache["at"] = now
    _themes_cache["names"] = names
    return names


def extract_directives(reply):
    """Split a reply into (spoken_text, [directive, ...]).

    Directive lines are stripped from the spoken text whether or not actions
    are enabled -- an ignored directive should not be read aloud either.
    Order is the order the agent asked for, identical requests collapse, and
    anything past MAX_DIRECTIVES is dropped rather than run.
    """
    directives = []
    kept = []
    for line in reply.splitlines():
        match = DIRECTIVE_RE.match(line)
        if match:
            directive = (_DIRECTIVE_KINDS[match.group(1)],
                         match.group(2).strip())
            if directive not in directives and len(directives) < MAX_DIRECTIVES:
                directives.append(directive)
            continue
        kept.append(line)
    return "\n".join(kept).strip(), directives


def run_directives(directives):
    """Run each directive in order. Returns (opened, failed) counts.

    One failure does not abandon the rest: if the music player opens and the
    browser does not, the music should still be playing.
    """
    opened = 0
    for directive in directives:
        if run_directive(directive):
            opened += 1
    return opened, len(directives) - opened


def run_directive(directive):
    """Validate one directive and exec the broker for it. True on success."""
    kind, value = directive
    # A bookmark query is held to the same shape as an app name: it is a
    # word or two someone said, and the broker only ever matches it against
    # titles it read itself.
    if kind in ("app", "bookmark") + DESKTOP_KINDS \
            and not APP_QUERY_RE.match(value):
        log(f"directive refused: {kind} name failed validation")
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
        # side door. Audit the destination's origin, never the full URL --
        # and build that origin from hostname/port rather than netloc, which
        # would carry any `user:password@` straight into the journal we are
        # trying to keep secrets out of.
        origin = urllib.parse.urlsplit(value)
        try:
            host, port = origin.hostname or "", origin.port
        except ValueError:      # a malformed port; the host is still the fact
            host, port = origin.hostname or "", None
        where = f"{origin.scheme}://{host}" + (f":{port}" if port else "")
        log(f"jarvis-open: opened {where} (full url not journaled)")
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
    reply, stray = extract_directives(ask_agent(agent, query, web=True))
    if stray:
        # Kinds only. The values came off the web, and the journal is not the
        # place to learn what a page asked us to open.
        log(f"{len(stray)} directive(s) in a web-tainted reply ignored "
            f"({', '.join(sorted({kind for kind, _ in stray}))})")
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
            proc = run_bounded(
                [VENV_PY, "-m", "piper", "-m", voice, "-f", out],
                input_text=text, timeout=180,
                stdout_limit=64 << 10, stderr_limit=64 << 10,
                file_limit=MAX_WAV_BYTES,
            )
        except subprocess.TimeoutExpired:
            log("speech synthesis timed out")
            return
        # A piper failure still leaves a bare 44-byte wav header behind.
        # The capped reply synthesises to at most a couple of minutes of
        # audio, so a playback still running at five is a wedged audio
        # server holding the listener hostage, not a long answer.
        size = os.path.getsize(out)
        if size <= 44:
            # Nothing but a header. Silence is the worst possible report: from
            # outside it is indistinguishable from a voice that simply does
            # not speak, and someone will change voices, restart the service
            # and doubt their speakers before suspecting this.
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            log(f"speech synthesis produced no audio with "
                f"{os.path.basename(voice)}"
                + (f": {detail[-1][:200]}" if detail else ""))
            return
        try:
            played = subprocess.run(["pw-play", out], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, check=False,
                                    timeout=300)
        except subprocess.TimeoutExpired:
            log("playback timed out; is the audio server healthy?")
            return
        if played.returncode != 0:
            reason = (played.stderr or b"").decode("utf-8", "replace").strip()
            log(f"playback failed (exit {played.returncode})"
                + (f": {reason.splitlines()[-1][:200]}" if reason else ""))
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
    answer, directives = extract_directives(ask_agent(agent, text))
    answer = answer[:MAX_SPOKEN_CHARS]
    # A search is a hand-off, not an action, and the prompt already forbids
    # combining it with an open. If one appears anyway it wins alone, because
    # the round it hands to is the one whose output may not be trusted to act.
    search = next((d for d in directives if d[0] == "search"), None)
    if search:
        answer = run_search(agent, search[1], log_text)
        directives = []
    desk = [d for d in directives if d[0] in DESKTOP_KINDS]
    opens = [d for d in directives if d[0] not in DESKTOP_KINDS]
    if desk:
        if agent.desktop:
            changed, failed = run_directives(desk)
            if changed and not failed and not answer:
                answer = "Done."
            elif failed:
                answer = (answer + " Sorry, I could not change that.").strip()
        else:
            log(f"agent sent {len(desk)} desktop directive(s) but desktop "
                f"controls are off; ignored")
            if not answer:
                answer = "Sorry, changing the desktop is turned off."
    if opens:
        if agent.actions:
            opened, failed = run_directives(opens)
            if not failed and not answer:
                answer = "Opening it now." if opened == 1 else "Opening them now."
            elif failed and opened:
                answer = (answer + " Sorry, part of that did not open.").strip()
            elif failed:
                answer = (answer + (" Sorry, that did not open." if failed == 1
                                    else " Sorry, those did not open.")).strip()
        else:
            log(f"agent sent {len(opens)} open directive(s) but actions "
                f"are off; ignored")
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

    # Seeded from the room itself on the first frame, not from a constant. A
    # fixed starting value is a guess about someone else's microphone: too
    # high and the bar sits above their voice until it decays, which is the
    # first minute after every restart, and that is exactly when someone is
    # standing there testing whether it works.
    ambient = None
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
            # Rolling floor, ignoring anything loud enough to be a voice.
            # Falls fast and rises slowly: a room that goes quiet should be
            # believed within a second, while a room that gets loud should
            # not drag the bar up over someone mid-sentence.
            if ambient is None:
                ambient = level
            elif level < ambient * 2:
                ambient = (ambient * 0.9 + level * 0.1) if level < ambient \
                    else (ambient * 0.995 + level * 0.005)

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
    parser.add_argument("--mic", action="store_true",
                        help="test the microphone, listening while you speak, then exit")
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
            kind = capability_label(agent)
            if agent.web:
                kind += " +web"
            print(f"{mark} {name:12} {found:15} {kind}")
        return 0

    agent = select_agent(cfg)
    voice = resolve_voice(cfg)
    wake_path, wake_key = resolve_wake_model(cfg)
    log_text = bool(cfg.get("log_transcripts", False))

    if args.mic:
        # Two phases, because one reading cannot tell a dead mic from a quiet
        # room. The room sets the bar; the voice has to clear it.
        # flush, because these are instructions and they are worth nothing
        # after the thing they were instructing. Python block-buffers stdout
        # when it is a pipe rather than a terminal, so unflushed prompts all
        # arrive at exit: run from a terminal this looks fine, run from the
        # panel it tells you to speak once the recording is already over.
        print("Say nothing for 3 seconds...", flush=True)
        quiet = describe_mic(sample_mic(3.0))
        print("Now say something, out loud, for 5 seconds...", flush=True)
        spoken = describe_mic(sample_mic(5.0))
        print()
        return 0 if report_mic(quiet, spoken) else 1

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
        # A mic that is muted or unselected passes every check above and then
        # never hears anyone, which is the hardest failure to work out from
        # the outside. Three seconds of room tone is enough to catch it.
        ok &= report_mic(describe_mic(sample_mic(3.0)))
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
