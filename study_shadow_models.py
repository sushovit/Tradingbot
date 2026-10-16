"""
study_shadow_models.py — S28: replay the last N paired gatekeeper decisions
through two local (Ollama) models and compare them. STUDY ONLY - nothing is
journaled; the worker is not involved.

    python study_shadow_models.py --old qwen3:4b --new qwen3.5:9b [--n 100]

Each decision's prompt is rebuilt with the worker's own
build_gatekeeper_kwargs from bars AS OF the decision time:
  daily setups  daily bars up to the prior session + the decision day's
                PARTIAL bar from 5-minute bars before the decision time
  trend         completed 5-minute bars before the decision time
Historical news cannot be refetched: news_headlines = [] for both models.

Per model: approval rate (approved and conviction >= 70), conviction spread,
timeouts / errors at the 45 s shadow timeout (one attempt, as the worker's
45 s budget allows), latency, an opening-burst test (5 concurrent calls),
and how well conviction RANKS the outcomes we have (outcome-tracker replays
of rejections, realized R of filled trades) next to Claude's conviction.
"""

import argparse
import json
import pickle
import sqlite3
import statistics
import threading
import time
from datetime import datetime

import pandas as pd

ET = "America/New_York"
CACHE = "data/s28"
LIVE_SETUPS = ("trend_continuation", "momentum_continuation",
               "mean_reversion_reclaim", "pullback_in_uptrend",
               "post_earnings_continuation")


def paired_decisions(conn, n):
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT d.* FROM decisions d WHERE d.source='claude' AND EXISTS ("
        " SELECT 1 FROM decisions s WHERE s.source='local_shadow' AND"
        " s.ticker=d.ticker AND s.setup_name=d.setup_name AND"
        " abs(julianday(s.timestamp)-julianday(d.timestamp))*86400 < 120)"
        " ORDER BY d.id DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows if r["setup_name"] in LIVE_SETUPS]


def outcome_r(conn, decision_id):
    """Realized R of a filled trade, else the tracker's resolved replay R."""
    row = conn.execute(
        "SELECT b.price, s.price AS exit_px, json_extract(d.context,'$.stop') AS stop "
        "FROM trades b JOIN trades s ON s.decision_id=b.decision_id AND "
        "s.action='SELL' AND s.id>b.id JOIN decisions d ON d.id=b.decision_id "
        "WHERE b.action='BUY' AND b.decision_id=?", (decision_id,)).fetchone()
    if row and row[2] and row[0] > row[2]:
        return (row[1] - row[0]) / (row[0] - row[2])
    row = conn.execute("SELECT r_achieved FROM rejection_outcomes WHERE "
                       "decision_id=? AND resolved=1 AND r_achieved IS NOT NULL",
                       (decision_id,)).fetchone()
    return float(row[0]) if row else None


def gk_frame(dec, daily, five):
    """The df the worker handed the gatekeeper, rebuilt without lookahead."""
    t = pd.Timestamp(datetime.fromisoformat(dec["timestamp"])).tz_localize(ET)
    b5 = five.get(dec["ticker"])
    if b5 is None or b5.empty:
        return None
    idx5 = b5.index.tz_convert(ET)
    done5 = b5[idx5 + pd.Timedelta(minutes=5) <= t]
    if dec["setup_name"] == "trend_continuation":
        return done5.tail(60) if len(done5) >= 30 else None
    d = daily.get(dec["ticker"])
    if d is None:
        return None
    day = t.strftime("%Y-%m-%d")
    prior = d[[str(x)[:10] < day for x in d.index]].tail(59)
    today5 = done5[done5.index.tz_convert(ET).strftime("%Y-%m-%d") == day]
    rth = today5[(today5.index.tz_convert(ET).hour * 60
                  + today5.index.tz_convert(ET).minute) >= 570]
    if rth.empty or len(prior) < 30:
        return None
    partial = pd.DataFrame({"open": [rth["open"].iloc[0]], "high": [rth["high"].max()],
                            "low": [rth["low"].min()], "close": [rth["close"].iloc[-1]],
                            "volume": [rth["volume"].sum()]},
                           index=[pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=4)])
    return pd.concat([prior[["open", "high", "low", "close", "volume"]], partial])


def build_kwargs(dec, df, risk_profile):
    import streamlit_app as app
    from strategies.base import Signal
    ctx = json.loads(dec["context"] or "{}")
    sig = Signal(setup_name=dec["setup_name"], ticker=dec["ticker"],
                 entry=float(ctx["entry"]), stop=float(ctx["stop"]),
                 target=float(ctx["target"]), reasoning=ctx.get("reasoning") or "")
    return app.build_gatekeeper_kwargs(sig, df, risk_profile, 5, [])


def ask(model, kwargs):
    import local_analyst
    t0 = time.monotonic()
    v = local_analyst.get_gatekeeper_decision(**kwargs, model=model, max_retries=1)
    dt = time.monotonic() - t0
    if "error" in v:
        kind = "timeout" if "timed out" in str(v["error"]).lower() or "timeout" in str(v["error"]).lower() else "other"
        return {"error": kind, "latency": dt}
    conv = v.get("conviction_score")
    try:
        conv = float(conv)
    except (TypeError, ValueError):
        conv = None
    appr = v.get("approved")
    if isinstance(appr, str):
        appr = appr.lower() == "true"
    return {"approved": bool(appr) and (conv or 0) >= 70, "conviction": conv,
            "latency": dt}


def warm(model):
    import requests
    import local_analyst
    t0 = time.monotonic()
    requests.post(f"{local_analyst.OLLAMA_URL}/api/chat",
                  json={"model": model, "think": False, "stream": False,
                        "keep_alive": -1,
                        "messages": [{"role": "user", "content": "OK"}]},
                  timeout=300)
    return time.monotonic() - t0


def unload(model):
    import requests
    import local_analyst
    requests.post(f"{local_analyst.OLLAMA_URL}/api/generate",
                  json={"model": model, "keep_alive": 0}, timeout=60)


def resident_gb(model):
    import requests
    import local_analyst
    ps = requests.get(f"{local_analyst.OLLAMA_URL}/api/ps", timeout=10).json()
    for m in ps.get("models", []):
        if m.get("name", "").startswith(model):
            return round(m.get("size", 0) / 1e9, 2)
    return None


def burst(model, kwargs_list, k=5):
    out = [None] * k
    def run(i):
        out[i] = ask(model, kwargs_list[i % len(kwargs_list)])
    ts = [threading.Thread(target=run, args=(i,)) for i in range(k)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 5:
        return None, len(pairs)
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2.0
            i = j + 1
        return r
    a, b = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return (num / den if den else None), len(pairs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default="qwen3:4b")
    ap.add_argument("--new", required=True)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--out")
    ap.add_argument("--journal", default="journal.db")
    a = ap.parse_args()
    cfg = json.load(open("bot_config.json"))
    rp = cfg["risk_profiles"]["Moderate"]
    conn = sqlite3.connect(f"file:{a.journal}?mode=ro", uri=True)
    decs = paired_decisions(conn, a.n)
    daily, five = pickle.load(open(f"{CACHE}/daily.pkl", "rb")), pickle.load(open(f"{CACHE}/five.pkl", "rb"))
    items = []
    for d in decs:
        df = gk_frame(d, daily, five)
        if df is None:
            continue
        try:
            items.append((d, build_kwargs(d, df, rp)))
        except Exception:
            continue
    res, meta = {}, {}
    for model in (a.old, a.new):
        cold = warm(model)
        rows = []
        for d, kw in items:
            rows.append(ask(model, kw))
        meta[model] = {"cold_load_s": cold, "resident_gb": resident_gb(model),
                       "burst": burst(model, [kw for _, kw in items[:5]])}
        res[model] = rows
        unload(model)
    claude_conv = [float(d["conviction_score"]) if d["conviction_score"] is not None else None
                   for d, _ in items]
    claude_appr = [bool(d["approved"]) and (d["conviction_score"] or 0) >= 70 for d, _ in items]
    outcomes = [outcome_r(conn, d["id"]) for d, _ in items]
    L = [f"Replayed {len(items)} of the last {a.n} paired gatekeeper decisions "
         f"({decs[-1]['timestamp'][:10] if decs else '?'} .. "
         f"{decs[0]['timestamp'][:10] if decs else '?'}); outcomes known for "
         f"{sum(o is not None for o in outcomes)}.", "",
         "| | Claude (as journaled) | " + " | ".join(res) + " |",
         "|---|---|" + "---|" * len(res)]
    def col(f):
        return " | ".join(f(m, res[m]) for m in res)
    ok = lambda rs: [r for r in rs if "error" not in r]
    L.append(f"| approval rate (>= 70) | {100*sum(claude_appr)/len(items):.0f}% | "
             + col(lambda m, rs: f"{100*sum(r['approved'] for r in ok(rs))/max(1,len(ok(rs))):.0f}%") + " |")
    L.append("| agree with Claude | - | " + col(lambda m, rs: (lambda pr: f"{100*sum(r['approved']==c for r,c in pr)/max(1,len(pr)):.0f}%")([(r, c) for r, c in zip(rs, claude_appr) if 'error' not in r])) + " |")
    L.append("| conviction mean / stdev / distinct | " +
             (lambda v: f"{statistics.mean(v):.0f} / {statistics.pstdev(v):.1f} / {len(set(v))}")([c for c in claude_conv if c is not None]) + " | " +
             col(lambda m, rs: (lambda v: f"{statistics.mean(v):.0f} / {statistics.pstdev(v):.1f} / {len(set(v))}" if v else "-")([r['conviction'] for r in ok(rs) if r['conviction'] is not None])) + " |")
    L.append("| timeouts / other errors (45 s) | - | " + col(lambda m, rs: f"{sum(r.get('error')=='timeout' for r in rs)} / {sum(r.get('error')=='other' for r in rs)}") + " |")
    L.append("| latency median / p95 / max (s) | - | " + col(lambda m, rs: (lambda v: f"{statistics.median(v):.1f} / {sorted(v)[int(0.95*(len(v)-1))]:.1f} / {max(v):.1f}")([r['latency'] for r in rs])) + " |")
    L.append("| burst of 5: timeouts / max latency (s) | - | " + col(lambda m, rs: f"{sum((b or {}).get('error')=='timeout' for b in meta[m]['burst'])} / {max(b['latency'] for b in meta[m]['burst']):.1f}") + " |")
    L.append("| cold load (s) / resident (GB) | - | " + col(lambda m, rs: f"{meta[m]['cold_load_s']:.1f} / {meta[m]['resident_gb']}") + " |")
    sc, n = spearman(claude_conv, outcomes)
    L.append(f"| conviction vs outcome R (Spearman, n) | {sc:+.2f} ({n})" if sc is not None else "| conviction vs outcome R | - ")
    L[-1] += " | " + col(lambda m, rs: (lambda s: f"{s[0]:+.2f} ({s[1]})" if s[0] is not None else "-")(spearman([r.get('conviction') for r in rs], outcomes))) + " |"
    text = "\n".join(L)
    print(text)
    pickle.dump({"items": [d["id"] for d, _ in items], "res": res, "meta": meta,
                 "claude": claude_conv, "outcomes": outcomes}, open(f"{CACHE}/replay.pkl", "wb"))
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text + "\n")


if __name__ == "__main__":
    main()
