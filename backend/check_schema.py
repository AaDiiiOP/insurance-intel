"""
Read-only database schema check (v0.14.1).

Compares the tables in DATABASE_URL with what the current code expects
(SCHEMA + MIGRATIONS on an empty database). Nothing is created, changed or
deleted: the expected tables are built in a scratch schema inside a
transaction that is rolled back.

    python3 check_schema.py

Exit code 0 = no missing tables or columns; 1 = drift found (start the
backend once so its startup migrations run, then check again).
"""
import sys

from repositories.connection import find_schema_drift


def main() -> int:
    drift = find_schema_drift()
    for t in drift["missing_tables"]:
        print(f"MISSING TABLE   {t}")
    for c in drift["missing_columns"]:
        print(f"MISSING COLUMN  {c}")
    for d in drift["type_differences"]:
        print(f"TYPE DIFFERENCE {d}")
    bad = drift["missing_tables"] or drift["missing_columns"]
    print("SCHEMA CHECK: " + ("FAIL" if bad else "PASS")
          + f" ({len(drift['missing_tables'])} missing tables, "
          f"{len(drift['missing_columns'])} missing columns, "
          f"{len(drift['type_differences'])} type differences)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
