"""Unit tests for the SQLite coordinator (no network, no GitHub writes)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from oss_audit.scheduling import lease_store as ls
from oss_audit.scheduling import queue as pq

ROOT = Path(__file__).resolve().parents[1]


def dead_pid() -> int:
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="oss-audit-queue-"))
        self.checkout = self.dir / "checkout"
        self.checkout.mkdir()
        self.db = self.dir / "queue.db"
        self.queue = pq.PipelineQueue(self.db)

    def tearDown(self):
        self.queue.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def envelope(self, **over):
        env = {
            "repo": "owner/repo",
            "scope": "pkg/module.py",
            "kind": "audit",
            "checkout": str(self.checkout),
            "base_sha": "a" * 40,
            "root_cause": "shared contract guard missing",
            "payload": {"notes": "fixture"},
        }
        env.update(over)
        return env

    def test_duplicate_enqueue_and_stable_root_cause_id(self):
        first = self.queue.enqueue(self.envelope())
        second = self.queue.enqueue(self.envelope())
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.queue.list_tasks()), 1)

        labeled = self.queue.enqueue(self.envelope(candidate_id="manual-label-1"))
        self.assertEqual(labeled["id"], first["id"])
        self.assertTrue(labeled.get("merged_by_stable_key"))

        other_cause = self.queue.enqueue(self.envelope(root_cause="different cause"))
        self.assertNotEqual(other_cause["id"], first["id"])
        self.assertEqual(
            other_cause["stable_key"],
            pq.stable_candidate_id("owner/repo", "different cause", "pkg/module.py"),
        )
        self.assertNotEqual(other_cause["stable_key"], first["stable_key"])

    def test_envelope_rejects_shell_commands(self):
        with self.assertRaises(pq.QueueError):
            self.queue.enqueue(self.envelope(payload={"command": "rm -rf /"}))
        with self.assertRaises(pq.QueueError):
            self.queue.enqueue(self.envelope(kind="run-shell"))
        with self.assertRaises(pq.QueueError):
            self.queue.enqueue(self.envelope(root_cause=""))

    def test_advance_preserves_identity_session_and_research_budget(self):
        env = self.envelope()
        first = self.queue.enqueue(env)
        lease = self.queue.claim("read", session_id="fixed-session", daily_research=1)
        self.queue.finish(first["id"], lease["lease_token"], "investigated", output_path="old-artifact")
        updated = self.queue.advance(self.envelope(kind="investigate"), "investigated", "contract reviewed")
        self.assertEqual(updated["id"], first["id"])
        self.assertEqual(updated["session_id"], "fixed-session")
        self.assertIsNone(updated["output_path"])
        heavy = self.queue.claim("heavy", daily_research=1)
        self.assertEqual(heavy["id"], first["id"])
        self.queue.finish(heavy["id"], heavy["lease_token"], "waiting")
        self.queue.advance(self.envelope(kind="followup"), "waiting", "maintainer feedback")
        self.queue.enqueue(self.envelope(root_cause="another candidate"))
        self.assertIsNone(self.queue.claim("read", daily_research=1))
        self.assertEqual(self.queue.claim("heavy", daily_research=1)["id"], first["id"])

    def test_advance_rejects_stale_state_and_identity_changes(self):
        first = self.queue.enqueue(self.envelope(candidate_id="stable"))
        lease = self.queue.claim("read")
        self.queue.finish(first["id"], lease["lease_token"], "waiting")
        with self.assertRaises(pq.QueueError):
            self.queue.advance(self.envelope(candidate_id="stable"), "investigated", "outdated read")
        with self.assertRaises(pq.QueueError):
            self.queue.advance(
                self.envelope(candidate_id="stable", root_cause="different"), "waiting", "wrong cause"
            )
        self.queue.advance(self.envelope(candidate_id="stable"), "waiting", "new policy evidence")
        with self.assertRaises(pq.QueueError):
            self.queue.advance(self.envelope(candidate_id="stable"), "waiting", "duplicate continuation")

    def test_advance_never_steals_a_lease(self):
        task = self.queue.enqueue(self.envelope())
        self.queue.claim("read", now=100, ttl=1)
        self.queue.conn.execute("UPDATE tasks SET state='waiting' WHERE id=?", (task["id"],))
        with self.assertRaises(pq.QueueError):
            self.queue.advance(self.envelope(), "waiting", "refresh", now=1000)

    def test_claim_heartbeat_finish_fencing(self):
        task = self.queue.enqueue(self.envelope())
        lease = self.queue.claim("read", ttl=60)
        self.assertEqual(lease["id"], task["id"])
        self.assertIsNotNone(lease["lease_token"])
        self.assertEqual(lease["purpose"], "read")
        self.queue.heartbeat(lease["lease_token"], ttl=60)
        done = self.queue.finish(lease["id"], lease["lease_token"], "investigated", output_path=None)
        self.assertEqual(done["state"], "investigated")
        with self.assertRaises(pq.StaleLease):
            self.queue.finish(lease["id"], lease["lease_token"], "no_finding")
        self.assertEqual(self.queue.get_task(lease["id"])["state"], "investigated")

    def test_result_requires_unexpired_token(self):
        self.queue.enqueue(self.envelope())
        lease = self.queue.claim("read", ttl=0.05)
        time.sleep(0.08)
        with self.assertRaises(pq.StaleLease):
            self.queue.finish(lease["id"], lease["lease_token"], "no_finding")
        with self.assertRaises(pq.StaleLease):
            self.queue.heartbeat(lease["lease_token"], ttl=60)

    def test_single_heavy_writer_per_repo_and_candidate(self):
        self.queue.enqueue(self.envelope(kind="investigate", root_cause="c1"))
        self.queue.enqueue(self.envelope(kind="investigate", root_cause="c2"))
        audit = self.queue.enqueue(self.envelope(root_cause="audit-read"))
        first = self.queue.claim("heavy", ttl=60)
        self.assertIsNotNone(first)
        self.assertIsNone(self.queue.claim("heavy", ttl=60))
        read = self.queue.claim("read", ttl=60)
        self.assertEqual(read["id"], audit["id"])
        self.queue.finish(first["id"], first["lease_token"], "investigated")
        second = self.queue.claim("heavy", ttl=60)
        self.assertIsNotNone(second)
        self.assertNotEqual(second["candidate_id"], first["candidate_id"])

        self.queue.enqueue(self.envelope(repo="other/repo", kind="investigate", root_cause="c3"))
        self.assertIsNotNone(self.queue.claim("heavy", ttl=60, repo_limit=2))
        self.assertIsNone(self.queue.claim("heavy", ttl=60, repo_limit=2))

    def test_heavy_limit_counts_distinct_repos(self):
        self.queue.enqueue(self.envelope(repo="a/one", kind="investigate", root_cause="r1"))
        self.queue.enqueue(self.envelope(repo="b/two", kind="investigate", root_cause="r2"))
        self.assertIsNotNone(self.queue.claim("heavy", ttl=60, repo_limit=1))
        self.assertIsNone(self.queue.claim("heavy", ttl=60, repo_limit=1))

    def test_dead_coordinator_recovery_and_old_token_invalid(self):
        task = self.queue.enqueue(self.envelope())
        lease = self.queue.claim("read", ttl=120, owner_pid=dead_pid(), owner_creation=None)
        actions = self.queue.recover(ttl=120)
        self.assertEqual(actions[0]["action"], "requeued")
        self.assertEqual(self.queue.get_task(task["id"])["state"], "pending")
        with self.assertRaises(pq.StaleLease):
            self.queue.finish(task["id"], lease["lease_token"], "no_finding")
        reclaimed = self.queue.claim("read", ttl=60)
        self.assertEqual(reclaimed["id"], task["id"])

    def test_artifact_after_crash_is_inspect_not_rerun(self):
        artifact = self.dir / "partial.json"
        artifact.write_text(json.dumps([{"contract": "X"}]), encoding="utf-8")
        task = self.queue.enqueue(self.envelope())
        self.queue.claim("read", ttl=120, owner_pid=dead_pid(), artifact_path=str(artifact))
        actions = self.queue.recover(ttl=120)
        self.assertEqual(actions[0]["action"], "inspect_artifact")
        self.assertEqual(self.queue.get_task(task["id"])["state"], "inspect_required")
        self.assertIsNone(self.queue.claim("read", ttl=60))
        self.assertTrue(artifact.exists())

    def test_live_owner_is_not_stolen_after_ttl(self):
        self.queue.enqueue(self.envelope())
        lease = self.queue.claim("read", ttl=0.01)
        time.sleep(0.03)
        actions = self.queue.recover(ttl=60)
        self.assertEqual(actions[0]["action"], "kept_alive")
        done = self.queue.finish(lease["id"], lease["lease_token"], "no_finding")
        self.assertEqual(done["state"], "no_finding")

    def test_unknown_identity_is_rechecked_not_stolen(self):
        self.queue.enqueue(self.envelope())
        lease = self.queue.claim("read", ttl=0.01)
        with mock.patch.object(pq, "process_status", return_value="unknown"):
            time.sleep(0.03)
            actions = self.queue.recover(ttl=60, quarantine=60)
        self.assertEqual(actions[0]["action"], "recheck_unknown")
        self.assertEqual(self.queue.get_task(lease["id"])["state"], "running")
        self.queue.finish(lease["id"], lease["lease_token"], "no_finding")

    def test_reviewer_only_can_mark_prepared(self):
        self.queue.enqueue(self.envelope(kind="investigate", root_cause="fixable"))
        worker = self.queue.claim("heavy", ttl=60)
        with self.assertRaises(pq.InvalidResult):
            self.queue.finish(worker["id"], worker["lease_token"], "prepared")
        self.queue.finish(worker["id"], worker["lease_token"], "review_pending")

        review = self.queue.claim("review", ttl=60)
        self.assertEqual(review["role"], "reviewer")
        with self.assertRaises(pq.InvalidResult):
            self.queue.finish(review["id"], review["lease_token"], "prepared")
        fake_evidence = self.dir / "evidence.json"
        fake_evidence.write_text("{}", encoding="utf-8")
        with self.assertRaises(pq.InvalidResult):
            self.queue.finish(review["id"], review["lease_token"], "prepared", output_path=str(fake_evidence))
        self.assertEqual(self.queue.get_task(review["id"])["state"], "running")

    def test_waiting_and_review_pending_are_not_claimable(self):
        task = self.queue.enqueue(self.envelope())
        lease = self.queue.claim("read", ttl=60)
        self.queue.finish(task["id"], lease["lease_token"], "review_pending")
        self.assertIsNone(self.queue.claim("read", ttl=60))
        self.assertFalse(self.queue.has_claimable(["audit"]))
        self.assertEqual(self.queue.counts()["review_pending"], 1)

    def test_requeue_keeps_task_pending_without_failure(self):
        task = self.queue.enqueue(self.envelope())
        lease = self.queue.claim("read", ttl=60)
        updated = self.queue.requeue(lease["lease_token"], "pi unavailable", backoff=0)
        self.assertEqual(updated["state"], "pending")
        self.assertEqual(updated["attempts"], 0)
        self.assertEqual(self.queue.claim("read", ttl=60)["id"], task["id"])

    def test_concurrent_os_process_claim_race(self):
        for i in range(8):
            self.queue.enqueue(self.envelope(repo=f"owner/repo{i}", root_cause=f"cause{i}"))
        script = (
            "import json, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from oss_audit.scheduling.queue import PipelineQueue\n"
            "with PipelineQueue(sys.argv[1]) as q:\n"
            "    claimed = []\n"
            "    while True:\n"
            "        task = q.claim('read', ttl=120)\n"
            "        if task is None:\n"
            "            break\n"
            "        claimed.append(task['id'])\n"
            "    print(json.dumps(claimed))\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.db)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(6)
        ]
        claimed = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
            claimed.extend(json.loads(out))
        self.assertEqual(sorted(claimed), list(range(1, 9)))
        self.assertEqual(len(claimed), len(set(claimed)))
        with self.queue._lock:
            leases = self.queue.conn.execute("SELECT task_id FROM leases").fetchall()
        self.assertEqual(len({row["task_id"] for row in leases}), 8)

    def test_restart_does_not_reclaim_terminal_states(self):
        task = self.queue.enqueue(self.envelope())
        lease = self.queue.claim("read", ttl=60)
        self.queue.finish(task["id"], lease["lease_token"], "no_finding")
        reopened = pq.PipelineQueue(self.db)
        try:
            reopened.recover(ttl=60)
            self.assertIsNone(reopened.claim("read", ttl=60))
            self.assertEqual(reopened.get_task(task["id"])["state"], "no_finding")
        finally:
            reopened.close()

    def test_no_finding_cooldown_suppresses_new_similar_candidate(self):
        task = self.queue.enqueue(self.envelope())
        lease = self.queue.claim("read", ttl=60)
        self.queue.finish(task["id"], lease["lease_token"], "no_finding")
        cooled = self.queue.enqueue(
            self.envelope(candidate_id="another-label"), config={"cooldown_no_finding_hours": 72}
        )
        self.assertEqual(cooled["id"], task["id"])
        self.assertTrue(cooled.get("cooldown"))

    def test_capacity_is_enforced_in_db_across_connections(self):
        for i in range(2):
            self.queue.enqueue(self.envelope(repo=f"cap/repo{i}", root_cause=f"cap{i}"))
        other = pq.PipelineQueue(self.db)
        try:
            self.assertIsNotNone(self.queue.claim("read", ttl=60, read_capacity=1))
            self.assertIsNone(other.claim("read", ttl=60, read_capacity=1))
            self.assertEqual(len(self.queue.list_tasks()), 2)
        finally:
            other.close()

    def test_daily_budget_limits_execution_not_discovery(self):
        for i in range(3):
            self.queue.enqueue(
                self.envelope(repo=f"daily/repo{i}", root_cause=f"cause{i}"), config={"daily_research": 1}
            )
        first = self.queue.claim("read", ttl=60, daily_research=1)
        self.assertIsNotNone(first)
        self.queue.finish(first["id"], first["lease_token"], "no_finding")
        self.assertIsNone(self.queue.claim("read", ttl=60, daily_research=1))
        self.assertEqual(len(self.queue.list_tasks()), 3)

    def test_new_base_and_expired_cooldown_wake_terminal_task(self):
        t0 = 1_700_000_000.0
        env = self.envelope(base_sha="a" * 40)
        task = self.queue.enqueue(env, now=t0)
        lease = self.queue.claim("read", ttl=60, now=t0)
        self.queue.finish(task["id"], lease["lease_token"], "no_finding", now=t0 + 1)
        woken = self.queue.enqueue(self.envelope(base_sha="b" * 40), now=t0 + 2)
        self.assertEqual(woken["id"], task["id"])
        self.assertTrue(woken.get("woken"))
        self.assertEqual(woken["state"], "pending")
        self.assertEqual(woken["base_sha"], "b" * 40)
        lease = self.queue.claim("read", ttl=60, now=t0 + 3)
        self.queue.finish(task["id"], lease["lease_token"], "no_finding", now=t0 + 4)
        cooled = self.queue.enqueue(
            self.envelope(base_sha="b" * 40), now=t0 + 5, config={"cooldown_no_finding_hours": 1}
        )
        self.assertTrue(cooled.get("cooldown"))
        expiry = self.queue.enqueue(
            self.envelope(base_sha="b" * 40), now=t0 + 7200, config={"cooldown_no_finding_hours": 1}
        )
        self.assertTrue(expiry.get("woken"))

    def test_prepared_rejects_fake_existing_json(self):
        self.queue.enqueue(self.envelope(kind="investigate", root_cause="prepared-gate"))
        worker = self.queue.claim("heavy", ttl=60)
        self.queue.finish(worker["id"], worker["lease_token"], "review_pending")
        review = self.queue.claim("review", ttl=60)
        fake = self.dir / "fake-bundle.json"
        fake.write_text(json.dumps({"contract": str(self.dir / "missing.json")}), encoding="utf-8")
        with self.assertRaises(pq.InvalidResult):
            self.queue.finish(review["id"], review["lease_token"], "prepared", output_path=str(fake))
        self.assertEqual(self.queue.get_task(review["id"])["state"], "running")

    def test_recover_holds_registered_live_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            task = self.queue.enqueue(self.envelope())
            lease = self.queue.claim("read", ttl=120, owner_pid=dead_pid())
            self.queue.record_run_identity(
                lease["lease_token"], run_pid=child.pid, run_creation=pq._creation_identity(child.pid)[1]
            )
            actions = self.queue.recover(ttl=60)
            self.assertEqual(actions[0]["action"], "hold_orphan_child")
            self.assertEqual(self.queue.get_task(task["id"])["state"], "running")
            self.assertIsNone(self.queue.claim("read", ttl=60))
        finally:
            child.kill()
            child.wait(timeout=10)

    def test_lease_store_wal_and_transaction_rollback_and_reexports(self):
        self.assertIs(pq.SQLiteLeaseStore, ls.SQLiteLeaseStore)
        self.assertIs(pq.StaleLease, ls.StaleLease)
        self.assertIs(pq.InvalidResult, ls.InvalidResult)
        self.assertIs(pq.QueueError, ls.QueueError)
        self.assertIs(pq.stable_candidate_id, ls.stable_candidate_id)
        self.assertTrue(issubclass(pq.PipelineQueue, ls.SQLiteLeaseStore))

        store_db = self.dir / "lease-store-unit.db"
        with ls.SQLiteLeaseStore(store_db) as store:
            mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(str(mode).lower(), "wal")
            with self.assertRaises(RuntimeError):
                with store._tx():
                    store._event(1, "cand-1", "transient_before_crash", {"k": "v"})
                    raise RuntimeError("abort transaction")
            self.assertEqual(store.events(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
