#!/usr/bin/env python3
"""Generate a hash-locked requirements file from the pinned requirements.txt.

For each `name==version`, ask PyPI for every artifact published under that
exact version and record all of their sha256 digests. pip accepts multiple
--hash entries per requirement and installs the artifact matching any one of
them, so a single lock file stays valid across Python versions and platforms
while still refusing anything whose bytes have changed since this snapshot.
"""
import json
import sys
import urllib.request

import os

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(os.path.dirname(HERE), "daemon")
SRC = os.path.join(DAEMON, "requirements.txt")
OUT = os.path.join(DAEMON, "requirements.lock")

# pip is what installs everything else, so it gets pinned too rather than
# being upgraded to whatever is current at install time.
EXTRA = [("pip", None)]


def pinned():
    out = []
    for line in open(SRC):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        out.append((name.strip(), version.strip() or None))
    return out


def artifacts(name, version):
    if version is None:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=60) as r:
            version = json.load(r)["info"]["version"]
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.load(r)
    files = data.get("urls", [])
    hashes, kinds = [], set()
    for f in files:
        h = f.get("digests", {}).get("sha256")
        if h:
            hashes.append(h)
            kinds.add(f.get("packagetype", "?"))
    return version, sorted(set(hashes)), kinds


def main():
    lines = [
        "# Hash-locked dependencies for the Jarvis daemon.",
        "#",
        "# Generated from requirements.txt. Every sha256 published by PyPI for the",
        "# pinned version is listed, so one lock file covers every Python version",
        "# and platform while still rejecting any artifact whose bytes differ from",
        "# this snapshot. install.sh installs with --require-hashes, which also",
        "# forces every transitive dependency to appear here.",
        "#",
        "# Regenerate after editing requirements.txt:",
        "#   python tools/genlock.py",
        "",
    ]
    total = 0
    for name, version in pinned() + EXTRA:
        v, hashes, kinds = artifacts(name, version)
        if not hashes:
            print(f"!! no artifacts for {name}=={v}", file=sys.stderr)
            return 1
        total += len(hashes)
        print(f"  {name}=={v}: {len(hashes)} artifacts ({','.join(sorted(kinds))})")
        entry = f"{name}=={v}"
        for h in hashes:
            entry += f" \\\n    --hash=sha256:{h}"
        lines.append(entry)
    open(OUT, "w").write("\n".join(lines) + "\n")
    print(f"\nwrote {OUT}: {total} hashes across {len(pinned())+len(EXTRA)} packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
