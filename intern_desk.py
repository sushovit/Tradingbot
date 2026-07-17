"""
intern_desk.py — the intern's independent daily analysis report.

    python intern_desk.py                                  -> run the daily scan
    python intern_desk.py grade <date> <ticker> <good|bad|ungradeable> "note"

Once per session (~15:30 ET, scheduled), the LOCAL model (never the API)
scans every ticker in universe_today.json with the same kind of context the
gatekeeper sees — indicators on daily bars, price structure, headlines — and
gives an independent verdict: long_setup / short_setup / no_trade with
conviction, invalidation level, key risk, and <=3 sentences of reasoning.

Output: reports/intern_<date>.md (table + top-3 ideas spelled out), posted
to Discord like the floor reports. Every verdict is journaled with
source="intern_desk" (excluded from shadow agreement stats — different job,
different scorecard) and recorded in intern_grades for the CEO's session
grades. Grades + outcomes become the SFT dataset.

HARD BOUNDARY: no imports from broker.py or orders.py — this desk is
read-only everywhere. Market data comes from its own alpaca DATA client
(INTERN keys preferred; falls back to main keys for DATA ONLY until the
intern account exists). 20s timeout per ticker, skip-and-log on failure —
a slow ticker never kills the report.
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pandas_ta as ta
import pytz
import requests
from dotenv import load_dotenv

import journal
from prompts import (get_system_prompt, build_intern_desk_prompt,
                     INTERN_DESK_REQUIRED_KEYS)

load_dotenv()
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EASTERN_TZ = pytz.timezone("US/Eastern")
UNIVERSE_FILE = "universe_today.json"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
LOCAL_MODEL = os.getenv("LOCAL_ANALYST_MODEL", "qwen3:4b")
PER_TICKER_TIMEOUT = 20
TEMPERATURE = 0.3
VALID_STANCES = {"long_setup", "short_setup", "no_trade"}
DISCORD_MSG_LIMIT = 2000


# ---------------------------------------------------------------- data (read-only)

def _data_client():
    """Read-only market-data client. Prefers the intern's own keys; falls
    back to the main keys for DATA ONLY (market data is not account-scoped).
    Never constructs a trading client of any kind."""
    from alpaca.data.historical import StockHistoricalDataClient
    key = os.getenv("INTERN_ALPACA_API_KEY") or os.getenv("ALPACA_API_KEY")
    secret = os.getenv("INTERN_ALPACA_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
    if not os.getenv("INTERN_ALPACA_API_KEY"):
        logger.warning("INTERN keys not set — using main keys for market DATA only.")
    if not key or not secret:
        raise RuntimeError("No Alpaca keys available for market data.")
    return StockHistoricalDataClient(key, secret)


def fetch_daily_bars(tickers, lookback_days=60) -> dict:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    client = _data_client()
    req = StockBarsRequest(symbol_or_symbols=list(tickers), timeframe=TimeFrame.Day,
                           start=datetime.now(timezone.utc) - timedelta(days=lookback_days),
                           feed=DataFeed.IEX)
    barset = client.get_stock_bars(req)
    df_all = barset.df if hasattr(barset, "df") else pd.DataFrame()
    out = {}
    for t in tickers:
        try:
            if df_all.empty:
                out[t] = pd.DataFrame()
                continue
            df = (df_all.xs(t, level="symbol").copy()
                  if isinstance(df_all.index, pd.MultiIndex) else df_all.copy())
            df.columns = [c.lower() for c in df.columns]
            out[t] = df[[c for c in ("open", "high", "low", "close", "volume")
                         if c in df.columns]]
        except Exception as e:
            logger.warning(f"bars unavailable for {t}: {e}")
            out[t] = pd.DataFrame()
    return out


def fetch_headlines(ticker: str) -> list:
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        return []
    try:
        import finnhub
        client = finnhub.Client(api_key=api_key)
        today = datetime.now(EASTERN_TZ).strftime("%Y-%m-%d")
        week_ago = (datetime.now(EASTERN_TZ) - timedelta(days=5)).strftime("%Y-%m-%d")
        news = client.company_news(ticker, _from=week_ago, to=today)
        return [n["headline"] for n in news[:5]]
    except Exception:
        return []


# ---------------------------------------------------------------- analysis

def build_context(ticker: str, df: pd.DataFrame):
    """Indicators + price structure on daily bars — the same kind of context
    the gatekeeper sees. Returns (candle_str, metrics_str, metrics_dict)."""
    df = df.copy()
    df["ema_9"] = ta.ema(df["close"], length=9)
    df["ema_21"] = ta.ema(df["close"], length=21)
    rsi = ta.rsi(df["close"], length=14)
    if rsi is not None:
        df["rsi_14"] = rsi
    adx = ta.adx(df["high"], df["low"], df["close"], length=14)
    if adx is not None and not adx.empty:
        df["adx_14"] = adx["ADX_14"]

    tail = df.tail(20)
    candle_str = tail.round(2).to_string()

    last = df.iloc[-1]
    high_20 = float(df["high"].tail(20).max())
    low_20 = float(df["low"].tail(20).min())
    close = float(last["close"])
    avg_vol = float(df["volume"].tail(20).mean())
    metrics = {
        "close": round(close, 2),
        "pct_off_20d_high": round((high_20 - close) / high_20 * 100, 1) if high_20 else None,
        "pct_above_20d_low": round((close - low_20) / low_20 * 100, 1) if low_20 else None,
        "rsi_14": round(float(last.get("rsi_14")), 1) if pd.notna(last.get("rsi_14")) else None,
        "adx_14": round(float(last.get("adx_14")), 1) if pd.notna(last.get("adx_14")) else None,
        "above_ema9": bool(close > last["ema_9"]) if pd.notna(last.get("ema_9")) else None,
        "above_ema21": bool(close > last["ema_21"]) if pd.notna(last.get("ema_21")) else None,
        "last_volume_vs_avg": round(float(last["volume"]) / avg_vol, 2) if avg_vol else None,
    }
    metrics_str = "\n".join(f"- {k}: {v}" for k, v in metrics.items())
    return candle_str, metrics_str, metrics


def _call_local_model(system_prompt: str, user_prompt: str) -> dict:
    resp = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={"model": LOCAL_MODEL,
              "messages": [{"role": "system", "content": system_prompt},
                           {"role": "user", "content": user_prompt}],
              "format": "json", "think": False, "stream": False,
              "options": {"temperature": TEMPERATURE}},
        timeout=PER_TICKER_TIMEOUT)
    resp.raise_for_status()
    return json.loads(resp.json()["message"]["content"])


def normalize_verdict(raw: dict):
    """Validate/normalize one intern verdict; None if unusable."""
    if not isinstance(raw, dict):
        return None
    missing = [k for k in INTERN_DESK_REQUIRED_KEYS if k not in raw]
    if missing:
        return None
    stance = str(raw.get("stance", "")).lower().strip()
    if stance not in VALID_STANCES:
        return None
    conviction = raw.get("conviction")
    if not isinstance(conviction, (int, float)) or isinstance(conviction, bool):
        return None
    invalidation = raw.get("invalidation")
    if not isinstance(invalidation, (int, float)) or isinstance(invalidation, bool):
        invalidation = None
    # Cap reasoning: 3 sentences / 400 chars, whichever bites first.
    reasoning = str(raw.get("reasoning", ""))
    sentences = [s for s in reasoning.replace("!", ".").replace("?", ".").split(".")
                 if s.strip()]
    reasoning = ". ".join(sentences[:3]).strip()[:400]
    return {
        "stance": stance,
        "setup_name": raw.get("setup_name") or None,
        "conviction": int(conviction),
        "invalidation": invalidation,
        "key_risk": str(raw.get("key_risk", ""))[:160],
        "reasoning": reasoning,
    }


def analyze_ticker(ticker: str, df: pd.DataFrame):
    """One local-model call. Returns (verdict, metrics) or (None, reason)."""
    if df is None or len(df) < 25:
        return None, "insufficient bars"
    try:
        candle_str, metrics_str, metrics = build_context(ticker, df)
    except Exception as e:
        return None, f"context build failed: {e}"
    news = fetch_headlines(ticker)
    news_str = ("\n".join(f"- {h}" for h in news)
                if news else "No recent headlines available.")
    prompt = build_intern_desk_prompt(ticker, candle_str, metrics_str, news_str)
    try:
        raw = _call_local_model(get_system_prompt("intern_desk"), prompt)
    except Exception as e:
        return None, f"model call failed: {e}"
    verdict = normalize_verdict(raw)
    if verdict is None:
        return None, "malformed verdict"
    verdict["metrics"] = metrics
    return verdict, None


# ---------------------------------------------------------------- report

def build_markdown(date_str: str, verdicts: dict, skipped: dict) -> str:
    lines = [f"# Intern desk — {date_str}",
             f"_Model: {LOCAL_MODEL} (local). Independent scan; graded by the "
             f"CEO on reasoning quality. Not trade instructions._", ""]
    lines.append("| Ticker | Stance | Setup | Conv. | Invalidation | Reasoning |")
    lines.append("|---|---|---|---|---|---|")
    ranked = sorted(verdicts.items(), key=lambda kv: kv[1]["conviction"], reverse=True)
    for ticker, v in ranked:
        inval = f"${v['invalidation']:,.2f}" if v.get("invalidation") else "—"
        one_line = v["reasoning"].split(". ")[0][:90]
        lines.append(f"| {ticker} | {v['stance']} | {v['setup_name'] or '—'} "
                     f"| {v['conviction']} | {inval} | {one_line} |")
    if skipped:
        lines.append("")
        lines.append("_Skipped: " + ", ".join(f"{t} ({r})"
                     for t, r in skipped.items()) + "_")

    ideas = [(t, v) for t, v in ranked if v["stance"] != "no_trade"][:3]
    lines.append("\n## Top ideas")
    if not ideas:
        lines.append("_No setups today — intern sees nothing actionable._")
    for ticker, v in ideas:
        inval = f"${v['invalidation']:,.2f}" if v.get("invalidation") else "NOT STATED"
        lines.append(f"\n### {ticker} — {v['stance']} ({v['setup_name'] or 'no setup named'}), "
                     f"conviction {v['conviction']}")
        lines.append(f"- **Invalidation:** {inval}")
        lines.append(f"- **Key risk:** {v['key_risk']}")
        lines.append(f"- {v['reasoning']}")
    return "\n".join(lines)


def post_discord(report: str, file_path: str, headline: str):
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        print("(no webhook configured — skipping Discord post)")
        return
    try:
        from discord_webhook import DiscordWebhook
        fenced = f"```markdown\n{report}\n```"
        if len(fenced) <= DISCORD_MSG_LIMIT:
            wh = DiscordWebhook(url=url, content=fenced)
        else:
            wh = DiscordWebhook(url=url, content=headline)
            with open(file_path, "rb") as f:
                wh.add_file(file=f.read(), filename=os.path.basename(file_path))
        resp = wh.execute()
        print(f"Posted to Discord (HTTP {getattr(resp, 'status_code', '?')})")
    except Exception as e:
        print(f"(Discord post failed: {e}) — report kept at {file_path}")


# ---------------------------------------------------------------- commands

def run_desk() -> int:
    journal.init_db()
    date_str = datetime.now(EASTERN_TZ).strftime("%Y-%m-%d")
    try:
        with open(UNIVERSE_FILE, "r") as f:
            candidates = json.load(f).get("candidates", [])
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"{UNIVERSE_FILE} missing/invalid — run 'python universe.py' first.")
        return 1
    tickers = [c["symbol"] for c in candidates]
    if not tickers:
        print("Universe is empty — nothing to scan.")
        return 1

    print(f"Intern desk scanning {len(tickers)} tickers with {LOCAL_MODEL}...")
    # Warm the model once (cold load costs ~15s and would eat the first
    # ticker's 20s budget).
    try:
        requests.post(f"{OLLAMA_URL}/api/chat",
                      json={"model": LOCAL_MODEL, "think": False, "stream": False,
                            "messages": [{"role": "user", "content": "OK"}]},
                      timeout=90)
    except requests.RequestException as e:
        logger.warning(f"model warm-up failed (continuing): {e}")

    bars = fetch_daily_bars(tickers)
    verdicts, skipped = {}, {}
    for ticker in tickers:
        verdict, why = analyze_ticker(ticker, bars.get(ticker))
        if verdict is None:
            skipped[ticker] = why
            logger.warning(f"{ticker}: skipped ({why})")
            print(f"  {ticker}: skipped ({why})")
            continue
        verdicts[ticker] = verdict
        print(f"  {ticker}: {verdict['stance']} conv={verdict['conviction']}")
        journal.log_decision(
            ticker, verdict.get("setup_name") or "intern_scan",
            {"metrics": verdict.pop("metrics", {}), "date": date_str},
            {"approved": verdict["stance"] == "long_setup",
             "conviction_score": verdict["conviction"],
             "stance": verdict["stance"],
             "setup_name": verdict["setup_name"],
             "invalidation": verdict["invalidation"],
             "rejection_reason": None,
             "key_risk": verdict["key_risk"],
             "reasoning": verdict["reasoning"]},
            source="intern_desk")
        journal.intern_record(date_str, ticker, verdict["stance"],
                              verdict["conviction"])

    report = build_markdown(date_str, verdicts, skipped)
    os.makedirs("reports", exist_ok=True)
    file_path = os.path.join("reports", f"intern_{date_str}.md")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n{report}\n\nWritten: {file_path}")

    n_setups = sum(1 for v in verdicts.values() if v["stance"] != "no_trade")
    headline = (f"🎓 Intern desk {date_str} — {len(verdicts)} scanned, "
                f"{n_setups} setup(s), {len(skipped)} skipped")
    post_discord(report, file_path, headline)
    return 0


def grade_cmd(argv) -> int:
    if len(argv) < 3:
        print('Usage: python intern_desk.py grade <date> <ticker> '
              '<good|bad|ungradeable> "note"')
        return 1
    date_str, ticker, grade = argv[0], argv[1], argv[2].lower()
    note = argv[3] if len(argv) > 3 else ""
    if grade not in ("good", "bad", "ungradeable"):
        print("Grade must be one of: good, bad, ungradeable")
        return 1
    journal.init_db()
    if journal.intern_grade(date_str, ticker, grade, note):
        print(f"Graded {ticker.upper()} {date_str}: {grade}"
              + (f' — "{note}"' if note else ""))
        return 0
    print(f"No intern call found for {ticker.upper()} on {date_str}.")
    return 1


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "grade":
        return grade_cmd(args[1:])
    return run_desk()


if __name__ == "__main__":
    sys.exit(main())
