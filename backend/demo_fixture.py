"""A throwaway database holding a pristine copy of the demo dataset.

Verification scripts that assert fixed demo figures - 30 workers, 198 shifts,
489 coverage hours per week and the representative preference rows - need a dataset
that nobody has edited. Running them against the working database was wrong
in two ways: they failed on a fresh checkout where no database exists yet,
and they failed once a worker was legitimately renamed or removed through
the application, reporting a problem where there was none.

Building the fixture in memory keeps those checks meaningful and keeps them
entirely away from anyone's real data. Nothing here opens the project's
`shiftops.db`.
"""

from database import create_schema, get_connection
from seed import initialize_demo_data


def demo_fixture_connection():
    """An in-memory database initialized with the demo dataset."""
    connection = get_connection(":memory:")
    create_schema(connection)
    initialize_demo_data(connection)
    return connection
