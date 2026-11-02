"""Validate and atomically publish synthetic workforce snapshots."""
import argparse
import csv
import datetime
import hashlib
import io
import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path

HEADERS = ["employee_id", "display_name", "department", "hire_date", "fte"]


def connect(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS employees (
            employee_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
            department TEXT NOT NULL, hire_date TEXT NOT NULL,
            fte REAL NOT NULL CHECK (fte > 0 AND fte <= 1));
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL,
            row_count INTEGER NOT NULL, error_code TEXT,
            at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS published_snapshot (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), digest TEXT NOT NULL);
    """)
    return db


def parse_snapshot(raw):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if reader.fieldnames != HEADERS:
        raise ValueError("Unexpected CSV schema")
    rows, seen = [], set()
    for number, row in enumerate(reader, start=2):
        if None in row or any(value is None or not value.strip() for value in row.values()):
            raise ValueError("Missing or extra fields at row %d" % number)
        row = {key: value.strip() for key, value in row.items()}
        if row["employee_id"] in seen:
            raise ValueError("Duplicate identity at row %d" % number)
        seen.add(row["employee_id"])
        try:
            date = datetime.date.fromisoformat(row["hire_date"])
            fte = float(row["fte"])
        except ValueError as exc:
            raise ValueError("Invalid date or FTE at row %d" % number) from exc
        if not 0 < fte <= 1:
            raise ValueError("FTE outside (0,1] at row %d" % number)
        rows.append((row["employee_id"], row["display_name"], row["department"], date.isoformat(), fte))
    if not rows:
        raise ValueError("Empty snapshot cannot replace the current dataset")
    return rows


def publish(db, raw, max_drop_fraction=0.25):
    """All target changes and success audit share a transaction; failures preserve state."""
    if not 0 <= max_drop_fraction <= 1:
        raise ValueError("max_drop_fraction must be between zero and one")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        rows = parse_snapshot(raw)
    except (ValueError, UnicodeError):
        with db:
            db.execute("INSERT INTO runs(digest,status,row_count,error_code) VALUES (?,'failed',0,'validation')", (digest,))
        raise
    try:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT digest FROM published_snapshot WHERE singleton=1").fetchone()
        if current and current[0] == digest:
            db.execute("INSERT INTO runs(digest,status,row_count) VALUES (?,'unchanged',?)", (digest, len(rows)))
            db.commit()
            return {"status": "unchanged", "rows": len(rows), "digest": digest}
        count = db.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
        if count and len(rows) < count * (1-max_drop_fraction):
            raise ValueError("Unexpected row-count drop; explicit override required")
        # The previous snapshot remains committed until this entire transaction succeeds.
        db.execute("DELETE FROM employees")
        db.executemany("INSERT INTO employees VALUES (?,?,?,?,?)", rows)
        db.execute("INSERT OR REPLACE INTO published_snapshot VALUES (1,?)", (digest,))
        db.execute("INSERT INTO runs(digest,status,row_count) VALUES (?,'published',?)", (digest, len(rows)))
        db.commit()
    except Exception:
        db.rollback()
        with db:
            db.execute("INSERT INTO runs(digest,status,row_count,error_code) VALUES (?,'failed',?,'publish')",
                       (digest, len(rows)))
        raise
    return {"status": "published", "rows": len(rows), "digest": digest}


def read_input(path, gpg_home=None):
    """Optional real GPG decryption. Keys belong to the caller, never this repository."""
    if path.suffix not in (".gpg", ".pgp"):
        return path.read_bytes()
    with tempfile.TemporaryDirectory(prefix="workforce-decrypt-") as temp:
        output = Path(temp) / "snapshot.csv"
        command = ["gpg", "--batch", "--no-tty"]
        if gpg_home:
            command += ["--homedir", str(gpg_home)]
        command += ["--output", str(output), "--decrypt", str(path)]
        subprocess.run(command, check=True, capture_output=True, timeout=60)
        return output.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path(__file__).parent / "examples/workforce.csv")
    parser.add_argument("--db", default=":memory:")
    parser.add_argument("--gpg-home", type=Path)
    parser.add_argument("--max-drop-fraction", type=float, default=0.25)
    args = parser.parse_args()
    db = connect(args.db)
    try:
        print(json.dumps(publish(db, read_input(args.input, args.gpg_home), args.max_drop_fraction), indent=2))
    except (ValueError, UnicodeError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        # Don't emit decrypted rows, SQL payloads, paths, or GPG diagnostics.
        parser.exit(1, "Import failed (%s); no new snapshot published.\n" % type(exc).__name__)
    finally:
        db.close()


if __name__ == "__main__":
    main()
