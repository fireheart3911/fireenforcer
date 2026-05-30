"""
Pure helper functions for the Elo system.
No Discord or bot references here — keeps logic testable in isolation.
"""

import re


def calculate_k_factor(matches_played: int) -> float:
    if matches_played <= 10:
        return 40.0
    elif matches_played <= 30:
        return 30.0
    return 20.0


def calculate_elo_change(
    player1_rating: float,
    player2_rating: float,
    player1_score: int,
    player2_score: int,
    k_factor: float,
) -> tuple[float, float]:
    """Return (p1_change, p2_change) for a single match."""
    if player1_score > player2_score:
        p1_result, p2_result = 1.0, 0.0
    elif player2_score > player1_score:
        p1_result, p2_result = 0.0, 1.0
    else:
        p1_result = p2_result = 0.5

    expected_p1 = 1 / (1 + 10 ** ((player2_rating - player1_rating) / 400))
    expected_p2 = 1 - expected_p1

    return k_factor * (p1_result - expected_p1), k_factor * (p2_result - expected_p2)


def parse_match_scores(text: str) -> list[dict]:
    """Parse 'X-Y' score tokens from free-form text.

    Rules
    -----
    - Each score: 0-5, total ≤ 9, no ties.
    - One player must reach 5 (win), OR it's a forfeit (lower total allowed).
    """
    raw = re.split(r"[,\s\n]+", text.strip())
    matches = []

    for token in raw:
        if not token:
            continue
        parts = token.split("-")
        if len(parts) != 2:
            raise ValueError(f"Invalid format: '{token}'. Use format X-Y (e.g., 5-4)")
        try:
            s1, s2 = int(parts[0]), int(parts[1])
        except ValueError:
            raise ValueError(f"Invalid numbers in: '{token}'")

        if not (0 <= s1 <= 5 and 0 <= s2 <= 5):
            raise ValueError(f"Individual scores must be 0-5: '{token}'")
        if s1 + s2 > 9:
            raise ValueError(f"Total score cannot exceed 9: '{token}' sums to {s1 + s2}")
        if s1 == s2:
            raise ValueError(f"Scores cannot be tied: '{token}'")
        if s1 != 5 and s2 != 5 and s1 + s2 >= 9:
            raise ValueError(
                f"Invalid score: '{token}'. One player must reach 5 to win, or forfeit with lower total."
            )

        matches.append({"player1_score": s1, "player2_score": s2})

    if not matches:
        raise ValueError("No valid matches found")

    return matches
