"""Wiring smoke tests.

Cheap checks for the class of break that only shows up at runtime, long after
the edit: a constant referenced but never defined, or an attribute the code
reads off an Agent that nothing ever sets. Neither is visible to a syntax
check, and the second only fires when someone actually speaks to it, so a
clean startup is not evidence that it works.

Also here: the two halves of keeping rendered text inert. A QML `Text` that
does not say `textFormat` guesses, and its guess is that something which
looks like markup is markup, which makes a `<img src=...>` in a helper's
stderr a network fetch. Nothing about that is visible until the day a string
arrives with a tag in it.

Also here: the ceilings on what the shell holds. The panel runs inside the
bar, which outlives it and is shared with every other plugin, so a file it
reads, a stream it buffers and a log it appends to all need a bound. None of
that is visible until the day something arrives that is too big.

  python3 tests/test_wiring.py
"""
import argparse
import ast
import builtins
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DAEMON = os.path.join(ROOT, "daemon")
sys.path.insert(0, DAEMON)
spec = importlib.util.spec_from_file_location(
    "jarvis_listen", os.path.join(DAEMON, "jarvis-listen.py"))
jl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jl)

results = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok and detail:
        print(f"        {detail}")
    results.append(ok)


# Every SHOUTING name the module reads is one the module defines. A missing
# constant is a NameError the first time that path runs, which for a rarely
# taken branch can be days after the edit that dropped it.
source = open(os.path.join(DAEMON, "jarvis-listen.py"), encoding="utf-8").read()
tree = ast.parse(source)
defined = set()
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        defined.add(node.name)
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                defined.add(target.id)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            defined.add((alias.asname or alias.name).split(".")[0])
known = defined | set(dir(builtins))
undefined = sorted({n.id for n in ast.walk(tree)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                    and n.id.isupper() and n.id not in known})
check("every module constant it reads is one it defines", not undefined,
      f"undefined: {undefined}")

# An Agent built from the shipped example answers everything the daemon asks
# of it, including the attributes only a reply touches.
example = os.path.join(ROOT, "config", "config.toml.example")
cfg = jl.load_config(example)
built, failures = 0, []
for name, agent_spec in (cfg.get("agents") or {}).items():
    try:
        agent = jl.Agent(name, agent_spec)
        agent.system_prompt
        jl.capability_label(agent)
        agent.build_invocation("hello", None)
        built += 1
    except Exception as exc:
        failures.append(f"{name}: {type(exc).__name__}: {exc}")
check(f"every agent in the example config builds and answers ({built} built)",
      built > 0 and not failures, "; ".join(failures))

# Every Text this plugin declares says so in plain words. Static labels get it
# too: the point is that no later edit can point an existing element at a
# helper's output and inherit rich text by default.
qml_missing = []
qml_checked = 0
for name in sorted(f for f in os.listdir(ROOT) if f.endswith(".qml")):
    qml_lines = open(os.path.join(ROOT, name), encoding="utf-8").read().split("\n")
    for i, line in enumerate(qml_lines):
        opening = re.match(r"^(\s*)Text \{\s*$", line)
        if not opening:
            continue
        qml_checked += 1
        indent = len(opening.group(1))
        body = []
        for later in qml_lines[i + 1:]:
            if later.strip() and (len(later) - len(later.lstrip())) <= indent:
                break
            body.append(later)
        if not any("textFormat: Text.PlainText" in b for b in body):
            qml_missing.append(f"{name}:{i + 1}")
check(f"every QML Text forces plain text ({qml_checked} checked)",
      qml_checked > 0 and not qml_missing,
      f"missing textFormat: {qml_missing}")

# The other half. An agent name and a voice id are the strings from outside
# that reach a dropdown label, and a dropdown is the shell's, so its
# textFormat is not ours to set. They are held to a shape instead.
# jarvis-config has no .py suffix, so it needs the loader named outright.
loader = importlib.machinery.SourceFileLoader(
    "jarvis_config", os.path.join(DAEMON, "jarvis-config"))
spec = importlib.util.spec_from_loader("jarvis_config", loader)
jc = importlib.util.module_from_spec(spec)
loader.exec_module(jc)

MARKUP = "<img src=x.png>"


class _Args:
    def __init__(self, config):
        self.config = config


with tempfile.TemporaryDirectory() as tmp:
    cfg_path = os.path.join(tmp, "config.toml")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write(open(example, encoding="utf-8").read())
        # `probe` carries the identical spec under a legal name, so if the
        # markup-named one is missing while probe is present, what dropped it
        # was the shape check and not a build failure.
        fh.write(f'\n[agents."{MARKUP}"]\n'
                 'command = ["true", "{prompt}"]\n'
                 '\n[agents.probe]\n'
                 'command = ["true", "{prompt}"]\n')

    held = io.StringIO()
    stdout, sys.stdout = sys.stdout, held
    try:
        jc.cmd_show(jl, _Args(cfg_path))
    finally:
        sys.stdout = stdout
    shown = [a["name"] for a in json.loads(held.getvalue())["agents"]]
    check("show never hands the panel an agent name shaped like markup",
          "probe" in shown and MARKUP not in shown, f"agents: {shown}")

    voices = os.path.join(tmp, "voices")
    os.makedirs(voices)
    for fname in ("en_US-amy-medium.onnx", MARKUP + ".onnx"):
        open(os.path.join(voices, fname), "w").close()
    real_dir, jc.voices_dir = jc.voices_dir, lambda: voices
    try:
        listed = jc.installed_voices()
    finally:
        jc.voices_dir = real_dir
    check("a voice file shaped like markup is not offered",
          listed == ["en_US-amy-medium.onnx"], f"listed: {listed}")

# The shell must not read files itself. A Quickshell FileView preloads by
# default, so merely pointing one at a path pulls the whole file into the bar
# before anything can check its size; `reload()` then does it again. The panel
# needs config.toml's identity, not its contents, and `stamp` supplies that.
qml_fileviews = []
for name in ("Panel.qml", "BarWidget.qml", "LogView.qml"):
    for i, line in enumerate(open(os.path.join(ROOT, name)).read().splitlines()):
        if re.match(r"^\s*FileView\s*\{", line):
            qml_fileviews.append(f"{name}:{i + 1}")
check("no shipped QML reads a file with FileView",
      not qml_fileviews, f"FileView at: {qml_fileviews}")

# A streamed log is appended to for as long as its process runs, and the mic
# test runs until someone stops it. appendLog keeps the tail and drops the
# head; a bare += has no ceiling at all.
unbounded = []
for name in ("Panel.qml", "BarWidget.qml", "LogView.qml"):
    for i, line in enumerate(open(os.path.join(ROOT, name)).read().splitlines()):
        if re.search(r"\b(setupLog|micLog)\s*\+=", line):
            unbounded.append(f"{name}:{i + 1}")
check("no log property is appended to without a ceiling",
      not unbounded, f"unbounded += at: {unbounded}")

# The property that matters about `stamp`: noticing a change must not depend
# on being willing to read the file. A config past the ceiling `show` enforces
# still has to stamp, and stamping must touch no bytes at all, which is
# asserted by making any read fail outright.
with tempfile.TemporaryDirectory() as tmp:
    oversized = os.path.join(tmp, "config.toml")
    with open(oversized, "wb") as fh:
        fh.write(b"#" + b"x" * (jc.safefile.MAX_CONFIG_BYTES + 16))

    real_read_bytes = jc.safefile.read_bytes

    def _refuse(*a, **k):
        raise AssertionError("stamp read the file")

    jc.safefile.read_bytes = _refuse
    buf = io.StringIO()
    rc, out, read_attempted = None, {}, False
    try:
        with contextlib.redirect_stdout(buf):
            rc = jc.cmd_stamp(None, argparse.Namespace(config=oversized))
        out = json.loads(buf.getvalue())
    except AssertionError:
        # _refuse fired: stamp read bytes it has no business reading. Report
        # that as a failed check rather than letting it kill the run.
        read_attempted = True
    finally:
        jc.safefile.read_bytes = real_read_bytes
    check("a config past the read ceiling still stamps, reading no bytes",
          not read_attempted and rc == 0 and out.get("present") is True
          and out.get("size", 0) > jc.safefile.MAX_CONFIG_BYTES,
          "stamp read the file" if read_attempted else f"rc={rc} out={out}")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = jc.cmd_stamp(None, argparse.Namespace(config=os.path.join(tmp, "gone")))
    check("a missing config stamps as absent rather than failing",
          rc == 0 and json.loads(buf.getvalue()) == {"present": False},
          f"rc={rc} out={buf.getvalue()!r}")


print()
if all(results):
    print("all wiring tests passed")
else:
    print("FAILURES")
    sys.exit(1)
