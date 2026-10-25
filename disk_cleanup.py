"""
disk_cleanup.py - the ONE routine disk cleanup (CEO 2026-10-25).

Every cleanup goes through here, so the protected paths below are never
touched. 2026-10-24 a cleanup removed the worktree that held the year of
5-minute study bars; they now live in data/bars_5m/ and are on KEEP.

    python disk_cleanup.py            # dry run: what would be freed
    python disk_cleanup.py --apply    # gzip logs older than 30 days, purge pip cache

It never deletes data, positions, the journal, .env, models or anything the
owner keeps outside this folder.
"""

import gzip
import os
import shutil
import subprocess
import sys
import time

# Never deleted, moved or compressed by any cleanup.
KEEP = (
    "data/bars_5m",          # permanent 5-minute bar store (studies)
    "journal.db",
    "positions.json",
    "bot_config.json",
    ".env",
    "universe_today.json",
    "sell_pin_state.json",
)
LOG_DIRS = ("logs", "reports")
GZIP_AFTER_DAYS = 30


def is_kept(path: str, root: str = ".") -> bool:
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    return any(rel == k or rel.startswith(k.rstrip("/") + os.sep) for k in KEEP)


def old_logs(root: str = ".", days: int = GZIP_AFTER_DAYS, now: float = None):
    cutoff = (now or time.time()) - days * 86400
    for d in LOG_DIRS:
        for dirpath, _dirs, files in os.walk(os.path.join(root, d)):
            for f in files:
                p = os.path.join(dirpath, f)
                if f.endswith(".gz") or is_kept(p, root):
                    continue
                try:
                    if os.path.getmtime(p) < cutoff:
                        yield p
                except OSError:
                    continue


def gzip_file(p: str):
    with open(p, "rb") as src, gzip.open(p + ".gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.remove(p)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    apply = "--apply" in argv
    logs = list(old_logs())
    size = sum(os.path.getsize(p) for p in logs)
    print(f"{len(logs)} log files older than {GZIP_AFTER_DAYS} days ({size / 1e6:,.1f} MB)"
          f"{'' if apply else ' - dry run'}")
    print("protected: " + ", ".join(KEEP))
    if apply:
        for p in logs:
            gzip_file(p)
        subprocess.run([sys.executable, "-m", "pip", "cache", "purge"], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
