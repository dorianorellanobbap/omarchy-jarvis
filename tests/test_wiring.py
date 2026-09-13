"""Wiring smoke tests.

Cheap checks for the class of break that only shows up at runtime, long after
the edit: a constant referenced but never defined, or an attribute the code
reads off an Agent that nothing ever sets. Neither is visible to a syntax
check, and the second only fires when someone actually speaks to it, so a
clean startup is not evidence that it works.

  python3 tests/test_wiring.py
"""
import ast
import builtins
import importlib.util
import os
import sys

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

print()
if all(results):
    print("all wiring tests passed")
else:
    print("FAILURES")
    sys.exit(1)
