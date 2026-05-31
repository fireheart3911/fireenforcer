"""
Pure vote-resolution logic for the council module. No Discord imports.

Threshold modes (denominator is always the total eligible council count):
  normal          : yes >= 51% of eligible
  two_thirds      : yes >= 67% of eligible
  unanimous       : yes >= 67% of eligible AND oppose == 0
  true_unanimous  : yes == 100% of eligible

Resolution at voting close:
  - passes  if yes meets the threshold
  - blocked if not passed AND oppose > yes + abstain
  - expired otherwise
"""

import math

THRESHOLD_MODES = {
    "normal":         {"label": "Normal (51%)",            "ratio": 0.51, "no_oppose": False, "all": False},
    "two_thirds":     {"label": "Two Thirds (67%)",        "ratio": 0.67, "no_oppose": False, "all": False},
    "unanimous":      {"label": "Unanimous (67%, no oppose)", "ratio": 0.67, "no_oppose": True,  "all": False},
    "true_unanimous": {"label": "True Unanimous (100%)",   "ratio": 1.0,  "no_oppose": False, "all": True},
}

DEFAULT_MODE = "normal"


def required_yes(mode: str, eligible: int) -> int:
    """Minimum yes votes needed to pass, given the mode and eligible count."""
    cfg = THRESHOLD_MODES[mode]
    if cfg["all"]:
        return eligible
    # ceil so that e.g. 51% of 11 = 5.61 -> needs 6
    return math.ceil(cfg["ratio"] * eligible)


def passes(mode: str, eligible: int, yes: int, oppose: int) -> bool:
    cfg = THRESHOLD_MODES[mode]
    if yes < required_yes(mode, eligible):
        return False
    if cfg["no_oppose"] and oppose > 0:
        return False
    return True


def resolve(mode: str, eligible: int, yes: int, oppose: int, abstain: int) -> str:
    """Return one of: 'passed', 'blocked', 'expired'."""
    if passes(mode, eligible, yes, oppose):
        return "passed"
    if oppose > yes + abstain:
        return "blocked"
    return "expired"