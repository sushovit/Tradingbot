"""
review_bot memo truncation (2026-09-24). No network, no trading behaviour.

THREE DAYS RUNNING. 09-21 produced 0 chars of memo, 09-22 24 chars, 09-23
584 chars, the last two cut mid-sentence. Every memo before them was 4-7 KB.

ROOT CAUSE. `max_tokens` is the WHOLE output allowance - thinking and answer
share it. At 8000, a thinking-capable model could spend the budget reasoning
and reach the answer with nothing left. The escalating lengths (0 -> 24 ->
584) are that same failure landing in slightly different places, not three
different faults.

THE FIX IS NOT budget_tokens. Sonnet 5, which is the configured review
model, REMOVED that parameter: sending {"type": "enabled", budget_tokens: N}
returns a 400 and would fail every review outright. `output_config.effort`
is the supported control over thinking depth. So: max_tokens 16000, adaptive
thinking, effort medium.

THE GUARD matters more than the budget. A bigger budget makes truncation
rarer; it cannot make it impossible, and a memo that stops after section 2
reads exactly like a complete review of a quiet day. Nothing is written or
posted unless stop_reason is end_turn AND the text reached section 5.
"""

import review_bot


# ============================================ the budget

def test_the_answer_budget_is_at_least_sixteen_thousand():
    assert review_bot.REVIEW_MAX_TOKENS >= 16000


def test_thinking_is_bounded_by_effort_not_budget_tokens():
    """Sonnet 5 400s on budget_tokens. If it ever appears in this file, every
    review fails at the API and the desk loses its memo silently."""
    with open("review_bot.py", encoding="utf-8") as f:
        src = f.read()
    call = src[src.index("resp = client.messages.create("):][:600]
    # Comment lines are stripped: the file explains WHY budget_tokens is not
    # used, and that prose must not be mistaken for the parameter itself.
    code = " ".join(line for line in call.splitlines()
                    if not line.strip().startswith("#"))
    assert "budget_tokens" not in code
    assert '"type": "adaptive"' in call
    assert 'output_config={"effort": REVIEW_EFFORT}' in call
    assert "max_tokens=max_tokens" in call


def test_the_effort_level_is_a_real_one():
    assert review_bot.REVIEW_EFFORT in ("low", "medium", "high", "xhigh",
                                        "max")


# ============================================ completeness

def test_a_memo_with_section_five_is_complete():
    assert review_bot.memo_is_complete("## 5. Tomorrow's Watch Items") is True


def test_a_memo_that_stops_early_is_not():
    """The 09-23 shape: real content, several sections, cut mid-sentence."""
    partial = ("## 1. Mark the Book\nFlat day.\n\n## 2. Grade the Decisions\n"
               "Nothing to grade.\n\n## 3. Anomalies\nThe 15:00 ET snapshot "
               "shows slightly different marks — SPCX $149.03/-$5.22, SWKS "
               "$90.56/+$0.")
    assert review_bot.memo_is_complete(partial) is False


def test_empty_and_none_are_not_complete():
    assert review_bot.memo_is_complete("") is False
    assert review_bot.memo_is_complete(None) is False


def test_the_marker_matches_what_the_model_actually_writes():
    """Checked against the memos that worked: both 09-17 and 09-18 use
    '## 5.'. A marker the model does not emit would reject every memo."""
    assert review_bot.REQUIRED_SECTION == "## 5."


# ============================================ the retry

class FakeUsage:
    def __init__(self, output_tokens):
        self.output_tokens = output_tokens
        self.input_tokens = 4000


class TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeResponse:
    def __init__(self, text, stop_reason="end_turn", output_tokens=8000):
        self.content = [TextBlock(text)] if text is not None else []
        self.stop_reason = stop_reason
        self.usage = FakeUsage(output_tokens)


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return (self.responses.pop(0) if self.responses
                else FakeResponse("truncated", "max_tokens"))


class FakeClient:
    def __init__(self, *responses):
        self.messages = FakeMessages(responses)


def patch_client(monkeypatch, client):
    monkeypatch.setattr(review_bot.claude_integration, "_get_client",
                        lambda: client)
    monkeypatch.setattr(review_bot.time, "sleep", lambda *_: None)
    monkeypatch.setattr(review_bot, "build_user_prompt",
                        lambda bundle: "(stub prompt)")


FULL_MEMO = ("# SESSION REVIEW\n## 1. Mark the Book\nflat\n"
             "## 2. Grade the Decisions\nnone\n## 3. Anomalies\nnone\n"
             "## 4. Probation Trades\nnone\n## 5. Tomorrow's Watch Items\n"
             "watch SPCX\n")


def test_a_max_tokens_response_is_rejected_and_retried(monkeypatch):
    """The order's first named test. A truncated memo must not be accepted,
    and the retry must not repeat the budget that just truncated."""
    client = FakeClient(
        FakeResponse("## 1. Mark the Book\ncut off mid-sen", "max_tokens",
                     16000),
        FakeResponse(FULL_MEMO, "end_turn", 5000))
    patch_client(monkeypatch, client)

    result = review_bot.request_review({"date": "2026-09-23"})

    assert "error" not in result
    assert result["text"] == FULL_MEMO
    assert len(client.messages.calls) == 2
    assert client.messages.calls[0]["max_tokens"] == \
        review_bot.REVIEW_MAX_TOKENS
    assert client.messages.calls[1]["max_tokens"] == \
        review_bot.REVIEW_MAX_TOKENS * 2


def test_an_end_turn_with_all_five_sections_is_written_unchanged(monkeypatch):
    """The order's second named test. One call, no retry, text untouched."""
    client = FakeClient(FakeResponse(FULL_MEMO, "end_turn", 5000))
    patch_client(monkeypatch, client)

    result = review_bot.request_review({"date": "2026-09-23"})

    assert result["text"] == FULL_MEMO
    assert len(client.messages.calls) == 1


def test_end_turn_alone_is_not_enough(monkeypatch):
    """A response can finish cleanly and still be short of section 5 — that
    is the case a stop_reason check on its own would wave through."""
    short = "## 1. Mark the Book\nflat\n## 2. Grade\nnothing\n"
    client = FakeClient(FakeResponse(short, "end_turn", 400),
                        FakeResponse(FULL_MEMO, "end_turn", 5000))
    patch_client(monkeypatch, client)
    assert review_bot.request_review({"date": "x"})["text"] == FULL_MEMO
    assert len(client.messages.calls) == 2


def test_section_five_alone_is_not_enough(monkeypatch):
    """And a max_tokens cut that happens to reach section 5 is still a cut."""
    client = FakeClient(
        FakeResponse(FULL_MEMO + "## 6. extra", "max_tokens", 16000),
        FakeResponse(FULL_MEMO, "end_turn", 5000))
    patch_client(monkeypatch, client)
    review_bot.request_review({"date": "x"})
    assert len(client.messages.calls) == 2


def test_persistent_truncation_returns_an_error_with_diagnostics(monkeypatch):
    client = FakeClient(*[FakeResponse("## 1. cut", "max_tokens", 16000)
                          for _ in range(4)])
    patch_client(monkeypatch, client)

    result = review_bot.request_review({"date": "2026-09-23"})

    assert "error" in result
    assert "incomplete review" in result["error"]
    assert result["diagnostics"]["stop_reason"] == "max_tokens"
    assert result["diagnostics"]["output_tokens"] == 16000
    assert result["diagnostics"]["has_section_5"] is False


def test_every_call_logs_what_it_was_given_and_what_came_back(
        monkeypatch, capsys):
    """Root-cause first: the three lost memos recorded none of this."""
    patch_client(monkeypatch, FakeClient(FakeResponse(FULL_MEMO, "end_turn",
                                                      5000)))
    review_bot.request_review({"date": "2026-09-23"})
    out = capsys.readouterr().out
    for field in ("stop_reason=", "output_tokens=", "max_tokens=",
                  "effort=", "thinking=adaptive", "text_chars=",
                  "has_section_5="):
        assert field in out, field


# ============================================ a failed run is visible

def test_a_failed_run_writes_a_one_line_stub(tmp_path, monkeypatch,
                                             temp_journal):
    """A missing file reads as 'not run yet'. The stub reads as 'ran and
    failed', which is the true state and the actionable one."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(review_bot, "journal", temp_journal)
    monkeypatch.setattr(review_bot, "collect_bundle",
                        lambda date=None: {"date": "2026-09-23",
                                           "clock": "16:30 ET"})
    monkeypatch.setattr(review_bot, "request_review", lambda b: {
        "error": "incomplete review",
        "diagnostics": {"stop_reason": "max_tokens", "output_tokens": 16000}})
    posted = []
    monkeypatch.setattr(review_bot, "post_discord",
                        lambda *a, **k: posted.append(a[0]))

    assert review_bot.main([]) == 0

    written = (tmp_path / "reports" / "review_2026-09-23.md").read_text(
        encoding="utf-8")
    assert "GENERATION FAILED: max_tokens, 16000 output tokens" in written
    assert "# Daily review — 2026-09-23" in written
    assert posted and "GENERATION FAILED" in posted[0]


def test_an_incomplete_memo_never_reaches_disk_as_a_memo(
        tmp_path, monkeypatch, temp_journal):
    """Supersedes the 09-22 behaviour of writing nothing: the run is now
    recorded, but a partial memo is still never presented as a memo."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(review_bot, "journal", temp_journal)
    monkeypatch.setattr(review_bot, "collect_bundle",
                        lambda date=None: {"date": "2026-09-23", "clock": ""})
    monkeypatch.setattr(review_bot, "request_review",
                        lambda b: {"text": "## 1. Mark the Book\ncut off"})
    monkeypatch.setattr(review_bot, "post_discord", lambda *a, **k: None)

    review_bot.main([])
    written = (tmp_path / "reports" / "review_2026-09-23.md").read_text(
        encoding="utf-8")
    assert "GENERATION FAILED" in written
    assert "Mark the Book" not in written


def test_a_complete_memo_is_written_verbatim(tmp_path, monkeypatch,
                                             temp_journal):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(review_bot, "journal", temp_journal)
    monkeypatch.setattr(review_bot, "collect_bundle",
                        lambda date=None: {"date": "2026-09-23",
                                           "clock": "16:30 ET"})
    monkeypatch.setattr(review_bot, "request_review",
                        lambda b: {"text": FULL_MEMO, "model": "m"})
    monkeypatch.setattr(review_bot, "post_discord", lambda *a, **k: None)

    assert review_bot.main([]) == 0
    written = (tmp_path / "reports" / "review_2026-09-23.md").read_text(
        encoding="utf-8")
    assert FULL_MEMO in written
    assert "GENERATION FAILED" not in written


# ============================================ backfill

def test_a_backfill_reads_that_session_not_today():
    bundle = review_bot.collect_bundle(date="2026-09-22")
    assert bundle["date"] == "2026-09-22"
    assert bundle.get("drop_date") == "2026-09-22"


def test_a_backfill_warns_that_the_positions_are_current():
    """The broker reports the book as it stands now. Without this the
    reviewer would read today's marks as that day's close and 'reconcile'
    two unrelated sets of numbers."""
    bundle = review_bot.collect_bundle(date="2026-09-22")
    assert "BACKFILL" in bundle["backfill"]
    assert "CURRENT, not" in bundle["backfill"]
    assert "BACKFILL" in review_bot.build_user_prompt(bundle)


def test_a_normal_run_carries_no_backfill_note():
    assert "backfill" not in review_bot.collect_bundle()


def test_the_drop_is_selected_by_date_not_by_latest():
    """drop/latest.md is a moving pointer; a backfill must name its file."""
    assert review_bot.newest_drop_for("2026-09-22").endswith(
        "session_ET2026-09-22_1501.md")
    assert review_bot.newest_drop_for("1999-01-01") is None
