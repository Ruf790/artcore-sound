"""Delay and reverb.

Delay: a ring buffer per channel with feedback; a one-pole low cut and high cut in the loop (each echo darker /
thinner than the one before); ping-pong for stereo (the echoes alternate between the sides); the time in ms or in
note values at a tempo (BPM).

Reverb: a feedback delay network (Jot): 8 delay lines mixed back through an orthogonal (Hadamard) matrix, each line's
gain set so that its sound falls 60 dB in the decay time (g = 10^(−3·d / (T60·rate))), a one-pole low-pass in each line
for the high-frequency damping, a pre-delay before it, every channel fed into and taken from the lines through its own
± pattern (so the channels' reverbs are decorrelated — the width blends towards their mean), the LFE left dry.
Algorithmic rather than convolution: the decay is set exactly, the memory is small however long the reverb, and
every parameter changes on the fly without making and transforming a new impulse response.

Both work in sub-blocks no longer than their shortest delay: inside one, every read comes from frames written before
it, so a sub-block is a few array operations, and the result does not depend on how the sound was chunked."""
from __future__ import annotations

import math

import numpy as np
from scipy import signal
from scipy.linalg import hadamard

from .base import Effect, Fifo, Param, Processor, mix_gains

MAX_DELAY_MS = 2000.0
SUB = 4096
NOTES = (("1/1", 4.0), ("1/2", 2.0), ("1/4.", 1.5), ("1/4", 1.0), ("1/4t", 2 / 3), ("1/8.", 0.75), ("1/8", 0.5),
         ("1/8t", 1 / 3), ("1/16", 0.25))
LFE = {"LFE"}


def one_pole_lp(fc: float, rate: int):
    """(b, a) of y = (1 − a)·x + a·y₋₁ with its −3 dB point near fc; None at the top of the band (off)."""
    if fc >= 0.45 * rate or fc >= 19999:
        return None
    a = math.exp(-2 * math.pi * fc / rate)
    return np.array([1 - a]), np.array([1, -a])


def one_pole_hp(fc: float, rate: int):
    if fc <= 20.5:
        return None
    a = math.exp(-2 * math.pi * fc / rate)
    return np.array([(1 + a) / 2, -(1 + a) / 2]), np.array([1, -a])


class _Filt:
    """An optional one-pole filter with its state (columns: channels / lines)."""

    def __init__(self, ba, width: int):
        self.ba = ba
        self.zi = np.zeros((1, width)) if ba is not None else None

    def __call__(self, x):
        if self.ba is None:
            return x
        y, self.zi = signal.lfilter(self.ba[0], self.ba[1], x, axis=0, zi=self.zi)
        return y

    def retune(self, ba):
        if (ba is None) != (self.ba is None):
            self.zi = np.zeros_like(self.zi) if self.zi is not None else np.zeros((1, 1))
        self.ba = ba
        if ba is not None and self.zi is None:
            self.zi = np.zeros((1, 1))


def delay_frames(p: dict, rate: int) -> int:
    if p["sync"]:
        beats = dict(NOTES).get(p["note"], 1.0)
        ms = 60000.0 / max(1.0, p["bpm"]) * beats
    else:
        ms = p["time"]
    return max(1, int(round(min(ms, MAX_DELAY_MS) * rate / 1000.0)))


class DelayProc(Processor):
    def configure(self, old: dict) -> None:
        p = self.p
        self.d = delay_frames(p, self.rate)
        self.fb = p["feedback"] / 100.0
        self.dry, self.wet = mix_gains(p["mix"] / 100.0)
        self.pp = bool(p["pingpong"]) and self.ch == 2
        lp, hp = one_pole_lp(p["highcut"], self.rate), one_pole_hp(p["lowcut"], self.rate)
        if not old:
            self.size = int(MAX_DELAY_MS * self.rate / 1000.0) + SUB + 2
            self.buf = np.zeros((self.size, self.ch))
            self.w = 0
            self.lp, self.hp = _Filt(lp, self.ch), _Filt(hp, self.ch)
        else:
            for f, ba in ((self.lp, lp), (self.hp, hp)):
                if (ba is None) != (f.ba is None):
                    f.zi = np.zeros((1, self.ch)) if ba is not None else None
                f.ba = ba

    def _process(self, x):
        n = len(x)
        out = np.empty_like(x)
        pos = 0
        d = self.d
        while pos < n:
            s = min(n - pos, d, SUB)
            xs = x[pos:pos + s]
            ri = (self.w - d + np.arange(s)) % self.size
            r = self.hp(self.lp(self.buf[ri]))
            wi = (self.w + np.arange(s)) % self.size
            if self.pp:
                inp = np.empty_like(xs)
                inp[:, 0] = (xs[:, 0] + xs[:, 1]) * 0.5 + self.fb * r[:, 1]
                inp[:, 1] = self.fb * r[:, 0]
                self.buf[wi] = inp
            else:
                self.buf[wi] = xs + self.fb * r
            out[pos:pos + s] = self.dry * xs + self.wet * r
            self.w = (self.w + s) % self.size
            pos += s
        return out

    def tail_frames(self) -> int:
        if self.fb <= 1e-4:
            echoes = 1
        else:
            echoes = int(math.ceil(math.log(1e-4) / math.log(self.fb))) + 1         # down 80 dB
        return int(min(60 * self.rate, self.d * (echoes + 1) + self.rate // 10))


class Delay(Effect):
    key = "delay"
    name = "fx_delay"
    has_tail = True
    params = [Param("time", lo=1.0, hi=MAX_DELAY_MS, default=350.0, unit="ms", decimals=0, log=True, label="dl_time"),
              Param("sync", "bool", default=False, label="dl_sync"),
              Param("bpm", lo=40.0, hi=240.0, default=120.0, unit="BPM", decimals=1, label="dl_bpm"),
              Param("note", "choice", default="1/4", choices=tuple((k, "") for k, _v in NOTES), label="dl_note"),
              Param("feedback", lo=0.0, hi=95.0, default=40.0, unit="%", decimals=0, label="dl_feedback"),
              Param("mix", lo=0.0, hi=100.0, default=35.0, unit="%", decimals=0, label="fx_mix"),
              Param("pingpong", "bool", default=False, label="dl_pingpong"),
              Param("lowcut", lo=20.0, hi=2000.0, default=20.0, unit="Hz", decimals=0, log=True, label="dl_lowcut"),
              Param("highcut", lo=1000.0, hi=20000.0, default=20000.0, unit="Hz", decimals=0, log=True,
                    label="dl_highcut")]
    proc_class = DelayProc
    presets = {
        "pr_slap": {"time": 110.0, "feedback": 10.0, "mix": 30.0, "highcut": 6000.0},
        "pr_echo": {"time": 380.0, "feedback": 45.0, "mix": 35.0, "lowcut": 150.0, "highcut": 5000.0},
        "pr_pingpong": {"sync": True, "bpm": 120.0, "note": "1/8.", "feedback": 50.0, "mix": 40.0,
                        "pingpong": True, "highcut": 7000.0},
        "pr_tape": {"time": 450.0, "feedback": 60.0, "mix": 40.0, "lowcut": 200.0, "highcut": 2500.0},
    }


# ── the reverb ────────────────────────────────────────────────
LINES = (1031, 1327, 1523, 1871, 2053, 2381, 2713, 3089)    # frames at 44.1 kHz and the largest room
_H = hadamard(8) / math.sqrt(8)


class ReverbProc(Processor):
    def configure(self, old: dict) -> None:
        p = self.p
        scale = (0.3 + 0.7 * p["size"] / 100.0) * self.rate / 44100.0
        lens = np.maximum(64, np.round(np.array(LINES) * scale)).astype(np.int64)
        self.t60 = float(p["decay"])
        self.g = 10.0 ** (-3.0 * lens / (self.t60 * self.rate))
        self.dry, self.wet = mix_gains(p["mix"] / 100.0)
        self.width = p["width"] / 100.0
        damp = p["damping"] / 100.0
        self.damp_a = 0.85 * damp ** 1.5
        pre = int(round(p["predelay"] * self.rate / 1000.0))
        wet_ch = [i for i, lab in enumerate(self.labels) if lab not in LFE] or list(range(self.ch))
        if not old or getattr(self, "pre_n", None) != pre:
            self.pre_n = pre
            self.pre = Fifo(pre, self.ch)
        if not old or getattr(self, "wet_ch", None) != wet_ch:
            self.wet_ch = wet_ch
            k = len(wet_ch)
            self.b_in = np.zeros((self.ch, 8))
            self.b_out = np.zeros((8, self.ch))
            for j, c in enumerate(wet_ch):
                self.b_in[c] = _H[(j + 1) % 8] * math.sqrt(8) / math.sqrt(8 * k)       # ±1 / √k per line
                self.b_out[:, c] = _H[(3 * j + 2) % 8] * math.sqrt(8) / 8 * 2.0
        if not old:
            self.size = int(max(LINES) * 1.0 * self.rate / 44100.0) + SUB + 8
            self.buf = np.zeros((self.size, 8))
            self.w = 0
            self.zi = np.zeros((1, 8))
        self.lens = np.minimum(lens, self.size - SUB - 4)
        self.sub = int(min(SUB, self.lens.min()))

    def _process(self, x):
        n = len(x)
        xp = self.pre(x)
        out = np.empty_like(x)
        cols = np.arange(8)[None, :]
        a = self.damp_a
        pos = 0
        while pos < n:
            s = min(n - pos, self.sub)
            ri = (self.w - self.lens[None, :] + np.arange(s)[:, None]) % self.size
            r = self.buf[ri, cols]
            if a > 0:
                r, self.zi = signal.lfilter([1 - a], [1, -a], r, axis=0, zi=self.zi)
            wi = (self.w + np.arange(s)) % self.size
            self.buf[wi] = xp[pos:pos + s] @ self.b_in + (r * self.g) @ _H.T
            wet = r @ self.b_out
            if self.width < 1.0 and len(self.wet_ch) > 1:
                m = wet[:, self.wet_ch].mean(axis=1, keepdims=True)
                wet[:, self.wet_ch] = self.width * wet[:, self.wet_ch] + (1 - self.width) * m
            out[pos:pos + s] = self.dry * x[pos:pos + s] + self.wet * wet
            self.w = (self.w + s) % self.size
            pos += s
        return out

    def tail_frames(self) -> int:
        return int(min(30 * self.rate, self.pre_n + 1.1 * self.t60 * self.rate + int(self.lens.max())))


class Reverb(Effect):
    key = "reverb"
    name = "fx_reverb"
    has_tail = True
    params = [Param("size", lo=0.0, hi=100.0, default=60.0, unit="%", decimals=0, label="rv_size"),
              Param("decay", lo=0.1, hi=20.0, default=1.8, unit="s", decimals=2, log=True, label="rv_decay"),
              Param("predelay", lo=0.0, hi=250.0, default=15.0, unit="ms", decimals=0, label="rv_predelay"),
              Param("damping", lo=0.0, hi=100.0, default=40.0, unit="%", decimals=0, label="rv_damping"),
              Param("width", lo=0.0, hi=100.0, default=100.0, unit="%", decimals=0, label="rv_width"),
              Param("mix", lo=0.0, hi=100.0, default=25.0, unit="%", decimals=0, label="fx_mix")]
    proc_class = ReverbProc
    presets = {
        "pr_room": {"size": 30.0, "decay": 0.6, "predelay": 5.0, "damping": 50.0, "mix": 20.0},
        "pr_hall": {"size": 85.0, "decay": 2.6, "predelay": 25.0, "damping": 35.0, "mix": 28.0},
        "pr_church": {"size": 100.0, "decay": 5.5, "predelay": 40.0, "damping": 25.0, "mix": 35.0},
        "pr_plate": {"size": 55.0, "decay": 1.6, "predelay": 0.0, "damping": 10.0, "mix": 25.0},
        "pr_vocal": {"size": 50.0, "decay": 1.3, "predelay": 30.0, "damping": 55.0, "mix": 18.0},
    }
