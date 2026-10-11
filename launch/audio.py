"""Synthesizes the launch video soundtrack from timeline.json (no samples, all original).

  python launch/audio.py launch/soundtrack.wav
"""

import json
import sys
import wave
from pathlib import Path

import numpy as np

SR = 48000
HERE = Path(__file__).parent
TL = json.loads((HERE / "timeline.json").read_text())
N = int(TL["duration"] * SR)
rng = np.random.default_rng(7)

music = np.zeros((N, 2))
sfx = np.zeros((N, 2))


def t_axis(sec: float) -> np.ndarray:
    return np.arange(int(sec * SR)) / SR


def place(buf: np.ndarray, at: float, sig: np.ndarray, gain: float = 1.0, pan: float = 0.0) -> None:
    i = int(at * SR)
    if i >= len(buf) or i + len(sig) <= 0:
        return
    sig = sig[: len(buf) - i]
    left, right = gain * np.sqrt((1 - pan) / 2), gain * np.sqrt((1 + pan) / 2)
    buf[i : i + len(sig), 0] += sig * left
    buf[i : i + len(sig), 1] += sig * right


def fft_filter(x: np.ndarray, lo: float = 0, hi: float = SR / 2) -> np.ndarray:
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    X[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(X, len(x))


def env(n: int, attack: float, decay: float) -> np.ndarray:
    tt = np.arange(n) / SR
    return np.minimum(1, tt / max(attack, 1e-4)) * np.exp(-tt / decay)


# --------------------------------------------------------------------------- UI sounds
def pop_in() -> np.ndarray:  # incoming bubble: soft two-tone marimba-ish pop
    tt = t_axis(0.35)
    a = np.sin(2 * np.pi * 1046 * tt) * env(len(tt), 0.002, 0.07)
    b = np.sin(2 * np.pi * 1568 * tt) * env(len(tt), 0.002, 0.09)
    b = np.concatenate([np.zeros(int(0.055 * SR)), b])[: len(tt)]
    return 0.5 * a + 0.45 * b


def swoosh_out() -> np.ndarray:  # outgoing bubble: quick airy upward swoosh + tick
    tt = t_axis(0.3)
    noise = rng.standard_normal(len(tt))
    sweep = np.sin(2 * np.pi * (600 * tt + 4000 * tt**2))
    body = fft_filter(noise, 1500, 9000) * env(len(tt), 0.04, 0.06) * 0.6 + 0.25 * sweep * env(len(tt), 0.01, 0.05)
    return body


def key_tap() -> np.ndarray:
    tt = t_axis(0.03)
    return fft_filter(rng.standard_normal(len(tt)), 2500, 12000) * env(len(tt), 0.0005, 0.006)


def whoosh(dur: float = 0.9) -> np.ndarray:
    tt = t_axis(dur)
    noise = rng.standard_normal(len(tt))
    out = np.zeros_like(noise)
    # band sweeping up then down, built from a few FFT-filtered slices
    slices = 12
    for k in range(slices):
        s0, s1 = int(len(tt) * k / slices), int(len(tt) * (k + 1) / slices)
        center = 300 + 3500 * np.sin(np.pi * (k + 0.5) / slices)
        out[s0:s1] = fft_filter(noise, center * 0.6, center * 1.6)[s0:s1]
    shape = np.sin(np.pi * np.clip(tt / dur, 0, 1)) ** 2
    return out * shape * 0.8


def riser(dur: float) -> np.ndarray:
    tt = t_axis(dur)
    p = tt / dur
    noise = fft_filter(rng.standard_normal(len(tt)), 800, 12000) * p**2.2
    tone = np.sin(2 * np.pi * (220 * tt + 330 * tt**2 / dur)) * p**2 * 0.35
    return (noise * 0.5 + tone) * 0.9


def kick(strength: float = 1.0) -> np.ndarray:
    tt = t_axis(0.45)
    freq = 45 + 110 * np.exp(-tt / 0.035)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    click = fft_filter(rng.standard_normal(len(tt)), 2000, 8000) * env(len(tt), 0.0003, 0.004) * 0.3
    return (np.sin(phase) * env(len(tt), 0.001, 0.16) + click) * strength


def impact() -> np.ndarray:
    tt = t_axis(2.5)
    boom = kick(1.4)
    boom = np.concatenate([boom, np.zeros(len(tt) - len(boom))])
    sub = np.sin(2 * np.pi * 41 * tt) * env(len(tt), 0.005, 0.8) * 0.7
    air = fft_filter(rng.standard_normal(len(tt)), 3000, 14000) * env(len(tt), 0.002, 0.6) * 0.18
    return boom + sub + air


def hat() -> np.ndarray:
    tt = t_axis(0.08)
    return fft_filter(rng.standard_normal(len(tt)), 7000, 16000) * env(len(tt), 0.0005, 0.018)


def clap() -> np.ndarray:
    tt = t_axis(0.3)
    n = fft_filter(rng.standard_normal(len(tt)), 900, 5000)
    e = env(len(tt), 0.001, 0.09)
    for d in (0.008, 0.017):  # flam
        e += np.concatenate([np.zeros(int(d * SR)), env(len(tt) - int(d * SR), 0.0005, 0.01)])
    return n * e * 0.5


def printer_ticks(dur: float) -> np.ndarray:
    out = np.zeros(int(dur * SR))
    tick = fft_filter(rng.standard_normal(int(0.012 * SR)), 1500, 7000) * env(int(0.012 * SR), 0.0003, 0.003)
    k = 0.0
    while k < dur - 0.02:
        i = int(k * SR)
        out[i : i + len(tick)] += tick * (0.6 + 0.4 * rng.random())
        k += 0.028 + 0.01 * rng.random()
    motor = np.sin(2 * np.pi * 120 * t_axis(dur)) * 0.03
    return out + motor


def chime() -> np.ndarray:
    tt = t_axis(0.6)
    return (np.sin(2 * np.pi * 1318.5 * tt) * 0.5 + np.sin(2 * np.pi * 1975.5 * tt) * 0.3) * env(len(tt), 0.003, 0.18)


# --------------------------------------------------------------------------- music
def note_freq(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


CHORDS = [  # Fmaj9, Am7, Dm9, Cmaj7 (voiced around middle C)
    [53, 60, 64, 67, 69],
    [57, 60, 64, 67, 71],
    [50, 57, 60, 64, 65],
    [48, 55, 59, 64, 67],
]


def pad_layer(start: float, end: float, bar: float, cutoff: float) -> np.ndarray:
    n = int((end - start) * SR)
    out = np.zeros(n)
    chord_len = bar * 2
    k = 0
    while k * chord_len < end - start:
        c0 = k * chord_len
        seg_len = min(chord_len + 0.6, end - start - c0)
        tt = t_axis(seg_len)
        sig = np.zeros(len(tt))
        for m in CHORDS[k % 4]:
            for det in (-0.06, 0.06):
                f = note_freq(m + det)
                sig += np.sin(2 * np.pi * f * tt) + 0.25 * np.sin(2 * np.pi * 2 * f * tt) + 0.1 * np.sin(2 * np.pi * 3 * f * tt)
        a = np.minimum(1, tt / 0.5) * np.minimum(1, np.maximum(0, (seg_len - tt) / 0.6))
        sig = fft_filter(sig * a, 0, cutoff)
        i = int(c0 * SR)
        out[i : i + len(sig)] += sig[: n - i]
        k += 1
    return out / 12


def reverb(x: np.ndarray, seconds: float = 1.8, mix: float = 0.25) -> np.ndarray:
    ir_n = int(seconds * SR)
    ir = rng.standard_normal(ir_n) * np.exp(-np.arange(ir_n) / SR / (seconds / 5))
    ir = fft_filter(ir, 200, 7000)
    ir /= np.sqrt(np.sum(ir**2))
    size = 1 << int(np.ceil(np.log2(len(x) + ir_n)))
    wet = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[: len(x)]
    return x * (1 - mix) + wet * mix


bpm = TL["beats"]["bpm"]
beat = 60 / bpm
bar = beat * 4
drop, mend = TL["beats"]["drop"], TL["beats"]["end"]

# intro pad (quiet, dark), then brighter pad after the drop
intro = pad_layer(0, drop, bar, 900) * 0.55
place(music, 0, intro * np.minimum(1, t_axis(drop) / 2.5), 1.0)
main_pad = pad_layer(drop, TL["duration"], bar, 2600)
duck = np.ones(len(main_pad))
bass = np.zeros(len(main_pad))
drums = np.zeros(len(main_pad))
k_sig, h_sig, c_sig = kick(), hat(), clap()

beat_i = 0
tb = 0.0
while drop + tb < mend:
    i = int(tb * SR)
    drums[i : i + len(k_sig)] += k_sig[: len(drums) - i]
    dl = int(0.28 * SR)
    duck[i : i + dl] = np.minimum(duck[i : i + dl], 0.35 + 0.65 * (np.arange(min(dl, len(duck) - i)) / dl) ** 1.5)
    if beat_i % 4 in (1, 3):
        drums[i : i + len(c_sig)] += c_sig[: len(drums) - i] * 0.8
    hi = int((tb + beat / 2) * SR)
    if hi < len(drums):
        drums[hi : hi + len(h_sig)] += h_sig[: len(drums) - hi] * 0.6
    # bass: chord root, 8th-note pulse
    chord = CHORDS[int(tb // (bar * 2)) % 4]
    f = note_freq(chord[0] - 12)
    for half in (0, 0.5):
        j = int((tb + half * beat) * SR)
        tt = t_axis(beat * 0.45)
        tone = (np.sin(2 * np.pi * f * tt) + 0.3 * np.sin(2 * np.pi * 2 * f * tt)) * env(len(tt), 0.004, 0.12)
        bass[j : j + len(tone)] += tone[: len(bass) - j] * 0.5
    beat_i += 1
    tb += beat

main = main_pad * duck * 0.9 + bass * duck * 0.8 + drums * 0.9
main = reverb(main, 1.4, 0.18)
fade_in = np.minimum(1, t_axis(TL["duration"] - drop) / 0.05)
outro = np.clip((TL["duration"] - 0.4 - (drop + t_axis(TL["duration"] - drop))) / 4.5, 0, 1)
place(music, drop, main * fade_in * outro, 0.95)

# --------------------------------------------------------------------------- place UI events
for c in TL["chat"]:
    if c["kind"] == "msg":
        place(sfx, c["at"], pop_in() if c["dir"] == "in" else swoosh_out(), 0.55 if c["dir"] == "in" else 0.5,
              pan=-0.1 if c["dir"] == "in" else 0.1)
    elif c["kind"] == "compose":
        n = len(c["text"])
        for k in range(n):
            if c["text"][k] == " " and rng.random() < 0.5:
                continue
            at = c["from"] + (c["to"] - c["from"]) * k / n + rng.uniform(-0.015, 0.015)
            place(sfx, at, key_tap(), 0.13 + 0.05 * rng.random(), pan=rng.uniform(-0.2, 0.2))

for e in TL["sfx"]:
    if e["type"] == "whoosh":
        place(sfx, e["at"] - 0.35, whoosh(), 0.55)
    elif e["type"] == "riser":
        place(sfx, e["from"], riser(e["to"] - e["from"]), 0.45)
    elif e["type"] == "impact":
        place(sfx, e["at"], impact(), 0.9)
    elif e["type"] == "printer":
        place(sfx, e["from"], printer_ticks(e["to"] - e["from"]), 0.5, pan=0.25)
    elif e["type"] == "chime":
        place(sfx, e["at"], chime(), 0.35, pan=0.3)

# outreach: soft pops as cards appear (playhead timing mirrors the HTML)
def eio(x: float) -> float:
    return 4 * x**3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


for o in TL["outreach"]:
    hh, mm = map(int, o["time"].split(":"))
    hour = hh + mm / 60
    s = 39.3
    while s < 46.2 and 11 + eio((s - 39.3) / 6.9) * 8.5 < hour:
        s += 1 / 120
    place(sfx, s + 0.05, swoosh_out(), 0.35, pan=(hour - 15) / 10)
    place(sfx, s + 1.1, pop_in(), 0.3, pan=(hour - 15) / 10)

# particles in the learning scene: tiny ticks
for k in range(14):
    place(sfx, 29.2 + k * 0.55 + rng.uniform(0, 0.2), key_tap(), 0.1)

mix = music * 0.55 + sfx
mix = np.tanh(mix * 1.2) / np.tanh(1.2)  # gentle limiter
peak = np.max(np.abs(mix))
mix = mix / peak * 0.89
pcm = (mix * 32767).astype("<i2")
out = Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "soundtrack.wav")
with wave.open(str(out), "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())
print("wrote", out, f"{len(pcm) / SR:.1f}s")
