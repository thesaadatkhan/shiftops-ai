"""SQLite connection and schema for ShiftOps AI.

Date/time conventions used throughout the database:

- One fixed local simulation clock. No timezone conversion, no UTC, no DST
  handling. Every stored time is a plain wall-clock time in that one clock.
- Absolute points in time (shifts, approved leave) are stored as
  'YYYY-MM-DD HH:MM' text, so a period crossing midnight has a different
  date on each end.
- Recurring weekly class meetings are stored as day_of_week + 'HH:MM' times,
  not dates, because a class repeats every week rather than happening once.
  day_of_week is 0=Monday through 6=Sunday, matching Python's
  datetime.weekday().
- A period ending exactly when another starts does not overlap.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

DATABASE_PATH = Path(__file__).parent / "shiftops.db"

MIGRATION_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M"

# Written as `migrated_at` for a `migrated_employees` row backfilled from a
# database that already completed legacy migration before that table
# existed - see `migrate_schema`'s backfill step below. The real moment
# those workers were actually migrated was never recorded anywhere, and
# nothing in this file will invent one; this fixed marker names that
# honestly rather than writing a plausible-looking but fabricated
# timestamp.
BACKFILLED_MIGRATION_MARKER = "backfilled - actual migration time not recorded"

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY,
        employee_code TEXT NOT NULL UNIQUE,
        full_name TEXT NOT NULL,
        student_type TEXT NOT NULL
            CHECK (student_type IN ('undergraduate', 'masters')),
        weekly_hour_limit INTEGER NOT NULL DEFAULT 20,
        is_active INTEGER NOT NULL DEFAULT 1,
        seed_key TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY,
        employee_id INTEGER NOT NULL REFERENCES employees(id),
        course_label TEXT NOT NULL,
        UNIQUE (employee_id, course_label)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS class_meetings (
        id INTEGER PRIMARY KEY,
        course_id INTEGER NOT NULL REFERENCES courses(id),
        day_of_week INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        UNIQUE (course_id, day_of_week, start_time)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shifts (
        id INTEGER PRIMARY KEY,
        hall TEXT NOT NULL,
        start_datetime TEXT NOT NULL,
        end_datetime TEXT NOT NULL,
        required_staff INTEGER NOT NULL DEFAULT 1,
        UNIQUE (hall, start_datetime, end_datetime)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shift_preferences (
        id INTEGER PRIMARY KEY,
        employee_id INTEGER NOT NULL REFERENCES employees(id),
        shift_id INTEGER NOT NULL REFERENCES shifts(id),
        preference TEXT NOT NULL CHECK (preference IN ('preferred', 'low')),
        UNIQUE (employee_id, shift_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS approved_leave (
        id INTEGER PRIMARY KEY,
        employee_id INTEGER NOT NULL REFERENCES employees(id),
        start_datetime TEXT NOT NULL,
        end_datetime TEXT NOT NULL,
        UNIQUE (employee_id, start_datetime, end_datetime)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assignments (
        id INTEGER PRIMARY KEY,
        employee_id INTEGER NOT NULL REFERENCES employees(id),
        shift_id INTEGER NOT NULL REFERENCES shifts(id),
        UNIQUE (employee_id, shift_id)
    )
    """,
    # Employee codes that belonged to a worker who has been permanently
    # deleted. Deliberately just the code and when it was retired: this is a
    # ledger of numbers that have been used up, not an archive of the deleted
    # worker, so it holds no name, student type or any other personal detail.
    #
    # It exists because Phase 5C will allocate codes automatically (D034) and
    # must never reuse one. Without this, deleting SW-031 would let the next
    # created worker be issued SW-031 again, so two different people would
    # share a code across the project's history. The allocator will take the
    # next number from the highest suffix in `employees` AND here, so a
    # deleted number stays spent. See D040.
    #
    # A new table, so CREATE TABLE IF NOT EXISTS reaches existing databases -
    # unlike a new column, which needs `migrate_schema()`.
    """
    CREATE TABLE IF NOT EXISTS retired_employee_codes (
        employee_code TEXT PRIMARY KEY,
        retired_at TEXT NOT NULL
    )
    """,
    # How far the automatic employee-code sequence has been issued (D034).
    # Exactly one row, holding the highest SW-number that has been reserved.
    #
    # A counter rather than a derived figure: the highest live employee row is
    # not the answer, because deleting the highest-numbered worker would make
    # the next create reuse their number, and neither is a row count, because
    # gaps are permanent. The counter only ever moves forward.
    #
    # Deliberately no row is written here. Creating the table is additive and
    # touches nothing; the row is created on first use, inside the same
    # transaction that issues a code. That keeps `ensure_schema()` free of
    # side effects on real data.
    """
    CREATE TABLE IF NOT EXISTS code_allocation (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        highest_issued INTEGER NOT NULL
    )
    """,
    # A worker's semester: the inclusive dates a timetable applies to, and
    # whether a supervisor has confirmed it is complete (D035).
    #
    # `confirmed_at` NULL means "not confirmed", which is deliberately
    # different from "no classes". A worker with no schedule at all has
    # missing information; a worker with a confirmed schedule and no class
    # blocks has deliberately declared they have no classes. Phase 6 needs to
    # tell those apart, so the schema must not collapse them.
    # `dates_provisional` records that these semester dates were ASSUMED by
    # the migration rather than supplied by a supervisor. It lives here, on
    # the schedule, and not on the class blocks.
    #
    # It used to be derived at read time from the migration notes on the
    # blocks. That was fine while blocks were immutable and wrong the moment
    # they became editable: deleting the last migrated block would have
    # silently erased the fact that the dates were never anybody's decision.
    # Provenance of the DATES belongs to the row that holds the dates.
    """
    CREATE TABLE IF NOT EXISTS semester_schedules (
        id INTEGER PRIMARY KEY,
        employee_id INTEGER NOT NULL REFERENCES employees(id),
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        confirmed_at TEXT,
        dates_provisional INTEGER NOT NULL DEFAULT 0,
        UNIQUE (employee_id, start_date, end_date)
    )
    """,
    # One recurring weekly class, owned by a semester schedule rather than by
    # a course: day_of_week (0=Monday, matching D025) plus 'HH:MM' start and
    # end. Course names and course entities are not operational inputs any
    # more, so there is deliberately no course reference here.
    #
    # There is NO unique constraint on (schedule_id, day_of_week, start_time,
    # end_time). Legacy `class_meetings` were unique per course, so one worker
    # could hold two identical-looking meetings under two courses. A unique
    # constraint here would force the migration to drop one of them, and
    # silently discarding stored class information is not acceptable. Every
    # legacy row is preserved one-to-one; rejecting duplicate and overlapping
    # blocks belongs to the supervisor-entry increment, where a person can be
    # told about the clash and decide.
    """
    CREATE TABLE IF NOT EXISTS class_blocks (
        id INTEGER PRIMARY KEY,
        schedule_id INTEGER NOT NULL REFERENCES semester_schedules(id),
        day_of_week INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        source_note TEXT
    )
    """,
    # A stored, immutable snapshot of one computed draft (Phase 7 increment
    # 3). Content never changes after creation - `proposal_assignments`
    # below is written once, in the same transaction as this row, and never
    # updated. Approving or rejecting only ever changes `status` and
    # `decided_at` here; it never rewrites what was proposed. That
    # immutability is what makes "bind approval to the exact proposal
    # content" possible: there is nothing to silently regenerate or drift.
    """
    CREATE TABLE IF NOT EXISTS schedule_proposals (
        id INTEGER PRIMARY KEY,
        week_start TEXT NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'approved', 'rejected')),
        decided_at TEXT,
        draft_snapshot TEXT
    )
    """,
    # One row per proposed (not existing) assignment a draft contained at
    # the moment it was persisted. Never updated after insertion; approving
    # a proposal reads these rows and writes matching `assignments` rows -
    # it does not edit these.
    """
    CREATE TABLE IF NOT EXISTS proposal_assignments (
        id INTEGER PRIMARY KEY,
        proposal_id INTEGER NOT NULL REFERENCES schedule_proposals(id),
        shift_id INTEGER NOT NULL REFERENCES shifts(id),
        employee_id INTEGER NOT NULL REFERENCES employees(id),
        UNIQUE (proposal_id, shift_id, employee_id)
    )
    """,
    # Append-only history of every proposal lifecycle event and every actual
    # assignment change (creation via approval, or an explicit replacement).
    # `employee_id_before`/`employee_id_after` are NULL where they do not
    # apply (e.g. a proposal-level 'proposal_created' row names no single
    # assignment). `detail` is a short factual sentence, never the
    # optimizer's internal solver reasoning - this is a record of WHAT
    # happened, not WHY the optimizer chose it. `agent_proposal_id` (Phase 9
    # increment 2) links a row to the `agent_proposals` row that caused it -
    # NULL for every ordinary Phase 7 row, exactly like `proposal_id` is
    # NULL for a row that has nothing to do with a Phase 7 proposal. Forward
    # references SQLite table (`agent_proposals` is created later in this
    # same list) are fine: SQLite does not validate a REFERENCES target's
    # existence at CREATE TABLE time, only at DML time.
    """
    CREATE TABLE IF NOT EXISTS assignment_audit (
        id INTEGER PRIMARY KEY,
        occurred_at TEXT NOT NULL,
        action TEXT NOT NULL
            CHECK (action IN (
                'proposal_created', 'proposal_approved', 'proposal_rejected',
                'assignment_created', 'assignment_replaced'
            )),
        proposal_id INTEGER REFERENCES schedule_proposals(id),
        shift_id INTEGER REFERENCES shifts(id),
        employee_id_before INTEGER REFERENCES employees(id),
        employee_id_after INTEGER REFERENCES employees(id),
        agent_proposal_id INTEGER REFERENCES agent_proposals(id),
        detail TEXT NOT NULL
    )
    """,
    # Records that one employee's legacy class data has already been run
    # through `migrate_class_schedules()`, independent of whether the
    # semester schedule that migration produced still exists. Without this,
    # `migrate_class_schedules()` had to infer "not yet migrated" from
    # "has no semester schedule" - and a supervisor deleting a migrated
    # worker's last semester made that inference wrong: the worker looked
    # unmigrated again, and the next startup silently recreated the exact
    # schedule they had just deleted. Presence of a row here is a permanent
    # fact about what migration has already done, never revisited by a
    # later change to `semester_schedules`.
    """
    CREATE TABLE IF NOT EXISTS migrated_employees (
        employee_id INTEGER PRIMARY KEY REFERENCES employees(id),
        migrated_at TEXT NOT NULL
    )
    """,
    # Phase 9 increment 1: one row per supervisor-directed AI scheduling
    # task. Deliberately a SEPARATE table from `schedule_proposals` (Phase
    # 7) - a Phase 7 proposal is a whole-week, optimizer-generated batch of
    # assignments; an agent task is one conversational investigation that
    # may, at most, produce ONE single-shift `agent_proposals` row (see
    # below). Reusing `schedule_proposals` for that would force an
    # optimizer-shaped table to represent something it was never designed
    # for. `request_text` is the supervisor's own words that started the
    # task - a fact worth keeping, never fabricated. `step_count` is a
    # plain counter of bounded-loop iterations consumed so far, for
    # observability; it is not a limit by itself (the loop enforces its own
    # `max_steps` argument at call time, see `agent_service.py`).
    """
    CREATE TABLE IF NOT EXISTS agent_tasks (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open', 'awaiting_approval', 'blocked', 'closed')),
        request_text TEXT NOT NULL,
        step_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    # The visible transcript only - never hidden chain-of-thought, and never
    # a fabricated narration of "why" the model chose something. `role`
    # distinguishes the supervisor's own words, the model's visible reply
    # text, an outgoing tool call the model requested (name + the exact
    # arguments used), and the tool's own factual result - the same
    # deterministic backend facts `agent_tools.py` returns, never a second,
    # looser summary of them. `sequence` is assigned by the caller (see
    # `agent_service.py`) so a transcript reads back in the exact order it
    # happened, independent of `id` reuse concerns.
    """
    CREATE TABLE IF NOT EXISTS agent_messages (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES agent_tasks(id),
        created_at TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        role TEXT NOT NULL
            CHECK (role IN ('supervisor', 'assistant', 'tool_call', 'tool_result')),
        tool_name TEXT,
        content TEXT NOT NULL,
        UNIQUE (task_id, sequence)
    )
    """,
    # The exact proposed scheduling action an agent task produced, if any -
    # AT MOST ONE PER TASK, enforced here with `UNIQUE(task_id)` (Codex
    # review: this was previously only an intended rule, enforced nowhere -
    # `agent_tools.propose_replacement` also checks it explicitly, inside
    # the same write-locked transaction as the insert, so the domain-level
    # check and this constraint agree; the constraint is the backstop for
    # any future code path that might bypass the domain check). `action_type`
    # names only what is actually implemented (`replace_assignment`); a
    # future increment adding another action type adds it to this CHECK
    # list the same way `assignment_audit.action` already grew its own
    # list, rather than this table speculatively allowing values nothing
    # can yet produce. `outgoing_employee_id` is NULL for filling a
    # currently-uncovered position (there is no outgoing worker to
    # replace). `rationale` is a short, factual sentence computed entirely
    # from deterministic backend data - preference, hours, ranking position
    # - by `agent_tools.propose_replacement` itself; the model never
    # supplies or influences this text (Codex review: an earlier version
    # accepted and stored a model-authored rationale, which could assert
    # anything). `execution_outcome`/`verification_outcome` and their
    # timestamps exist now so this table needs no further migration when a
    # later increment adds the actual execute-and-verify step; this
    # increment leaves them NULL for every row it writes, because nothing
    # here is ever approved or executed by this increment (see
    # `agent_service.py`).
    """
    CREATE TABLE IF NOT EXISTS agent_proposals (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES agent_tasks(id),
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'approved', 'rejected')),
        decided_at TEXT,
        action_type TEXT NOT NULL CHECK (action_type IN ('replace_assignment')),
        shift_id INTEGER NOT NULL REFERENCES shifts(id),
        outgoing_employee_id INTEGER REFERENCES employees(id),
        incoming_employee_id INTEGER NOT NULL REFERENCES employees(id),
        rationale TEXT NOT NULL,
        executed_at TEXT,
        execution_outcome TEXT,
        verified_at TEXT,
        verification_outcome TEXT,
        UNIQUE (task_id)
    )
    """,
]

# The fictional demo semester. It must contain the sample reporting week of
# Monday 2026-10-05 to Sunday 2026-10-11, and it does. Inclusive dates, in
# the project's single local clock (D025). Fictional, like everything else in
# this simulation.
DEMO_SEMESTER_START = "2026-08-24"
DEMO_SEMESTER_END = "2026-12-11"

# When a demo timetable counts as confirmed. The demo generator's timetables
# are validated by construction - fixed course loads, no overlapping classes
# (D023/D027) - so migrating them records a confirmation. A fixed timestamp,
# not `now`, so the same database migrated twice looks identical and tests do
# not depend on when they run.
DEMO_SEMESTER_CONFIRMED_AT = "2026-08-24 00:00"

# What a migrated class block records about where it came from. Kept as
# constants because the details view reads them back: a schedule holding a
# block with one of these notes was created by the migration, which means its
# semester dates are the assumed demo ones above rather than dates a
# supervisor entered. That is the only evidence the database has for calling
# those dates provisional, so both sides must agree on the exact text.
MIGRATION_NOTE_PREFIX = "migrated from"
DEMO_MIGRATION_NOTE = (
    f"{MIGRATION_NOTE_PREFIX} demo course data, matched the generated timetable"
)
LEGACY_MIGRATION_NOTE = (
    f"{MIGRATION_NOTE_PREFIX} legacy course data, timetable unconfirmed"
)

# Recorded in `migrated_employees.migrated_at` for a worker this migration
# deliberately did NOT create a semester for, because the evidence was
# ambiguous (see `_migration_has_ever_run` and the "upgrade ambiguity" note
# on `migrate_class_schedules`) - never a plausible-looking timestamp for
# something that did not happen.
#
# **Explicit recovery policy**, since this is the one case where migration
# will not resolve itself: a worker recorded with this marker has their
# legacy `courses`/`class_meetings` rows fully intact and untouched, and no
# semester. If a person determines they genuinely were never migrated (not a
# past deletion), the supervisor-entry screens can enter their semester and
# classes directly - migration is not the only way to get one. There is
# deliberately no automated "retry migration for this worker" action: doing
# that safely requires a person to have actually looked at the specific
# case, which an automated retry cannot do.
AMBIGUOUS_DELETION_MARKER = (
    "ambiguous - possible pre-ledger deletion; not migrated, legacy data preserved"
)


def get_connection(database_path=None):
    """Open a connection, defaulting to the project's database file.

    `database_path` lets tests point at a temporary or in-memory database
    without touching the real one.
    """
    connection = sqlite3.connect(database_path or DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def table_columns(connection, table):
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def table_exists(connection, table):
    """Whether a table is present.

    `table_columns` returns an empty set both for a table with no columns -
    which cannot happen - and for a table that does not exist, so a migration
    that keys off it alone would try to ALTER something absent. Column
    migrations are written to be runnable on their own, against a fixture
    holding only the tables that migration cares about.
    """
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def migrate_schema(connection):
    """Add columns that `CREATE TABLE IF NOT EXISTS` cannot add to an
    existing table.

    `CREATE TABLE IF NOT EXISTS` does nothing at all once a table exists, so
    a new column never reaches a database that was created before it was
    added. Each migration below therefore checks for the column first and is
    safe to run on every startup.

    Migrations only ever ADD things. Nothing here drops a table, deletes a
    row, or rebuilds the database.

    **Why this runs inside one `BEGIN IMMEDIATE` transaction.** Review
    reproduced a real race: two connections both ran `PRAGMA table_info` and
    both saw `dates_provisional` absent, because the check happened before
    either held the write lock. Both then issued `ALTER TABLE`; the second
    failed with `OperationalError: duplicate column name`. The fix is lock
    ordering, the same one `migrate_class_schedules` already uses - take the
    write lock FIRST, and only then decide what needs adding. The second
    caller blocks until the first commits, then re-checks against a database
    that already has the column and adds nothing. Both calls succeed.
    `ALTER TABLE` exception-swallowing, a process-local lock, or suppressing
    "duplicate column name" specifically would only paper over the first
    connection to hit it and would do nothing for a second OS process or a
    second worker - they were deliberately not used.

    Returns the list of migrations that were applied, so callers and tests
    can see whether anything actually changed.
    """
    previous_isolation = connection.isolation_level
    connection.isolation_level = None
    applied = []

    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if "is_active" not in table_columns(connection, "employees"):
                # A NOT NULL column needs a default so existing rows stay
                # valid; 1 means every worker already in the database stays
                # active.
                connection.execute(
                    "ALTER TABLE employees ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
                )
                applied.append("employees.is_active")

            if "seed_key" not in table_columns(connection, "employees"):
                # Legacy provenance: records which generated worker a row
                # came from, and is NULL for workers created through the
                # application. It no longer controls anything. It was
                # introduced when seeding ran repeatedly and had to recognise
                # a demo worker whose employee_code had been edited; demo
                # initialization now runs only on an empty database, so
                # nothing reads this column to decide whether to seed. Kept
                # because dropping it would mean rebuilding the table, and
                # knowing which rows came from the demo data is still useful.
                connection.execute("ALTER TABLE employees ADD COLUMN seed_key TEXT")
                connection.execute(
                    "UPDATE employees SET seed_key = employee_code WHERE seed_key IS NULL"
                )
                applied.append("employees.seed_key")

            if table_exists(
                connection, "semester_schedules"
            ) and "dates_provisional" not in table_columns(
                connection, "semester_schedules"
            ):
                # Default 0: a schedule is only provisional if we can SHOW it
                # was assumed, and the safe default is "a person chose these
                # dates".
                connection.execute(
                    "ALTER TABLE semester_schedules"
                    " ADD COLUMN dates_provisional INTEGER NOT NULL DEFAULT 0"
                )
                # Backfill from the evidence that used to be read at request
                # time, so a database upgraded today reports exactly what it
                # reported yesterday. The migration is the only writer of
                # `source_note`, and every schedule it creates gets the same
                # assumed demo dates, so a block carrying a migration note
                # marks its schedule as migrated.
                #
                # AMBIGUITY, stated rather than papered over: this evidence
                # lives on the blocks, so a migrated schedule whose blocks had
                # all been deleted before this ran would backfill as NOT
                # provisional. That case cannot exist in practice - deleting a
                # block only becomes possible in the same increment that adds
                # this column, so at the moment this migration runs every
                # migrated schedule still holds its blocks. Where the stored
                # evidence is genuinely absent the narrower answer is chosen:
                # not provisional, rather than a guess.
                if table_exists(connection, "class_blocks"):
                    connection.execute(
                        """
                        UPDATE semester_schedules SET dates_provisional = 1
                        WHERE id IN (
                            SELECT DISTINCT schedule_id FROM class_blocks
                            WHERE source_note IS NOT NULL AND source_note LIKE ?
                        )
                        """,
                        (f"{MIGRATION_NOTE_PREFIX}%",),
                    )
                applied.append("semester_schedules.dates_provisional")

            if table_exists(
                connection, "schedule_proposals"
            ) and "draft_snapshot" not in table_columns(
                connection, "schedule_proposals"
            ):
                # The immutable draft review snapshot (Phase 7 increment 4):
                # the full `optimizer.generate_draft` response, stored
                # verbatim as JSON text at creation time, so a supervisor can
                # see existing/proposed assignments, coverage totals, and
                # uncovered reasons again after a refresh without recomputing
                # anything. NULL for any proposal created before this column
                # existed - those still work, they simply have no `review`.
                connection.execute(
                    "ALTER TABLE schedule_proposals ADD COLUMN draft_snapshot TEXT"
                )
                applied.append("schedule_proposals.draft_snapshot")

            # Backfill `migrated_employees` for a database that already
            # completed legacy migration before that table existed. Every
            # employee_id currently in `semester_schedules` got there either
            # through the migration or through a supervisor manually
            # creating a semester - either way, `migrate_class_schedules`
            # must never touch that worker again as though they were still
            # unmigrated, so both cases are safely marked migrated here.
            # `INSERT OR IGNORE` makes this a no-op on every later startup
            # once it has run once. Run unconditionally (not gated on the
            # table having just been created) because a database can reach
            # this code with the table already present but empty - the
            # ordinary case for one that has never restarted since this
            # feature was added.
            if table_exists(connection, "migrated_employees") and table_exists(
                connection, "semester_schedules"
            ):
                backfilled = connection.execute(
                    """
                    INSERT OR IGNORE INTO migrated_employees (employee_id, migrated_at)
                    SELECT DISTINCT employee_id, ? FROM semester_schedules
                    """,
                    (BACKFILLED_MIGRATION_MARKER,),
                ).rowcount
                if backfilled > 0:
                    applied.append(f"migrated_employees.backfilled={backfilled}")

            # Created here rather than alongside the CREATE TABLE statements:
            # those run before this function, so on a database that predates
            # seed_key the index would reference a column that does not exist
            # yet. `IF NOT EXISTS` makes this safe to repeat, and it is
            # created inside the same transaction so a concurrent caller
            # cannot see the seed_key column without this index either.
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS employees_seed_key "
                "ON employees (seed_key) WHERE seed_key IS NOT NULL"
            )

            # Phase 9 increment 2: links an `assignment_audit` row to the
            # Phase 9 `agent_proposals` row that caused it (a decision event
            # with no single shift/employee, or the actual assignment
            # mutation an approved proposal performed), the same way
            # `proposal_id` already links a row to a Phase 7
            # `schedule_proposals` row. `assignment_audit` predates
            # `agent_proposals` (Phase 7 vs. Phase 9), so - unlike
            # `agent_proposals` itself, which is new enough this pass that
            # editing its `CREATE TABLE` directly was safe - this column
            # genuinely needs the `ALTER TABLE` migration path: `CREATE
            # TABLE IF NOT EXISTS` cannot add it to a database that already
            # has `assignment_audit` from an earlier startup. NULL for
            # every row that has nothing to do with the agent (the entire
            # history before this feature existed, and every ordinary Phase
            # 7 manual replacement/approval row from now on).
            if table_exists(
                connection, "assignment_audit"
            ) and "agent_proposal_id" not in table_columns(connection, "assignment_audit"):
                connection.execute(
                    "ALTER TABLE assignment_audit"
                    " ADD COLUMN agent_proposal_id INTEGER REFERENCES agent_proposals(id)"
                )
                applied.append("assignment_audit.agent_proposal_id")

            # Committed here regardless of whether anything was applied, so
            # the no-work path releases the write lock rather than leaving
            # the transaction open.
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.isolation_level = previous_isolation

    return applied


def expected_demo_timetables():
    """Every demo worker's validated timetable, straight from the generator.

    Returned as {employee_code: sorted [(day_of_week, start, end), ...]}, with
    duplicates kept, so a comparison against stored rows notices a missing,
    extra, duplicated or altered meeting rather than just a different count.

    Imported lazily. `synthetic_data` is pure and imports nothing from here,
    but keeping the import inside the function means `database` stays usable
    on its own and nothing is generated unless a migration actually needs it.
    """
    from synthetic_data import generate_required_shifts, generate_workers

    shifts = generate_required_shifts()
    expected = {}
    for worker in generate_workers(shifts):
        meetings = []
        for _course_label, occurrences in worker["courses"]:
            meetings.extend(tuple(occurrence) for occurrence in occurrences)
        expected[worker["employee_code"]] = sorted(meetings)
    return expected


def stored_meetings(connection, employee_id):
    """A worker's legacy class meetings as sorted (day, start, end) tuples."""
    return sorted(
        (row["day_of_week"], row["start_time"], row["end_time"])
        for row in connection.execute(
            """
            SELECT m.day_of_week, m.start_time, m.end_time
            FROM class_meetings m
            JOIN courses c ON c.id = m.course_id
            WHERE c.employee_id = ?
            """,
            (employee_id,),
        )
    )


def is_intact_demo_timetable(connection, employee, expected):
    """Whether this worker's stored meetings ARE the validated demo timetable.

    `seed_key` alone is not enough and is deliberately not trusted on its own.
    It records provenance, not integrity: an early migration backfilled it
    from `employee_code` for every row, so a hand-made worker can carry one,
    and a genuine demo worker's meetings may since have been edited, deleted
    or duplicated by hand.

    So the key must name a worker the generator actually produces, AND the
    stored meetings must equal that worker's generated timetable exactly,
    compared with multiplicity. Anything else - a missing meeting, an extra
    one, a duplicate, a changed time - fails the comparison and the timetable
    is left unconfirmed rather than being vouched for.
    """
    key = employee["seed_key"]
    if key is None or key not in expected:
        return False
    return stored_meetings(connection, employee["id"]) == expected[key]


def _migration_has_ever_run(connection):
    """Whether `migrate_class_schedules` has completed at least once on this
    database, using evidence that predates `migrated_employees` itself.

    Deliberately checked via `class_blocks.source_note` (the
    `MIGRATION_NOTE_PREFIX` every migrated block carries), not via
    `migrated_employees` - a database that ran migration before the ledger
    existed has this note evidence but no ledger rows yet, and it is exactly
    that database this function must recognize correctly. See the "upgrade
    ambiguity" note on `migrate_class_schedules` for why this matters.
    """
    return (
        connection.execute(
            "SELECT 1 FROM class_blocks WHERE source_note LIKE ? LIMIT 1",
            (f"{MIGRATION_NOTE_PREFIX}%",),
        ).fetchone()
        is not None
    )


def migrate_class_schedules(connection):
    """Move course-linked class meetings into employee-owned semester
    schedules and recurring class blocks (D035).

    **Why this runs in its own explicit transaction.** `create_schema()`
    commits after the CREATE TABLE statements, and Python's sqlite3 leaves
    DDL in autocommit while opening a transaction only for DML. Relying on
    those boundaries would leave the data transformation partly committed if
    it failed halfway. `BEGIN IMMEDIATE` here makes the whole transformation
    genuinely all-or-nothing, and takes the write lock up front so another
    writer cannot interleave.

    **Repeatable, and safe when two callers start together.** Only employees
    who have class meetings and are not already recorded in
    `migrated_employees` are migrated, so running it again - on every
    startup, say - moves nothing and duplicates nothing. That list is read
    *inside* the transaction, after the write lock is held, so a second
    caller cannot act on a view of the database that the first has already
    changed.

    **Migration completion is tracked independently of its output.**
    `migrated_employees` records that an employee has been migrated, and
    that row is never removed - not by this function, and not by deleting
    the semester schedule migration produced. Checking `semester_schedules`
    directly for "already migrated" was a real bug: a supervisor deleting a
    migrated worker's last semester (an ordinary, supported action) made
    them look unmigrated again, and the next startup silently recreated the
    exact schedule and classes they had just deleted. A worker who no
    longer has a semester because one was intentionally deleted stays
    deleted; migration is a one-time historical fact about the LEGACY data,
    not a statement that a semester currently exists.

    **Nothing is discarded.** Every legacy meeting becomes exactly one class
    block, keeping its weekday and its start and end times unchanged. Rows
    that look identical are both kept; see the note on `class_blocks` about
    why there is no unique constraint. The legacy `courses` and
    `class_meetings` rows are left in place as evidence of where the blocks
    came from - nothing reads them operationally any more, and dropping them
    would destroy information this migration cannot recreate.

    **The dates are marked provisional.** The legacy model stored no semester
    dates at all, so the ones written here are assumed. That fact is recorded
    on the schedule itself (`dates_provisional`), not inferred later from the
    notes on its blocks: once blocks can be edited, deleting the last migrated
    block must not erase the knowledge that nobody chose these dates.

    **Confirmation is not invented.** A migrated schedule is confirmed only
    when the stored meetings are PROVED to be a validated demo timetable: the
    worker's `seed_key` must name a worker the generator produces, and their
    stored meetings must equal that generated timetable exactly, compared
    with multiplicity. `seed_key` on its own is provenance, not integrity -
    an early migration backfilled it from `employee_code` for every row, and
    a demo worker's meetings may since have been edited. Anything that does
    not match exactly is migrated unconfirmed, with its meetings preserved
    untouched. A worker with no class meetings at all gets no schedule, which
    reads as missing information rather than as a deliberate "no classes" -
    those are different states and Phase 6 must be able to tell them apart.

    **Upgrade ambiguity, and the explicit policy for it.** `migrated_employees`
    prevents a migrated worker's DELETED semester from resurrecting on a
    later startup (see above) - but only for a deletion that happens AFTER
    the ledger exists to record it. A database that already ran migration
    and had a supervisor delete a migrated worker's semester BEFORE this
    ledger was ever added has no record of that: no semester (deleted), and
    no `migrated_employees` row (did not exist yet) - which looks, from
    stored data alone, EXACTLY like a worker who was simply never migrated.
    Nothing in this schema records deletions, so this is a genuine, storage-
    level ambiguity that cannot be resolved by reading harder.

    The policy: before deciding anything, `_migration_has_ever_run` checks
    for evidence, independent of the ledger, that migration has run on this
    database before (a `class_blocks` row carrying the migration note). If
    that evidence exists, EVERY worker this pass would otherwise consider
    "pending" is treated as ambiguous rather than migrated - because legacy
    `class_meetings` data is never created after initial setup in this
    application, a worker who still looks pending AFTER migration has
    already run once can only be explained by a deletion, never by
    genuinely new legacy data. An ambiguous worker gets NO semester (their
    timetable is not silently recreated) but IS recorded in
    `migrated_employees` with `AMBIGUOUS_DELETION_MARKER`, so this decision
    is a one-time, inspectable fact rather than repeated silently on every
    future startup. Their legacy `courses`/`class_meetings` rows are left
    completely untouched, exactly as for a normal migration. See
    `AMBIGUOUS_DELETION_MARKER`'s own comment for the recovery policy.

    This check runs once per call, before the loop below, against the state
    of the database as the write lock found it - not re-checked per-worker,
    so a GENUINE first-time migration processing several workers in one
    pass never sees its own just-written evidence and mistakes itself for a
    later, ambiguous run.

    **Accepted residual limitation - this is a mitigation, not a complete
    solution.** `_migration_has_ever_run` itself depends on at least ONE
    `class_blocks` row carrying the migration note still existing somewhere
    in the database. If EVERY migrated worker's semester/blocks were deleted
    before the ledger existed - not just one worker's, but all of them, so
    no surviving row anywhere still carries that note - this check finds no
    evidence, `ambiguous` is `False`, and every one of those workers is
    silently re-migrated from their still-present legacy `class_meetings`
    data: exactly the resurrection this whole mechanism exists to prevent.
    No stored data can tell that historical database apart from one that
    was genuinely never migrated at all; there is no surviving evidence left
    to reason from, by construction of what this schema keeps. This is a
    known, accepted gap in a prototype, not a claim that upgrade ambiguity
    is fully solved - a real deployment migrating from a pre-ledger install
    would need an external record (a backup, a migration log outside this
    database) to close it, which is out of scope here.

    Returns the number of employees migrated (never counts an ambiguous
    worker, since nothing was migrated for them).
    """
    previous_isolation = connection.isolation_level
    connection.isolation_level = None
    migrated = 0

    try:
        # The write lock is taken FIRST, and only then is it decided who
        # still needs migrating. Reading that list before the lock was a real
        # race: two servers starting together both saw the same worker as
        # pending, and the second to commit hit the unique constraint on
        # (employee_id, start_date, end_date). The transaction protected the
        # inserts, but the decision about what to insert was made outside it.
        #
        # Inside the lock, the second caller blocks until the first commits,
        # then reads a database that already has the schedule and finds
        # nothing pending. Both calls succeed and the work happens once.
        connection.execute("BEGIN IMMEDIATE")
        try:
            pending = connection.execute(
                """
                SELECT DISTINCT c.employee_id
                FROM class_meetings m
                JOIN courses c ON c.id = m.course_id
                WHERE c.employee_id NOT IN (SELECT employee_id FROM migrated_employees)
                """
            ).fetchall()

            if not pending:
                # Nothing to do. End the transaction rather than leaving it
                # open, so the write lock is released on the way out.
                connection.execute("COMMIT")
                return 0

            # Snapshot taken once, before this pass writes anything of its
            # own - see the "upgrade ambiguity" docstring section above.
            ambiguous = _migration_has_ever_run(connection)

            # Only generated once there is something to migrate, which in a
            # database's life is at most once.
            expected = expected_demo_timetables()

            for row in pending:
                employee_id = row["employee_id"]
                employee = connection.execute(
                    "SELECT id, seed_key FROM employees WHERE id = ?", (employee_id,)
                ).fetchone()
                if employee is None:
                    # A meeting whose owner no longer exists. Left exactly as
                    # it is rather than attached to an invented worker, but
                    # still recorded as migrated - there is no worker left to
                    # ever legitimately produce a schedule for this
                    # employee_id, so leaving it off `migrated_employees`
                    # would only make every future startup re-check the same
                    # permanently-orphaned row forever.
                    connection.execute(
                        "INSERT OR IGNORE INTO migrated_employees (employee_id, migrated_at) VALUES (?, ?)",
                        (employee_id, datetime.now().strftime(MIGRATION_TIMESTAMP_FORMAT)),
                    )
                    continue

                if ambiguous:
                    # Migration has run on this database before (evidence
                    # predating this pass), so a worker who still looks
                    # pending cannot be new legacy data - only a deletion
                    # explains it, and which deletion cannot be known.
                    # Recorded, not silently recreated; see the "upgrade
                    # ambiguity" docstring section above.
                    connection.execute(
                        "INSERT OR IGNORE INTO migrated_employees (employee_id, migrated_at) VALUES (?, ?)",
                        (employee_id, AMBIGUOUS_DELETION_MARKER),
                    )
                    continue

                from_demo = is_intact_demo_timetable(connection, employee, expected)
                connection.execute(
                    """
                    INSERT INTO semester_schedules
                        (employee_id, start_date, end_date, confirmed_at,
                         dates_provisional)
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (
                        employee_id,
                        DEMO_SEMESTER_START,
                        DEMO_SEMESTER_END,
                        DEMO_SEMESTER_CONFIRMED_AT if from_demo else None,
                    ),
                )
                schedule_id = connection.execute(
                    "SELECT id FROM semester_schedules WHERE employee_id = ?"
                    " AND start_date = ? AND end_date = ?",
                    (employee_id, DEMO_SEMESTER_START, DEMO_SEMESTER_END),
                ).fetchone()["id"]

                note = DEMO_MIGRATION_NOTE if from_demo else LEGACY_MIGRATION_NOTE
                connection.execute(
                    """
                    INSERT INTO class_blocks
                        (schedule_id, day_of_week, start_time, end_time, source_note)
                    SELECT ?, m.day_of_week, m.start_time, m.end_time, ?
                    FROM class_meetings m
                    JOIN courses c ON c.id = m.course_id
                    WHERE c.employee_id = ?
                    ORDER BY m.id
                    """,
                    (schedule_id, note, employee_id),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO migrated_employees (employee_id, migrated_at) VALUES (?, ?)",
                    (employee_id, datetime.now().strftime(MIGRATION_TIMESTAMP_FORMAT)),
                )
                migrated += 1

            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.isolation_level = previous_isolation

    return migrated


def create_schema(connection):
    """Create any missing tables, apply column migrations, then move class
    data into the semester model.

    Existing tables and rows are untouched apart from that documented
    transformation, which is itself repeatable and all-or-nothing.
    """
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.commit()
    applied = migrate_schema(connection)
    migrated = migrate_class_schedules(connection)
    if migrated:
        applied.append(f"class_blocks.migrated_employees={migrated}")
    return applied


def ensure_schema():
    """Create any missing tables in the project's database file."""
    connection = get_connection()
    try:
        create_schema(connection)
    finally:
        connection.close()


if __name__ == "__main__":
    ensure_schema()
    print(f"Schema ready at {DATABASE_PATH}")
