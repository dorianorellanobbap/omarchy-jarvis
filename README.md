# Jarvis — a wake-word voice assistant for the Omarchy bar

Say **"hey jarvis"**, ask a question, hear the answer. A bar widget arms and
disarms the listener and shows what it is doing.

Everything except the agent call runs on your machine: [openWakeWord] listens
on a continuous 16kHz mic stream, [voxtype]'s local whisper model transcribes,
[piper] speaks the reply. Only the transcribed *text* ever leaves the machine —
and only when you say the wake word.

Which agent answers is configuration, not code. Ships working presets for
**Claude Code** and **Codex**; adding another CLI is a few lines of TOML.

[openWakeWord]: https://github.com/dscripka/openWakeWord
[voxtype]: https://github.com/omarchy/voxtype
[piper]: https://github.com/rhasspy/piper

---

## ⚠️ Read this before installing

This puts an **always-on microphone daemon** on your machine. It holds the mic
open whenever it is armed, and when it hears the wake word it sends what you
said to whichever agent you configured — a cloud service, unless you point it
at a local model.

It is off until you arm it, and the widget shows when it is listening. But you
should be comfortable with that trade before installing. Omarchy plugins run
unsandboxed with your user's permissions.

With `actions = true` the agent can also **launch apps and open URLs**. That
goes through `jarvis-open`, a wrapper that can start an installed `.desktop`
entry or open an `http(s)` URL and nothing else — it is the entire blast
radius. Set `actions = false` and the agent can only talk.

---

## Install

```sh
omarchy plugin add https://github.com/<you>/omarchy-jarvis.git --enable
~/.config/omarchy/plugins/dorian.voice/install.sh
```

Or from a clone:

```sh
git clone https://github.com/<you>/omarchy-jarvis.git
cd omarchy-jarvis && ./install.sh
```

The script builds a Python venv, fetches and checksums the piper voice (63MB),
installs a systemd **user** unit, writes a starter config, and verifies the
result. It is idempotent — re-run it any time. Then add the **Voice Assistant**
widget to your bar and log out and back in.

**Requires:** Python 3.11+, `curl`, `jq`, PipeWire (`pw-record`/`pw-play`),
`voxtype` (ships with Omarchy), and the CLI of whichever agent you pick.

## Use

| Click | Does |
| --- | --- |
| Left | Open the settings panel |
| Right | Arm / disarm |
| Middle | Restart the listener |

Armed, it costs about 3% of one core. Say the wake word, wait for the chime,
then talk. The widget icon shows where it is: waiting, listening, thinking,
speaking.

```sh
J=~/.local/share/jarvis
$J/bin/jarvis-config show                                # settings as JSON
$J/venv/bin/python $J/jarvis-listen.py --agents          # what's configured
$J/venv/bin/python $J/jarvis-listen.py --check           # verify deps
$J/venv/bin/python $J/jarvis-listen.py --ask "hello"     # test without the mic
journalctl --user -u jarvis -f                           # watch it work
```

## Configure

The panel covers the everyday settings — agent, wake word, sensitivity, how
long a pause ends your question, how long a question may run. Changes are
written straight to the config file. The listener reads its config once at
startup, so the panel restarts it for you when it is armed; when it is
disarmed the change simply applies next time you arm it.

Everything else lives in `~/.config/jarvis/config.toml` — see
[`config/config.toml.example`](config/config.toml.example) for every option,
commented.

```toml
agent     = "claude"        # or "codex"
wake_word = "hey_jarvis"    # or alexa, hey_mycroft, hey_marvin
```

Hand-edit it freely: writes from the panel go through `jarvis-config`, which
rewrites a single line and leaves the rest of the file — comments included —
exactly as you wrote it. It also refuses to touch anything under `[agents.*]`,
so no click in the panel can change the command the daemon executes.

### Adding an agent

`command` is argv, never a shell string, so nothing you say can be interpreted
as shell. Three placeholders are substituted into individual arguments:

| Placeholder | Becomes |
| --- | --- |
| `{prompt}` | what you said, transcribed |
| `{system}` | the voice-style system prompt Jarvis builds |
| `{outfile}` | a temp file — if present, the reply is read from there instead of stdout |

```toml
[agents.mycli]
command = ["mycli", "--quiet", "{system}\n\n{prompt}"]
actions = false
```

Use `{outfile}` when the CLI prints progress logs to stdout, or
`strip_prefixes = ["INFO", "Loading"]` to drop noise lines. Then check it with
`--agents` and `--ask`. **PRs adding a working preset are welcome.**

`actions = true` only makes sense if the CLI can be told to permit exactly one
command. Claude Code can (`--allowedTools`); Codex's sandbox modes are
all-or-nothing, so its preset is answer-only.

## How it works

```
mic ──> openWakeWord ──> [wake word] ──> record until silence
                                              │
                                         voxtype (local whisper)
                                              │
                                          agent CLI
                                              │
                                       piper ──> speakers
```

Pipeline state is written to `$XDG_RUNTIME_DIR/jarvis/state`, so the widget
reads a file instead of talking to the process.

The settings panel is QML and the daemon's config is TOML, which QML cannot
parse. Rather than teach the widget about TOML — or move the settings somewhere
the daemon would need Omarchy to read them — the panel shells out to
`jarvis-config`, which prints JSON and writes single lines.

## Notes from building it

- An always-on mic forces AirPods into mono HFP. Pin your default source to
  the internal mic if that bites.
- `pw-record` exits early if its stderr is `DEVNULL` — it needs a real file.
- While the agent is thinking, nothing drains the mic pipe, so it holds stale
  audio including Jarvis's own reply. The stream is restarted after every
  exchange rather than replayed.

## Uninstall

```sh
./uninstall.sh          # keeps your config
./uninstall.sh --purge  # removes it too
```

## License

MIT — see [LICENSE](LICENSE).
