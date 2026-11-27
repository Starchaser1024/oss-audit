"""SQLite task coordinator for the local contribution pipeline.

Standard library only. The queue is the single source of execution facts for
new work; the historical ``ledger.json`` is never written here.

Low-level SQLite WAL connection setup, ``BEGIN IMMEDIATE`` transactions, and
PID+creation-time lease fencing primitives live in ``oss_audit.scheduling.lease_store``
and are re-exported here for compatibility.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from oss_audit.core.paths import REPO_ROOT, WorkspacePaths
from oss_audit.scheduling.lease_store import (
    CLAIMABLE,
    FORBIDDEN_PAYLOAD_KEYS,
    ImmediateTransaction,
    InvalidResult,
    QueueError,
    SQLiteLeaseStore,
    StaleLease,
    _creation_identity,
    check_payload,
    day_start,
    has_nonempty_artifact,
    my_identity,
    process_status,
    stable_candidate_id,
    task_row_to_dict,
    verify_prepared_evidence,
)

PRODUCTION_DB = REPO_ROOT / "queue" / "pipeline.db"
KINDS = {"audit": "read", "investigate": "heavy", "followup": "heavy"}
TERMINAL_STATES = {
    "done", "failed", "no_finding", "review_pending", "waiting",
    "inspect_required", "prepared", "rejected", "needs_model", "investigated",
}
READ_RESULTS = {"investigated", "no_finding", "review_pending", "failed", "inspect_required", "waiting"}
HEAVY_RESULTS = {"investigated", "no_finding", "review_pending", "failed", "inspect_required", "waiting"}
REVIEW_RESULTS = {"prepared", "rejected", "review_pending", "done", "failed"}
PRIORITY = {"followup": 0, "investigate": 1, "audit": 2}

_Transaction = ImmediateTransaction
_check_payload = check_payload
_day_start = day_start

__all__ = [
    "CLAIMABLE", "FORBIDDEN_PAYLOAD_KEYS", "HEAVY_RESULTS", "InvalidResult", "KINDS",
    "PRIORITY", "PRODUCTION_DB", "PipelineQueue", "QueueError", "READ_RESULTS",
    "REVIEW_RESULTS", "SQLiteLeaseStore", "StaleLease", "TERMINAL_STATES",
    "_creation_identity", "cli", "my_identity", "process_status",
    "stable_candidate_id", "task_row_to_dict", "validate_envelope",
]


def validate_envelope(env: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(env, dict):
        raise QueueError("envelope must be a JSON object")
    for key in ("repo", "scope", "kind", "checkout"):
        if not str(env.get(key) or "").strip():
            raise QueueError(f"envelope missing {key}")
    kind = str(env["kind"])
    if kind not in KINDS:
        raise QueueError(f"unsupported kind {kind!r}")
    repo = str(env["repo"]).strip()
    scope = str(env["scope"]).strip()
    payload = env.get("payload") or {}
    if not isinstance(payload, dict):
        raise QueueError("payload must be an object")
    check_payload(payload)
    root_cause = str(env.get("root_cause") or payload.get("root_cause") or "").strip()
    if not root_cause:
        raise QueueError("envelope missing root_cause (top level or payload)")
    cand_id = str(env.get("candidate_id") or "").strip()
    stable = stable_candidate_id(repo, root_cause, scope)
    if not cand_id:
        cand_id = stable
    if len(cand_id) > 200:
        raise QueueError("candidate_id too long")
    return {
        "candidate_id": cand_id,
        "stable_key": stable,
        "repo": repo,
        "scope": scope,
        "root_cause": root_cause,
        "kind": kind,
        "checkout": str(env["checkout"]),
        "base_sha": str(env.get("base_sha") or ""),
        "head_sha": str(env.get("head_sha") or ""),
        "payload": payload,
        "priority": int(env.get("priority", PRIORITY[kind])),
    }


class PipelineQueue(SQLiteLeaseStore):
    """High-level task queue state machine backed by ``SQLiteLeaseStore``."""

    def enqueue(
        self,
        envelope: dict[str, Any],
        config: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = self.now() if now is None else now
        cfg = config or {}
        data = validate_envelope(envelope)
        cooldown_h = float(cfg.get("cooldown_no_finding_hours", 72) or 0)
        with self._tx():

            def maybe_wake(row: sqlite3.Row | None) -> dict[str, Any] | None:
                if row is None:
                    return None
                if self.conn.execute("SELECT 1 FROM leases WHERE task_id=?", (row["id"],)).fetchone():
                    return None
                if row["state"] not in ("done", "no_finding", "investigated", "failed", "rejected"):
                    return None
                base_changed = bool(data["base_sha"] and data["base_sha"] != row["base_sha"])
                cooled = (
                    row["state"] == "no_finding"
                    and bool(cooldown_h)
                    and ts - row["updated_at"] >= cooldown_h * 3600
                )
                if not (base_changed or cooled):
                    return None
                self.conn.execute(
                    "UPDATE tasks SET state=?, base_sha=?, updated_at=?, not_before=0, head_sha='' WHERE id=?",
                    (CLAIMABLE, data["base_sha"] or row["base_sha"], ts, row["id"]),
                )
                self._event(
                    row["id"],
                    row["candidate_id"],
                    "woken_base_change" if base_changed else "woken_cooldown",
                    {"base_sha": data["base_sha"]},
                    ts,
                )
                task = self._get_task(row["id"])
                assert task is not None
                task["woken"] = True
                return task

            existing = self.conn.execute(
                "SELECT * FROM tasks WHERE candidate_id=?", (data["candidate_id"],)
            ).fetchone()
            if existing:
                woken = maybe_wake(existing)
                if woken:
                    return woken
                task = self._task_dict(existing)
                assert task is not None
                if cooldown_h and task["state"] == "no_finding" and ts - task["updated_at"] < cooldown_h * 3600:
                    task["cooldown"] = True
                return task
            if cooldown_h:
                cutoff = ts - cooldown_h * 3600
                cooled = self.conn.execute(
                    "SELECT * FROM tasks WHERE stable_key=? AND state='no_finding' "
                    "AND updated_at >= ? ORDER BY updated_at DESC LIMIT 1",
                    (data["stable_key"], cutoff),
                ).fetchone()
                if cooled:
                    task = self._task_dict(cooled)
                    assert task is not None
                    task["cooldown"] = True
                    return task
            merged = self.conn.execute(
                "SELECT * FROM tasks WHERE stable_key=? AND state NOT IN ('rejected') ORDER BY id ASC LIMIT 1",
                (data["stable_key"],),
            ).fetchone()
            if merged:
                woken = maybe_wake(merged)
                if woken:
                    woken["merged_by_stable_key"] = True
                    return woken
                task = self._task_dict(merged)
                assert task is not None
                task["merged_by_stable_key"] = True
                return task
            cur = self.conn.execute(
                """INSERT INTO tasks(candidate_id, stable_key, repo, scope, root_cause,
                       kind, checkout, base_sha, head_sha, payload, state, priority,
                       created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    data["candidate_id"], data["stable_key"], data["repo"], data["scope"],
                    data["root_cause"], data["kind"], data["checkout"], data["base_sha"],
                    data["head_sha"], json.dumps(data["payload"], ensure_ascii=False),
                    CLAIMABLE, data["priority"], ts, ts,
                ),
            )
            task_id = cur.lastrowid
            self._event(
                task_id, data["candidate_id"], "enqueued", {"kind": data["kind"], "repo": data["repo"]}, ts
            )
            created = self.get_task(task_id)
            assert created is not None
            return created

    def advance(
        self,
        envelope: dict[str, Any],
        expected_state: str,
        reason: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        data = validate_envelope(envelope)
        ts = self.now() if now is None else now
        if not isinstance(reason, str) or not reason.strip():
            raise QueueError("advance requires a reason for the refreshed context")
        if expected_state not in TERMINAL_STATES:
            raise QueueError("advance requires a non-running, non-pending expected state")
        with self._tx():
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE candidate_id=?", (data["candidate_id"],)
            ).fetchone()
            if row is None or row["state"] != expected_state:
                raise QueueError("advance state changed or candidate is missing; inspect first")
            if row["stable_key"] != data["stable_key"]:
                raise QueueError("advance cannot change the candidate's repository, cause or scope")
            if self.conn.execute("SELECT 1 FROM leases WHERE task_id=?", (row["id"],)).fetchone():
                raise QueueError("advance cannot touch a leased task, even if the lease expired")
            self.conn.execute(
                "UPDATE tasks SET state='pending', kind=?, checkout=?, base_sha=?, head_sha='', "
                "payload=?, priority=?, updated_at=?, attempts=0, not_before=0, output_path=NULL, "
                "last_error=NULL WHERE id=?",
                (
                    data["kind"], data["checkout"], data["base_sha"],
                    json.dumps(data["payload"], ensure_ascii=False), data["priority"], ts, row["id"],
                ),
            )
            self._event(
                row["id"],
                row["candidate_id"],
                "advanced",
                {
                    "from_state": expected_state,
                    "from_kind": row["kind"],
                    "to_kind": data["kind"],
                    "reason": reason.strip(),
                    "prior_output_path": row["output_path"],
                    "prior_head_sha": row["head_sha"],
                    "base_sha": data["base_sha"],
                },
                ts,
            )
            updated = self.get_task(row["id"])
            assert updated is not None
            return updated

    def claim(
        self,
        purpose: str,
        owner_pid: int | None = None,
        owner_creation: str | None = None,
        coordinator_pid: int | None = None,
        ttl: float = 120.0,
        repo_limit: int | None = None,
        max_active: int | None = None,
        read_capacity: int | None = None,
        heavy_capacity: int | None = None,
        review_capacity: int | None = None,
        task_id: int | None = None,
        session_id: str | None = None,
        artifact_path: str | None = None,
        now: float | None = None,
        daily_research: int | None = None,
    ) -> dict[str, Any] | None:
        if purpose not in ("read", "heavy", "review"):
            raise QueueError(f"bad purpose {purpose!r}")
        ts = self.now() if now is None else now
        if owner_pid is None:
            owner_pid, owner_creation = my_identity()
        token = uuid.uuid4().hex
        with self._tx():
            if not self._within_lease_capacity(
                purpose, max_active, read_capacity, heavy_capacity, review_capacity, repo_limit
            ):
                return None
            kinds = list(KINDS) if purpose == "review" else [k for k, v in KINDS.items() if v == purpose]
            state_filter = "review_pending" if purpose == "review" else CLAIMABLE
            params: list[Any] = [state_filter, ts]
            sql = [
                "SELECT * FROM tasks WHERE state=? AND not_before<=?",
                "AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.task_id=tasks.id)",
            ]
            if task_id is not None:
                sql.append("AND id=?")
                params.append(task_id)
            if purpose != "review":
                sql.append("AND kind IN (%s)" % ",".join("?" * len(kinds)))
                params.extend(kinds)
            sql.append("ORDER BY priority ASC, created_at ASC, id ASC")
            rows = self.conn.execute(" ".join(sql), params).fetchall()
            role = "reviewer" if purpose == "review" else "worker"
            used_research_ids: set[int] | None = None
            for row in rows:
                if daily_research and row["kind"] in ("audit", "investigate") and purpose != "review":
                    if used_research_ids is None:
                        used_research_ids = self._used_daily_research_ids(ts)
                    if row["id"] not in used_research_ids and len(used_research_ids) >= int(daily_research):
                        continue
                if purpose == "heavy" and self.conn.execute(
                    "SELECT 1 FROM leases WHERE purpose='heavy' AND repo=?", (row["repo"],)
                ).fetchone():
                    continue
                if not self._try_insert_lease(
                    token=token, row=row, purpose=purpose, role=role,
                    owner_pid=owner_pid, owner_creation=owner_creation,
                    coordinator_pid=coordinator_pid, session_id=session_id,
                    artifact_path=artifact_path, ts=ts, ttl=ttl,
                ):
                    continue
                self.conn.execute(
                    "UPDATE tasks SET state='running', updated_at=?, session_id=COALESCE(?, session_id) WHERE id=?",
                    (ts, session_id, row["id"]),
                )
                self._event(
                    row["id"], row["candidate_id"], "claimed",
                    {"purpose": purpose, "kind": row["kind"], "token": token[:8], "pid": owner_pid}, ts,
                )
                task = self.get_task(row["id"])
                assert task is not None
                task["lease_token"] = token
                task["purpose"] = purpose
                task["role"] = role
                task["expires"] = ts + float(ttl)
                task["session_id"] = session_id or task.get("session_id")
                task["artifact_path"] = artifact_path
                return task
            return None

    def finish(
        self,
        task_id: int,
        token: str,
        status: str,
        output_path: str | None = None,
        head_sha: str | None = None,
        detail: dict[str, Any] | None = None,
        retry_after: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = self.now() if now is None else now
        with self._tx():
            lease = self._require_valid_lease(
                token, ts, task_id=task_id, missing_msg="unknown token (already released or replaced)"
            )
            allowed = {"read": READ_RESULTS, "heavy": HEAVY_RESULTS, "review": REVIEW_RESULTS}.get(
                lease["purpose"], set()
            )
            if status not in allowed:
                raise InvalidResult(f"status {status!r} not allowed for {lease['purpose']} lease")
            if status == "prepared":
                if lease["purpose"] != "review" or lease["role"] != "reviewer":
                    raise InvalidResult("prepared requires a reviewer lease")
                task_row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                head_sha = verify_prepared_evidence(task_row, output_path, head_sha)
            attempts = self.conn.execute(
                "SELECT attempts, max_attempts FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            state = status
            not_before = 0.0
            if status == "failed" and retry_after is not None and attempts:
                if attempts["attempts"] < attempts["max_attempts"]:
                    state = CLAIMABLE
                    not_before = ts + float(retry_after)
            self.conn.execute(
                "UPDATE tasks SET state=?, updated_at=?, attempts=attempts+1, "
                "not_before=?, head_sha=COALESCE(?, head_sha), "
                "output_path=COALESCE(?, output_path), last_error=? WHERE id=?",
                (
                    state, ts, not_before, head_sha or None, output_path,
                    (detail or {}).get("error") if isinstance(detail, dict) else None, task_id,
                ),
            )
            self.conn.execute("DELETE FROM leases WHERE token=?", (token,))
            self._close_run_record(token, ts, detail)
            self._event(
                task_id, lease["candidate_id"], f"finished:{status}",
                {"token": token[:8], **({"detail": detail} if detail else {})}, ts,
            )
            finished = self.get_task(task_id)
            assert finished is not None
            return finished

    def request_changes(
        self, task_id: int, token: str, note: str, now: float | None = None
    ) -> dict[str, Any]:
        return self.finish(task_id, token, "review_pending", detail={"note": note}, now=now)

    def requeue(
        self, token: str, reason: str, backoff: float = 300.0, now: float | None = None
    ) -> dict[str, Any]:
        ts = self.now() if now is None else now
        with self._tx():
            lease = self._require_valid_lease(token, ts)
            self.conn.execute(
                "UPDATE tasks SET state=?, not_before=?, updated_at=? WHERE id=?",
                (CLAIMABLE, ts + float(backoff), ts, lease["task_id"]),
            )
            self.conn.execute("DELETE FROM leases WHERE token=?", (token,))
            self._event(lease["task_id"], lease["candidate_id"], "requeued", {"reason": reason}, ts)
            task = self.get_task(lease["task_id"])
            assert task is not None
            return task

    def abandon(self, token: str, reason: str, now: float | None = None) -> dict[str, Any]:
        ts = self.now() if now is None else now
        with self._tx():
            lease = self._lease(token)
            if not lease:
                raise StaleLease("unknown token")
            artifact = lease["artifact_path"]
            state = "inspect_required" if has_nonempty_artifact(artifact) else CLAIMABLE
            self.conn.execute(
                "UPDATE tasks SET state=?, not_before=?, updated_at=? WHERE id=?",
                (state, ts if state == CLAIMABLE else 0.0, ts, lease["task_id"]),
            )
            self.conn.execute("DELETE FROM leases WHERE token=?", (token,))
            self._event(
                lease["task_id"], lease["candidate_id"], "abandoned",
                {"reason": reason, "artifact": artifact}, ts,
            )
            task = self.get_task(lease["task_id"])
            assert task is not None
            return task

    def recover(
        self, ttl: float = 120.0, quarantine: float = 60.0, now: float | None = None
    ) -> list[dict[str, Any]]:
        ts = self.now() if now is None else now
        actions: list[dict[str, Any]] = []
        with self._tx():
            leases = self.conn.execute("SELECT * FROM leases").fetchall()
            for lease in leases:
                status = process_status(lease["owner_pid"], lease["owner_creation"])
                has_artifact = has_nonempty_artifact(lease["artifact_path"])
                if status == "alive":
                    if lease["expires"] <= ts:
                        self.conn.execute(
                            "UPDATE leases SET expires=? WHERE token=?", (ts + float(ttl), lease["token"])
                        )
                        self._event(
                            lease["task_id"], lease["candidate_id"], "lease_overdue_alive",
                            {"token": lease["token"][:8]}, ts,
                        )
                        actions.append({"token": lease["token"], "action": "kept_alive"})
                    continue
                child = (
                    process_status(lease["run_pid"], lease["run_creation"]) if lease["run_pid"] else "dead"
                )
                if status == "dead" and child in ("alive", "unknown"):
                    self.conn.execute(
                        "UPDATE leases SET expires=? WHERE token=?", (ts + float(ttl), lease["token"])
                    )
                    self._event(
                        lease["task_id"], lease["candidate_id"], "orphan_child_hold",
                        {"token": lease["token"][:8], "run_pid": lease["run_pid"], "child": child}, ts,
                    )
                    actions.append({"token": lease["token"], "action": "hold_orphan_child"})
                    continue
                if status == "dead":
                    state = "inspect_required" if has_artifact else CLAIMABLE
                    self.conn.execute(
                        "UPDATE tasks SET state=?, updated_at=?, not_before=? WHERE id=?",
                        (state, ts, ts if state == CLAIMABLE else 0.0, lease["task_id"]),
                    )
                    self.conn.execute("DELETE FROM leases WHERE token=?", (lease["token"],))
                    self.conn.execute(
                        "UPDATE runs SET ended_at=?, detail=? WHERE token=?",
                        (ts, json.dumps({"recovered": status, "artifact": bool(has_artifact)}), lease["token"]),
                    )
                    self._event(
                        lease["task_id"], lease["candidate_id"],
                        "recovered_artifact" if has_artifact else "recovered",
                        {"token": lease["token"][:8], "pid": lease["owner_pid"]}, ts,
                    )
                    actions.append(
                        {"token": lease["token"], "action": "inspect_artifact" if has_artifact else "requeued"}
                    )
                    continue
                if lease["expires"] <= ts:
                    self.conn.execute(
                        "UPDATE leases SET expires=? WHERE token=?",
                        (ts + float(quarantine), lease["token"]),
                    )
                    self._event(
                        lease["task_id"], lease["candidate_id"], "lease_unknown_recheck",
                        {"token": lease["token"][:8], "pid": lease["owner_pid"]}, ts,
                    )
                    actions.append({"token": lease["token"], "action": "recheck_unknown"})
        return actions


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or advance the pipeline SQLite queue.")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--events", type=int, default=0)
    parser.add_argument("--advance", type=Path, help="Refreshed envelope JSON for explicit continuation")
    parser.add_argument("--expected-state", help="Compare-and-set state for --advance")
    parser.add_argument("--reason", help="New evidence or review feedback that warrants continuation")
    args = parser.parse_args(argv)
    if args.advance and (not args.expected_state or not args.reason):
        parser.error("--advance requires --expected-state and --reason")
    db_path = Path(args.db) if args.db else WorkspacePaths.from_workspace(args.workspace).queue_db
    with PipelineQueue(db_path) as queue:
        if args.advance:
            try:
                task = queue.advance(
                    json.loads(args.advance.read_text(encoding="utf-8")), args.expected_state, args.reason
                )
            except (QueueError, ValueError, OSError) as exc:
                parser.error(str(exc))
            print(json.dumps(task, ensure_ascii=False, indent=2))
            return 0
        if args.events:
            for ev in queue.events(args.events):
                print(ev["id"], ev["event"], ev["candidate_id"], ev["detail"] or "")
            return 0
        for task in queue.list_tasks():
            print(
                f"{task['id']:>4} {task['state']:<16} {task['kind']:<10} "
                f"{task['repo']:<28} {task['candidate_id']} {task['scope']}"
            )
        return 0


if __name__ == "__main__":
    sys.exit(cli())
