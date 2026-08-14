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

from dotenv import load_dotenv

import clockline

load_dotenv()   # DISCORD_WEBHOOK_URL for the --discord notification

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


def latest_review_memo() -> str:
    """The newest review_*.md, so the CEO memo rides along with the session
    bundle instead of living only in Discord."""
    import glob
    memos = sorted(glob.glob(os.path.join("reports", "review_*.md")))
    if not memos:
        return "(no review memo yet — review_bot runs ~16:30 ET)"
    with open(memos[-1], "r", encoding="utf-8", errors="replace") as f:
        return f"_Source: {os.path.basename(memos[-1])}_\n\n" + f.read()


def todays_intern_report() -> str:
    path = os.path.join("reports",
                        f"intern_{clockline.now_et():%Y-%m-%d}.md")
    if not os.path.exists(path):
        return "(no intern report for today yet)"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def post_discord(session_path: str, latest_path: str, content: str):
    """Ping Discord that the drop file is ready, with the file attached so a
    phone can grab it too. One post per run; failure never fails the run."""
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        print("(no webhook configured - skipping Discord notification)")
        return
    try:
        from discord_webhook import DiscordWebhook
        headline = ""
        for line in content.splitlines():
            if "Effective capital" in line:
                headline = " - " + line.replace("**", "").strip()
                break
        msg = (f"[DROP] Session file ready {clockline.two_zone_line()}{headline}\n"
               f"Upload `drop\\latest.md` (attached below).")
        wh = DiscordWebhook(url=url, content=msg[:1900])
        with open(session_path, "rb") as f:
            wh.add_file(file=f.read(), filename=os.path.basename(session_path))
        resp = wh.execute()
        print(f"Discord notified (HTTP {getattr(resp, 'status_code', '?')})")
    except Exception as e:
        print(f"(Discord notification failed: {e}) - file is still at {latest_path}")


def main() -> int:
    to_discord = "--discord" in sys.argv
    sections = []
    for script in SECTION_SCRIPTS:
        started = time.monotonic()
        print(f"running {script} ...", flush=True)
        sections.append((script.replace(".py", ""), run_script(script)))
        print(f"  done ({int(time.monotonic() - started)}s)", flush=True)
    print("collecting intern report ...", flush=True)
    sections.append(("intern desk (today)", todays_intern_report()))
    # The CEO memo rides along as the FINAL section of every session bundle.
    print("collecting review memo ...", flush=True)
    sections.append(("CEO REVIEW MEMO (latest)", latest_review_memo()))

    content = assemble(sections)
    session_path, latest_path = write_drop(content)
    print(f"\nWrote {session_path}")
    print(f"Wrote {latest_path} - upload this file.")
    if to_discord:
        post_discord(session_path, latest_path, content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
