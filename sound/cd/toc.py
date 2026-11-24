"""A disc's table of contents (2.0, Audio CD): tracks by their first sector (LBA), the lead-out, the lengths of the
audio tracks (an Enhanced CD's data track and the gap before its session left out), times as MM:SS:FF, the
MusicBrainz disc ID.

A sector is 2352 bytes of sound: 588 stereo frames at 44.1 kHz, 16 bit; 75 sectors a second."""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field

RATE = 44100
FRAMES_PER_SECTOR = 588
SECTOR = 2352
PREGAP = 150                    # the 2 s before track 1 (LBA 0 is MSF 00:02:00)
SESSION_GAP = 11400             # the lead-out + lead-in + pregap between an audio session and a data one
MAX_SECTORS = 80 * 60 * 75      # an 80-minute disc


def msf(lba: int, absolute: bool = True) -> tuple[int, int, int]:
    """LBA → (minutes, seconds, frames); absolute adds the 2 s pregap (as the disc's own times do)."""
    n = lba + (PREGAP if absolute else 0)
    if n < 0:
        n += 100 * 60 * 75                       # the lead-in counts down from 99:59:74
    return n // 4500, n // 75 % 60, n % 75


def msf_text(lba: int, absolute: bool = False) -> str:
    m, s, f = msf(lba, absolute)
    return f"{m:02d}:{s:02d}:{f:02d}"


def bcd(n: int) -> int:
    return ((n // 10) << 4) | (n % 10)


def unbcd(b: int) -> int:
    return (b >> 4) * 10 + (b & 15)


@dataclass
class Track:
    number: int
    start: int                  # LBA of index 1
    audio: bool = True
    preemphasis: bool = False
    length: int = 0             # sectors (audio tracks)
    isrc: str = ""

    @property
    def seconds(self) -> float:
        return self.length / 75.0


@dataclass
class Toc:
    tracks: list = field(default_factory=list)
    leadout: int = 0            # LBA of the lead-out

    def finish(self) -> "Toc":
        """Each track's length from the next start (an audio track before a data session ends SESSION_GAP
        earlier)."""
        ts = sorted(self.tracks, key=lambda t: t.number)
        for i, t in enumerate(ts):
            end = ts[i + 1].start if i + 1 < len(ts) else self.leadout
            if i + 1 < len(ts) and t.audio and not ts[i + 1].audio:
                end -= SESSION_GAP
            t.length = max(0, end - t.start)
        self.tracks = ts
        return self

    @property
    def audio(self) -> list:
        return [t for t in self.tracks if t.audio]

    @property
    def first(self) -> int:
        return self.tracks[0].number if self.tracks else 0

    @property
    def last(self) -> int:
        return self.tracks[-1].number if self.tracks else 0

    def audio_end(self) -> int:
        """The sector after the last audio track."""
        a = self.audio
        return a[-1].start + a[-1].length if a else 0

    def track(self, number: int) -> Track | None:
        return next((t for t in self.tracks if t.number == number), None)

    def musicbrainz_id(self) -> str:
        """The MusicBrainz disc ID (audio tracks only; an Enhanced CD's lead-out is the audio session's)."""
        a = self.audio
        if not a:
            return ""
        lead = self.leadout if a[-1] is self.tracks[-1] else a[-1].start + a[-1].length
        s = f"{a[0].number:02X}{a[-1].number:02X}{lead + PREGAP:08X}"
        offs = {t.number: t.start + PREGAP for t in a}
        s += "".join(f"{offs.get(i, 0):08X}" for i in range(1, 100))
        h = hashlib.sha1(s.encode("ascii")).digest()
        return base64.b64encode(h).decode("ascii").replace("+", ".").replace("/", "_").replace("=", "-")

    def musicbrainz_toc(self) -> str:
        """The «toc» argument of the MusicBrainz lookup (a fuzzy match when the ID is unknown)."""
        a = self.audio
        if not a:
            return ""
        lead = self.leadout if a[-1] is self.tracks[-1] else a[-1].start + a[-1].length
        return " ".join(str(x) for x in [a[0].number, a[-1].number, lead + PREGAP] + [t.start + PREGAP for t in a])

    def freedb_id(self) -> str:
        a = self.audio
        if not a:
            return ""

        def digits(n):
            return sum(int(c) for c in str(n))
        n = sum(digits((t.start + PREGAP) // 75) for t in a)
        lead = a[-1].start + a[-1].length
        secs = (lead + PREGAP) // 75 - (a[0].start + PREGAP) // 75
        return f"{(n % 255) << 24 | secs << 8 | len(a):08x}"


def from_lengths(lengths: list[int], gaps: list[int] | None = None) -> Toc:
    """A disc of audio tracks of these lengths (sectors), track 1 at LBA 0 after the 2 s pregap; gaps[i]: the
    pregap (index 0) before track i + 2."""
    gaps = gaps or [0] * max(0, len(lengths) - 1)
    pos = 0
    tracks = []
    for i, n in enumerate(lengths):
        if i:
            pos += gaps[i - 1]
        tracks.append(Track(i + 1, pos, length=n))
        pos += n
    return Toc(tracks, pos)
