# Patch notes

Each version names the commit the marketplace verified it at, because a
marketplace snapshot is pinned to an exact commit while `omarchy plugin add`
clones whatever is at branch head. Those two can differ, and knowing which
you have starts with knowing what each version was.

## 1.1.0 (2026-09-24)

Twenty-three commits since the listed 1.0.0 snapshot. Two things changed that
you would notice: it acts on the desktop now instead of only answering, and it
hears people it used to miss.

### It can act, if you let it

- **Workspaces, themes, volume and brightness, spoken.** "Switch to workspace
  two", "turn it down", "use gruvbox". A separate grant from opening apps,
  off by default, because changing the volume is a different decision from
  launching things. Each value is checked against a fixed list or a number
  range, and each one is undone by saying the opposite. (`88cdca0`)
- **Those controls actually land, and say so when they do not.** Current
  Hyprland parses `hyprctl dispatch` as Lua, so the old form failed silently
  while Jarvis still said "switching to workspace two". Both forms are tried,
  the exit code decides, and a failure reaches the speaker. (`e4a6cbd`)
- **Play, pause, next, previous.** Spoken at whatever is playing, over MPRIS
  through `busctl`, so there is nothing extra to install. (`979e2d5`)
- **Switches in the panel for both grants.** Read back from the config on
  every load, so a switch shows what is granted rather than what was last
  clicked. The command Jarvis runs your agent with stays hand-edited, by
  design. (`979e2d5`)
- **Bookmarks by name.** "Open my bank" matches your Chromium bookmarks
  locally. The agent never sees the bookmark list. (`fd172f2`)
- **More than one thing per request.** "Open spotify and a browser" does
  both. (`c8b0ae8`)
- **You end up on the workspace you asked for.** Launching an app that is
  already running raises its existing window, and the compositor follows it to
  whatever workspace that window was on, which used to undo the switch you
  just asked for. The workspace is asked for again after the opens.
  (`a9be5ec`)

### It hears you

- **Speech is decided by a model, not a volume threshold.** A voice arriving
  at 200 in a room of 130 gave the old level test none of its speech frames,
  and no threshold fixes that: any line low enough to catch the speaker also
  catches the room. A local Silero voice-activity model judges the shape of
  the sound instead. 2.3MB, about a millisecond a frame, on the onnxruntime
  already there for the wake word. Measured on real speech mixed into a real
  recording of a real room: 78% of frames found at a 1.5x margin, against 0%
  for loudness. Levels remain as the fallback, so an install without the model
  still works. (`930c01a`)
- **It learns how loud you actually are.** No calibrate button, because a
  snapshot goes stale the moment you carry the laptop into a louder room. The
  bar is set from the voice it just heard, held under the quieter of your wake
  word and your last question, since people announce "hey jarvis" and then ask
  at a normal volume. On one machine the bar moved 338 to 180 for a 300 voice
  after four questions. (`afd8f14`, `f268676`)
- **Silence is judged by the room's loud end, not its quiet end**, so a louder
  room no longer means it listens forever. (`75110e9`)
- **It stops cutting people off mid-sentence.** (`3170277`)
- **It works on hardware whose microphone does not rest at zero.** Loudness is
  measured properly rather than reading a DC offset as signal, which on one
  laptop read 2818 in silence. (`809b6b8`)
- **Levels are measured against your own room** instead of assuming an input
  gain, so it behaves the same at 30% input volume as at 70%. (`efba984`)

### Set it up and diagnose it from the bar

- **One button installs it**, and another tests your microphone and tells you
  in plain words whether your voice clears the noise in your room. (`67ec10c`,
  `ef5a9dc`, `17a7127`)
- **A voice that produces no sound now says so.** A failed piper synthesis
  left a bare 44-byte wav and skipped playback silently, which is
  indistinguishable from working speakers you cannot hear. It names the voice
  and quotes piper's complaint. `pw-play` failures are logged too, instead of
  being sent to `/dev/null`. (`e4a04dd`)
- **The README leads with what you get** rather than with how it works.
  (`09faad5`)

### Security and correctness

- **Everything rendered in the panel is forced to plain text.** A QML `Text`
  with no `textFormat` guesses, and its guess treats a string that looks like
  markup as markup, so a tag in a helper's diagnostic could pull a remote
  resource. Every `Text` this plugin declares now says `Text.PlainText`. The
  strings handed to components this plugin does not own, an agent name and a
  voice id on their way to a dropdown label, are held to a shape that cannot
  carry a tag instead. Reported by a marketplace maintainer. (`9bd1138`)
- **Runtime breaks that a syntax check cannot see are now caught at the
  edit**: every constant the module reads must be one it defines, and every
  agent in the shipped example config must build and answer. Both had broken
  in one afternoon, and the daemon started cleanly each time. (`be11e1c`)
- **A company name and a private config key left the public repo.** The
  bookmark tests used a made-up bookmark named after the author's employer,
  and a comment named a config key that only exists on a private branch.
  (`df02665`)

### Housekeeping

- Preview image for the marketplace card, and the plugin is named **Jarvis**
  rather than "Voice Assistant". (`59e5cab`)
- The card description says what the plugin does now, including that it can
  act and that it needs no account, no API key and no per-minute bill. It had
  been the original 93-character sentence from August. (`9019529`)

## 1.0.0 (2026-09-12)

First marketplace listing, verified at `17c39b6`.

Say "hey jarvis", ask a question, hear the answer. openWakeWord listens on a
continuous 16kHz stream, voxtype's local whisper transcribes, piper speaks the
reply, and only the transcribed text ever leaves the machine. Which agent
answers is configuration rather than code. It can open apps and URLs when you
grant it, through a broker that takes a validated argument list and never a
shell.

Eight review rounds went into that listing. The ones worth knowing about:

- Every local file is read and written descriptor-first, so a path cannot be
  swapped between the check and the open. (`a0e33b2`)
- pip dependencies are hash-locked, and so are the panel's voice downloads.
  (`707982b`, `e702a25`, `592cbc6`, `7a1cc94`)
- `claude -p` with no tool flags is not answer-only: it keeps its built-in
  read tools and loads your own MCP config. The shipped presets pass
  `--tools ""` and `--strict-mcp-config`, which is the pair that actually
  denies. (`b2cb10c`)
- The Codex preset was removed rather than fixed. `codex exec -s read-only`
  sandboxes writes, not reads, so a spoken sentence still had a shell that
  could read every file you can, and Codex has no `--tools` equivalent to fix
  it with. (`0f1142c`)
- A capability label is only worth what the argv behind it does. The panel and
  the journal used to infer "answer-only" from a flag that governs Jarvis's
  own broker, not the agent CLI's tools, so an agent given a shell command
  could still be reported as answer-only. Now only a CLI verified to deny its
  tools earns that label, and everything else reads "tools not verified".
  (`17c39b6`)
