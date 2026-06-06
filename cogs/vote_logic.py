"""
Pure vote-resolution logic for the council module. No Discord imports.

Threshold modes (denominator is always the total eligible council count):
  normal          : strict majority (more than half) of eligible
  two_thirds      : at least two-thirds of eligible
  unanimous       : at least two-thirds of eligible AND oppose == 0
  true_unanimous  : all eligible vote yes

Resolution at voting close:
  - passes  if yes meets the threshold
  - blocked if not passed AND oppose > yes + abstain
  - expired otherwise
"""

import math
from fractions import Fraction

THRESHOLD_MODES = {
    "normal":         {"label": "Normal (51%)",                "kind": "majority",   "no_oppose": False},
    "two_thirds":     {"label": "Two Thirds (67%)",            "kind": "two_thirds", "no_oppose": False},
    "unanimous":      {"label": "Unanimous (67%, no oppose)",  "kind": "two_thirds", "no_oppose": True},
    "true_unanimous": {"label": "True Unanimous (100%)",       "kind": "all",        "no_oppose": False},
}

DEFAULT_MODE = "normal"


def required_yes(mode: str, eligible: int) -> int:
    """Minimum yes votes needed to pass, given the mode and eligible count."""
    kind = THRESHOLD_MODES[mode]["kind"]
    if kind == "all":
        return eligible
    if kind == "majority":
        # Strict majority — more than half (a tie does not pass).
        return eligible // 2 + 1
    if kind == "two_thirds":
        # Exact two-thirds, rounded up.
        return math.ceil(Fraction(2, 3) * eligible)
    raise ValueError(f"Unknown threshold kind: {kind}")


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