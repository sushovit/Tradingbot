"""
safe_io.py — crash-safe file writes and corruption-tolerant reads.

Incident 2026-07-29: the machine took four unclean shutdowns in 48h. One
landed mid-write on bot_status.log, leaving 1,284 bytes of NUL — NTFS had
extended the file's size but the data never flushed. floor.py then rendered
the garbage.

atomic_write_text() writes a PRIVATE temp file, flushes + fsyncs it, then
os.replace()s it over the target. os.replace is atomic on Windows and POSIX,
so a reader either sees the whole old file or the whole new one.

Windows failure modes, both seen live:
  WinError 32 (destination locked) — a reader holds the target open (the
      dashboard polls status every 15s, floor.py reads it, AV scans it).
      Retrying the replace works: the lock is transient.
  WinError 2 (source missing at rename) — the temp file vanished between
      write and rename, almost certainly an antivirus scanner quarantining
      or holding it. Retrying the replace is POINTLESS here: the source is
      gone, so every retry fails identically. That case now rebuilds the
      temp file instead, and the temp name is unique per process+timestamp
      so nothing else can collide with or reuse it.

If every strategy fails the write still happens (in place, non-atomic) so
the heartbeat survives — but the degradation is JOURNALED so the reviewer
sees that write integrity was compromised rather than silently trusting it.
"""

import logging
import os
import time

logger = logging.getLogger(__name__)

REPLACE_ATTEMPTS = 3
REPLACE_BACKOFF = 0.05          # 50ms, 100ms, 150ms
TMP_SWEEP_AGE = 600             # orphaned temp files older than 10 min


def _tmp_name(path: str) -> str:
    """Unique per process and instant: nothing else can grab or reuse it."""
    return f"{path}.{os.getpid()}.{time.time_ns()}.tmp"


def _write_tmp(tmp: str, text: str, encoding: str):
    with open(tmp, "w", encoding=encoding) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())        # durable before the swap


def _sweep_orphans(path: str, max_age: int = TMP_SWEEP_AGE):
    """Unique temp names cannot self-clean by being overwritten, so drop
    any leftovers from crashed runs."""
    directory = os.path.dirname(os.path.abspath(path))
    prefix = os.path.basename(path) + "."
    now = time.time()
    try:
        entries = os.listdir(directory)
    except OSError:
        return
    for name in entries:
        if not (name.startswith(prefix) and name.endswith(".tmp")):
            continue
        full = os.path.join(directory, name)
        try:
            if now - os.path.getmtime(full) > max_age:
                os.remove(full)
        except OSError:
            pass


def _journal_degradation(path: str, error, strategy: str):
    """Record that a write could not be made atomically. Best-effort and
    lazily imported: safe_io is the lowest layer and must never fail a
    write because journalling failed."""
    try:
        import journal
        journal.log_integrity_event(
            "write_integrity_degraded",
            f"{os.path.basename(path)}: atomic replace failed ({strategy}: "
            f"{type(error).__name__}: {error}); wrote in place instead")
    except Exception as e:                      # noqa: BLE001
        logger.warning(f"could not journal write degradation: {e}")


def atomic_write_text(path: str, text: str, encoding: str = "utf-8"):
    """Write text so an interrupted write can never corrupt `path`."""
    _sweep_orphans(path)
    tmp = _tmp_name(path)
    _write_tmp(tmp, text, encoding)

    last_err = None
    rebuilt = False
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)               # atomic on Windows + POSIX
            return
        except FileNotFoundError as e:
            # WinError 2: the SOURCE vanished (AV). Retrying the rename is
            # useless — rebuild the temp file once, then try again.
            last_err = e
            if rebuilt:
                break
            rebuilt = True
            tmp = _tmp_name(path)
            try:
                _write_tmp(tmp, text, encoding)
            except OSError as write_err:
                last_err = write_err
                break
            continue
        except OSError as e:                    # WinError 32: dest locked
            last_err = e
            time.sleep(REPLACE_BACKOFF * (attempt + 1))

    # Every strategy failed: write in place so the heartbeat survives, and
    # make the degradation visible instead of silent.
    strategy = "source vanished" if isinstance(last_err, FileNotFoundError) \
        else "destination locked"
    try:
        with open(path, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
        logger.warning(f"atomic replace failed for {path} ({strategy}: "
                       f"{last_err}); wrote in place instead")
        _journal_degradation(path, last_err, strategy)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


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
