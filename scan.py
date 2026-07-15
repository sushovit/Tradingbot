"""scan.py — one-shot session snapshot. Streams output live, hard timeout."""

import subprocess
import sys

def run(script, timeout=180):
    print(f"--- {script} ---", flush=True)
    try:
        r = subprocess.run([sys.executable, script], timeout=timeout)
        if r.returncode != 0:
            print(f"[{script} exited with code {r.returncode}]")
    except subprocess.TimeoutExpired:
        print(f"[{script} TIMED OUT after {timeout}s — likely broker/network stall]")

run("report.py")
run("universe.py")
run("floor.py")
print("--- end of scan ---")