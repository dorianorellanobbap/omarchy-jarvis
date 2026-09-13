"""Bookmark resolution tests.

The bookmarks file belongs to Chromium, not to us, so everything here is
about not trusting it: a bookmark can hold a javascript:, file: or chrome:
URL, and this is the path that turns a spoken sentence into something handed
to the browser.

  python3 tests/test_bookmarks.py
"""
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile

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


def bookmark(name, url):
    return {"type": "url", "name": name, "url": url}


def write_profile(tree):
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(tree, fh)
    fh.close()
    jo.BOOKMARKS_PATH = fh.name
    return fh.name


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got:    {got!r}\n        wanted: {want!r}")
    results.append(ok)


profile = {"roots": {
    "bookmark_bar": {"type": "folder", "children": [
        bookmark("My Bank", "https://bank.example.com/login"),
        bookmark("Team Wiki", "http://wiki.internal/start"),
        {"type": "folder", "children": [
            bookmark("Buried", "https://deep.example.com/"),
        ]},
    ]},
    "other": {"type": "folder", "children": [
        bookmark("Run This", "javascript:alert(document.cookie)"),
        bookmark("My Passwords", "file:///home/dorian/.ssh/id_rsa"),
        bookmark("Settings", "chrome://settings/passwords"),
        bookmark("Weird", "data:text/html,<script>x</script>"),
        bookmark("Odd Codes", "https://user:secret@private.example.com:8443/x"),
    ]},
}}
write_profile(profile)

# The dangerous half of a real bookmarks file never survives the walk.
titles = [t for t, _ in jo.bookmarks()]
check("only http(s) bookmarks are kept",
      sorted(titles),
      sorted(["My Bank", "Team Wiki", "Buried", "Odd Codes"]))

check("a javascript: bookmark cannot be resolved",
      jo.resolve_bookmark("run this"), None)
check("a file: bookmark cannot be resolved",
      jo.resolve_bookmark("my passwords"), None)
check("a chrome: bookmark cannot be resolved",
      jo.resolve_bookmark("settings"), None)
check("a data: bookmark cannot be resolved",
      jo.resolve_bookmark("weird"), None)

# Matching: exact title, part of a title, then the site's host.
check("an exact title matches",
      jo.resolve_bookmark("My Bank"), ("My Bank", "https://bank.example.com/login"))
check("part of a title matches",
      jo.resolve_bookmark("bank"), ("My Bank", "https://bank.example.com/login"))
check("a folder does not hide a bookmark",
      jo.resolve_bookmark("buried"), ("Buried", "https://deep.example.com/"))
check("the host matches when the title does not",
      jo.resolve_bookmark("wiki.internal"),
      ("Team Wiki", "http://wiki.internal/start"))
check("nothing matches nothing",
      jo.resolve_bookmark("a bookmark that is not there"), None)
check("an empty query matches nothing", jo.resolve_bookmark("   "), None)

# What gets printed is journaled by the daemon, so it must not carry
# credentials or the rest of the URL.
check("printed origin drops credentials and path",
      jo.origin_of("https://user:secret@private.example.com:8443/x"),
      "https://private.example.com:8443")

# A file that is missing, unreadable, or not JSON is not a crash.
jo.BOOKMARKS_PATH = "/nonexistent/Bookmarks"
check("a missing profile is empty, not an error", jo.bookmarks(), [])
bad = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
bad.write("{not json at all")
bad.close()
jo.BOOKMARKS_PATH = bad.name
check("a corrupt profile is empty, not an error", jo.bookmarks(), [])

print()
if all(results):
    print("all bookmark tests passed")
else:
    print("FAILURES")
    sys.exit(1)
