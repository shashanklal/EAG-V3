"""
Convert CSV data from Sample Data folder into a SQLite database.
Creates Database/SyntheticClaims.db with four tables:
  - claims_tbl
  - claims_events_tbl
  - claims_lines_tbl
  - claims_flat_tbl
"""
import csv
import sqlite3
from pathlib import Path

HERE = Path(__file__).parent
DB_DIR = HERE / "Database"
SAMPLE_DIR = DB_DIR / "Sample Data"
SQLITE_PATH = DB_DIR / "SyntheticClaims.db"

TABLES = {
    "claims_tbl": SAMPLE_DIR / "synthetic_claims_claims.csv",
    "claims_events_tbl": SAMPLE_DIR / "synthetic_claims_events.csv",
    "claims_lines_tbl": SAMPLE_DIR / "synthetic_claims_lines.csv",
    "claims_flat_tbl": SAMPLE_DIR / "synthetic_claims_flat.csv",
}

# Columns that should be stored as REAL (numeric)
NUMERIC_COLUMNS = {
    "charge_amt", "allowed_amt", "paid_amt",
    "total_charge_amt", "total_allowed_amt", "total_paid_amt",
    "units", "line_count", "event_count", "tat_days",
    "clean_claim_ind", "auto_adjudicated_ind",
    "event_seq", "line_num",
}


def _infer_type(col_name: str) -> str:
    col = col_name.strip().lower()
    if col in NUMERIC_COLUMNS:
        return "REAL"
    return "TEXT"


def _cast(value: str, col_name: str):
    if not value or value.strip() == "":
        return None
    if col_name.strip().lower() in NUMERIC_COLUMNS:
        try:
            return float(value)
        except ValueError:
            return value
    return value


def create_sqlite_db():
    """Read CSVs and create the SQLite database."""
    if SQLITE_PATH.exists():
        SQLITE_PATH.unlink()

    conn = sqlite3.connect(str(SQLITE_PATH))
    cur = conn.cursor()

    for table_name, csv_path in TABLES.items():
        if not csv_path.exists():
            print(f"WARNING: {csv_path} not found, skipping.")
            continue

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            headers = [h.strip() for h in next(reader)]

            col_defs = ", ".join(
                f'"{h}" {_infer_type(h)}' for h in headers
            )
            cur.execute(f'DROP TABLE IF EXISTS "{table_name}"')
            cur.execute(f'CREATE TABLE "{table_name}" ({col_defs})')

            placeholders = ", ".join("?" for _ in headers)
            insert_sql = f'INSERT INTO "{table_name}" VALUES ({placeholders})'

            rows = []
            for row in reader:
                converted = [_cast(row[i], headers[i]) if i < len(row) else None
                             for i in range(len(headers))]
                rows.append(converted)

            cur.executemany(insert_sql, rows)
            print(f"  Inserted {len(rows)} rows into {table_name}")

    conn.commit()
    conn.close()
    print(f"SQLite database created at: {SQLITE_PATH}")
    return SQLITE_PATH


if __name__ == "__main__":
    create_sqlite_db()
