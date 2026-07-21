"""run_worker.py — start the live bot worker as a standalone process.

Equivalent to the dashboard's Start button, but headless: creates the run
lock and runs live_bot_worker() in THIS process. The dashboard's Stop button
(which removes bot.run) still stops it; the dashboard Start button stays
disabled while the lock exists, so two workers can never run at once.
"""
import os

with open("bot.run", "w"):
    pass

import streamlit_app  # noqa: E402  (bare-mode import)

streamlit_app.live_bot_worker()
