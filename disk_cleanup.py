"""
disk_cleanup.py - the ONE routine disk cleanup (CEO 2026-10-25).

Every cleanup goes through here, so the protected paths below are never
touched. 2026-10-24 a cleanup removed the worktree that held the year of
5-minute study bars; they now live in data/bars_5m/ and are on KEEP.

    python disk_cleanup.py            # dry run: what would be freed
    python disk_cleanup.py --apply    # do it (weekly launchd job, Sat 12:00 ET)

What it does:
  * rotates any log over ROTATE_MB (copy to <name>.<YYYYMMDD>.gz, then
    truncate in place, so a process holding the log open keeps writing);
  * gzips logs/reports older than GZIP_AFTER_DAYS;
  * clears tool leftovers: the venv pip cache, Hugging Face caches, the
    Ollama app's update cache, and Ollama blobs no model manifest references.
It never deletes data, positions, the journal, .env, a model in use, or
anything the owner keeps outside this folder.
"""

import glob
import gzip
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

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
ROTATE_MB = 2.0
HOME = os.path.expanduser("~")
LEFTOVER_DIRS = (
    os.path.join(HOME, ".cache", "huggingface"),
    os.path.join(HOME, "Library", "Caches", "huggingface"),
    os.path.join(HOME, "Library", "Caches", "ollama"),       # Ollama app updates
)
OLLAMA_MODELS = os.path.join(HOME, ".ollama", "models")


def is_kept(path: str, root: str = ".") -> bool:
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    return any(rel == k or rel.startswith(k.rstrip("/") + os.sep) for k in KEEP)


def _logs(root: str = "."):
    for d in LOG_DIRS:
        for dirpath, _dirs, files in os.walk(os.path.join(root, d)):
            for f in files:
                p = os.path.join(dirpath, f)
                if not f.endswith(".gz") and not is_kept(p, root):
                    yield p


def old_logs(root: str = ".", days: int = GZIP_AFTER_DAYS, now: float = None):
    cutoff = (now or time.time()) - days * 86400
    for p in _logs(root):
        try:
            if os.path.getmtime(p) < cutoff:
                yield p
        except OSError:
            continue


def big_logs(root: str = ".", mb: float = ROTATE_MB):
    for p in _logs(root):
        try:
            if p.endswith(".log") and os.path.getsize(p) > mb * 1e6:
                yield p
        except OSError:
            continue


def gzip_file(p: str):
    with open(p, "rb") as src, gzip.open(p + ".gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.remove(p)


def rotate(p: str, stamp: str = None) -> str:
    """Copy-truncate: the old content goes to <p>.<stamp>.gz, the file stays
    (same inode) and starts empty."""
    stamp = stamp or datetime.now().strftime("%Y%m%d")
    dst = f"{p}.{stamp}.gz"
    n = 1
    while os.path.exists(dst):
        dst = f"{p}.{stamp}-{n}.gz"
        n += 1
    with open(p, "rb") as src, gzip.open(dst, "wb") as out:
        shutil.copyfileobj(src, out)
    with open(p, "r+b") as f:
        f.truncate(0)
    return dst


def ollama_orphans(models: str = OLLAMA_MODELS) -> list:
    """Blobs that no manifest references (never a blob of an installed model)."""
    blobs = os.path.join(models, "blobs")
    if not os.path.isdir(blobs):
        return []
    refs = set()
    for m in glob.glob(os.path.join(models, "manifests", "**", "*"), recursive=True):
        if not os.path.isfile(m):
            continue
        try:
            with open(m, encoding="utf-8") as f:
                j = json.load(f)
        except (OSError, ValueError):
            return []                    # unreadable manifest: delete nothing
        for layer in [j.get("config") or {}] + list(j.get("layers") or []):
            if layer.get("digest"):
                refs.add(layer["digest"].replace(":", "-"))
    if not refs:
        return []
    return sorted(os.path.join(blobs, b) for b in os.listdir(blobs)
                  if b not in refs and not b.endswith("-partial"))


def _size(p: str) -> int:
    if os.path.isfile(p):
        return os.path.getsize(p)
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(p) for f in fs
               if os.path.isfile(os.path.join(d, f)))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    apply = "--apply" in argv
    mode = "" if apply else " (dry run)"
    big = list(big_logs())
    old = [p for p in old_logs() if p not in big]
    left = [d for d in LEFTOVER_DIRS if os.path.isdir(d)]
    orph = ollama_orphans()
    print(f"rotate {len(big)} log(s) over {ROTATE_MB:g} MB "
          f"({sum(_size(p) for p in big) / 1e6:,.1f} MB){mode}: "
          + ", ".join(os.path.basename(p) for p in big))
    print(f"gzip {len(old)} file(s) older than {GZIP_AFTER_DAYS} days "
          f"({sum(_size(p) for p in old) / 1e6:,.1f} MB){mode}")
    print(f"leftover caches {sum(_size(d) for d in left) / 1e6:,.0f} MB{mode}: "
          + (", ".join(left) or "none"))
    print(f"Ollama orphan blobs {len(orph)} ({sum(_size(p) for p in orph) / 1e6:,.0f} MB){mode}")
    print("protected: " + ", ".join(KEEP))
    if apply:
        for p in big:
            rotate(p)
        for p in old:
            gzip_file(p)
        for d in left:
            shutil.rmtree(d, ignore_errors=True)
        for p in orph:
            os.remove(p)
        subprocess.run([sys.executable, "-m", "pip", "cache", "purge"], check=False,
                       capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
