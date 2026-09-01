"""
analyst.py — routes gatekeeper calls by analyst_mode and journals every verdict.

Modes (bot_config.json "analyst_mode"):
  claude  — Claude decides (source="claude"). Current behaviour.
  local   — the local Ollama model decides (source="local"). Claude not called.
            Switching to this is a MANUAL config decision; nothing in code
            ever promotes the local model to trade authority.
  shadow  — DEFAULT. Claude decides and is AUTHORITATIVE; the local model is
            called in a background thread (30s timeout) purely for comparison.
            Its verdict is journaled with source="local_shadow" plus an
            agreement flag. The shadow call can never delay or block the
            trade path, and shadow errors never crash the loop.
"""

import logging
import threading

import journal
import claude_integration
import local_analyst

logger = logging.getLogger(__name__)

SHADOW_TIMEOUT_SECS = 30


def _shadow_worker(gk_kwargs: dict, ticker: str, setup_name: str,
                   context: dict, claude_verdict: dict):
    """Runs in a daemon thread: local verdict -> journal with agreement flag."""
    result_holder = {}

    def _call():
        try:
            result_holder["verdict"] = local_analyst.get_gatekeeper_decision(**gk_kwargs)
        except Exception as e:
            result_holder["verdict"] = {"error": f"shadow call crashed: {e}"}

    inner = threading.Thread(target=_call, daemon=True)
    inner.start()
    inner.join(timeout=SHADOW_TIMEOUT_SECS)

    if inner.is_alive() or "verdict" not in result_holder:
        verdict = {"error": "timeout", "source": "local_shadow"}
    else:
        verdict = result_holder["verdict"]

    claude_approved = bool(claude_verdict.get("approved", False))
    if "error" in verdict:
        agreement = None
    else:
        agreement = bool(verdict.get("approved", False)) == claude_approved

    shadow_context = dict(context)
    shadow_context["claude_approved"] = claude_approved
    shadow_context["claude_conviction"] = claude_verdict.get("conviction_score")
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


def get_verdict(mode: str, ticker: str, setup_name: str, context: dict,
                gk_kwargs: dict) -> tuple:
    """Returns (authoritative_verdict, decision_id).

    gk_kwargs are the keyword args for get_gatekeeper_decision (identical for
    both models). context is the decision context journaled with the verdict.
    """
    mode = (mode or "shadow").lower()

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
        # Fire-and-forget: the main path returns immediately.
        t = threading.Thread(
            target=_shadow_worker,
            args=(gk_kwargs, ticker, setup_name, context, verdict),
            daemon=True,
        )
        t.start()

    return verdict, decision_id
