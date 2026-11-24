"""Scheduler tests: real bounded AST audits, isolation, and failure handling."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from oss_audit.scheduling import tick
from oss_audit.scheduling.queue import PRODUCTION_DB, PipelineQueue


def make_repo(parent: Path, name: str, with_lead: bool = True) -> Path:
    repo = parent / name
    (repo / "pkg").mkdir(parents=True)
    if with_lead:
        (repo / "pkg" / "base.py").write_text(
            "from typing import Protocol\n\n"
            "class Store(Protocol):\n"
            "    def get(self, key): ...\n"
            "    def put(self, key, value): ...\n",
            encoding="utf-8",
        )
        (repo / "pkg" / "a.py").write_text(
            "from .base import Store\n\n"
            "class MemoryStore(Store):\n"
            "    def get(self, key):\n"
            "        return None\n"
            "    def put(self, key, value):\n"
            "        pass\n",
            encoding="utf-8",
        )
        (repo / "pkg" / "b.py").write_text(
            "from .base import Store\n\n"
            "class DictStore(Store):\n"
            "    def get(self, key):\n"
            "        return 1\n"
            "    def put(self, key, value):\n"
            "        return None\n",
            encoding="utf-8",
        )
    else:
        (repo / "pkg" / "plain.py").write_text("def helper(x):\n    return x + 1\n", encoding="utf-8")
    return repo


def git_init(repo: Path) -> str:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "fixture"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


def envelope(repo: Path, name: str, kind: str = "audit", root_cause: str = "shared guard"):
    return {
        "candidate_id": f"fixture-{name}",
        "repo": f"fixture/{name}",
        "scope": "pkg",
        "kind": kind,
        "checkout": str(repo),
        "base_sha": "",
        "root_cause": root_cause,
        "payload": {"allowed_paths": [str(repo)]},
    }


class TickTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="oss-audit-tick-"))
        self.db = self.dir / "queue.db"
        self.art = self.dir / "artifacts"
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def logger(self, *args, **kwargs):
        self.logs.append(" ".join(str(a) for a in args))

    def test_real_audits_run_concurrently_in_isolated_db(self):
        one = make_repo(self.dir, "repo-one")
        two = make_repo(self.dir, "repo-two")
        summary = tick.run_tick(
            db_path=self.db,
            dry_run=True,
            workers=2,
            max_minutes=2,
            enqueue=[envelope(one, "one"), envelope(two, "two")],
            artifact_dir=self.art,
            logger=self.logger,
            poll_interval=0.05,
        )
        self.assertEqual(summary["counts"].get("investigated"), 2, summary)
        self.assertEqual(summary["failed"], 0)
        artifacts = [r.get("artifact") for r in summary["results"]]
        self.assertEqual(len(artifacts), 2)
        for artifact in artifacts:
            self.assertTrue(Path(artifact).is_file(), artifact)
            groups = json.loads(Path(artifact).read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(groups), 1)
            self.assertTrue(Path(artifact).is_relative_to(self.art.resolve()))
        with PipelineQueue(self.db) as queue:
            for task in queue.list_tasks():
                self.assertEqual(task["state"], "investigated")
                self.assertTrue(Path(task["output_path"]).is_file())

    def test_resource_pressure_leaves_work_pending_without_spawning(self):
        from unittest.mock import patch

        one = make_repo(self.dir, "repo-low-memory")
        with patch.object(tick, "admission", return_value={"ok": False, "reasons": ["low_memory"]}):
            summary = tick.run_tick(
                db_path=self.db,
                dry_run=True,
                enqueue=[envelope(one, "low-memory")],
                artifact_dir=self.art,
                logger=self.logger,
                worker=lambda *a: self.fail("no task should start under pressure"),
            )
        self.assertEqual(summary["claimed"], 0)
        self.assertEqual(summary["counts"]["pending"], 1)
        self.assertIn("low_memory", summary["deferred"])

    def test_dry_run_refuses_production_db(self):
        repo = make_repo(self.dir, "repo-guard")
        before = PRODUCTION_DB.exists()
        with self.assertRaises(tick.SchedulerError):
            tick.run_tick(
                db_path=PRODUCTION_DB,
                dry_run=True,
                max_minutes=1,
                enqueue=[envelope(repo, "guard")],
                logger=self.logger,
            )
        self.assertEqual(PRODUCTION_DB.exists(), before)

    def test_no_finding_is_a_valid_completion(self):
        empty = make_repo(self.dir, "repo-empty", with_lead=False)
        summary = tick.run_tick(
            db_path=self.db,
            dry_run=True,
            workers=1,
            max_minutes=2,
            enqueue=[envelope(empty, "empty", root_cause="no shared contract")],
            artifact_dir=self.art,
            logger=self.logger,
            poll_interval=0.05,
        )
        self.assertEqual(summary["counts"].get("no_finding"), 1, summary)
        self.assertGreaterEqual(summary["finished"], 1)

    def test_one_task_exception_does_not_block_others(self):
        good = make_repo(self.dir, "repo-good")
        bad = make_repo(self.dir, "repo-bad")

        def worker(task, ctx):
            if task["repo"] == "fixture/bad":
                raise RuntimeError("fixture failure")
            return {"status": "investigated", "artifact": None, "detail": {"ok": True}}

        summary = tick.run_tick(
            db_path=self.db,
            dry_run=True,
            workers=2,
            max_minutes=2,
            enqueue=[envelope(good, "good"), envelope(bad, "bad")],
            artifact_dir=self.art,
            worker=worker,
            logger=self.logger,
            poll_interval=0.05,
        )
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["finished"], 1)
        with PipelineQueue(self.db) as queue:
            tasks = queue.list_tasks()
            states = {t["repo"]: t["state"] for t in tasks}
            bad_task = next(t for t in tasks if t["repo"] == "fixture/bad")
        self.assertEqual(states["fixture/good"], "investigated")
        self.assertEqual(states["fixture/bad"], "pending")
        self.assertIn("fixture failure", bad_task["last_error"])

    def test_worker_cannot_self_declare_prepared(self):
        repo = make_repo(self.dir, "repo-selfapprove")

        def worker(task, ctx):
            return {"status": "prepared", "artifact": str(self.dir / "fake.json")}

        summary = tick.run_tick(
            db_path=self.db,
            dry_run=True,
            workers=1,
            max_minutes=2,
            enqueue=[envelope(repo, "selfapprove")],
            artifact_dir=self.art,
            worker=worker,
            logger=self.logger,
            poll_interval=0.05,
        )
        self.assertEqual(summary["failed"], 1)
        with PipelineQueue(self.db) as queue:
            task = queue.list_tasks()[0]
        self.assertNotEqual(task["state"], "prepared")
        self.assertIn("InvalidResult", task["last_error"] or "")

    def test_review_pending_releases_worker_slot(self):
        one = make_repo(self.dir, "repo-r1")
        two = make_repo(self.dir, "repo-r2")

        def worker(task, ctx):
            return {"status": "review_pending", "artifact": None}

        summary = tick.run_tick(
            db_path=self.db,
            dry_run=True,
            workers=1,
            max_minutes=2,
            enqueue=[envelope(one, "r1"), envelope(two, "r2")],
            artifact_dir=self.art,
            worker=worker,
            logger=self.logger,
            poll_interval=0.05,
        )
        self.assertEqual(summary["counts"].get("review_pending"), 2, summary)
        self.assertEqual(summary["finished"], 2)

    def test_restart_does_not_rerun_done_tasks(self):
        repo = make_repo(self.dir, "repo-restart")
        calls = []

        def worker(task, ctx):
            calls.append(task["id"])
            return {"status": "no_finding", "artifact": None}

        kwargs = dict(
            db_path=self.db,
            dry_run=True,
            workers=1,
            max_minutes=2,
            artifact_dir=self.art,
            worker=worker,
            logger=self.logger,
            poll_interval=0.05,
        )
        first = tick.run_tick(enqueue=[envelope(repo, "restart")], **kwargs)
        self.assertEqual(len(calls), 1)
        second = tick.run_tick(**kwargs)
        self.assertEqual(len(calls), 1, "completed task ran again")
        self.assertEqual(second["claimed"], 0)
        self.assertEqual(first["counts"].get("no_finding"), 1)

    def test_model_tasks_stay_pending_without_execute_pi(self):
        repo = make_repo(self.dir, "repo-model")
        env = envelope(repo, "model", kind="investigate", root_cause="needs model")
        summary = tick.run_tick(
            db_path=self.db,
            dry_run=True,
            workers=1,
            max_minutes=2,
            enqueue=[env],
            artifact_dir=self.art,
            logger=self.logger,
            poll_interval=0.05,
        )
        self.assertEqual(summary["claimed"], 0)
        with PipelineQueue(self.db) as queue:
            self.assertEqual(queue.list_tasks()[0]["state"], "pending")
        summary = tick.run_tick(
            db_path=self.db,
            dry_run=True,
            workers=1,
            max_minutes=2,
            execute_pi=True,
            artifact_dir=self.art,
            logger=self.logger,
            poll_interval=0.05,
        )
        self.assertEqual(summary["claimed"], 0)

    def test_unavailable_model_requeues_without_failure(self):
        repo = make_repo(self.dir, "repo-unavailable")

        def worker(task, ctx):
            return {"unavailable": "pi executable not found"}

        summary = tick.run_tick(
            db_path=self.db,
            dry_run=False,
            execute_pi=True,
            workers=1,
            max_minutes=2,
            enqueue=[envelope(repo, "unavailable", kind="investigate")],
            artifact_dir=self.art,
            worker=worker,
            logger=self.logger,
            poll_interval=0.05,
        )
        self.assertEqual(summary["requeued"], 1)
        with PipelineQueue(self.db) as queue:
            task = queue.list_tasks()[0]
        self.assertEqual(task["state"], "pending")
        self.assertEqual(task["attempts"], 0)

    def test_cli_enqueue_and_list(self):
        repo = make_repo(self.dir, "repo-cli")
        env_file = self.dir / "enqueue.json"
        env_file.write_text(json.dumps([envelope(repo, "cli")]), encoding="utf-8")
        self.assertEqual(tick.cli(["--dry-run", "--db", str(self.db), "--enqueue", str(env_file)]), 0)
        self.assertEqual(tick.cli(["--db", str(self.db), "--list"]), 0)

    def test_git_head_and_scope_are_enforced(self):
        repo = make_repo(self.dir, "repo-git")
        head = git_init(repo)
        env = envelope(repo, "git")
        env["base_sha"] = head
        summary = tick.run_tick(
            db_path=self.db,
            dry_run=True,
            workers=1,
            max_minutes=2,
            enqueue=[env],
            artifact_dir=self.art,
            logger=self.logger,
            poll_interval=0.05,
        )
        self.assertEqual(summary["counts"].get("investigated"), 1, summary)
        with PipelineQueue(self.db) as queue:
            task = queue.list_tasks()[0]
        self.assertEqual(task["state"], "investigated")
        bad = envelope(repo, "git-bad")
        bad["base_sha"] = "f" * 40
        bad["candidate_id"] = "fixture-git-bad"
        bad["root_cause"] = "different guard"
        bad_db = self.dir / "bad.db"
        summary = tick.run_tick(
            db_path=bad_db,
            dry_run=True,
            workers=1,
            max_minutes=2,
            enqueue=[bad],
            artifact_dir=self.art / "bad",
            logger=self.logger,
            poll_interval=0.05,
        )
        self.assertEqual(summary["failed"], 1)
        with PipelineQueue(bad_db) as queue:
            self.assertNotEqual(queue.list_tasks()[0]["state"], "investigated")

    def test_pi_session_identity_and_config_override(self):
        task = {"stable_key": "abc123", "candidate_id": "c1"}
        first = tick.pi_session_id(task)
        self.assertEqual(first, tick.pi_session_id(task))
        self.assertNotEqual(first, tick.pi_session_id({"stable_key": "other", "candidate_id": "c1"}))
        self.assertEqual(len(first), 36)
        cfg_ctx = {"config": {"pi": {"provider": "other", "model": "evil"}}, "queue": None}
        with self.assertRaises(tick.SchedulerError):
            tick.run_pi(
                {"lease_token": "x", "stable_key": "abc", "payload": {}, "repo": "a/b", "checkout": "."},
                dict(cfg_ctx, artifact_dir=str(self.dir)),
            )

    def test_policy_gate_blocks_heavy_without_evidence(self):
        task = {"repo": "a/b", "payload": {}, "checkout": str(self.dir)}
        gate = tick.heavy_policy_gate(task, {})
        self.assertFalse(gate["allowed"])
        self.assertIn("missing_contract_or_claim_context", gate["reasons"])
        self.assertIn("missing_authorized_worktree", gate["reasons"])

    def test_observed_identity_uses_message_provider_model(self):
        lines = [
            json.dumps(
                {
                    "type": "message",
                    "message": {"provider": "magpie", "model": "opencode-go/deepseek-v4.1-flash"},
                }
            ),
            json.dumps({"type": "done"}),
        ]
        stdout = chr(10).join(lines)
        provider, model, events = tick._observed_identity(stdout)
        self.assertEqual((provider, model), ("magpie", "opencode-go/deepseek-v4.1-flash"))
        self.assertEqual(len(events), 1)
        self.assertEqual(tick._observed_identity("")[:2], (None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
