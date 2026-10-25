"""
local_analyst.py — local open-source gatekeeper via Ollama.

Drop-in mirror of claude_integration.get_gatekeeper_decision: identical
signature, identical prompts (shared via prompts.py), same JSON verdict shape.

Runs against Ollama's native /api/chat endpoint on localhost — free, fully
local, no API key. Qwen3 thinking mode is disabled ("think": false) because
the live loop needs a fast structured verdict, not a long deliberation.
(Measured on this machine: ~8s warm with think=false vs ~60s with thinking
leaking through the OpenAI-compat endpoint.)

On any failure this returns {"error": ...} — NEVER a fake approval.
"""

import os
import sys
import json
import time
import logging

import requests
import threading

from prompts import (
    GATEKEEPER_REQUIRED_KEYS,
    build_gatekeeper_user_prompt,
    get_system_prompt,
)

# Observed junior analyst: journaled and compared, never trade authority.
SYSTEM_PROMPT = get_system_prompt("junior_analyst")

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
# Chosen for this machine: RTX 3060 Laptop 4GB VRAM -> qwen3:4b is the largest
# model in the fallback chain (14b -> 8b -> 4b) that fits on GPU and returns a
# full gatekeeper JSON in well under 20 seconds.
LOCAL_MODEL = os.getenv("LOCAL_ANALYST_MODEL", "qwen3:4b")   # legacy default
REQUEST_TIMEOUT = 45  # seconds per attempt (S28: was 30)
# S28: never unload the model DURING the session (CEO 2026-10-25: the 16:30 ET
# job unloads it after the close - see unload()). Ollama's default keep_alive (5 min) unloaded
# it between the 09:25 warm-up and the first 09:31 signals, so the first
# calls of the day paid a cold load inside their timeout.
KEEP_ALIVE = -1
# S28: bounded memory for the 9B shadow. Prompts are ~3.6k tokens; 8k leaves
# room for the verdict. One request in flight per process: five concurrent
# opening-burst calls made Ollama allocate five context slots (16.2 GB
# resident on a 16 GB Mac) - a shadow must never push the worker into swap.
NUM_CTX = 8192
_OLLAMA_LOCK = threading.Lock()


def shadow_model(config: dict = None) -> str:
    """The shadow model: LOCAL_ANALYST_MODEL env > bot_config.json
    models.shadow > the legacy default."""
    env = os.getenv("LOCAL_ANALYST_MODEL")
    if env:
        return env
    if config is None:
        try:
            with open("bot_config.json", encoding="utf-8") as f:
                config = json.load(f)
        except (OSError, ValueError):
            config = {}
    return ((config or {}).get("models") or {}).get("shadow") or "qwen3:4b"


def classify_error(err: Exception) -> str:
    """Name the failure precisely. 'Ollama unreachable' covered three very
    different faults; only one of them is worth retrying the same way."""
    text = str(err).lower()
    refused = "refused" in text or "newconnectionerror" in text
    if isinstance(err, requests.ConnectionError) or refused:
        return "connection_refused"          # the service is not running
    if isinstance(err, requests.Timeout) or "timed out" in text:
        return "read_timeout"                # busy or loading the model
    if isinstance(err, (json.JSONDecodeError, KeyError, IndexError)):
        return "malformed_response"
    return "unknown"


def is_up(timeout: float = 2.0) -> bool:
    try:
        requests.get(f"{OLLAMA_URL}/api/tags", timeout=timeout)
        return True
    except requests.RequestException:
        return False


DARWIN_OLLAMA_PATHS = ("/Applications/Ollama.app", "/opt/homebrew/bin/ollama")


def _darwin_ollama_launch():
    """The command that starts Ollama on macOS, or None if it isn't installed.
    The app (which runs the server itself) is preferred over the bare CLI."""
    app, cli = DARWIN_OLLAMA_PATHS
    if os.path.exists(app):
        return ["open", "-a", app]
    if os.path.exists(cli):
        return [cli, "serve"]
    return None


def ensure_ollama(warm: bool = True, wait_secs: int = 60) -> dict:
    """Make sure Ollama is up and the model is resident BEFORE the session's
    first gatekeeper call.

    Diagnosis 2026-09-01: 27 of 30 shadow failures were CONNECTION REFUSED,
    every one in the session's first hour — the service simply was not
    running when the desk started. It was never GPU contention (zero errors
    at the 15:30 intern run) and only 3 were timeouts. So the fix is to
    start it and warm it, not to retry harder.

    Returns {'up', 'started', 'warmed', 'detail'} and never raises."""
    result = {"up": False, "started": False, "warmed": False, "detail": ""}
    if is_up():
        result["up"] = True
    else:
        # Try to start the service ourselves rather than losing the hour.
        try:
            import subprocess
            launch = (_darwin_ollama_launch() if sys.platform == "darwin"
                      else None)
            exe = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                               "Programs", "Ollama", "ollama app.exe")
            if sys.platform == "darwin" and launch is None:
                result["detail"] = ("ollama not found at "
                                    f"{' or '.join(DARWIN_OLLAMA_PATHS)}")
            elif launch is not None or os.path.exists(exe):
                if launch is not None:
                    # Its own session: Ollama is a service, not a child the
                    # worker's process-group kill should take down.
                    subprocess.Popen(launch, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     start_new_session=True)
                else:
                    subprocess.Popen([exe],
                                     creationflags=getattr(subprocess,
                                                           "CREATE_NO_WINDOW", 0))
                result["started"] = True
                deadline = time.time() + wait_secs
                while time.time() < deadline:
                    if is_up():
                        result["up"] = True
                        break
                    time.sleep(2)
            else:
                result["detail"] = f"ollama app not found at {exe}"
        except Exception as e:
            result["detail"] = f"could not start Ollama: {e}"

    if result["up"] and warm:
        # Load the model into VRAM so the first real call is not a cold
        # start racing a 30s timeout.
        try:
            requests.post(f"{OLLAMA_URL}/api/chat",
                          json={"model": shadow_model(), "think": False,
                                "stream": False, "keep_alive": KEEP_ALIVE,
                                "messages": [{"role": "user", "content": "OK"}]},
                          timeout=120)
            result["warmed"] = True
        except requests.RequestException as e:
            result["detail"] = f"warm-up failed: {classify_error(e)}"
    if not result["detail"]:
        result["detail"] = ("ready" if result["warmed"]
                            else "up (not warmed)" if result["up"] else "DOWN")
    return result


def _call_ollama(system_prompt: str, user_prompt: str,
                 timeout: int = None, model: str = None) -> dict:
    """One Ollama chat call in JSON mode with thinking disabled.
    Raises on transport errors."""
    timeout = timeout or REQUEST_TIMEOUT
    # Wait for the single slot at most this call's own budget. A call the
    # worker has already given up on must never reach Ollama later and hold
    # it while the next real signals queue behind it.
    if not _OLLAMA_LOCK.acquire(timeout=timeout):
        raise requests.Timeout(f"shadow queue: no slot within {timeout}s")
    try:
        return _post_chat(system_prompt, user_prompt, timeout, model)
    finally:
        _OLLAMA_LOCK.release()


def _post_chat(system_prompt, user_prompt, timeout, model):
    resp = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": model or shadow_model(),
            "keep_alive": KEEP_ALIVE,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "format": "json",      # Ollama JSON mode: forces valid JSON output
            "think": False,        # disable Qwen3 thinking for fast verdicts
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": NUM_CTX},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    text = resp.json()["message"]["content"]
    return json.loads(text)


def _validate_verdict(result: dict) -> dict:
    missing = [k for k in GATEKEEPER_REQUIRED_KEYS if k not in result]
    if missing:
        return {"error": f"Local analyst returned JSON missing keys: {missing}"}
    if isinstance(result.get("approved"), str):
        result["approved"] = result["approved"].lower() == "true"
    if not isinstance(result.get("conviction_score"), (int, float)):
        return {"error": "Local analyst conviction_score is not numeric."}
    return result


def build_user_prompt(ticker, df20, ema_spread_pct, volume_trend,
                      crossover_count, dist_to_resistance_pct, entry_price,
                      stop_price, target_price, rr_ratio, interval_mins,
                      fast_ema, slow_ema, news_headlines,
                      setup_name="trend_continuation", setup_description=None):
    """The exact gatekeeper user prompt the shadow model sees - one place,
    shared by the live call and the S28 fine-tune export."""
    required_cols = ['open', 'high', 'low', 'close', 'volume',
                     'ema_fast', 'ema_slow', 'rsi_14', 'adx_14']
    available_cols = [c for c in required_cols if c in df20.columns]
    candle_data_str = df20[available_cols].round(4).to_string()
    last = df20.iloc[-1]
    adx_val = last['adx_14'] if 'adx_14' in df20.columns else 0
    rsi_val = last['rsi_14'] if 'rsi_14' in df20.columns else 50
    news_str = ("\n".join(f"- {h}" for h in news_headlines)
                if news_headlines else "No recent news available.")
    user_prompt = build_gatekeeper_user_prompt(
        ticker=ticker,
        candle_data_str=candle_data_str,
        adx_val=float(adx_val),
        rsi_val=float(rsi_val),
        ema_spread_pct=ema_spread_pct,
        volume_trend=volume_trend,
        crossover_count=crossover_count,
        dist_to_resistance_pct=dist_to_resistance_pct,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        rr_ratio=rr_ratio,
        interval_mins=interval_mins,
        fast_ema=fast_ema,
        slow_ema=slow_ema,
        news_str=news_str,
        setup_name=setup_name,
        setup_description=setup_description,
    )
    return user_prompt


def get_gatekeeper_decision(
    ticker: str,
    df20,
    ema_spread_pct: float,
    volume_trend: str,
    crossover_count: int,
    dist_to_resistance_pct: float,
    entry_price: float,
    stop_price: float,
    target_price: float,
    rr_ratio: float,
    interval_mins: int,
    fast_ema: int,
    slow_ema: int,
    news_headlines: list,
    setup_name: str = "trend_continuation",
    setup_description: str = None,
    model: str = None,
    max_retries: int = 3,
) -> dict:
    """Same contract as claude_integration.get_gatekeeper_decision.
    `model` overrides the shadow model (the S28 replay compares two)."""
    if df20 is None or df20.empty:
        return {"approved": False, "conviction_score": 0,
                "rejection_reason": "No candle data provided."}

    user_prompt = build_user_prompt(
        ticker=ticker, df20=df20, ema_spread_pct=ema_spread_pct,
        volume_trend=volume_trend, crossover_count=crossover_count,
        dist_to_resistance_pct=dist_to_resistance_pct, entry_price=entry_price,
        stop_price=stop_price, target_price=target_price, rr_ratio=rr_ratio,
        interval_mins=interval_mins, fast_ema=fast_ema, slow_ema=slow_ema,
        news_headlines=news_headlines, setup_name=setup_name,
        setup_description=setup_description)

    last_err = None
    for attempt in range(max_retries):
        try:
            raw = _call_ollama(SYSTEM_PROMPT, user_prompt, model=model)
            return _validate_verdict(raw)
        except (requests.ConnectionError, requests.Timeout,
                json.JSONDecodeError, KeyError, IndexError, Exception) as e:
            # Name the fault so the journal says WHICH failure this was.
            last_err = f"{classify_error(e)}: {e}"
        if attempt < max_retries - 1:
            wait = 2 ** attempt
            logger.warning(f"Local analyst attempt {attempt + 1} failed, "
                           f"retrying in {wait}s: {last_err}")
            time.sleep(wait)
    return {"error": f"Local analyst failed after {max_retries} attempts: {last_err}"}


def unload(config: dict = None, post=None, get=None) -> dict:
    """CEO 2026-10-25: free the shadow model's ~16 GB after the close (the
    16:30 ET launchd job). keep_alive -1 holds it all day; resident overnight
    it pushed the Mac into ~10 GB of swap on disk. The 09:20 warm job loads
    it again before the session. {'ok', 'model', 'detail'}; never raises."""
    post = post or requests.post
    get = get or requests.get
    model = shadow_model(config)
    try:
        post(f"{OLLAMA_URL}/api/generate", json={"model": model, "keep_alive": 0},
             timeout=30)
        loaded = [m.get("name") for m in
                  (get(f"{OLLAMA_URL}/api/ps", timeout=10).json().get("models") or [])]
        if model in loaded:
            return {"ok": False, "model": model, "detail": "still loaded after keep_alive 0"}
        return {"ok": True, "model": model, "detail": "unloaded"}
    except Exception as e:
        return {"ok": False, "model": model,
                "detail": f"unload failed ({type(e).__name__}: {str(e)[:120]})"}


if __name__ == "__main__":
    # S28: `python local_analyst.py --warm` - the 09:20 ET launchd job loads
    # the shadow model (keep_alive -1) before the worker's 09:25 start.
    pass  # sys already imported
    if "--unload" in sys.argv:
        r = unload()
        print(f"shadow model {r['model']}: {r['detail']}")
        sys.exit(0 if r["ok"] else 1)
    if "--warm" in sys.argv:
        state = ensure_ollama(warm=True)
        print(f"shadow model {shadow_model()}: {state['detail']} "
              f"(started={state['started']}, warmed={state['warmed']})")
        sys.exit(0 if state["warmed"] else 1)
