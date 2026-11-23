"""Errors in plain words: a full disk, a missing or locked file, a path that is too long, what ffmpeg said — for the
messages of the editor, the batch log and the crash report."""
from __future__ import annotations

import errno

from .i18n import t

DISK_FULL_WIN = (39, 112)            # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
FFMPEG_DISK_FULL = ("no space left on device", "disk full", "not enough space")


def is_disk_full(e: BaseException) -> bool:
    if isinstance(e, OSError):
        if e.errno in (errno.ENOSPC, errno.EDQUOT) or getattr(e, "winerror", None) in DISK_FULL_WIN:
            return True
    text = str(e).lower()
    return any(s in text for s in FFMPEG_DISK_FULL)


def human(e: BaseException) -> str:
    """The error as a sentence for the user (the technical text after it when it adds something)."""
    if is_disk_full(e):
        where = getattr(e, "filename", "") or ""
        return t("err_disk_full", where) if where else t("err_disk_full_any")
    if isinstance(e, MemoryError):
        return t("err_memory")
    if isinstance(e, FileNotFoundError):
        return t("err_not_found", e.filename or str(e))
    if isinstance(e, PermissionError):
        return t("err_access", e.filename or str(e))
    if isinstance(e, OSError) and (e.errno == errno.ENAMETOOLONG or getattr(e, "winerror", None) == 206):
        return t("err_long_path", e.filename or "")
    msg = str(e).strip()
    if msg in ("no audio",):
        return t("err_no_audio")
    if msg == "silence":
        return t("err_silence")
    if msg == "ffmpeg not found" or msg == "ffprobe":
        return t("err_no_ffmpeg")
    if "invalid data found" in msg.lower() or "could not find codec" in msg.lower() or \
            "moov atom not found" in msg.lower():
        return t("err_broken", msg.splitlines()[-1])
    return msg or type(e).__name__
