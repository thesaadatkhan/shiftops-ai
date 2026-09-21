"""Shared week parsing and boundary logic (Phase 7).

The one definition of "a valid reporting-week start" and its Monday-Sunday
boundary. Schedule preparation (`scheduling.py`), the read-only weekly
schedule query (`scheduling.py`), and the optional per-week employee
reporting parameter (`main.list_employees`) all import from here instead of
writing their own date rules, so they cannot drift into disagreeing about
what counts as a valid week or where one ends.

No timezone or DST handling here, consistent with D025's single local
simulation clock.
"""

import re
from datetime import datetime, timedelta

DATE_FORMAT = "%Y-%m-%d"

# Exactly four digits, a literal '-', two digits, a literal '-', two digits.
# strptime alone is not strict enough for this: it accepts unpadded values
# such as '2026-1-5', which would silently normalize a form nobody actually
# asked for rather than being rejected as malformed.
_STRICT_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvalidWeekStart(ValueError):
    """Not a strictly-formatted ISO 'YYYY-MM-DD' Monday. Maps to HTTP 400."""


def parse_week_start(week_start_text):
    """Parse and strictly validate a week_start string into a Monday datetime.

    Requires the exact 'YYYY-MM-DD' shape and a real calendar date that falls
    on a Monday. An arbitrary date is rejected outright rather than silently
    normalized onto the nearest Monday.
    """
    if not isinstance(week_start_text, str) or not _STRICT_DATE_PATTERN.match(
        week_start_text
    ):
        raise InvalidWeekStart(f"'{week_start_text}' is not a 'YYYY-MM-DD' date.")
    try:
        parsed = datetime.strptime(week_start_text, DATE_FORMAT)
    except ValueError as error:
        raise InvalidWeekStart(
            f"'{week_start_text}' is not a valid calendar date."
        ) from error
    if parsed.weekday() != 0:
        raise InvalidWeekStart(f"'{week_start_text}' is not a Monday.")
    return parsed


def validate_week_start_datetime(week_start):
    """Validate an already-parsed `week_start` value, not a string.

    Used by `synthetic_data.generate_required_shifts` to enforce, at the
    generator itself, the same invariant `parse_week_start` establishes from
    a string - so a caller that bypasses `parse_week_start` (by constructing
    a `datetime` directly, say) cannot silently generate a shift pattern for
    something that is not actually a valid week start. A value is accepted
    only if it is:

      - an actual `datetime` (not a `date`, a string, or any other type);
      - naive - no `tzinfo` (D025's single local simulation clock never
        carries a timezone; a timezone-aware value would compare unequal to
        every other stored, naive datetime in this project);
      - a real Monday;
      - exactly midnight (hour, minute, second and microsecond all zero) -
        a week always starts at the beginning of its Monday, never partway
        through it.

    Raises `InvalidWeekStart`, the same exception `parse_week_start` raises,
    so callers never need to distinguish which validator caught the problem.
    """
    if not isinstance(week_start, datetime):
        raise InvalidWeekStart(f"{week_start!r} is not a datetime.")
    if week_start.tzinfo is not None:
        raise InvalidWeekStart(
            f"{week_start!r} is timezone-aware; only a naive local datetime "
            "is accepted (D025)."
        )
    if week_start.weekday() != 0:
        raise InvalidWeekStart(f"{week_start!r} is not a Monday.")
    if (week_start.hour, week_start.minute, week_start.second, week_start.microsecond) != (
        0,
        0,
        0,
        0,
    ):
        raise InvalidWeekStart(f"{week_start!r} is not exactly midnight.")


def week_bounds(week_start):
    """Monday 00:00 through the following Monday 00:00, exclusive (D025)."""
    return week_start, week_start + timedelta(days=7)


def week_dates(week_start):
    """The seven calendar dates (Monday-Sunday) of one week, as ISO strings."""
    return [
        (week_start + timedelta(days=offset)).strftime(DATE_FORMAT)
        for offset in range(7)
    ]
