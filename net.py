"""
net.py — S13. Network timeouts and secret redaction for every process.

2026-10-12: snapshot.py sat for 24 hours in a TLS read on a Discord socket
that died when the Mac slept (no read timeout anywhere), and launchd will not
start a job whose previous run is still alive - so every later snapshot,
drop and Day-trades post silently never ran. The same logs printed the full
Discord webhook URL, token included.

install() is idempotent and process-wide:
  * every requests call without an explicit timeout gets (5 s connect,
    30 s read). That covers alpaca-py (its RESTClient passes no timeout and
    accepts none), Finnhub, discord_webhook and plain requests.
  * every log record and everything written to stdout/stderr has webhook
    URLs and API keys/tokens masked.

Per-library timeouts that the libraries accept directly:
  webhook()            Discord, 10 s
  ANTHROPIC_TIMEOUT_S  Anthropic client, 60 s
  YFINANCE_TIMEOUT_S   yfinance, 30 s

    python -m net script.py [args...]   # install(), then run the script
"""

import logging
import re
import runpy
import sys

CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 30
DEFAULT_TIMEOUT = (CONNECT_TIMEOUT_S, READ_TIMEOUT_S)
DISCORD_TIMEOUT_S = 10
YFINANCE_TIMEOUT_S = 30
ANTHROPIC_TIMEOUT_S = 60            # gatekeeper and every other call
# The daily review is one long, thinking-heavy generation: its own client,
# streaming, 600 s read (2026-10-14/15 both died at the shared 60 s).
REVIEW_READ_TIMEOUT_S = 600

_installed = False

# ============================================================ redaction

_PATTERNS = [
    # Discord webhooks: keep the host so a log still says WHERE it failed
    (re.compile(r"(https?://(?:[\w-]+\.)?discord(?:app)?\.com/api/webhooks/)"
                r"[^\s'\"<>)]+", re.I), r"\1<redacted>"),
    # ...and the bare path urllib3 prints ("Max retries exceeded with url:
    # /api/webhooks/<id>/<token>") - the leak that actually happened
    (re.compile(r"(?<![\w.])(/api/webhooks/)\d+/[^\s'\"<>)]+"), r"\1<redacted>"),
    # Anthropic keys
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-<redacted>"),
    # Authorization headers / bearer tokens
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]{8,}"), r"\1<redacted>"),
    # key=/token=/secret= style query or kwarg values
    (re.compile(r"(?i)\b((?:api[_-]?key|apikey|token|secret(?:[_-]?key)?|"
                r"password|access[_-]?key)[\"']?\s*[=:]\s*[\"']?)"
                r"[A-Za-z0-9._\-~+/]{6,}"), r"\1<redacted>"),
    # Alpaca key ids (PK/AK + 16-24 upper-alnum)
    (re.compile(r"\b(?:PK|AK)[A-Z0-9]{16,24}\b"), "<alpaca-key-redacted>"),
]


def redact(text):
    """Mask secrets in any string. Non-strings pass through untouched."""
    if not isinstance(text, str) or not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _install_log_redaction():
    """Every LogRecord from every logger: message and traceback redacted
    before any handler formats it (filters on one logger miss records that
    propagate from its children; the record factory does not)."""
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            record.msg = redact(record.getMessage())
            record.args = None
            if record.exc_info and not record.exc_text:
                record.exc_text = redact(
                    logging.Formatter().formatException(record.exc_info))
        except Exception:
            pass
        return record

    logging.setLogRecordFactory(factory)


class _RedactingStream:
    """Wraps stdout/stderr: print() of an exception that embeds a webhook
    URL is the leak that actually happened."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, s):
        return self._stream.write(redact(s))

    def writelines(self, lines):
        return self._stream.writelines(redact(x) for x in lines)

    def __getattr__(self, name):
        return getattr(self._stream, name)


# ============================================================ timeouts

def _install_requests_default():
    import requests

    original = requests.Session.request
    if getattr(original, "_s13_default_timeout", False):
        return

    def request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = DEFAULT_TIMEOUT
        return original(self, method, url, **kwargs)

    request._s13_default_timeout = True
    requests.Session.request = request


def install(streams: bool = True):
    """Idempotent. `streams=False` leaves stdout/stderr alone (Streamlit
    owns them in the dashboard process)."""
    global _installed
    if not _installed:
        _install_requests_default()
        _install_log_redaction()
        _installed = True
    if streams:
        for name in ("stdout", "stderr"):
            current = getattr(sys, name)
            if current is not None and not isinstance(current, _RedactingStream):
                setattr(sys, name, _RedactingStream(current))


def webhook(url, **kwargs):
    """A DiscordWebhook that can never hang: 10 s timeout unless the caller
    sets one."""
    from discord_webhook import DiscordWebhook
    kwargs.setdefault("timeout", DISCORD_TIMEOUT_S)
    return DiscordWebhook(url=url, **kwargs)


if __name__ == "__main__":
    # python -m net script.py args... : run a job with install() in force.
    if len(sys.argv) < 2:
        print("usage: python -m net script.py [args...]", file=sys.stderr)
        sys.exit(2)
    install()
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    sys.path.insert(0, ".")
    runpy.run_path(script, run_name="__main__")
