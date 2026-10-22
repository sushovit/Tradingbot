"""
analyst.py — routes gatekeeper calls by analyst_mode and journals every verdict.

Modes (bot_config.json "analyst_mode"):
  claude  — Claude decides (source="claude"). Current behaviour.
  local   — the local Ollama model decides (source="local"). Claude not called.
            Switching to this is a MANUAL config decision; nothing in code
            ever promotes the local model to trade authority.
  shadow  — DEFAULT. Claude decides and is AUTHORITATIVE; the local model is
            queued FIFO and answered one at a time (S31: 45 s per
            inference from its start; items older than 10 min dropped),
            purely for comparison.
            Its verdict is journaled with source="local_shadow" plus an
            agreement flag. The shadow call can never delay or block the
            trade path, and shadow errors never crash the loop.
"""

import logging
import queue
import threading
import time

import journal
import prompts
import claude_integration
import local_analyst

logger = logging.getLogger(__name__)

SHADOW_TIMEOUT_SECS = 45      # per inference, counted from its start (S31)
SHADOW_MAX_AGE_SECS = 600     # a signal older than this is dropped, not run


def _journal_shadow(job: dict, verdict: dict):
    """Journal one shadow verdict (or error) with the agreement flag and the
    dissent ledger. Never raises."""
    ticker, setup_name = job["ticker"], job["setup_name"]
    context, claude_verdict = job["context"], job["claude_verdict"]
    claude_approved = bool(claude_verdict.get("approved", False))
    if "error" in verdict:
        agreement = None
    else:
        agreement = bool(verdict.get("approved", False)) == claude_approved
    shadow_context = dict(context)
    shadow_context["claude_approved"] = claude_approved
    shadow_context["claude_conviction"] = claude_verdict.get("conviction_score")
    shadow_context["shadow_queue_wait_s"] = job.get("waited_s")
    try:
        journal.log_decision(ticker, setup_name, shadow_context, verdict,
                             source="local_shadow", agreement=agreement)
        # DISSENT LEDGER: Claude approved, the shadow rejected. The shadow is
        # advisory and non-blocking by CEO ruling — this row is how that
        # ruling gets tested against outcomes rather than argued about.
        if ("error" not in verdict and claude_approved
                and not bool(verdict.get("approved", False))):
            journal.log_shadow_dissent(ticker, setup_name, claude_verdict,
                                       verdict,
                                       decision_id=context.get("decision_id"))
            logger.info(f"SHADOW DISSENT logged for {ticker}: Claude "
                        f"{claude_verdict.get('conviction_score')} vs shadow "
                        f"{verdict.get('conviction_score')}")
    except Exception as e:
        logger.error(f"Failed to journal shadow verdict for {ticker}: {e}")


def _infer(job: dict) -> dict:
    """One shadow inference, single attempt. Its 45 s starts when the
    inference starts - time spent waiting in the queue is never counted
    (S31). The HTTP call carries the same 45 s; the join is the watchdog
    that keeps the queue moving if a call hangs anyway."""
    holder = {}

    def _call():
        try:
            holder["v"] = local_analyst.get_gatekeeper_decision(
                **job["gk_kwargs"], max_retries=1)
        except Exception as e:
            holder["v"] = {"error": f"shadow call crashed: {e}"}

    inner = threading.Thread(target=_call, daemon=True)
    inner.start()
    inner.join(timeout=SHADOW_TIMEOUT_SECS)
    if inner.is_alive() or "v" not in holder:
        return {"error": "timeout", "source": "local_shadow"}
    v = holder["v"]
    if "error" in v:
        err = str(v["error"]).lower()
        if "timed out" in err or "timeout" in err:
            return {"error": "timeout", "source": "local_shadow"}
    return v


def _shadow_worker(gk_kwargs, ticker, setup_name, context, claude_verdict):
    """Run one shadow comparison inline (tests and one-off tools)."""
    job = {"gk_kwargs": gk_kwargs, "ticker": ticker, "setup_name": setup_name,
           "context": context, "claude_verdict": claude_verdict}
    _journal_shadow(job, _infer(job))


SHADOW_BUSY_FILE = "shadow_busy.marker"


def _busy(on: bool):
    """CEO 2026-10-23: a marker other local-model users (watch_movers.py)
    read so they yield to the shadow queue. Never raises."""
    import os
    try:
        if on:
            with open(SHADOW_BUSY_FILE, "w") as f:
                f.write(str(time.time()))
        elif os.path.exists(SHADOW_BUSY_FILE):
            os.remove(SHADOW_BUSY_FILE)
    except OSError:
        pass


class ShadowQueue:
    """CEO 2026-10-17 (S31). FIFO; ONE inference at a time (the 8.35 GB
    memory cap of S28 is unchanged); each inference gets its full 45 s from
    the moment it starts; an item that waited longer than max_age is
    journaled as dropped_stale and never sent to the model. The trade path
    only ever calls submit(), which returns at once."""

    def __init__(self, infer=_infer, record=_journal_shadow,
                 max_age_s=SHADOW_MAX_AGE_SECS, clock=time.monotonic):
        self._q = queue.Queue()
        self._infer, self._record = infer, record
        self.max_age_s, self._clock = max_age_s, clock
        self._worker = None
        self._lock = threading.Lock()

    def submit(self, job: dict):
        job = dict(job, enqueued=self._clock())
        self._q.put(job)
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run,
                                                name="shadow-queue", daemon=True)
                self._worker.start()

    def pending(self) -> int:
        return self._q.qsize()

    def run_one(self, block: bool = True, timeout=None) -> bool:
        """Process one item. Returns False if the queue was empty."""
        try:
            job = self._q.get(block=block, timeout=timeout)
        except queue.Empty:
            return False
        try:
            job["waited_s"] = round(self._clock() - job["enqueued"], 1)
            if job["waited_s"] > self.max_age_s:
                self._record(job, {"error": "dropped_stale",
                                   "source": "local_shadow"})
                logger.info(f"shadow: dropped {job['ticker']} after "
                            f"{job['waited_s']:.0f}s in the queue")
            else:
                _busy(True)
                try:
                    verdict = self._infer(job)
                finally:
                    _busy(False)
                self._record(job, verdict)
        except Exception as e:
            logger.error(f"shadow queue item failed: {e}")
        finally:
            self._q.task_done()
        return True

    def _run(self):
        while True:
            self.run_one(block=True)


SHADOW_QUEUE = ShadowQueue()


def get_verdict(mode: str, ticker: str, setup_name: str, context: dict,
                gk_kwargs: dict) -> tuple:
    """Returns (authoritative_verdict, decision_id).

    gk_kwargs are the keyword args for get_gatekeeper_decision (identical for
    both models). context is the decision context journaled with the verdict.
    """
    mode = (mode or "shadow").lower()

    # Stamp the prompt version on EVERY verdict, before any branch. A prompt
    # change splits the statistics: a reclaim approval rate from v4 and one
    # from v5 measure two different gates and must never be pooled. It is
    # also what scopes the reclaim probation count (agenda 10.2).
    context = dict(context or {})
    context["prompt_version"] = prompts.GATEKEEPER_PROMPT_VERSION

    if mode == "local":
        verdict = local_analyst.get_gatekeeper_decision(**gk_kwargs)
        decision_id = journal.log_decision(ticker, setup_name, context, verdict,
                                           source="local")
        return verdict, decision_id

    # claude and shadow: Claude is authoritative.
    verdict = claude_integration.get_gatekeeper_decision(**gk_kwargs)
    decision_id = journal.log_decision(ticker, setup_name, context, verdict,
                                       source="claude")

    if mode == "shadow":
        # Fire-and-forget into the S31 FIFO: the main path returns at once.
        SHADOW_QUEUE.submit({"gk_kwargs": gk_kwargs, "ticker": ticker,
                             "setup_name": setup_name, "context": context,
                             "claude_verdict": verdict})

    return verdict, decision_id
