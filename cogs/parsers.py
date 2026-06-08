"""
Shared parsing helpers for dates, durations and clock times.

Used by the status (vacations, away) and signup (scheduling) modules so the
accepted formats stay consistent.

Conventions:
  • Absolute datetimes use European order: dd-mm-yyyy, optional hh:mm.
  • Durations are combos of d/h/m: "30m", "2h", "2h30m", "1d6h".
  • Clock times are HH:MM, interpreted in a supplied tz (next occurrence).
"""

import datetime
import re

_DURATION_RE = re.compile(r"(\d+)\s*([dhm])")
_DURATION_UNITS = {"d": 86400, "h": 3600, "m": 60}


# ---------------------------------------------------------------------------
# Durations  (timezone-independent)
# ---------------------------------------------------------------------------

def parse_duration_seconds(text: str) -> int:
    """Parse '24h', '2d', '1d6h' → seconds. Raises ValueError."""
    text = text.strip().lower()
    matches = _DURATION_RE.findall(text)
    consumed = "".join(f"{n}{u}" for n, u in matches)
    if not matches or consumed != re.sub(r"\s+", "", text):
        raise ValueError("Invalid duration. Use forms like 24h, 2d, 1d6h.")
    total = sum(int(n) * _DURATION_UNITS[u] for n, u in matches)
    if total <= 0:
        raise ValueError("Duration must be greater than zero.")
    return total


# ---------------------------------------------------------------------------
# Absolute datetime  dd-mm-yyyy [hh:mm]
# ---------------------------------------------------------------------------

def parse_eu_datetime(text: str, tz=None) -> datetime.datetime:
    """Parse 'dd-mm-yyyy' or 'dd-mm-yyyy hh:mm'.

    Returns a tz-aware datetime if tz is given (time defaults to 00:00).
    Raises ValueError on anything else.
    """
    text = text.strip()
    for fmt in ("%d-%m-%Y %H:%M", "%d-%m-%Y"):
        try:
            dt = datetime.datetime.strptime(text, fmt)
            return dt.replace(tzinfo=tz) if tz is not None else dt
        except ValueError:
            continue
    raise ValueError(f"Invalid date '{text}'. Use `dd-mm-yyyy` or `dd-mm-yyyy hh:mm`.")


# ---------------------------------------------------------------------------
# Away time  (duration | clock | indefinite)  — used by status away statuses
# ---------------------------------------------------------------------------

def parse_away_duration(text: str, tz=None) -> datetime.datetime | None:
    """Future datetime, or None for indefinite.

    "" / indefinite / none / -  → None
    duration combos              → now + delta (tz-independent)
    HH:MM                        → next occurrence in tz
    """
    text = text.strip().lower()
    if not text or text in ("indefinite", "none", "-"):
        return None

    if ":" in text and not _DURATION_RE.search(text):
        try:
            hour, minute = map(int, text.split(":"))
        except ValueError:
            raise ValueError("Invalid clock time. Use HH:MM, e.g. 14:30")
        now_tz = datetime.datetime.now(tz)
        rt = now_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if rt <= now_tz:
            rt += datetime.timedelta(days=1)
        return rt

    return datetime.datetime.now() + datetime.timedelta(seconds=parse_duration_seconds(text))


def parse_play_start(text: str, tz=None) -> datetime.datetime:
    """'playing since' time (counts up). Blank = now; duration = that long ago;
    HH:MM = today in tz (or yesterday if that's still in the future)."""
    text = text.strip().lower()
    if not text:
        return datetime.datetime.now()

    if ":" in text and not _DURATION_RE.search(text):
        try:
            hour, minute = map(int, text.split(":"))
        except ValueError:
            raise ValueError("Invalid clock time. Use HH:MM, e.g. 14:30")
        now_tz = datetime.datetime.now(tz)
        start = now_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start > now_tz:
            start -= datetime.timedelta(days=1)
        return start

    return datetime.datetime.now() - datetime.timedelta(seconds=parse_duration_seconds(text))