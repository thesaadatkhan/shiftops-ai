"""Verify shift preferences against the representative worker examples.

Preferences are soft: nothing here affects eligibility or coverage. These
checks exist because an earlier generator matched patterns on start time
alone, which wrongly marked overlapping-but-different shifts as preferred.

Checks:

1. Priya's preferred 8:00 AM-2:00 PM pattern does not select the
   11:00 AM-5:00 PM shift (same hall, same day, start inside the old window).
2. Jordan's preferred 10:00 PM-3:00 AM pattern does not select the
   11:00 PM-5:00 AM shift (a different overnight shift).
3. Overnight shifts that genuinely match a pattern do receive it, with the
   correct end date on the following day.
4. Shifts matching no pattern have no preference row at all, which is how
   "neutral" is represented.
5. The four representative workers' stored rows equal an independently
   written expectation - the literal rows below, transcribed by hand from
   revision 3's patterns against the operating model, not read back from the
   generator constants under test.
6. Every worker's stored rows agree with what their declared patterns match.
   This one is a database/generator consistency check only: both sides come
   from `synthetic_data.py`, so it proves seeding and matching agree, not
   that the patterns themselves are right. Check 5 is what pins the
   representative patterns to revision 3.

Runs against a throwaway in-memory demo fixture. The project's own
`backend/shiftops.db` is never opened, so this works on a fresh checkout and
is unaffected by workers edited or removed through the application.

Run with:  python verify_preferences.py
Exits non-zero if any check fails.
"""

import sys

from demo_fixture import demo_fixture_connection
from synthetic_data import (
    REPRESENTATIVE_WORKERS,
    expand_preferences,
    generate_required_shifts,
    generate_workers,
)

TIME_FORMAT = "%Y-%m-%d %H:%M"

# Written out by hand from revision 3's preference patterns applied to the
# sample week (Mon 2026-10-05 to Sun 2026-10-11), independently of the
# generator constants these checks are testing. Patterns whose shift does not
# exist in the generated set contribute nothing, which is why Priya and Devon
# have no preferred rows at all:
#
#   SW-001 Vega Mon-Fri 17:00-22:00 preferred     -> 5 shifts exist
#          Sirius Sat 08:00-13:00 preferred       -> no such shift (Sat is 08:00-14:00)
#          Capella Sun 22:00-03:00 low            -> no such shift (Sun night is 19:00-00:00)
#   SW-002 Capella Mon-Fri 22:00-03:00 preferred  -> Mon-Thu only; Friday runs 23:00-05:00
#          Capella Thu 03:00-08:00 low            -> 1 shift exists
#          Sirius Mon-Fri 17:00-22:00 low         -> 5 shifts exist
#   SW-003 Andromeda Sat/Sun 08:00-14:00 preferred-> no such shift either day
#          Andromeda Mon-Fri 03:00-08:00 low      -> Tue-Fri only; Monday runs 04:00-08:00
#   SW-004 Helix Sat 08:00-13:00 preferred        -> no such shift (Sat is 08:00-14:00)
#          Helix Mon-Fri 22:00-02:00 low          -> 5 shifts exist
EXPECTED_REPRESENTATIVE_PREFERENCES = {
    "SW-001": {
        ("Vega", "2026-10-05 17:00", "2026-10-05 22:00"): "preferred",
        ("Vega", "2026-10-06 17:00", "2026-10-06 22:00"): "preferred",
        ("Vega", "2026-10-07 17:00", "2026-10-07 22:00"): "preferred",
        ("Vega", "2026-10-08 17:00", "2026-10-08 22:00"): "preferred",
        ("Vega", "2026-10-09 17:00", "2026-10-09 22:00"): "preferred",
    },
    "SW-002": {
        ("Capella", "2026-10-05 22:00", "2026-10-06 03:00"): "preferred",
        ("Capella", "2026-10-06 22:00", "2026-10-07 03:00"): "preferred",
        ("Capella", "2026-10-07 22:00", "2026-10-08 03:00"): "preferred",
        ("Capella", "2026-10-08 22:00", "2026-10-09 03:00"): "preferred",
        ("Capella", "2026-10-08 03:00", "2026-10-08 08:00"): "low",
        ("Sirius", "2026-10-05 17:00", "2026-10-05 22:00"): "low",
        ("Sirius", "2026-10-06 17:00", "2026-10-06 22:00"): "low",
        ("Sirius", "2026-10-07 17:00", "2026-10-07 22:00"): "low",
        ("Sirius", "2026-10-08 17:00", "2026-10-08 22:00"): "low",
        ("Sirius", "2026-10-09 17:00", "2026-10-09 22:00"): "low",
    },
    "SW-003": {
        ("Andromeda", "2026-10-06 03:00", "2026-10-06 08:00"): "low",
        ("Andromeda", "2026-10-07 03:00", "2026-10-07 08:00"): "low",
        ("Andromeda", "2026-10-08 03:00", "2026-10-08 08:00"): "low",
        ("Andromeda", "2026-10-09 03:00", "2026-10-09 08:00"): "low",
    },
    "SW-004": {
        ("Helix", "2026-10-05 22:00", "2026-10-06 02:00"): "low",
        ("Helix", "2026-10-06 22:00", "2026-10-07 02:00"): "low",
        ("Helix", "2026-10-07 22:00", "2026-10-08 02:00"): "low",
        ("Helix", "2026-10-08 22:00", "2026-10-09 02:00"): "low",
        ("Helix", "2026-10-09 22:00", "2026-10-10 02:00"): "low",
    },
}


def stored_preferences(connection, employee_code):
    rows = connection.execute(
        """
        SELECT s.hall, s.start_datetime, s.end_datetime, p.preference
        FROM shift_preferences p
        JOIN shifts s ON s.id = p.shift_id
        JOIN employees e ON e.id = p.employee_id
        WHERE e.employee_code = ?
        """,
        (employee_code,),
    ).fetchall()
    return {
        (row["hall"], row["start_datetime"], row["end_datetime"]): row["preference"]
        for row in rows
    }


def main():
    shifts = generate_required_shifts()
    # A pristine in-memory demo fixture, never the working database: the
    # expectations below describe the generated dataset, so a worker renamed
    # or removed through the application must not make them fail.
    connection = demo_fixture_connection()
    failures = []

    try:
        priya = stored_preferences(connection, "SW-003")
        jordan = stored_preferences(connection, "SW-002")
        maria = stored_preferences(connection, "SW-001")
        devon = stored_preferences(connection, "SW-004")

        # 1. An 11:00 AM-5:00 PM shift is not an 8:00 AM-2:00 PM shift.
        near_miss = ("Andromeda", "2026-10-10 11:00", "2026-10-10 17:00")
        if near_miss in priya:
            failures.append(
                "Priya's 8 AM-2 PM preference wrongly selected the 11 AM-5 PM shift"
            )
        else:
            print("PASS  Priya 8 AM-2 PM does not select Andromeda Sat 11:00-17:00")

        # 2. An 11:00 PM-5:00 AM shift is not a 10:00 PM-3:00 AM shift.
        overnight_near_miss = ("Capella", "2026-10-09 23:00", "2026-10-10 05:00")
        if overnight_near_miss in jordan:
            failures.append(
                "Jordan's 10 PM-3 AM preference wrongly selected the 11 PM-5 AM shift"
            )
        else:
            print("PASS  Jordan 10 PM-3 AM does not select Capella Fri 23:00-05:00")

        # 3. Overnight shifts that do match are stored, with next-day end dates.
        expected_overnight = {
            ("Capella", "2026-10-05 22:00", "2026-10-06 03:00"): "preferred",
            ("Capella", "2026-10-06 22:00", "2026-10-07 03:00"): "preferred",
            ("Capella", "2026-10-07 22:00", "2026-10-08 03:00"): "preferred",
            ("Capella", "2026-10-08 22:00", "2026-10-09 03:00"): "preferred",
        }
        for key, level in expected_overnight.items():
            if jordan.get(key) != level:
                failures.append(f"Jordan missing {level} overnight shift {key[1]}")
        if not failures:
            print(
                f"PASS  Jordan's 4 matching 22:00->03:00 overnight shifts stored as preferred"
            )

        devon_overnight = ("Helix", "2026-10-09 22:00", "2026-10-10 02:00")
        if devon.get(devon_overnight) != "low":
            failures.append("Devon missing low-preference Helix Fri 22:00-02:00")
        else:
            print("PASS  Devon's 22:00->02:00 overnight shift stored as low preference")

        # 4. Patterns absent from the generated shift set produce no rows, and
        #    unmatched shifts stay neutral.
        sirius_saturday = [
            key for key in maria if key[0] == "Sirius" and key[1].startswith("2026-10-10")
        ]
        if sirius_saturday:
            failures.append(
                "Maria's Sirius 8 AM-1 PM pattern matched a shift that does not exist"
            )
        else:
            print("PASS  Maria's absent Sirius 8 AM-1 PM template produced no rows")

        all_keys = {
            (shift["hall"], shift["start_datetime"], shift["end_datetime"])
            for shift in shifts
        }
        neutral = all_keys - set(priya)
        sample_neutral = ("Andromeda", "2026-10-10 11:00", "2026-10-10 17:00")
        if sample_neutral not in neutral:
            failures.append("Expected Andromeda Sat 11:00-17:00 to be neutral for Priya")
        else:
            print(
                f"PASS  {len(neutral)} of {len(all_keys)} shifts are neutral for Priya "
                "(no preference row)"
            )

        # 5. The representative workers match an independently written
        #    expectation, not the generator constants under test.
        for employee_code, expected in EXPECTED_REPRESENTATIVE_PREFERENCES.items():
            actual = stored_preferences(connection, employee_code)
            if actual != expected:
                only_stored = set(actual) - set(expected)
                only_expected = set(expected) - set(actual)
                failures.append(
                    f"{employee_code}: stored rows differ from the independent "
                    f"expectation (unexpected: {sorted(only_stored)}, "
                    f"missing: {sorted(only_expected)})"
                )
        if not failures:
            total = sum(len(rows) for rows in EXPECTED_REPRESENTATIVE_PREFERENCES.values())
            print(
                f"PASS  Representative workers match {total} independently listed "
                "rows derived from revision 3"
            )

        # 6. Database/generator consistency only - both sides come from
        #    synthetic_data.py, so this proves seeding and matching agree.
        for worker in generate_workers(shifts):
            expected = expand_preferences(worker["preferences"], shifts)
            actual = stored_preferences(connection, worker["employee_code"])
            if expected != actual:
                failures.append(
                    f"{worker['employee_code']}: stored preferences do not match patterns"
                )
        if not any("stored preferences" in failure for failure in failures):
            print(
                "PASS  All 30 workers' stored rows agree with their declared patterns "
                "(generator/database consistency)"
            )

        print("\nRepresentative worker preference rows:")
        for worker in REPRESENTATIVE_WORKERS:
            actual = stored_preferences(connection, worker["employee_code"])
            preferred = sum(1 for level in actual.values() if level == "preferred")
            low = sum(1 for level in actual.values() if level == "low")
            print(
                f"  {worker['employee_code']} {worker['full_name']:18s} "
                f"preferred={preferred} low={low}"
            )
    finally:
        connection.close()

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll preference checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
