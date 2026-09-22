"""
review_bot fixes (2026-09-22). No network, no trading behaviour.

(1) THE EMPTY MEMO. reports/review_2026-09-21.md was written 129 bytes long
    — the heading and the clock line and nothing else — while the job logged
    "Memo written" and "Posted to Discord (HTTP 200)". Every other memo in
    reports/ is 4-7 KB. The response carried no text block, extract_text
    correctly returned "", and "" then formatted into the memo template
    without complaint. Nothing in the pipeline treated an empty review as a
    failure, so a silent one looked exactly like a success.

(2) TWO NEW DESK FACTS, so the reviewer stops mis-reading things the desk
    has already settled: soft-volume reclaims reaching the gatekeeper (S8),
    and mean_reversion_reclaim being back on probation from v5 (S7).
"""

import review_bot


# ============================================ (1) an empty review is a failure

class FakeUsage:
    output_tokens = 8000


class FakeResponse:
    """A response shaped like the 09-21 one: content present, no text."""

    def __init__(self, blocks=None, stop_reason="max_tokens"):
        self.content = blocks if blocks is not None else [
            type("ThinkingBlock", (), {"type": "thinking"})()]
        self.stop_reason = stop_reason
        self.usage = FakeUsage()


class TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return self.responses.pop(0) if self.responses else FakeResponse()


class FakeClient:
    def __init__(self, *responses):
        self.messages = FakeMessages(responses)


def patch_client(monkeypatch, client):
    monkeypatch.setattr(review_bot.claude_integration, "_get_client",
                        lambda: client)
    monkeypatch.setattr(review_bot.time, "sleep", lambda *_: None)
    # These tests exercise how an empty RESPONSE is handled, not how the
    # user prompt is assembled; a real bundle needs a dozen keys.
    monkeypatch.setattr(review_bot, "build_user_prompt",
                        lambda bundle: "(stub prompt)")


def test_an_empty_response_is_an_error_not_a_memo(monkeypatch):
    """The core of the fix. Before it, this returned {'text': ''} and the
    caller wrote a header-only file and called it success."""
    patch_client(monkeypatch, FakeClient(FakeResponse(), FakeResponse(),
                                         FakeResponse()))
    result = review_bot.request_review({"date": "2026-09-21"})
    assert "error" in result
    assert "empty review text" in result["error"]


def test_the_error_carries_what_is_needed_to_diagnose_it(monkeypatch):
    """The 09-21 run left no evidence at all. stop_reason and the block
    types are what distinguish 'spent the whole budget thinking' from
    'refused' from 'returned nothing'."""
    patch_client(monkeypatch, FakeClient(FakeResponse(), FakeResponse(),
                                         FakeResponse()))
    error = review_bot.request_review({"date": "2026-09-21"})["error"]
    assert "stop_reason=max_tokens" in error
    assert "thinking" in error
    assert "output_tokens=8000" in error


def test_it_retries_before_giving_up(monkeypatch):
    """An empty response is worth one more attempt — it costs a call, and a
    lost memo costs the day's review."""
    client = FakeClient(FakeResponse(), FakeResponse(), FakeResponse())
    patch_client(monkeypatch, client)
    review_bot.request_review({"date": "2026-09-21"})
    assert client.messages.calls == review_bot.MAX_RETRIES + 1


def test_a_retry_that_succeeds_returns_the_memo(monkeypatch):
    client = FakeClient(FakeResponse(),
                        FakeResponse([TextBlock("## Marks\nSPCX +1.1%")],
                                     stop_reason="end_turn"))
    patch_client(monkeypatch, client)
    result = review_bot.request_review({"date": "2026-09-21"})
    assert "error" not in result
    assert "SPCX" in result["text"]
    assert client.messages.calls == 2


def test_whitespace_only_counts_as_empty(monkeypatch):
    patch_client(monkeypatch, FakeClient(*[FakeResponse([TextBlock("   \n\n")])
                                           for _ in range(3)]))
    assert "error" in review_bot.request_review({"date": "2026-09-21"})


def test_a_thinking_block_beside_real_text_is_still_a_memo(monkeypatch):
    """extract_text walks to the first TEXT block. The guard must not
    reject a normal thinking-plus-answer response."""
    blocks = [type("ThinkingBlock", (), {"type": "thinking"})(),
              TextBlock("## Marks\nthe book is flat")]
    patch_client(monkeypatch, FakeClient(FakeResponse(blocks, "end_turn")))
    result = review_bot.request_review({"date": "2026-09-21"})
    assert result["text"].startswith("## Marks")


# ============================================ (1) nothing is written

def test_no_memo_file_is_created_when_the_review_is_empty(
        tmp_path, monkeypatch, temp_journal):
    """The observable failure: a 129-byte file on disk that drop.py would
    carry to the CEO as if it were the day's review."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(review_bot, "journal", temp_journal)
    monkeypatch.setattr(review_bot, "collect_bundle",
                        lambda: {"date": "2026-09-21", "clock": "16:30 ET"})
    monkeypatch.setattr(review_bot, "request_review",
                        lambda b: {"text": "   "})
    posted = []
    monkeypatch.setattr(review_bot, "post_discord",
                        lambda *a, **k: posted.append(a[0]))

    assert review_bot.main() == 0
    assert not (tmp_path / "reports" / "review_2026-09-21.md").exists()
    assert posted and "empty" in posted[0].lower()


def test_the_empty_case_is_journaled_as_unavailable(
        tmp_path, monkeypatch, temp_journal):
    """A missing memo has to be visible in the ledger, or the only trace of
    the failure is a file that is not there."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(review_bot, "journal", temp_journal)
    monkeypatch.setattr(review_bot, "collect_bundle",
                        lambda: {"date": "2026-09-21", "clock": ""})
    monkeypatch.setattr(review_bot, "request_review", lambda b: {"text": ""})
    monkeypatch.setattr(review_bot, "post_discord", lambda *a, **k: None)

    review_bot.main()
    rows = temp_journal.governance_rows()
    hit = [r for r in rows if r["ticker"] == "DESK"]
    assert hit, "the empty review must appear in the ledger"


def test_a_real_memo_is_still_written_in_full(tmp_path, monkeypatch,
                                              temp_journal):
    """The guard must not cost the normal path anything."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(review_bot, "journal", temp_journal)
    monkeypatch.setattr(review_bot, "collect_bundle",
                        lambda: {"date": "2026-09-21",
                                 "clock": "16:30 ET  |  02:15 Nepal"})
    memo = "## Marks\nSPCX +1.1%, SWKS +0.2%\n\n## Grades\nA-\n" * 40
    monkeypatch.setattr(review_bot, "request_review",
                        lambda b: {"text": memo, "model": "claude-sonnet-5"})
    monkeypatch.setattr(review_bot, "post_discord", lambda *a, **k: None)

    assert review_bot.main() == 0
    written = (tmp_path / "reports" / "review_2026-09-21.md").read_text(
        encoding="utf-8")
    assert written.startswith("# Daily review — 2026-09-21")
    assert "16:30 ET" in written
    assert "SPCX +1.1%" in written
    assert len(written) > 1000            # not a 129-byte header


def test_the_log_line_states_the_size(tmp_path, monkeypatch, temp_journal,
                                      capsys):
    """"Memo written" alone was true on 09-21 and told nobody anything."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(review_bot, "journal", temp_journal)
    monkeypatch.setattr(review_bot, "collect_bundle",
                        lambda: {"date": "2026-09-21", "clock": ""})
    monkeypatch.setattr(review_bot, "request_review",
                        lambda b: {"text": "x" * 4321})
    monkeypatch.setattr(review_bot, "post_discord", lambda *a, **k: None)
    review_bot.main()
    assert "4321 chars" in capsys.readouterr().out


# ============================================ (2) the new desk facts

PROMPT = review_bot.review_system_prompt()


def test_the_soft_volume_fact_is_present():
    assert "(f) SOFT VOLUME" in PROMPT
    assert "FLAGGED soft_volume" in PROMPT
    assert "no longer refused in code" in PROMPT


def test_it_separates_reaching_the_gatekeeper_from_being_approved():
    """The specific misreading this exists to stop."""
    assert "do NOT read 'reached the gatekeeper' as 'approved by the rules'" \
        in PROMPT
    assert "the START of the decision, never the end of it" in PROMPT


def test_it_states_the_floor_that_still_applies():
    assert "Below 1.0x is still refused in code" in PROMPT


def test_the_reclaim_probation_fact_is_present():
    assert "(g) mean_reversion_reclaim IS ON PROBATION" in PROMPT
    assert "20 live trades" in PROMPT
    assert "ONE concurrent position" in PROMPT
    assert "prompt_version v5 ONLY" in PROMPT


def test_it_explains_why_the_count_reads_zero():
    """Without this the reviewer reports the 0/20 count as a lost ledger."""
    assert "0/20" in PROMPT
    assert "not a reset and not a lost record" in PROMPT


def test_it_puts_reclaim_in_the_probation_section():
    assert "PROBATION section alongside pullback_in_uptrend" in PROMPT


def test_the_facts_are_lettered_in_order():
    """(f) and (g) follow (e); an out-of-order list reads as a mistake and
    invites the reviewer to treat the block as unreliable."""
    positions = [PROMPT.index(f"  ({c}) ") for c in "abcdefg"]
    assert positions == sorted(positions)


def test_the_new_facts_sit_inside_the_settled_block():
    assert (PROMPT.index("DESK FACTS THE REVIEWER MUST NOT RE-FLAG")
            < PROMPT.index("(f) SOFT VOLUME")
            < PROMPT.index("(g) mean_reversion_reclaim")
            < PROMPT.index("HARD CONSTRAINT"))


def test_the_earlier_facts_all_survived():
    for marker in ("max(ATR trail, entry price)", "16:15 ET",
                   "NOK and ORCL, 2026-08-13", "ADVISORY and non-blocking",
                   "A WORKER RESTART is an ops artifact"):
        assert marker in PROMPT, marker


def test_the_reviewer_is_still_read_only():
    assert "HARD CONSTRAINT — YOU ARE READ-ONLY" in PROMPT


# ============================================ (3) S2(b) is still in force

def test_the_shadow_figure_is_computed_not_hardcoded():
    """S2(b) replaced a dated snapshot with a live query. The prompt must
    carry no frozen percentage — including 38%, which is a pre-fix number
    (the August connection_refused cluster) that the Ollama pre-flight has
    already made obsolete."""
    assert "{shadow_fact}" not in PROMPT
    for frozen in ("~38%", "38% error", "~11%", "as of 2026-09-05"):
        assert frozen not in PROMPT, frozen


def test_the_figure_matches_the_journal_right_now():
    live = review_bot.journal.shadow_error_rate()
    if live["total"]:
        assert f"approved {live['approved']} of {live['total']}" in PROMPT
        assert f"{live['rate_pct']}% error rate" in PROMPT
