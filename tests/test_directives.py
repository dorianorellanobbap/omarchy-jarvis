"""Directive parsing tests.

What the agent writes at the end of a reply is the only thing that can make
Jarvis act, so what counts as a directive, and what does not, is a boundary
worth pinning down. Nothing here runs the broker: these are string tests.

  python3 tests/test_directives.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(os.path.dirname(HERE), "daemon")
sys.path.insert(0, DAEMON)
spec = importlib.util.spec_from_file_location(
    "jarvis_listen", os.path.join(DAEMON, "jarvis-listen.py"))
jl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jl)

results = []


def case(name, reply, want_text, want_directives):
    text, directives = jl.extract_directives(reply)
    ok = text == want_text and directives == want_directives
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        text:       {text!r}\n        wanted:     {want_text!r}")
        print(f"        directives: {directives}\n        wanted:     {want_directives}")
    results.append(ok)


# The thing Dorian asked for: music and a browser in one sentence.
case("two things at once",
     "Opening your music and a browser.\n"
     "<<jarvis:open-app spotify>>\n"
     "<<jarvis:open-url https://example.com>>",
     "Opening your music and a browser.",
     [("app", "spotify"), ("url", "https://example.com")])

# Order is the order asked for, not sorted or reversed.
case("order is preserved",
     "<<jarvis:open-app b>>\n<<jarvis:open-app a>>",
     "", [("app", "b"), ("app", "a")])

# Saying the same thing twice should not open two windows.
case("duplicates collapse",
     "<<jarvis:open-app firefox>>\n<<jarvis:open-app firefox>>",
     "", [("app", "firefox")])

# A confused or hostile reply cannot turn one sentence into a launch storm.
case("more than the cap is dropped",
     "\n".join(f"<<jarvis:open-app app{i}>>" for i in range(10)),
     "", [("app", f"app{i}") for i in range(jl.MAX_DIRECTIVES)])

# One is still one.
case("a single directive still works",
     "Here you go.\n<<jarvis:open-app kitty>>",
     "Here you go.", [("app", "kitty")])

# Nothing to do.
case("a plain answer has no directives",
     "It is about twenty degrees out.", "It is about twenty degrees out.", [])

# Not a directive: prose that mentions one, or a line with trailing text.
case("a mention in prose is not a directive",
     "You would write <<jarvis:open-app foo>> to do that, but I will not.",
     "You would write <<jarvis:open-app foo>> to do that, but I will not.", [])

# Directive lines are stripped from speech even when they will be ignored,
# so a refusal is never read aloud as markup.
case("directives never reach the spoken text",
     "Sure.\n<<jarvis:open-url https://example.com/a?b=c>>\nAll set.",
     "Sure.\nAll set.", [("url", "https://example.com/a?b=c")])

# Desktop controls parse as their own kinds, so respond() can gate them on a
# different flag from the ones that open things.
case("desktop controls parse",
     "Switching over.\n<<jarvis:workspace 3>>\n<<jarvis:volume mute>>",
     "Switching over.", [("workspace", "3"), ("volume", "mute")])

case("a theme name with spaces survives",
     "<<jarvis:theme Catppuccin Latte>>", "", [("theme", "Catppuccin Latte")])

# Exactly what the desktop grant covers. Pinned deliberately: adding a kind
# here widens what one switch turns on, so it should be a decision rather
# than something that happens quietly.
ok = sorted(jl.DESKTOP_KINDS) == ["brightness", "media", "theme", "volume",
                                  "workspace"]
print(f"  {'PASS' if ok else 'FAIL'}  the desktop grant covers exactly five controls")
results.append(ok)

case("media parses as a desktop kind",
     "<<jarvis:media playpause>>", "", [("media", "playpause")])

print()
if all(results):
    print("all directive tests passed")
else:
    print("FAILURES")
    sys.exit(1)
