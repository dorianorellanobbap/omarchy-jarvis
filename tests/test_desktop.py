"""Desktop control tests.

These four controls are the only things Jarvis can change about the machine
itself, so what a value is allowed to be is the whole security boundary. Each
check here is a value that must not become part of a command.

Nothing is executed: run_desktop returns the argument list it *would* run.

  python3 tests/test_desktop.py
"""
import importlib.machinery
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(os.path.dirname(HERE), "daemon")
sys.path.insert(0, DAEMON)
spec = importlib.util.spec_from_loader(
    "jarvis_open",
    importlib.machinery.SourceFileLoader("jarvis_open",
                                         os.path.join(DAEMON, "jarvis-open")))
jo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jo)

results = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got:    {got!r}\n        wanted: {want!r}")
    results.append(ok)


# Every accepted value lands in one slot of a fixed argument list.
# Two forms are offered because Hyprland changed how dispatch parses its
# arguments; the broker tries them in order and checks the exit code.
check("a workspace offers both hyprctl dispatch forms",
      jo.run_desktop("workspace", "3"),
      ([["hyprctl", "dispatch", 'hl.dsp.focus({workspace="3"})'],
        ["hyprctl", "dispatch", "workspace", "3"]], "workspace 3"))
check("volume up is a named action, not a number",
      jo.run_desktop("volume", "up"),
      ([["omarchy-audio-output-volume", "raise"]], "volume up"))
check("mute toggles",
      jo.run_desktop("volume", "mute"),
      ([["omarchy-audio-output-volume", "mute-toggle"]], "mute toggled"))
check("an absolute volume goes through wpctl",
      jo.run_desktop("volume", "40"),
      ([["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "40%"]], "volume 40%"))
check("brightness down is a relative step",
      jo.run_desktop("brightness", "down"),
      ([["omarchy-brightness-display", "10%-"]], "brightness down"))
check("an absolute brightness is a percentage",
      jo.run_desktop("brightness", "55"),
      ([["omarchy-brightness-display", "55%"]], "brightness 55%"))

# Anything that is not an expected value is refused rather than passed on.
# None of these can reach a shell even if they were accepted, because every
# path is an argument list, but they do not get that far.
check("a shell fragment in a workspace is refused",
      jo.run_desktop("workspace", "3; reboot"), None)
check("a substituted command is refused",
      jo.run_desktop("volume", "$(id)"), None)
check("a flag-shaped value is refused",
      jo.run_desktop("brightness", "--help"), None)
check("workspace 0 is refused", jo.run_desktop("workspace", "0"), None)
check("workspace 11 is refused", jo.run_desktop("workspace", "11"), None)
check("volume over 100 is refused", jo.run_desktop("volume", "101"), None)
check("a volume that is a word is refused", jo.run_desktop("volume", "loud"), None)
check("an unknown control is refused", jo.run_desktop("reboot", "now"), None)

# A theme is resolved against the themes actually installed, so the value
# handed to omarchy-theme-set is always a name this script read itself.
installed = jo.theme_names()
if installed:
    partial = installed[0].split()[0][:4].lower()
    hit = jo.run_desktop("theme", partial)
    ok = hit is not None and hit[0][0][0] == "omarchy-theme-set" \
        and hit[0][0][1] in installed
    print(f"  {'PASS' if ok else 'FAIL'}  a partial theme name resolves to an "
          f"installed one")
    results.append(ok)
else:
    print("  SKIP  no themes installed to resolve against")
check("a theme that is not installed is refused",
      jo.run_desktop("theme", "hacker green"), None)

# Media is MPRIS over the session bus, so the only thing to pin down here is
# which words are accepted. The bus names are read off the bus, never taken
# from anything spoken.
check("an unknown media action is refused", jo.run_media("eject"), False)
ok = sorted(jo.MEDIA_METHODS) == ["next", "pause", "play", "playpause", "previous"]
print(f"  {'PASS' if ok else 'FAIL'}  media accepts only the five transport words")
results.append(ok)
ok = all(jo.BUS_NAME_RE.match(n) for n in ["org.mpris.MediaPlayer2.cliamp"]) \
    and not jo.BUS_NAME_RE.match("org.mpris.MediaPlayer2.x;reboot")
print(f"  {'PASS' if ok else 'FAIL'}  a bus name with a shell fragment is rejected")
results.append(ok)

print()
if all(results):
    print("all desktop tests passed")
else:
    print("FAILURES")
    sys.exit(1)
