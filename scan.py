"""scan.py — one-shot session snapshot. Hard timeout per script.

Output is captured per script and passed through clockline.annotate_age at
PRINT time, so header stamps gain "generated Xm ago" and stale content
self-identifies."""

import subprocess
import sys

import clockline

def run(script, timeout=180):
    print(f"--- {script} ---", flush=True)
    try:
        r = subprocess.run([sys.executable, script], timeout=timeout,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        print(clockline.annotate_age(r.stdout or ""), flush=True)
        if r.returncode != 0:
            if r.stderr:
                print(r.stderr[-400:])
            print(f"[{script} exited with code {r.returncode}]")
    except subprocess.TimeoutExpired:
        print(f"[{script} TIMED OUT after {timeout}s — likely broker/network stall]")

run("report.py")
run("universe.py")
run("floor.py")
print("--- end of scan ---")