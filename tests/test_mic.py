"""Microphone verdict tests.

report_mic is what tells someone why Jarvis cannot hear them. The failures it
has to name are ones we cannot reproduce on the developer's own working
microphone, so they are fed in as measurements rather than recorded.

  python3 tests/test_mic.py
"""
import contextlib
import importlib.util
import io
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(os.path.dirname(HERE), "daemon")
sys.path.insert(0, DAEMON)
spec = importlib.util.spec_from_file_location(
    "jarvis_listen", os.path.join(DAEMON, "jarvis-listen.py"))
jl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jl)


def frames(level, seconds, offset=0):
    n = int(round(seconds * jl.RATE / jl.CHUNK_SAMPLES))
    one = np.empty(jl.CHUNK_SAMPLES, dtype=np.int16)
    one[0::2] = int(level) + offset
    one[1::2] = -int(level) + offset
    return [one.copy() for _ in range(n)]


def verdict(quiet, spoken=None):
    """Returns (ok, printed text)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ok = jl.report_mic(quiet, spoken)
    return ok, out.getvalue()


def case(name, quiet, spoken, want_ok, want_text):
    ok, text = verdict(quiet, spoken)
    good = ok == want_ok and want_text in text
    print(f"  {'PASS' if good else 'FAIL'}  {name}")
    if not good:
        print(f"        wanted ok={want_ok} containing {want_text!r}")
        print("        got ok=%s:\n%s" % (ok, text))
    return good


d = jl.describe_mic
results = []

# The mic never opened, or pw-record died immediately.
results.append(case(
    "a dead mic is named as such",
    None, None, False, "produced no audio at all"))

# Muted, or input volume at zero: every sample identical.
results.append(case(
    "a muted mic is not mistaken for a quiet room",
    d([np.zeros(jl.CHUNK_SAMPLES, dtype=np.int16)] * 30), None,
    False, "producing silence"))

# A good mic in a quiet room, someone speaking normally.
results.append(case(
    "a working mic passes",
    d(frames(45, 3.0)), d(frames(2500, 3.0)), True, "56x the room"))

# Working, centred or not: the offset is a note, never a failure.
results.append(case(
    "a DC offset is reported but not a failure",
    d(frames(45, 3.0, offset=-2817)), d(frames(2500, 3.0, offset=-2817)),
    True, "DC offset"))

# Speaking, but too quietly to ever clear the bar. This is the case someone
# hits when their input volume is low, and it looks identical to "the wake
# word does not work" from the outside.
results.append(case(
    "a voice under the threshold is named",
    d(frames(45, 3.0)), d(frames(200, 3.0)), False, "turn the input volume up"))

# The verdict has to be scale free: the same voice-to-room ratio must read the
# same whether the mic is at 30% or 90%. These two are the identical situation
# measured ten times apart, and both are fine.
results.append(case(
    "a quiet mic with a good ratio passes",
    d(frames(20, 3.0)), d(frames(320, 3.0)), True, "16x the room"))
results.append(case(
    "a loud mic with the same ratio passes identically",
    d(frames(200, 3.0)), d(frames(3200, 3.0)), True, "16x the room"))

# Loud enough to be heard, but barely above the room, so the gaps between
# words stay over the line that keeps a sentence open and the question runs to
# the ceiling. Clearing onset already takes 5x the floor, so only the band
# between 5x and 8x can reach this warning at all.
results.append(case(
    "a narrow margin over the room warns",
    d(frames(100, 3.0)), d(frames(600, 3.0)), True, "narrow margin"))

print()
if all(results):
    print("all microphone tests passed")
else:
    print("FAILURES")
    sys.exit(1)
