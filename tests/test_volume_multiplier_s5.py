"""
S5 (PM_PLAN.md / agenda 10.5): momentum's volume multiplier drops to 1.0.
Reclaim keeps 1.3. Config-only — no code changed.
"""

import json


def test_the_ratified_volume_multipliers():
    mults = json.load(open("bot_config.json", encoding="utf-8"))["volume_multipliers"]
    assert mults["momentum_continuation"] == 1.0
    assert mults["mean_reversion_reclaim"] == 1.3
