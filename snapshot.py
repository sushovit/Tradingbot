"""snapshot.py — run scan.py, archive to reports/, post to Discord.

Filenames are ET-anchored (scan_ET<date>_<time>.md) so the filename and the
report header can never disagree about which session a file belongs to.
Report age is annotated at post time via clockline.annotate_age."""
import subprocess
import sys
import os
from datetime import datetime

from dotenv import load_dotenv

import clockline

load_dotenv()
os.makedirs("reports", exist_ok=True)
stamp = datetime.now(clockline.ET).strftime("%Y-%m-%d_%H%M")
out_path = f"reports/scan_ET{stamp}.md"

# scan.py gives report.py and universe.py 180s each — outer timeout must
# exceed their sum or a slow broker day kills the snapshot mid-write.
# PYTHONUTF8 forces the child to emit UTF-8; on Windows a redirected stdout
# otherwise defaults to cp1252 and the em-dashes corrupt the file.
child_env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
with open(out_path, "w", encoding="utf-8") as f:
    try:
        subprocess.run([sys.executable, "scan.py"], stdout=f,
                       stderr=subprocess.STDOUT, timeout=420, env=child_env)
    except subprocess.TimeoutExpired:
        f.write("\n[snapshot: scan.py timed out — partial output above]\n")

# Age annotation at PRINT time: a stale file self-identifies when read.
try:
    with open(out_path, "r", encoding="utf-8", errors="replace") as f:
        annotated = clockline.annotate_age(f.read())
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(annotated)
except OSError:
    pass

url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
if url:
    try:
        from discord_webhook import DiscordWebhook
        # headline: pull the equity line if present
        headline = f"📊 Scan ET{stamp}"
        with open(out_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "Effective capital" in line:
                    headline += f" — {line.strip().replace('**', '')}"
                    break
        wh = DiscordWebhook(url=url, content=headline)
        with open(out_path, "rb") as f:
            wh.add_file(file=f.read(), filename=f"scan_{stamp}.md")
        resp = wh.execute()
        status = getattr(resp, "status_code", "?")
        print(f"Snapshot written and posted to Discord (HTTP {status}): {out_path}")
    except Exception as e:
        print(f"Snapshot written: {out_path} (Discord post failed: {e})")
else:
    print(f"Snapshot written: {out_path} (no webhook configured)")
