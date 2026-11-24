"""The raw image of an Audio CD (2.0) — what IMAPI2 writes in Disc-At-Once mode (DAO-96: each sector 2352 bytes of
sound and 96 bytes of the P…W subcode, «raw»: byte i holds bit i of P, of Q and the six bits of R…W), from MSF 95:00:00
on, so the lead-in is the program's own: its Q subcode carries the table of contents (each entry three times, round
and round), its R…W the CD-TEXT packs (four in a sector, round and round). Then the 2 s pregap of track 1, the tracks
(with their pregaps — index 0 — between them), the lead-out (90 s).

Built with numpy a few thousand sectors at a time; read() takes an image apart again (the tests, a check before
writing). write_cue() writes the same disc as .cue + .bin for other burning programs."""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np

from . import cdtext
from .toc import PREGAP, SECTOR, Toc

SUB = 96
RAW = SECTOR + SUB
LEADIN_START = -22650           # MSF 95:00:00 as an LBA (lead-in times count back from 100:00:00; LBA 0 is 00:02:00)
LEADOUT = 6750                  # 90 s
BATCH = 2048

_CRC = np.zeros(256, np.uint16)
for _i in range(256):
    _c = _i << 8
    for _k in range(8):
        _c = ((_c << 1) ^ 0x1021) & 0xFFFF if _c & 0x8000 else (_c << 1) & 0xFFFF
    _CRC[_i] = _c


def _bcd(n):
    n = np.asarray(n, np.int64)
    return ((n // 10) << 4) | (n % 10)


def _msf_bcd(frames):
    f = np.asarray(frames, np.int64)
    return _bcd(f // 4500), _bcd(f // 75 % 60), _bcd(f % 75)


def q_crc(q: np.ndarray) -> np.ndarray:
    """CRC of the first 10 bytes of each Q row (N, 12), stored inverted, big-endian."""
    c = np.zeros(len(q), np.uint16)
    for i in range(10):
        c = _CRC[((c >> 8) ^ q[:, i]) & 0xFF] ^ ((c << 8) & 0xFFFF).astype(np.uint16)
    return c ^ 0xFFFF


@dataclass
class Layout:
    """Where everything is on the disc: track starts (index 1), pregap starts (index 0), the lead-out."""
    toc: Toc
    pregaps: np.ndarray          # the first sector of each track's pregap (track 1: −150)
    starts: np.ndarray           # index 1
    numbers: np.ndarray

    @classmethod
    def of(cls, toc: Toc) -> "Layout":
        ts = toc.audio
        starts = np.array([t.start for t in ts], np.int64)
        pre = np.empty_like(starts)
        pre[0] = -PREGAP
        for i in range(1, len(ts)):
            pre[i] = ts[i - 1].start + ts[i - 1].length
        return cls(toc, pre, starts, np.array([t.number for t in ts], np.int64))


def toc_entries(toc: Toc) -> list[tuple[int, int, int, int]]:
    """The lead-in's table of contents: (POINT, PMIN, PSEC, PFRAME) as BCD bytes — the tracks, A0 (the first
    track, a CD-DA disc), A1 (the last), A2 (the lead-out)."""
    out = []
    for t in toc.audio:
        f = t.start + PREGAP
        out.append((int(_bcd(t.number)), int(_bcd(f // 4500)), int(_bcd(f // 75 % 60)), int(_bcd(f % 75))))
    a = toc.audio
    out.append((0xA0, int(_bcd(a[0].number)), 0x00, 0x00))
    out.append((0xA1, int(_bcd(a[-1].number)), 0x00, 0x00))
    f = toc.leadout + PREGAP
    out.append((0xA2, int(_bcd(f // 4500)), int(_bcd(f // 75 % 60)), int(_bcd(f % 75))))
    return out


def subcode(lay: Layout, lba0: int, n: int, packs: list[bytes], leadin_start: int = LEADIN_START) -> np.ndarray:
    """The raw P…W subcode (n, 96) of the sectors lba0 … lba0 + n."""
    toc = lay.toc
    lba = np.arange(lba0, lba0 + n, dtype=np.int64)
    q = np.zeros((n, 12), np.uint8)
    p = np.zeros(n, np.uint8)
    rw = np.zeros((n, SUB), np.uint8)
    q[:, 0] = 0x01                                         # control 0 (2-channel audio), ADR 1
    lead_in = lba < -PREGAP
    if lead_in.any():
        ents = np.array(toc_entries(toc), np.uint8)
        k = lba[lead_in] - leadin_start
        e = ents[(k // 3) % len(ents)]
        q[lead_in, 1] = 0
        q[lead_in, 2] = e[:, 0]
        m, s, f = _msf_bcd(k)
        q[lead_in, 3], q[lead_in, 4], q[lead_in, 5] = m, s, f
        q[lead_in, 7], q[lead_in, 8], q[lead_in, 9] = e[:, 1], e[:, 2], e[:, 3]
        if packs:
            sym = np.array([cdtext.to_symbols(pk) for pk in packs], np.uint8)       # (P, 24)
            idx = (k[:, None] * 4 + np.arange(4)[None, :]) % len(packs)             # 4 packs a sector
            rw[lead_in] = sym[idx].reshape(-1, SUB)
    prog = (lba >= -PREGAP) & (lba < toc.leadout)
    if prog.any():
        lp = lba[prog]
        k = np.searchsorted(lay.pregaps, lp, side="right") - 1
        st = lay.starts[k]
        pause = lp < st
        rel = np.where(pause, st - lp, lp - st)
        q[prog, 1] = _bcd(lay.numbers[k])
        q[prog, 2] = np.where(pause, 0, 1)
        m, s, f = _msf_bcd(rel)
        q[prog, 3], q[prog, 4], q[prog, 5] = m, s, f
        m, s, f = _msf_bcd(lp + PREGAP)
        q[prog, 7], q[prog, 8], q[prog, 9] = m, s, f
        p[prog] = pause
    out = lba >= toc.leadout
    if out.any():
        lo = lba[out]
        q[out, 1] = 0xAA
        q[out, 2] = 1
        m, s, f = _msf_bcd(lo - toc.leadout)
        q[out, 3], q[out, 4], q[out, 5] = m, s, f
        m, s, f = _msf_bcd(lo + PREGAP)
        q[out, 7], q[out, 8], q[out, 9] = m, s, f
        p[out] = ((lo - toc.leadout) // 19) % 2 == 0     # the lead-out's 2 Hz flag
    c = q_crc(q)
    q[:, 10] = c >> 8
    q[:, 11] = c & 0xFF
    qbits = np.unpackbits(q, axis=1)                     # (n, 96)
    return (p[:, None] << 7) | (qbits << 6) | (rw & 0x3F)


def build(path: str, toc: Toc, program, packs: list[bytes] | None = None, progress=None,
          leadin_start: int = LEADIN_START, leadout: int = LEADOUT) -> int:
    """The image into path. program(lba, n) → 2352·n bytes of the program area (LBA 0 … the lead-out: the tracks
    and the pregaps between them). → the number of sectors written."""
    packs = packs or []
    lay = Layout.of(toc)
    end = toc.leadout + leadout
    total = end - leadin_start
    done = 0
    with open(path, "wb") as f:
        lba = leadin_start
        while lba < end:
            n = min(BATCH, end - lba)
            main = np.zeros((n, SECTOR), np.uint8)
            a, b = max(lba, 0), min(lba + n, toc.leadout)
            if b > a:
                data = np.frombuffer(program(a, b - a), np.uint8)
                main[a - lba:b - lba] = data.reshape(b - a, SECTOR)
            sub = subcode(lay, lba, n, packs, leadin_start)
            f.write(np.concatenate((main, sub), axis=1).tobytes())
            lba += n
            done += n
            if progress is not None:
                progress(done / total)
    return total


# ── taking one apart (tests; a check) ─────────────────────────
@dataclass
class Parsed:
    sectors: int
    bad_q: int                          # sectors whose Q CRC is wrong
    toc: list                           # the lead-in's entries seen (POINT, PMIN, PSEC, PFRAME), in order, unique
    packs: list                         # the CD-TEXT packs of the lead-in (unique, in order)
    tracks: dict                        # track number → (first sector of index 0 or None, first sector of index 1)
    leadout: int | None
    pause: np.ndarray                   # P per sector


def read(path: str, leadin_start: int = LEADIN_START) -> Parsed:
    size = os.path.getsize(path)
    n_all = size // RAW
    bad = 0
    entries: list = []
    packs: list = []
    seen_p: set = set()
    tracks: dict = {}
    leadout = None
    pause = np.zeros(n_all, np.uint8)
    with open(path, "rb") as f:
        pos = 0
        while pos < n_all:
            n = min(BATCH, n_all - pos)
            buf = np.frombuffer(f.read(n * RAW), np.uint8).reshape(n, RAW)
            sub = buf[:, SECTOR:]
            pause[pos:pos + n] = sub[:, 0] >> 7
            q = np.packbits((sub >> 6) & 1, axis=1)
            ok = q_crc(q) == ((q[:, 10].astype(np.uint16) << 8) | q[:, 11])
            bad += int((~ok).sum())
            for i in range(n):
                lba = leadin_start + pos + i
                row = q[i]
                if row[1] == 0 and lba < -PREGAP:
                    e = (int(row[2]), int(row[7]), int(row[8]), int(row[9]))
                    if e not in entries:
                        entries.append(e)
                    rw = sub[i] & 0x3F
                    for k in range(4):
                        pk = cdtext.from_symbols(list(rw[k * 24:(k + 1) * 24]))
                        if any(pk) and pk not in seen_p:
                            seen_p.add(pk)
                            packs.append(pk)
                elif row[1] == 0xAA:
                    if leadout is None:
                        leadout = lba
                elif lba >= -PREGAP:
                    tno = int(row[1] >> 4) * 10 + int(row[1] & 15)
                    t = tracks.setdefault(tno, [None, None])
                    if row[2] == 0 and t[0] is None:
                        t[0] = lba
                    if row[2] == 1 and t[1] is None:
                        t[1] = lba
            pos += n
    return Parsed(n_all, bad, entries, packs, {k: tuple(v) for k, v in tracks.items()}, leadout, pause)


def read_audio(path: str, lba: int, n: int, leadin_start: int = LEADIN_START) -> bytes:
    """The sound of n sectors from lba (tests)."""
    with open(path, "rb") as f:
        f.seek((lba - leadin_start) * RAW)
        raw = np.frombuffer(f.read(n * RAW), np.uint8).reshape(n, RAW)
    return raw[:, :SECTOR].tobytes()


# ── .cue + .bin ───────────────────────────────────────────────
def _q(s: str) -> str:
    return '"' + (s or "").replace('"', "'") + '"'


def write_cue(cue_path: str, toc: Toc, program, text: cdtext.CdText | None = None, progress=None) -> str:
    """The program area as a .bin (16 bit little-endian stereo) next to the .cue, with TITLE / PERFORMER (burning
    programs make CD-TEXT of them) and the pregaps as INDEX 00. → the .bin's path."""
    from .toc import msf_text
    bin_path = os.path.splitext(cue_path)[0] + ".bin"
    with open(bin_path, "wb") as f:
        pos = 0
        while pos < toc.leadout:
            n = min(BATCH, toc.leadout - pos)
            f.write(program(pos, n))
            pos += n
            if progress is not None:
                progress(pos / max(1, toc.leadout))
    lines = []
    text = text or cdtext.CdText()
    if text.album.get("performer"):
        lines.append(f"PERFORMER {_q(text.album['performer'])}")
    if text.album.get("title"):
        lines.append(f"TITLE {_q(text.album['title'])}")
    lines.append(f'FILE "{os.path.basename(bin_path)}" BINARY')
    lay = Layout.of(toc)
    for i, t in enumerate(toc.audio):
        lines.append(f"  TRACK {t.number:02d} AUDIO")
        d = text.tracks[i] if i < len(text.tracks) else {}
        if d.get("title"):
            lines.append(f"    TITLE {_q(d['title'])}")
        if d.get("performer"):
            lines.append(f"    PERFORMER {_q(d['performer'])}")
        if t.isrc:
            lines.append(f"    ISRC {t.isrc}")
        if i and lay.pregaps[i] < t.start:
            lines.append(f"    INDEX 00 {msf_text(int(lay.pregaps[i]))}")
        lines.append(f"    INDEX 01 {msf_text(t.start)}")
    with open(cue_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return bin_path
