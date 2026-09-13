"""Endpointing tests: does capture_command stop while you are still talking?

Runs without a microphone. read_chunk is replaced with a scripted sequence of
frames at chosen loudness, so a "sentence" here is just a level over time.

  python3 tests/test_endpoint.py
"""
import importlib.util
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

FRAME = jl.CHUNK_SAMPLES / jl.RATE          # 0.08s
LISTEN = {"silence_tail": 1.2, "min_speech": 0.4, "max_command": 15.0}


def frames_at(level, seconds):
    """Frames whose RMS is exactly `level`, alternating sign so it is audio."""
    n = int(round(seconds / FRAME))
    one = np.empty(jl.CHUNK_SAMPLES, dtype=np.int16)
    one[0::2] = int(level)
    one[1::2] = -int(level)
    return [one.copy() for _ in range(n)]


def run(script, ambient=100.0, listen=LISTEN):
    """Feed `script` to capture_command and return seconds of audio kept."""
    queue = list(script)
    jl.read_chunk = lambda mic: queue.pop(0) if queue else None
    jl._running = True
    out = jl.capture_command(None, ambient, listen)
    return None if out is None else len(out) / jl.RATE


def check(name, got, expected, tolerance=0.2):
    ok = got is not None and abs(got - expected) <= tolerance
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: kept {got}s, wanted ~{expected}s")
    return ok


results = []

# A sentence that gets quieter in the middle, which is what ordinary speech
# does. 260 is under the 300 onset but over the 150 sustain: the old single
# threshold called this silence and cut the question off after 1.2s of it.
results.append(check(
    "quiet middle of a sentence does not end it",
    run(frames_at(900, 1.0) + frames_at(260, 1.5) + frames_at(900, 1.0)
        + frames_at(20, 2.0)),
    1.0 + 1.5 + 1.0 + LISTEN["silence_tail"]))

# Actually stopping still stops, and promptly.
results.append(check(
    "a finished sentence ends after the tail",
    run(frames_at(900, 1.5) + frames_at(20, 3.0)),
    1.5 + LISTEN["silence_tail"]))

# A stale room level, measured while something was briefly noisy, used to
# hold the bar above the speaker for the whole question. It should fall back
# to the real room during the gaps between words. Speech at 500 against a
# claimed floor of 400 is under the 1200 onset that stale value implies, so
# nothing is heard until the floor catches up: the first word or so is lost,
# and the rest of the question is not.
stale = []
for _ in range(6):
    stale += frames_at(500, 0.4) + frames_at(30, 0.3)
kept = run(stale + frames_at(20, 2.0), ambient=400.0)
ok = kept is not None and kept >= 2.0
print(f"  {'PASS' if ok else 'FAIL'}  a stale room level recovers: kept {kept}s, wanted >=2.0s")
results.append(ok)

# A cough is not a question.
blip = run(frames_at(900, 0.2) + frames_at(20, 3.0))
ok = blip is None
print(f"  {'PASS' if ok else 'FAIL'}  a blip under min_speech is dropped: {blip}")
results.append(ok)

# The ceiling still holds, so a stuck-open mic cannot record forever.
results.append(check(
    "max_command still caps the recording",
    run(frames_at(900, 30.0), listen={**LISTEN, "max_command": 5.0}),
    5.0))

print()
if all(results):
    print("all endpointing tests passed")
else:
    print("FAILURES")
    sys.exit(1)
