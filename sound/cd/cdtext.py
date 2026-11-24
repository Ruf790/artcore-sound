"""CD-TEXT (2.0): the album's and the tracks' titles and performers as the packs of a disc's lead-in, and back.

A pack is 18 bytes: type (0x80 title, 0x81 performer, 0x82 songwriter, 0x83 composer, 0x84 arranger, 0x85 message,
0x86 disc ID, 0x87 genre, 0x8E UPC / ISRC, 0x8F the size information), track (0 — the album), sequence number,
the block and character position, 12 bytes of text (strings ending with NUL, one after another — album, track 1,
track 2…), a CRC. One block (language), ISO 8859-1: Cyrillic does not exist in CD-TEXT, so it is spelt in Latin
letters (translit()) — players would show rubbish otherwise."""
from __future__ import annotations

from dataclasses import dataclass, field

TITLE, PERFORMER, SONGWRITER, COMPOSER, ARRANGER, MESSAGE, DISC_ID, GENRE, UPC_ISRC, SIZE = \
    0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87, 0x8E, 0x8F
KINDS = {TITLE: "title", PERFORMER: "performer", SONGWRITER: "songwriter", COMPOSER: "composer",
         ARRANGER: "arranger", MESSAGE: "message"}
LANG_ENGLISH = 0x09
PACK = 18


def crc16(data: bytes) -> int:
    """CRC-16/CCITT (x^16 + x^12 + x^5 + 1, from 0) — of the Q subcode and of the CD-TEXT packs (stored inverted)."""
    c = 0
    for b in data:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


_RU = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i",
       "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
       "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e",
       "ю": "yu", "я": "ya", "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ў": "u"}
_OTHER = {"«": '"', "»": '"', "„": '"', "“": '"', "”": '"', "‘": "'", "’": "'", "—": "-", "–": "-", "…": "...",
          "№": "No.", " ": " "}


def translit(s: str) -> str:
    """Cyrillic in Latin letters («Кино — Группа крови» → «Kino - Gruppa krovi»); what ISO 8859-1 still lacks
    becomes «?»."""
    out = []
    for ch in s:
        low = ch.lower()
        if low in _RU:
            r = _RU[low]
            if ch != low and r:
                r = r[0].upper() + r[1:]
            out.append(r)
        else:
            out.append(_OTHER.get(ch, ch))
    text = "".join(out)
    return text.encode("latin-1", "replace").decode("latin-1")


def latin1(s: str) -> bytes:
    return (s or "").encode("latin-1", "replace")


@dataclass
class CdText:
    """album: {"title": …, "performer": …, …}; tracks: one such dict per track (number order)."""
    album: dict = field(default_factory=dict)
    tracks: list = field(default_factory=list)

    def empty(self) -> bool:
        return not any(v for d in [self.album] + self.tracks for v in d.values())

    def spelled(self) -> "CdText":
        """Every text in Latin letters."""
        return CdText({k: translit(v) for k, v in self.album.items()},
                      [{k: translit(v) for k, v in d.items()} for d in self.tracks])


def encode(text: CdText, first_track: int = 1) -> list[bytes]:
    """The packs of one block (ISO 8859-1, English), the size information last."""
    packs: list[bytes] = []
    counts = [0] * 16
    seq = 0
    last = first_track + max(0, len(text.tracks) - 1)
    for kind in (TITLE, PERFORMER, SONGWRITER, COMPOSER, ARRANGER, MESSAGE):
        name = KINDS[kind]
        values = [text.album.get(name, "")] + [d.get(name, "") for d in text.tracks]
        if not any(values):
            continue
        stream = b""
        owners = []                                       # for each byte of the stream: its track and position
        for i, v in enumerate(values):
            b = latin1(v) + b"\0"
            stream += b
            owners += [(i if i == 0 else first_track + i - 1, j) for j in range(len(b))]
        for off in range(0, len(stream), 12):
            chunk = stream[off:off + 12].ljust(12, b"\0")
            trk, pos = owners[off]
            head = bytes((kind, trk, seq, min(15, pos)))
            body = head + chunk
            c = crc16(body) ^ 0xFFFF
            packs.append(body + bytes((c >> 8, c & 0xFF)))
            counts[kind - 0x80] += 1
            seq += 1
    if not packs:
        return []
    # the size information: three packs (36 bytes)
    counts[SIZE - 0x80] = 3
    info = bytes((0x00, first_track, last, 0x00)) + bytes(counts) + bytes((seq + 2, 0, 0, 0, 0, 0, 0, 0)) + \
        bytes((LANG_ENGLISH, 0, 0, 0, 0, 0, 0, 0))
    for k in range(3):
        head = bytes((SIZE, k, seq, 0))
        body = head + info[k * 12:(k + 1) * 12]
        c = crc16(body) ^ 0xFFFF
        packs.append(body + bytes((c >> 8, c & 0xFF)))
        seq += 1
    return packs


def pack_ok(p: bytes) -> bool:
    return len(p) == PACK and (crc16(p[:16]) ^ 0xFFFF) == (p[16] << 8 | p[17])


def decode(packs: list[bytes], tracks: int | None = None) -> CdText:
    """Packs (as read from a disc: any order, repeated, some damaged) → the texts of block 0."""
    seen: dict = {}
    for p in packs:
        if len(p) < PACK or not (0x80 <= p[0] <= 0x8F) or (p[3] >> 4) & 7:
            continue
        if not pack_ok(p[:PACK]):
            continue
        seen.setdefault(p[2], bytes(p[:PACK]))
    out = CdText()
    by_kind: dict = {}
    for _s, p in sorted(seen.items()):
        by_kind.setdefault(p[0], []).append(p)
    n_tracks = tracks
    if SIZE in by_kind and n_tracks is None:
        info = b"".join(p[4:16] for p in sorted(by_kind[SIZE], key=lambda p: p[1]))
        if len(info) >= 3:
            n_tracks = info[2] - info[1] + 1
    for kind, name in KINDS.items():
        ps = by_kind.get(kind)
        if not ps:
            continue
        stream = b"".join(p[4:16] for p in ps)
        first = ps[0][1]
        values = stream.split(b"\0")
        idx = first
        for v in values:
            if idx > 99:
                break
            text = v.decode("latin-1")
            if text == "\t" and idx:                      # «the same as the one before»
                text = _get(out, idx - 1, name)
            _put(out, idx, name, text)
            idx += 1
    if n_tracks is not None:
        while len(out.tracks) < n_tracks:
            out.tracks.append({})
        out.tracks = out.tracks[:n_tracks]
    return out


def _get(t: CdText, idx: int, name: str) -> str:
    d = t.album if idx == 0 else (t.tracks[idx - 1] if idx - 1 < len(t.tracks) else {})
    return d.get(name, "")


def _put(t: CdText, idx: int, name: str, text: str) -> None:
    if idx == 0:
        if text:
            t.album[name] = text
        return
    while len(t.tracks) < idx:
        t.tracks.append({})
    if text:
        t.tracks[idx - 1][name] = text


def to_symbols(pack: bytes) -> list[int]:
    """18 bytes → 24 six-bit symbols (R…W of 24 subcode symbols), most significant bits first."""
    bits = int.from_bytes(pack, "big")
    return [(bits >> (138 - 6 * i)) & 0x3F for i in range(24)]


def from_symbols(sym: list[int]) -> bytes:
    bits = 0
    for s in sym[:24]:
        bits = (bits << 6) | (int(s) & 0x3F)
    return bits.to_bytes(PACK, "big")
