import sqlite3
import tempfile
import unittest
from pathlib import Path

from oss_audit.scheduling.queue import PipelineQueue
from oss_audit.verification.metrics import local_snapshot


class LocalMetrics(unittest.TestCase):
    def test_read_only_funnel_does_not_promote_leads_or_missing_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "queue.db"
            with PipelineQueue(db) as queue:
                task = queue.enqueue(
                    {
                        "repo": "o/r",
                        "scope": "pkg",
                        "root_cause": "audit surface",
                        "kind": "audit",
                        "checkout": directory,
                        "payload": {},
                    }
                )
                lease = queue.claim("read")
                queue.record_run_identity(lease["lease_token"], artifact_path="intent.json")
                queue.record_run_identity(lease["lease_token"], run_pid=123, run_creation="fixture")
                queue.finish(task["id"], lease["lease_token"], "investigated")
                events_before = queue.events(100)
                result = local_snapshot(db)
                self.assertEqual(result["funnel"]["investigated"], 1)
                self.assertEqual(result["funnel"]["verified_current_prepared"], 0)
                self.assertIsNone(result["funnel"]["submitted"])
                self.assertIsNone(result["execution"]["model_tokens"])
                self.assertEqual(result["execution"]["recorded_runs"], 1)
                self.assertEqual(result["execution"]["registration_records"], 2)
                self.assertEqual(queue.events(100), events_before)
                queue.conn.execute("UPDATE tasks SET state='prepared', output_path='missing.json'")
                result = local_snapshot(db)
                self.assertEqual(result["funnel"]["prepared_current"], 0)
                self.assertEqual(len(result["invalid_prepared"]), 1)

    def test_missing_database_does_not_create_an_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.db"
            with self.assertRaises(sqlite3.Error):
                local_snapshot(path)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
