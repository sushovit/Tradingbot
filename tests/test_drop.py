"""
drop.py — one-command session file. No network (subprocess layer untested).

  - valid UTF-8 round trip, pure ASCII content
  - self-dating header present and first
  - sections in order
  - second run overwrites latest.md
"""

import os

import drop
import clockline


SECTIONS = [
    ("report", "Effective capital: $1,972.73 — 🟢 running ✅"),
    ("universe", "| MRVL | $211.95 | ≥ $20M | washout_reclaim |"),
    ("floor", "⚪ not running · Gatekeeper ❌ rejected"),
    ("intern desk (today)", "(no intern report for today yet)"),
]


def test_asciify_replacements():
    out = drop.asciify("🟢 running — conviction ≥ 70 ✅ / ❌ ⚠️ STALE")
    assert out == "OK running - conviction >= 70 OK / NO WARN STALE"
    assert out.encode("ascii")                    # pure ASCII by construction


def test_assemble_header_first_sections_in_order():
    md = drop.assemble(SECTIONS)
    lines = md.splitlines()
    assert lines[0] == "# Session drop"
    assert " ET  |  " in lines[1] and "Nepal" in lines[1]   # dual-zone header
    assert "generated 0m ago" in lines[1]                    # self-dating
    order = [md.index(f"## {t}") for t, _ in SECTIONS]
    assert order == sorted(order)                            # sections in order
    assert all(ord(c) < 128 for c in md)                     # fully ASCII


def test_write_drop_utf8_roundtrip_and_latest_overwrite(tmp_path):
    d = str(tmp_path / "drop")

    p1, latest1 = drop.write_drop("FIRST RUN CONTENT\n", drop_dir=d)
    assert os.path.exists(p1) and os.path.exists(latest1)
    with open(latest1, encoding="utf-8", errors="strict") as f:  # strict: real UTF-8
        assert f.read() == "FIRST RUN CONTENT\n"

    p2, latest2 = drop.write_drop("SECOND RUN CONTENT\n", drop_dir=d)
    assert latest2 == latest1                                # fixed name
    with open(latest1, encoding="utf-8", errors="strict") as f:
        assert f.read() == "SECOND RUN CONTENT\n"            # overwritten


def test_no_utf16_bom():
    md = drop.assemble(SECTIONS)
    raw = md.encode("utf-8")
    assert not raw.startswith(b"\xff\xfe") and not raw.startswith(b"\xfe\xff")
