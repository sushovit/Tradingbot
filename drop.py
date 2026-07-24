"""
drop.py — one-command, drop-ready session file.

    python drop.py

Runs the full session bundle — report + universe + floor + today's intern
report (if it exists) — and writes ONE ASCII-safe, UTF-8 file:

    drop\\session_ET<date>_<time>.md     (ET-stamped name)
    drop\\latest.md                      (fixed name, overwritten each run)

The file is written with an explicit encoding="utf-8" open() — NEVER via
shell redirection (PowerShell's > writes UTF-16 and produces mojibake).
Emoji/box characters are replaced with plain ASCII (OK/NO/WARN...) so the
file survives any viewer. The dual-timezone self-dating header stays on top.
"""

import os
import shutil
import subprocess
import sys
import time

import clockline

DROP_DIR = "drop"
SCRIPT_TIMEOUT = 180
SECTION_SCRIPTS = ["report.py", "universe.py", "floor.py"]

# Known glyph -> ASCII. Anything else non-ASCII is dropped.
_ASCII_MAP = {
    "🟢": "OK", "⚪": "idle", "⚠️": "WARN", "⚠": "WARN", "✅": "OK",
    "❌": "NO", "⛔": "BLOCKED", "—": "-", "–": "-", "·": "-", "•": "-",
    "…": "...", "≥": ">=", "≤": "<=", "×": "x", "→": "->", "←": "<-",
    "📊": "", "📋": "", "🎓": "", "🧠": "", "📄": "", "🏛️": "", "🔬": "",
    "🤖": "", "📈": "", "🚀": "", "🛑": "", "💬": "", "🌍": "", "📰": "",
    "🗂️": "", "🔔": "",
}


def asciify(text: str) -> str:
    """Replace known glyphs with ASCII; drop any other non-ASCII byte."""
    for glyph, repl in _ASCII_MAP.items():
        text = text.replace(glyph, repl)
    return text.encode("ascii", errors="ignore").decode("ascii")


def assemble(sections) -> str:
    """sections: list of (title, text). Header first, sections in order."""
    parts = ["# Session drop", clockline.two_zone_line(), ""]
    for title, text in sections:
        parts.append(f"\n{'=' * 60}\n## {title}\n{'=' * 60}\n")
        parts.append(text.rstrip())
    combined = "\n".join(parts) + "\n"
    return asciify(clockline.annotate_age(combined))


def write_drop(content: str, drop_dir: str = DROP_DIR):
    """Write the ET-stamped file + the fixed latest.md. Explicit UTF-8."""
    os.makedirs(drop_dir, exist_ok=True)
    stamp = clockline.now_et().strftime("%Y-%m-%d_%H%M")
    session_path = os.path.join(drop_dir, f"session_ET{stamp}.md")
    with open(session_path, "w", encoding="utf-8") as f:
        f.write(content)
    latest_path = os.path.join(drop_dir, "latest.md")
    shutil.copyfile(session_path, latest_path)
    return session_path, latest_path


def run_script(script: str) -> str:
    """Run one generator, capture its output (child forced to UTF-8)."""
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run([sys.executable, script], timeout=SCRIPT_TIMEOUT,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        out = r.stdout or ""
        if r.returncode != 0:
            out += f"\n[{script} exited with code {r.returncode}]"
            if r.stderr:
                out += f"\n{r.stderr[-400:]}"
        return out
    except subprocess.TimeoutExpired:
        return f"[{script} TIMED OUT after {SCRIPT_TIMEOUT}s]"


def todays_intern_report() -> str:
    path = os.path.join("reports",
                        f"intern_{clockline.now_et():%Y-%m-%d}.md")
    if not os.path.exists(path):
        return "(no intern report for today yet)"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def main() -> int:
    sections = []
    for script in SECTION_SCRIPTS:
        started = time.monotonic()
        print(f"running {script} ...", flush=True)
        sections.append((script.replace(".py", ""), run_script(script)))
        print(f"  done ({int(time.monotonic() - started)}s)", flush=True)
    print("collecting intern report ...", flush=True)
    sections.append(("intern desk (today)", todays_intern_report()))

    session_path, latest_path = write_drop(assemble(sections))
    print(f"\nWrote {session_path}")
    print(f"Wrote {latest_path} - upload this file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
