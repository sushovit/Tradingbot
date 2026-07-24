"""scan.py — one-shot session snapshot. Streams output LIVE, hard timeout.

Each line is passed through clockline.annotate_age as it streams, so header
stamps gain "generated Xm ago" without sacrificing live output (capturing
whole-script output made a normal 60s report.py look like a hang)."""

import subprocess
import sys
import threading

import clockline

def run(script, timeout=180):
    print(f"--- {script} ---", flush=True)
    proc = subprocess.Popen([sys.executable, script],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")

    def pump():
        for line in proc.stdout:
            print(clockline.annotate_age(line.rstrip("\n")), flush=True)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        proc.kill()
        reader.join(5)
        print(f"[{script} TIMED OUT after {timeout}s — killed; likely broker/network stall]")
        return
    rc = proc.wait()
    if rc:
        print(f"[{script} exited with code {rc}]")

run("report.py")
run("universe.py")
run("floor.py")
print("--- end of scan ---")