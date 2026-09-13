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

# These exercise the loudness fallback, the path taken when the speech model
# is not installed. Synthetic tones are not speech and the model says so
# correctly, so leaving it enabled here would test the model against audio
# nobody claims is a voice. tests/test_vad.py covers the model itself, with
# real synthesised speech.
jl._vad_tried = True
jl._vad = None

FRAME = jl.CHUNK_SAMPLES / jl.RATE          # 0.08s
LISTEN = {"silence_tail": 1.2, "min_speech": 0.4, "max_command": 15.0}


def frames_at(level, seconds, offset=0):
    """Frames whose RMS is exactly `level`, alternating sign so it is audio.

    `offset` shifts the whole frame off zero, the way a microphone with a DC
    bias does.
    """
    n = int(round(seconds / FRAME))
    one = np.empty(jl.CHUNK_SAMPLES, dtype=np.int16)
    one[0::2] = int(level) + offset
    one[1::2] = -int(level) + offset
    return [one.copy() for _ in range(n)]


def run(script, ambient=100.0, listen=LISTEN, voice_level=0.0):
    """Feed `script` to capture_command and return seconds of audio kept.

    voice_level is how loud the wake word was. The daemon always has one; a
    zero here exercises the fallback for a capture that never heard one.
    """
    queue = list(script)
    jl.read_chunk = lambda mic: queue.pop(0) if queue else None
    jl._running = True
    out = jl.capture_command(None, ambient, listen, voice_level)
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
kept = run(stale + frames_at(20, 2.0), ambient=400.0, voice_level=500)
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

# A microphone that rests off zero, like the PX13's internal one at -2817.
# Measured without removing the offset, a silent room reads ~2817, which is
# louder than speech: nothing is ever quiet, and the recorder runs to
# max_command on every question instead of stopping when the speaker does.
results.append(check(
    "a mic with a DC offset still endpoints",
    run(frames_at(900, 1.5, offset=-2817) + frames_at(20, 3.0, offset=-2817)),
    1.5 + LISTEN["silence_tail"]))

# The room someone moved into is louder than the one they set this up in.
# A fixed multiple of the room put the bar over their head: nothing counted
# as talking, so the recorder ran to max_command on every question and threw
# the result away. The wake word is the measurement that fixes it.
results.append(check(
    "a voice in a loud room is still heard",
    run(frames_at(600, 2.0) + frames_at(150, 2.0),
        ambient=150.0, voice_level=600),
    2.0 + LISTEN["silence_tail"]))

# And the misfire case: nobody says anything at all. Waiting out max_command
# to discard it costs the speaker the whole ceiling for nothing.
gave_up = run(frames_at(30, 12.0), ambient=30.0, voice_level=600)
ok = gave_up is None
print(f"  {'PASS' if ok else 'FAIL'}  silence after a wake word gives up early: "
      f"{gave_up}")
results.append(ok)

# A real room, measured: quiet end 99, median 161, loud end 225, peaks 251.
# Judging silence from the quiet end put the line under 93% of that room's own
# frames, so the timer reset constantly and the sentence never ended.
noisy = 225.0
on, sus = jl.thresholds(noisy, 600)
ok = sus > 251 * 0.9 and on < 600
print(f"  {'PASS' if ok else 'FAIL'}  a fluctuating room is judged by its loud "
      f"end (onset {on:.0f}, sustain {sus:.0f})")
results.append(ok)

results.append(check(
    "a sentence ends in that room",
    run(frames_at(600, 2.0) + frames_at(160, 2.0),
        ambient=noisy, voice_level=600),
    2.0 + LISTEN["silence_tail"]))

# People announce the wake word and then speak the question normally, so a
# bar set from the wake word alone sits above the sentence it should catch.
# After a few questions the listener knows better than the wake word does.
jl._spoken_peaks.clear()
loud_wake, normal_voice = 600, 300
on_before, _ = jl.thresholds(225.0, jl.voice_reference(loud_wake))
for _ in range(4):
    jl._spoken_peaks.append(normal_voice)
on_after, _ = jl.thresholds(225.0, jl.voice_reference(loud_wake))
ok = on_before > normal_voice and on_after < normal_voice
print(f"  {'PASS' if ok else 'FAIL'}  it learns a normal speaking voice "
      f"(bar {on_before:.0f} -> {on_after:.0f}, voice {normal_voice})")
results.append(ok)
jl._spoken_peaks.clear()

print()
if all(results):
    print("all endpointing tests passed")
else:
    print("FAILURES")
    sys.exit(1)
