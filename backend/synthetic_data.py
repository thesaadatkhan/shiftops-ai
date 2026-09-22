"""Deterministic synthetic data generation for ShiftOps AI.

Nothing in this module touches the database; `seed.py` is the only module
that writes rows. Everything here is a pure function of the constants below,
so re-running produces byte-identical data (a fixed RNG seed is used for the
generated workers). That is what makes seeding safely repeatable.

All people, course labels, preferences, and leave periods are fictional.

Canonical demo weeks: Monday 2026-09-21 through Sunday 2026-10-04 (see D025
for the date/time conventions). ``WEEK_START`` remains the default reporting
week; demo initialization explicitly generates it and the following Monday.

Required shifts are derived from the operating model rather than hard-coded:

    student coverage = hall open hours - professional staff hours

Professional staff cover every hall Monday-Friday 08:00-17:00. Andromeda and
Capella are open 24/7; Vega, Helix and Sirius are open 08:00-02:00. That
yields 123 student hours/week at each 24/7 hall and 81 at each 18-hour hall,
totalling 489 - the figure in docs/PROJECT_SPEC.md.

Approved leave periods below are all already-approved (D022): each was
constructed so the request would have preceded the leave by at least the
fictional one-week notice rule. Only the approved period itself is stored -
there is no request/approval workflow or pending status in Phase 5.
"""

import random
from datetime import datetime, time, timedelta

import weeks

TIME_FORMAT = "%Y-%m-%d %H:%M"

WEEK_START = datetime(2026, 9, 21)
WEEK_END = WEEK_START + timedelta(days=7)
SECOND_WEEK_START = WEEK_END
DEMO_WEEK_STARTS = (WEEK_START, SECOND_WEEK_START)

HALLS_24_HOUR = ["Andromeda", "Capella"]
HALLS_LIMITED = ["Vega", "Helix", "Sirius"]
ALL_HALLS = HALLS_24_HOUR + HALLS_LIMITED

PROFESSIONAL_START_HOUR = 8
PROFESSIONAL_END_HOUR = 17
LIMITED_OPEN_HOUR = 8
LIMITED_CLOSE_HOUR = 2

MAX_SHIFT_HOURS = 6

UNDERGRADUATE_COUNT = 20
MASTERS_COUNT = 10

# Identities (names, courses, class times) and preferences are drawn from
# two independent random streams. With a single stream, changing anything
# about preference generation would shift every later draw and silently
# rename workers and reshuffle their timetables.
IDENTITY_SEED = 20260921
PREFERENCE_SEED = 20260922
LEAVE_SEED = 20260923

# Named constraints anchor two demo stories; the remaining leave periods are
# fixed-seed background variation. These workers are deliberately unavailable
# for two of the hand-selected uncovered shifts, making the reason visible in
# Coverage without making the scenario depend on an RNG draw.
CANONICAL_LEAVE_OVERRIDES = {
    "SW-001": ("2026-09-21 17:00", "2026-09-21 22:00"),
    "SW-002": ("2026-09-28 17:00", "2026-09-28 22:00"),
}


# --------------------------------------------------------------------------
# Required shift generation
# --------------------------------------------------------------------------


def _day(week_start, offset, hour=0):
    return week_start + timedelta(days=offset, hours=hour)


def hall_open_periods(hall, week_start=WEEK_START):
    """Periods the desk is open, clipped to the given Monday week."""
    week_end = week_start + timedelta(days=7)
    if hall in HALLS_24_HOUR:
        return [(week_start, week_end)]

    periods = []
    for day_offset in range(-1, 7):
        opens = _day(week_start, day_offset, LIMITED_OPEN_HOUR)
        closes = _day(week_start, day_offset + 1, LIMITED_CLOSE_HOUR)
        start = max(opens, week_start)
        end = min(closes, week_end)
        if start < end:
            periods.append((start, end))
    return periods


def professional_periods(week_start=WEEK_START):
    """Monday-Friday 08:00-17:00, the same at every hall."""
    return [
        (
            _day(week_start, day_offset, PROFESSIONAL_START_HOUR),
            _day(week_start, day_offset, PROFESSIONAL_END_HOUR),
        )
        for day_offset in range(5)
    ]


def subtract_periods(periods, blocks):
    """Remove every block from every period, keeping the remainder in order."""
    remaining = list(periods)
    for block_start, block_end in blocks:
        next_remaining = []
        for start, end in remaining:
            if block_end <= start or block_start >= end:
                next_remaining.append((start, end))
                continue
            if start < block_start:
                next_remaining.append((start, block_start))
            if block_end < end:
                next_remaining.append((block_end, end))
        remaining = next_remaining
    return remaining


def split_period(start, end):
    """Split one coverage period into shifts of at most MAX_SHIFT_HOURS.

    Chunks are as even as possible, so a 15-hour weekday period becomes
    5 + 5 + 5 (17:00-22:00, 22:00-03:00, 03:00-08:00) and a 9-hour period
    becomes 5 + 4 (17:00-22:00, 22:00-02:00), matching the shift patterns in
    docs/PROJECT_SPEC.md section 7 without hard-coding them.
    """
    total_hours = int((end - start).total_seconds() // 3600)
    chunk_count = -(-total_hours // MAX_SHIFT_HOURS)
    base_hours, remainder = divmod(total_hours, chunk_count)

    shifts = []
    cursor = start
    for index in range(chunk_count):
        hours = base_hours + (1 if index < remainder else 0)
        shift_end = cursor + timedelta(hours=hours)
        shifts.append((cursor, shift_end))
        cursor = shift_end
    return shifts


def generate_required_shifts(week_start=None):
    """Every student-covered shift for one Monday week, one worker each.

    Defaults to the first canonical demo week (2026-09-21) - the behavior demo
    initialization, the semester migration's expected timetable, and every
    existing verification script rely on. Passing another value generates
    the identical hall-coverage pattern - same halls, same relative offsets,
    same required_staff, same cross-midnight end dates - shifted onto that
    week; nothing about the operating model itself changes. An explicitly
    supplied `week_start` is enforced by `weeks.validate_week_start_datetime`:
    it must be an actual naive (no timezone) `datetime` that is exactly
    midnight on a real Monday, the same invariant `weeks.parse_week_start`
    establishes when parsing a `week_start` string. Rejecting an invalid
    value here, not just at the string-parsing boundary, means a caller
    cannot silently generate a shift pattern for something that was never
    actually a valid week start.
    """
    if week_start is None:
        week_start = WEEK_START
    else:
        weeks.validate_week_start_datetime(week_start)
    shifts = []
    blocks = professional_periods(week_start)
    for hall in ALL_HALLS:
        student_periods = subtract_periods(hall_open_periods(hall, week_start), blocks)
        for period_start, period_end in student_periods:
            for shift_start, shift_end in split_period(period_start, period_end):
                shifts.append(
                    {
                        "hall": hall,
                        "start_datetime": shift_start.strftime("%Y-%m-%d %H:%M"),
                        "end_datetime": shift_end.strftime("%Y-%m-%d %H:%M"),
                        "required_staff": 1,
                    }
                )
    return shifts


# --------------------------------------------------------------------------
# Synthetic workers
# --------------------------------------------------------------------------

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY = 0, 1, 2, 3, 4

# The four representative workers from .ai/SYNTHETIC_WORKER_EXAMPLES.md
# revision 3, transcribed exactly: course labels, meeting days and times,
# approved leave, and shift preferences all match that document. Preferences
# are whole shift patterns (hall, days, start, end) rather than time windows,
# so a pattern matches only a shift with exactly that start AND end. An end
# time at or before the start time means the shift runs into the next day.
#
# Some of these patterns deliberately match no generated shift - revision 3
# states that a preference for a template absent from the final shift set
# creates no rows and no coverage requirement. Those patterns are kept here
# as written rather than widened to force a match.
REPRESENTATIVE_WORKERS = [
    {
        "employee_code": "SW-001",
        "full_name": "Maria Alvarez",
        "student_type": "undergraduate",
        "courses": [
            ("Writing A", [(MONDAY, "09:00", "10:15"), (WEDNESDAY, "09:00", "10:15")]),
            ("History A", [(MONDAY, "13:00", "14:15"), (WEDNESDAY, "13:00", "14:15")]),
            ("Mathematics A", [(TUESDAY, "16:00", "17:15"), (THURSDAY, "16:00", "17:15")]),
        ],
        "approved_leave": [],
        # Revision 3: preferred Vega Mon-Fri 5:00 PM-10:00 PM; preferred
        # Sirius Saturday 8:00 AM-1:00 PM; low Capella Sunday 10:00 PM -
        # Monday 3:00 AM.
        "preferences": [
            ("Vega", (0, 1, 2, 3, 4), "17:00", "22:00", "preferred"),
            ("Sirius", (5,), "08:00", "13:00", "preferred"),
            ("Capella", (6,), "22:00", "03:00", "low"),
        ],
    },
    {
        "employee_code": "SW-002",
        "full_name": "Jordan Kim",
        "student_type": "masters",
        "courses": [
            ("Distributed Systems A", [(THURSDAY, "08:00", "10:45")]),
            ("Policy Seminar A", [(MONDAY, "14:00", "16:45")]),
            ("Data Ethics A", [(WEDNESDAY, "18:00", "20:45")]),
        ],
        "approved_leave": [],
        # Revision 3: preferred Capella Mon-Fri starts 10:00 PM - next day
        # 3:00 AM; low Capella Thursday 3:00 AM-8:00 AM; low Sirius Mon-Fri
        # 5:00 PM-10:00 PM.
        "preferences": [
            ("Capella", (0, 1, 2, 3, 4), "22:00", "03:00", "preferred"),
            ("Capella", (3,), "03:00", "08:00", "low"),
            ("Sirius", (0, 1, 2, 3, 4), "17:00", "22:00", "low"),
        ],
    },
    {
        "employee_code": "SW-003",
        "full_name": "Priya Natarajan",
        "student_type": "undergraduate",
        "courses": [
            ("Biology A", [(MONDAY, "10:00", "11:15"), (WEDNESDAY, "10:00", "11:15")]),
            ("Chemistry A", [(MONDAY, "15:00", "16:15"), (WEDNESDAY, "15:00", "16:15")]),
            ("Psychology A", [(TUESDAY, "13:00", "14:15"), (THURSDAY, "13:00", "14:15")]),
        ],
        "approved_leave": [("2026-10-10 08:00", "2026-10-10 14:00")],
        # Revision 3: preferred Andromeda Saturday and Sunday 8:00 AM-2:00 PM;
        # low Andromeda Mon-Fri 3:00 AM-8:00 AM.
        "preferences": [
            ("Andromeda", (5, 6), "08:00", "14:00", "preferred"),
            ("Andromeda", (0, 1, 2, 3, 4), "03:00", "08:00", "low"),
        ],
    },
    {
        "employee_code": "SW-004",
        "full_name": "Devon Brooks",
        "student_type": "masters",
        "courses": [
            ("Policy Analysis A", [(TUESDAY, "16:15", "19:00")]),
            ("Applied Statistics A", [(THURSDAY, "09:00", "11:45")]),
        ],
        "approved_leave": [],
        # Revision 3: preferred Helix Saturday 8:00 AM-1:00 PM; low Helix
        # Mon-Fri starts 10:00 PM - next day 2:00 AM.
        "preferences": [
            ("Helix", (5,), "08:00", "13:00", "preferred"),
            ("Helix", (0, 1, 2, 3, 4), "22:00", "02:00", "low"),
        ],
    },
]

FIRST_NAMES = [
    "Amara", "Bennett", "Carmen", "Dmitri", "Elena", "Farid", "Grace", "Hassan",
    "Imani", "Julian", "Kavya", "Lorenzo", "Mei", "Nadia", "Omar", "Paloma",
    "Quinn", "Rosa", "Samuel", "Tomas", "Uma", "Victor", "Wren", "Xiomara",
    "Yusuf", "Zara", "Adrian", "Blythe", "Cyrus", "Delia",
]

LAST_NAMES = [
    "Okonkwo", "Salazar", "Whitfield", "Petrov", "Nakamura", "Abadi", "Lindqvist",
    "Moreau", "Castellanos", "Ferreira", "Bhatt", "Novak", "Osei", "Halvorsen",
    "Rahimi", "Quintero", "Delacroix", "Marchetti", "Ibarra", "Sandoval",
    "Thackeray", "Vasquez", "Ashford", "Bergstrom", "Cardoso", "Dupree",
    "Eriksen", "Fontaine", "Gallardo", "Hidalgo",
]

SUBJECTS = [
    "Writing", "History", "Mathematics", "Biology", "Chemistry", "Psychology",
    "Economics", "Sociology", "Physics", "Statistics", "Philosophy", "Geology",
    "Linguistics", "Anthropology", "Marketing", "Astronomy",
]

GRADUATE_SUBJECTS = [
    "Distributed Systems", "Policy Seminar", "Data Ethics", "Policy Analysis",
    "Applied Statistics", "Operations Research", "Urban Planning",
    "Machine Learning", "Public Finance", "Research Methods",
]

# Undergraduate meetings are 75 minutes on a Mon/Wed or Tue/Thu pair (D023).
UNDERGRADUATE_SLOTS = [
    (days, start, end)
    for days in ((MONDAY, WEDNESDAY), (TUESDAY, THURSDAY))
    for start, end in [
        ("08:00", "09:15"),
        ("09:30", "10:45"),
        ("11:00", "12:15"),
        ("12:30", "13:45"),
        ("14:00", "15:15"),
        ("15:30", "16:45"),
        ("16:00", "17:15"),
        ("17:00", "18:15"),
    ]
]

# Master's meetings are a single 165-minute block per course (D023).
MASTERS_SLOTS = [
    ((day,), start, end)
    for day in (MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY)
    for start, end in [
        ("08:00", "10:45"),
        ("11:00", "13:45"),
        ("14:00", "16:45"),
        ("16:15", "19:00"),
        ("18:00", "20:45"),
    ]
]

def _generated_leave_periods(workers):
    """One reproducible, short approved-leave period for every demo worker.

    These are deliberately spread over both canonical weeks and kept to
    2-4 hours so leave is visible and affects selected shifts without making
    the workforce broadly unschedulable.
    """
    rng = random.Random(LEAVE_SEED)
    periods = {}
    for worker in workers:
        day_offset = rng.randrange(14)
        start_hour = rng.choice((9, 10, 12, 14, 17, 18))
        duration = rng.choice((2, 3, 4))
        start = WEEK_START + timedelta(days=day_offset, hours=start_hour)
        end = start + timedelta(hours=duration)
        periods[worker["employee_code"]] = (
            start.strftime(TIME_FORMAT),
            end.strftime(TIME_FORMAT),
        )
    periods.update(CANONICAL_LEAVE_OVERRIDES)
    return periods


def _overlaps(slot_a, slot_b):
    days_a, start_a, end_a = slot_a
    days_b, start_b, end_b = slot_b
    if not set(days_a) & set(days_b):
        return False
    return start_a < end_b and start_b < end_a


def _pick_slots(rng, pool, count):
    """Pick `count` class slots that do not overlap each other."""
    candidates = list(pool)
    rng.shuffle(candidates)
    chosen = []
    for candidate in candidates:
        if all(not _overlaps(candidate, taken) for taken in chosen):
            chosen.append(candidate)
            if len(chosen) == count:
                break
    return chosen


def _generate_worker(
    identity_rng, preference_rng, number, student_type, used_names, pattern_pool
):
    while True:
        name = f"{identity_rng.choice(FIRST_NAMES)} {identity_rng.choice(LAST_NAMES)}"
        if name not in used_names:
            used_names.add(name)
            break

    employee_code = f"SW-{number:03d}"

    if student_type == "undergraduate":
        slots = _pick_slots(identity_rng, UNDERGRADUATE_SLOTS, 3)
        subjects = identity_rng.sample(SUBJECTS, 3)
    else:
        slots = _pick_slots(identity_rng, MASTERS_SLOTS, identity_rng.choice([2, 3, 3]))
        subjects = identity_rng.sample(GRADUATE_SUBJECTS, len(slots))

    courses = [
        (
            f"{subject} {chr(ord('A') + index)}",
            [(day, start, end) for day in days],
        )
        for index, (subject, (days, start, end)) in enumerate(zip(subjects, slots))
    ]

    # Patterns are drawn from shifts that actually exist, so a generated
    # worker's preferences describe real shifts rather than templates that
    # match nothing.
    chosen_patterns = preference_rng.sample(pattern_pool, 3)
    preferences = [
        (hall, days, start, end, level)
        for (hall, days, start, end), level in zip(
            chosen_patterns, ("preferred", "preferred", "low")
        )
    ]

    return {
        "employee_code": employee_code,
        "full_name": name,
        "student_type": student_type,
        "courses": courses,
        "approved_leave": [],
        "preferences": preferences,
    }


def generate_workers(shifts=None):
    """The full synthetic workforce: 4 representative workers plus 26 more.

    `shifts` supplies the pool of real shift patterns offered to the
    generated workers; it defaults to the generated required shifts.
    """
    pattern_pool = available_shift_patterns(shifts or generate_required_shifts())
    workers = [dict(worker) for worker in REPRESENTATIVE_WORKERS]

    undergraduates_needed = UNDERGRADUATE_COUNT - sum(
        1 for worker in workers if worker["student_type"] == "undergraduate"
    )
    masters_needed = MASTERS_COUNT - sum(
        1 for worker in workers if worker["student_type"] == "masters"
    )
    remaining_types = ["undergraduate"] * undergraduates_needed + ["masters"] * masters_needed

    identity_rng = random.Random(IDENTITY_SEED)
    preference_rng = random.Random(PREFERENCE_SEED)
    identity_rng.shuffle(remaining_types)

    used_names = {worker["full_name"] for worker in workers}
    for index, student_type in enumerate(remaining_types, start=len(workers) + 1):
        workers.append(
            _generate_worker(
                identity_rng,
                preference_rng,
                index,
                student_type,
                used_names,
                pattern_pool,
            )
        )

    leave_periods = _generated_leave_periods(workers)
    workers = [
        {**worker, "approved_leave": [leave_periods[worker["employee_code"]]]}
        for worker in workers
    ]

    return workers


def pattern_matches_shift(hall, days, start_time, end_time, shift):
    """True when a shift is exactly the shift this pattern describes.

    The hall must match, the shift must start on one of the pattern's
    weekdays, and BOTH the start and the end must match the pattern. Matching
    on the start alone would wrongly claim, for example, that a preference for
    an 8:00 AM-2:00 PM shift also covers an 11:00 AM-5:00 PM one.

    An end time at or before the start time means the shift runs into the
    next day, so "22:00"-"03:00" matches a 10 PM shift ending 3 AM the
    following morning, and does not match one ending at 5 AM.
    """
    if shift["hall"] != hall:
        return False

    start = datetime.strptime(shift["start_datetime"], TIME_FORMAT)
    if start.weekday() not in days:
        return False
    if start.strftime("%H:%M") != start_time:
        return False

    expected_end = datetime.combine(start.date(), time.fromisoformat(end_time))
    if end_time <= start_time:
        expected_end += timedelta(days=1)

    return datetime.strptime(shift["end_datetime"], TIME_FORMAT) == expected_end


def expand_preferences(preference_patterns, shifts):
    """Turn shift patterns into {shift key: level} for the shifts they match.

    A pattern that matches no generated shift produces no rows and creates no
    coverage requirement (D025, and revision 3 of the worker examples). Where
    both a preferred and a low pattern match the same shift, preferred wins.
    """
    matched = {}
    for hall, days, start_time, end_time, level in preference_patterns:
        for shift in shifts:
            if not pattern_matches_shift(hall, days, start_time, end_time, shift):
                continue
            key = (shift["hall"], shift["start_datetime"], shift["end_datetime"])
            if matched.get(key) == "preferred":
                continue
            matched[key] = level
    return matched


def available_shift_patterns(shifts):
    """Distinct (hall, weekdays, start, end) patterns present in the shifts.

    Derived from the generated shifts themselves so that patterns handed to
    the generated workers describe real shifts. The four representative
    workers do not use this pool - their patterns come from revision 3 and are
    allowed to match nothing.
    """
    grouped = {}
    for shift in shifts:
        start = datetime.strptime(shift["start_datetime"], TIME_FORMAT)
        end = datetime.strptime(shift["end_datetime"], TIME_FORMAT)
        key = (shift["hall"], start.strftime("%H:%M"), end.strftime("%H:%M"))
        grouped.setdefault(key, set()).add(start.weekday())

    return [
        (hall, tuple(sorted(weekdays)), start_time, end_time)
        for (hall, start_time, end_time), weekdays in sorted(grouped.items())
    ]
