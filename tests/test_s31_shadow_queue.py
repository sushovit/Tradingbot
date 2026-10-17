"""S31 (CEO 2026-10-17) - the shadow queue: FIFO, one inference at a time,
45 s counted from the inference start, items older than 10 min dropped."""

import threading
import time

import analyst


def job(t):
    return {"gk_kwargs": {}, "ticker": t, "setup_name": "s", "context": {},
            "claude_verdict": {"approved": False}}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(infer=None, max_age=600):
    seen, clock = [], Clock()
    q = analyst.ShadowQueue(infer=infer or (lambda j: {"approved": False}),
                            record=lambda j, v: seen.append((j["ticker"], v)),
                            max_age_s=max_age, clock=clock)
    q.submit = _no_thread(q)          # drive it by hand with run_one()
    return q, seen, clock


def _no_thread(q):
    def submit(j):
        q._q.put(dict(j, enqueued=q._clock()))
    return submit


def test_fifo_and_every_item_of_a_burst_is_answered():
    q, seen, _ = make()
    for t in "ABCDE":
        q.submit(job(t))
    while q.run_one(block=False):
        pass
    assert [t for t, _ in seen] == list("ABCDE")
    assert all("error" not in v for _, v in seen)


def test_queue_wait_is_not_counted_against_the_45s():
    """The fifth signal of the open waits ~4 x 40 s = 160 s, then still gets
    its own full inference (S28 timed 4 of 5 out by design)."""
    clock = Clock()
    def infer(j):
        clock.t += 40.0               # each inference takes 40 s
        return {"approved": False}
    q, seen, _ = make(infer=infer)
    q._clock = clock
    for t in "ABCDE":
        q.submit(job(t))
    while q.run_one(block=False):
        pass
    assert [v for _, v in seen] == [{"approved": False}] * 5


def test_items_older_than_10_min_are_dropped_not_run():
    ran = []
    q, seen, clock = make(infer=lambda j: ran.append(j["ticker"]) or {"approved": False})
    q.submit(job("OLD"))
    clock.t = 601
    q.submit(job("NEW"))
    while q.run_one(block=False):
        pass
    assert ran == ["NEW"]
    assert seen[0] == ("OLD", {"error": "dropped_stale", "source": "local_shadow"})
    assert seen[1][0] == "NEW" and "error" not in seen[1][1]


def test_one_inference_at_a_time():
    live, peak, lock = [0], [0], threading.Lock()
    def infer(j):
        with lock:
            live[0] += 1; peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return {"approved": False}
    done = []
    q = analyst.ShadowQueue(infer=infer, record=lambda j, v: done.append(1))
    for t in "ABCDE":
        q.submit(job(t))
    deadline = time.monotonic() + 5
    while len(done) < 5 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(done) == 5 and peak[0] == 1


def test_submit_returns_at_once_and_a_crash_does_not_stop_the_queue():
    done = []
    def infer(j):
        if j["ticker"] == "BAD":
            raise RuntimeError("boom")
        time.sleep(0.2)
        return {"approved": False}
    q = analyst.ShadowQueue(infer=infer, record=lambda j, v: done.append(j["ticker"]))
    t0 = time.monotonic()
    q.submit(job("BAD")); q.submit(job("OK"))
    assert time.monotonic() - t0 < 0.1
    deadline = time.monotonic() + 3
    while "OK" not in done and time.monotonic() < deadline:
        time.sleep(0.02)
    assert done == ["OK"]


def test_the_45s_starts_with_the_inference(monkeypatch):
    """_infer times out a hung call SHADOW_TIMEOUT_SECS after it started,
    single attempt (a retry would push the queue behind it)."""
    calls = []
    def hung(**k):
        calls.append(k.get("max_retries"))
        time.sleep(2)
        return {"approved": True}
    monkeypatch.setattr(analyst.local_analyst, "get_gatekeeper_decision", hung)
    monkeypatch.setattr(analyst, "SHADOW_TIMEOUT_SECS", 0.3)
    t0 = time.monotonic()
    v = analyst._infer(job("X"))
    assert v == {"error": "timeout", "source": "local_shadow"}
    assert time.monotonic() - t0 < 1.0 and calls == [1]


def test_an_http_timeout_is_journaled_as_timeout(monkeypatch):
    monkeypatch.setattr(analyst.local_analyst, "get_gatekeeper_decision",
                        lambda **k: {"error": "Local analyst failed after 1 attempts: read_timeout: x"})
    assert analyst._infer(job("X"))["error"] == "timeout"


def test_memory_cap_unchanged():
    import local_analyst
    assert local_analyst.NUM_CTX == 8192 and local_analyst.REQUEST_TIMEOUT == 45
    assert analyst.SHADOW_MAX_AGE_SECS == 600
