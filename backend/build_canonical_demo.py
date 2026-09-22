"""Build the deterministic Phase 9A demo database at an explicit new path.

This utility is intentionally separate from application startup and refuses
to overwrite any file. The resulting database can be validated in isolation
before a human-authorized backup and file swap. It never targets the managed
``backend/shiftops.db`` path.

Run with: python build_canonical_demo.py PATH_TO_NEW_DATABASE
"""

import sys
from pathlib import Path

from database import DATABASE_PATH, create_schema, get_connection
from seed import initialize_demo_data


def build(output_path):
    output = Path(output_path).resolve()
    managed = DATABASE_PATH.resolve()
    if output == managed:
        raise ValueError("Refusing to build directly into the managed shiftops.db file.")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    connection = get_connection(output)
    try:
        create_schema(connection)
        counts = initialize_demo_data(connection)
    except Exception:
        connection.close()
        output.unlink(missing_ok=True)
        raise
    connection.close()
    return output, counts


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("Usage: python build_canonical_demo.py PATH_TO_NEW_DATABASE", file=sys.stderr)
        return 2
    try:
        output, counts = build(arguments[0])
    except (ValueError, FileExistsError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Built canonical demo database: {output}")
    print(counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
