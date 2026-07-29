"""
safe_io.py — crash-safe file writes and corruption-tolerant reads.

Incident 2026-07-29: the machine took four unclean shutdowns in 48h. One
landed mid-write on bot_status.log, leaving 1,284 bytes of NUL — NTFS had
extended the file's size but the data never flushed. floor.py then rendered
the garbage.

atomic_write_text() writes a sibling .tmp, flushes + fsyncs it, then
os.replace()s it over the target. os.replace is atomic on Windows and POSIX,
so a reader either sees the whole old file or the whole new one — a dying
process can no longer corrupt the target.
"""

import os


def atomic_write_text(path: str, text: str, encoding: str = "utf-8"):
    """Write text so an interrupted write can never corrupt `path`."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding=encoding) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())        # durable before the swap
    os.replace(tmp, path)           # atomic on Windows + POSIX


def read_text_tolerant(path: str, encoding: str = "utf-8") -> str:
    """Read a file that may contain NUL bytes from an interrupted write.
    NULs are stripped and any line left empty by them is dropped, so a
    corrupted status file degrades to 'nothing to show' instead of
    rendering control characters."""
    with open(path, "r", encoding=encoding, errors="replace") as f:
        raw = f.read()
    if "\x00" in raw:
        raw = raw.replace("\x00", "")
    return "\n".join(ln for ln in raw.splitlines() if ln.strip())


def is_corrupted(path: str) -> bool:
    """True if the file exists but contains NUL bytes (interrupted write)."""
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read()
    except OSError:
        return False
