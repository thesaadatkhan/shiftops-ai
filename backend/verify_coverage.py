"""Verify the generated required shifts against the operating model.

Checks, per hall and overall:

1. Total student coverage equals 489 hours/week (docs/PROJECT_SPEC.md).
2. Shifts do not overlap each other (no unintended double coverage).
3. Shifts leave no gap inside a required student-coverage period.
4. No shift covers a period that is not required (professional hours or
   hours when the desk is closed).
5. Every shift requires exactly one worker, lasts a positive whole number
   of hours, and carries the correct end date when it crosses midnight.

Runs against a throwaway in-memory demo fixture. The project's own
`backend/shiftops.db` is never opened, so this works on a fresh checkout and
is unaffected by workers edited or removed through the application.

Run with:  python verify_coverage.py
Exits non-zero if any check fails.
"""

import sys
from datetime import datetime

from demo_fixture import demo_fixture_connection
from reporting import InvalidWorkDuration, shift_duration_hours
from synthetic_data import (
    ALL_HALLS,
    hall_open_periods,
    professional_periods,
    subtract_periods,
)

TIME_FORMAT = "%Y-%m-%d %H:%M"
EXPECTED_TOTAL_HOURS = 489


def hours(periods):
    return sum((end - start).total_seconds() for start, end in periods) / 3600


def load_shifts_by_hall():
    # A pristine in-memory demo fixture, never the working database: these
    # figures describe the generated dataset, not whatever anyone has since
    # edited through the application.
    connection = demo_fixture_connection()
    try:
        rows = connection.execute(
            """
            SELECT hall, start_datetime, end_datetime, required_staff
            FROM shifts
            ORDER BY hall, start_datetime
            """
        ).fetchall()
    finally:
        connection.close()

    by_hall = {hall: [] for hall in ALL_HALLS}
    for row in rows:
        by_hall.setdefault(row["hall"], []).append(
            (
                datetime.strptime(row["start_datetime"], TIME_FORMAT),
                datetime.strptime(row["end_datetime"], TIME_FORMAT),
                row["required_staff"],
            )
        )
    return by_hall


def merge(periods):
    merged = []
    for start, end in sorted(periods):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def main():
    by_hall = load_shifts_by_hall()
    blocks = professional_periods()
    failures = []
    total_hours = 0

    for hall in ALL_HALLS:
        required = subtract_periods(hall_open_periods(hall), blocks)
        shifts = by_hall.get(hall, [])
        covered = [(start, end) for start, end, _ in shifts]
        hall_hours = hours(covered)
        total_hours += hall_hours

        for index in range(1, len(covered)):
            if covered[index][0] < covered[index - 1][1]:
                failures.append(f"{hall}: shifts overlap at {covered[index][0]}")

        if merge(covered) != merge(required):
            failures.append(f"{hall}: covered periods do not match required periods")

        if hall_hours != hours(required):
            failures.append(
                f"{hall}: {hall_hours} covered hours != {hours(required)} required"
            )

        for start, end, required_staff in shifts:
            if required_staff != 1:
                failures.append(f"{hall}: shift at {start} requires {required_staff} workers")
            try:
                # Work shifts must be a positive whole number of hours.
                shift_duration_hours(start, end, label=f"at {hall}")
            except InvalidWorkDuration as error:
                failures.append(f"{hall}: {error}")

        crossing = sum(1 for start, end, _ in shifts if start.date() != end.date())
        print(
            f"{hall:10s} shifts={len(shifts):3d} hours={hall_hours:6.1f} "
            f"required={hours(required):6.1f} cross-midnight={crossing}"
        )

    print(f"\ntotal student coverage hours: {total_hours}")
    print(f"expected (PROJECT_SPEC.md):   {EXPECTED_TOTAL_HOURS}")

    if total_hours != EXPECTED_TOTAL_HOURS:
        failures.append(f"total {total_hours} != expected {EXPECTED_TOTAL_HOURS}")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll coverage checks passed: no gaps, no double coverage, 489 hours.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
