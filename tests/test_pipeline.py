import subprocess
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from workforce_pipeline import connect, publish

SAMPLE = (Path(__file__).resolve().parents[1] / "examples/workforce.csv").read_bytes()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)
        publish(self.db, SAMPLE)

    def rows(self):
        return self.db.execute("SELECT * FROM employees ORDER BY employee_id").fetchall()

    def test_replay_keeps_dataset_and_records_attempt(self):
        before = self.rows()
        self.assertEqual(publish(self.db, SAMPLE)["status"], "unchanged")
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)

    def test_bad_date_does_not_replace_good_data(self):
        before = self.rows()
        with self.assertRaises(ValueError):
            publish(self.db, SAMPLE.replace(b"2024-01-15", b"2024-99-99"))
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.db.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0], "failed")

    def test_database_failure_rolls_back_delete_and_insert(self):
        before = self.rows()
        self.db.executescript("""CREATE TRIGGER reject_insert BEFORE INSERT ON employees
            WHEN NEW.employee_id='DEMO-002' BEGIN SELECT RAISE(ABORT,'simulated write failure'); END;""")
        with self.assertRaises(sqlite3.IntegrityError):
            publish(self.db, SAMPLE.replace(b"Support", b"Services"))
        self.assertEqual(self.rows(), before)

    def test_duplicate_ids_and_nonfinite_fte_rejected(self):
        for raw in [SAMPLE.replace(b"DEMO-002", b"DEMO-001"), SAMPLE.replace(b"0.8", b"NaN")]:
            with self.assertRaises(ValueError):
                publish(self.db, raw)
        self.assertEqual(len(self.rows()), 4)

    def test_empty_and_unexpected_shrink_rejected(self):
        lines = SAMPLE.splitlines(keepends=True)
        for raw in [lines[0], b"".join(lines[:2])]:
            with self.assertRaises(ValueError):
                publish(self.db, raw)
        result = publish(self.db, b"".join(lines[:2]), max_drop_fraction=0.8)
        self.assertEqual(result["rows"], 1)

    def test_previous_digest_can_be_republished_after_different_snapshot(self):
        publish(self.db, SAMPLE.replace(b"Support", b"Services"))
        self.assertEqual(publish(self.db, SAMPLE)["status"], "published")

    def test_cli_failure_is_nonzero(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.csv"
            path.write_text("wrong,columns\n")
            result = subprocess.run([sys.executable, "workforce_pipeline.py", "--input", str(path)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no new snapshot published", result.stderr)
