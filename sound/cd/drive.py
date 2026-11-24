"""Reading an Audio CD (2.0): the drives of the computer, a disc's table of contents and CD-TEXT, its sectors of
sound. On Windows straight through the CD-ROM driver (DeviceIoControl: IOCTL_CDROM_READ_TOC_EX, IOCTL_CDROM_RAW_READ),
nothing installed. ARTCORE_FAKE_CD (a JSON file: the sound files of the tracks, the pregaps, the CD-TEXT, sectors that
read wrong the first time) puts in a drive of its own instead — the tests and the self-test."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

import numpy as np

from . import cdtext
from .toc import PREGAP, SECTOR, Toc, Track

CHUNK = 26                     # sectors per read (61 152 bytes: under the 64 KB a driver takes at once)


class DriveError(RuntimeError):
    pass


@dataclass
class DriveInfo:
    path: str                  # "E:" (Windows) or the fake's file
    vendor: str = ""
    product: str = ""

    @property
    def name(self) -> str:
        what = " ".join(x for x in (self.vendor, self.product) if x)
        return f"{self.path} {what}".strip()


class Drive:
    """One drive, open. read(lba, n): n sectors of sound (2352 bytes each, 16 bit little-endian stereo)."""
    info: DriveInfo

    def read_toc(self) -> Toc:
        raise NotImplementedError

    def read_cdtext(self) -> cdtext.CdText | None:
        return None

    def read(self, lba: int, n: int) -> bytes:
        raise NotImplementedError

    def eject(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def drives() -> list[DriveInfo]:
    fake = os.environ.get("ARTCORE_FAKE_CD")
    if fake:
        return [DriveInfo(fake, "ArtCore", "Fake CD")]
    if sys.platform == "win32":
        return _win_drives()
    return []


def open_drive(info: DriveInfo) -> Drive:
    if os.environ.get("ARTCORE_FAKE_CD") and info.path == os.environ["ARTCORE_FAKE_CD"]:
        return FakeDrive.from_file(info.path)
    if sys.platform == "win32":
        return WinDrive(info)
    raise DriveError("no CD drives here")


# ── Windows ───────────────────────────────────────────────────
IOCTL_CDROM_READ_TOC_EX = 0x00024054
IOCTL_CDROM_RAW_READ = 0x0002403E
IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
OPEN_EXISTING = 3
DRIVE_CDROM = 5
CDDA = 2


def _win_drives() -> list[DriveInfo]:
    import ctypes
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    out = []
    for i in range(26):
        if not mask & (1 << i):
            continue
        letter = chr(65 + i) + ":"
        if k32.GetDriveTypeW(letter + "\\") != DRIVE_CDROM:
            continue
        info = DriveInfo(letter)
        try:
            vendor, product = _win_identity(letter)
            info.vendor, info.product = vendor, product
        except OSError:
            pass
        out.append(info)
    return out


def _open_handle(letter: str, write: bool = False):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    access = GENERIC_READ | (GENERIC_WRITE if write else 0)
    h = k32.CreateFileW("\\\\.\\" + letter, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    if h in (None, 0, ctypes.c_void_p(-1).value):
        raise OSError(ctypes.get_last_error() or k32.GetLastError(), f"cannot open {letter}")
    return h


def _ioctl(h, code: int, inp: bytes | None, out_size: int) -> bytes:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.DeviceIoControl.restype = wintypes.BOOL
    ib = ctypes.create_string_buffer(inp, len(inp)) if inp else None
    ob = ctypes.create_string_buffer(max(1, out_size))
    got = wintypes.DWORD(0)
    ok = k32.DeviceIoControl(h, code, ib, len(inp) if inp else 0, ob, out_size, ctypes.byref(got), None)
    if not ok:
        raise OSError(k32.GetLastError(), f"DeviceIoControl {code:#x} failed")
    return ob.raw[:got.value]


def _win_identity(letter: str) -> tuple[str, str]:
    import ctypes
    import struct
    h = _open_handle(letter)
    try:
        raw = _ioctl(h, IOCTL_STORAGE_QUERY_PROPERTY, struct.pack("<II4x", 0, 0), 1024)
    finally:
        ctypes.windll.kernel32.CloseHandle(h)

    def at(off):
        if not off or off >= len(raw):
            return ""
        end = raw.find(b"\0", off)
        return raw[off:end if end >= 0 else None].decode("ascii", "replace").strip()
    vendor_off, product_off = struct.unpack_from("<II", raw, 12)
    return at(vendor_off), at(product_off)


class WinDrive(Drive):
    def __init__(self, info: DriveInfo):
        self.info = info
        try:
            self.h = _open_handle(info.path)
        except OSError as e:
            raise DriveError(str(e)) from e

    def read_toc(self) -> Toc:
        try:
            raw = _ioctl(self.h, IOCTL_CDROM_READ_TOC_EX, bytes((0x00, 1, 0, 0)), 804)
        except OSError as e:
            raise DriveError(f"no disc or not readable ({e.args[0]})") from e
        return parse_toc(raw)

    def read_cdtext(self) -> cdtext.CdText | None:
        try:
            raw = _ioctl(self.h, IOCTL_CDROM_READ_TOC_EX, bytes((0x05, 0, 0, 0)), 0xFFFF)
        except OSError:
            return None
        packs = [raw[i:i + 18] for i in range(4, len(raw) - 17, 18)]
        t = cdtext.decode(packs)
        return None if t.empty() else t

    def read(self, lba: int, n: int) -> bytes:
        import struct
        out = []
        while n > 0:
            k = min(CHUNK, n)
            inp = struct.pack("<qII", lba * 2048, k, CDDA)
            try:
                out.append(_ioctl(self.h, IOCTL_CDROM_RAW_READ, inp, k * SECTOR).ljust(k * SECTOR, b"\0"))
            except OSError as e:
                raise DriveError(f"read error at sector {lba} ({e.args[0]})") from e
            lba += k
            n -= k
        return b"".join(out)

    def eject(self) -> None:
        try:
            _ioctl(self.h, IOCTL_STORAGE_EJECT_MEDIA, None, 0)
        except OSError as e:
            raise DriveError(str(e)) from e

    def close(self) -> None:
        if getattr(self, "h", None):
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self.h)
            self.h = None


def parse_toc(raw: bytes) -> Toc:
    """CDROM_TOC (addresses as LBA) → Toc."""
    if len(raw) < 4:
        raise DriveError("no table of contents")
    first, last = raw[2], raw[3]
    tracks, leadout = [], 0
    for i in range(4, len(raw) - 7, 8):
        ctl = raw[i + 1] & 0x0F
        num = raw[i + 2]
        lba = int.from_bytes(raw[i + 4:i + 8], "big", signed=True)
        if num == 0xAA:
            leadout = lba
            break
        if first <= num <= last:
            tracks.append(Track(num, lba, audio=not ctl & 0x04, preemphasis=bool(ctl & 0x01)))
    if not tracks:
        raise DriveError("no tracks")
    return Toc(tracks, leadout).finish()


# ── the fake drive (tests) ────────────────────────────────────
class FakeDrive(Drive):
    """A disc made of sound: pcm (16 bit stereo little-endian, LBA 0 on), the table of contents, the CD-TEXT;
    offset: the drive's read offset as AccurateRip lists it (asked for frame N it gives the disc's N − offset, so a
    ripper reads offset frames further on); flaky: sectors that read wrong (each `flaky_times` times)."""

    def __init__(self, pcm: bytes, toc: Toc, text: cdtext.CdText | None = None, offset: int = 0,
                 flaky: set | None = None, flaky_times: int = 1, info: DriveInfo | None = None):
        self.pcm, self.toc, self.text, self.offset = pcm, toc, text, offset
        self.flaky = {int(x): flaky_times for x in (flaky or ())}
        self.info = info or DriveInfo("FAKE", "ArtCore", "Fake CD")
        self.reads = 0
        self.ejected = False

    @classmethod
    def from_file(cls, path: str) -> "FakeDrive":
        """{"files": [sound files], "gaps": [sectors before tracks 2…], "album": {…}, "tracks": [{…}],
        "offset": frames, "flaky": [lba…]}"""
        from .toc import from_lengths
        with open(path, encoding="utf-8") as f:
            spec = json.load(f)
        base = os.path.dirname(os.path.abspath(path))
        parts, lengths = [], []
        for name in spec["files"]:
            pcm = decode_pcm(os.path.join(base, name))
            n = -(-len(pcm) // SECTOR)
            parts.append(pcm.ljust(n * SECTOR, b"\0"))
            lengths.append(n)
        gaps = list(spec.get("gaps") or [0] * (len(lengths) - 1))
        toc = from_lengths(lengths, gaps)
        pcm = b""
        for i, p in enumerate(parts):
            if i:
                pcm += b"\0" * (gaps[i - 1] * SECTOR)
            pcm += p
        text = cdtext.CdText(spec.get("album") or {}, spec.get("tracks") or []) if spec.get("album") or \
            spec.get("tracks") else None
        return cls(pcm, toc, text, int(spec.get("offset", 0)), set(spec.get("flaky") or ()),
                   info=DriveInfo(path, "ArtCore", "Fake CD"))

    def read_toc(self) -> Toc:
        if self.ejected:
            raise DriveError("no disc")
        return self.toc

    def read_cdtext(self):
        return self.text

    def read(self, lba: int, n: int) -> bytes:
        self.reads += 1
        start = lba * SECTOR - self.offset * 4
        a, b = start, start + n * SECTOR
        data = self.pcm[max(0, a):max(0, min(len(self.pcm), b))]
        if a < 0:
            data = b"\0" * min(-a, n * SECTOR) + data
        data = data.ljust(n * SECTOR, b"\0")
        bad = [x for x in range(lba, lba + n) if self.flaky.get(x, 0) > 0]
        if bad:
            buf = bytearray(data)
            for x in bad:
                self.flaky[x] -= 1
                i = (x - lba) * SECTOR
                buf[i:i + 64] = bytes((j * 37 + self.reads) & 0xFF for j in range(64))     # a scratch
            data = bytes(buf)
        return data

    def eject(self) -> None:
        self.ejected = True


def decode_pcm(path: str) -> bytes:
    """A sound file → 44.1 kHz 16 bit stereo little-endian (ffmpeg)."""
    import subprocess
    from .. import ff
    r = subprocess.run([ff.ffmpeg(), "-v", "error", "-i", path, "-f", "s16le", "-ac", "2", "-ar", "44100", "-"],
                       capture_output=True, check=True)
    return r.stdout


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, "<i2").reshape(-1, 2).astype(np.float32) / 32768.0


__all__ = ["Drive", "DriveError", "DriveInfo", "FakeDrive", "PREGAP", "drives", "open_drive", "parse_toc"]
