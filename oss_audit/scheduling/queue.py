"""SQLite task coordinator for the local contribution pipeline.

Standard library only. The queue is the single source of execution facts for
new work; the historical ``ledger.json`` is never written here.

Guarantees:
* stable candidate identity derived from ``repo + root_cause + scope``;
* atomic enqueue/claim (``BEGIN IMMEDIATE``) with duplicate suppression;
* one heavy (writer) lease per candidate and per repository, while read leases
  may overlap on the same repository;
* leases carry a random fencing token plus the owner PID and process creation
  identity, heartbeat, and ``expires_at``;
* every result update requires the matching, unexpired token, so a stale or
  resurrected worker can never write an accepted state;
* recovery only releases a lease when the owner process is confirmed dead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from oss_audit.core.paths import REPO_ROOT, WorkspacePaths

PRODUCTION_DB = REPO_ROOT / "queue" / "pipeline.db"

KINDS = {"audit": "read", "investigate": "heavy", "followup": "heavy"}
CLAIMABLE = "pending"
TERMINAL_STATES = {
    "done",
    "failed",
    "no_finding",
    "review_pending",
    "waiting",
    "inspect_required",
    "prepared",
    "rejected",
    "needs_model",
    "investigated",
}
READ_RESULTS = {"investigated", "no_finding", "review_pending", "failed", "inspect_required", "waiting"}
HEAVY_RESULTS = {"investigated", "no_finding", "review_pending", "failed", "inspect_required", "waiting"}
REVIEW_RESULTS = {"prepared", "rejected", "review_pending", "done", "failed"}
FORBIDDEN_PAYLOAD_KEYS = {
    "command",
    "commands",
    "shell",
    "cmd",
    "argv",
    "script",
    "executable",
    "powershell",
    "bash",
    "sh",
    "run_command",
    "exec",
}

PRIORITY = {"followup": 0, "investigate": 1, "audit": 2}


class QueueError(RuntimeError):
    """Base error for queue misuse."""


class StaleLease(QueueError):
    """The caller's fencing token is missing, expired, or already replaced."""


class InvalidResult(QueueError):
    """Result status is not allowed for this lease role/purpose."""


def stable_candidate_id(repo: str, root_cause: str, scope: str) -> str:
    """Stable root-cause identity; deliberately independent of dates or issue numbers."""
    raw = "\x1f".join(
        [(repo or "").strip().lower(), (root_cause or "").strip().lower(), (scope or "").strip().lower()]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _creation_identity(pid: Any) -> tuple[str, str | None]:
    """Return ``(status, creation)`` where status is ``alive``/``dead``/``unknown``."""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return "unknown", None
    if pid_int <= 0:
        return "dead", None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(0x1000, False, pid_int)
        if not handle:
            err = ctypes.get_last_error()
            return ("dead", None) if err == 87 else ("unknown", None)
        try:
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value != 259:
                return "dead", None
            creation = wintypes.FILETIME()
            exit_t = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_t),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                return "unknown", None
            if exit_t.dwHighDateTime or exit_t.dwLowDateTime:
                return "dead", None
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return "alive", str(ticks)
        finally:
            kernel32.CloseHandle(handle)
    proc = Path("/proc") / str(pid_int) / "stat"
    try:
        text = proc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        try:
            os.kill(pid_int, 0)
        except ProcessLookupError:
            return "dead", None
        except PermissionError:
            return "unknown", None
        return "unknown", None
    try:
        fields = text.rsplit(")", 1)[1].split()
        return "alive", fields[19]
    except (IndexError, ValueError):
        return "unknown", None


def process_status(pid: Any, creation: str | None = None) -> str:
    status, current = _creation_identity(pid)
    if status != "alive":
        return status
    if creation not in (None, "") and str(creation) != str(current):
        return "dead"
    return "alive"


def my_identity() -> tuple[int, str | None]:
    pid = os.getpid()
    return pid, _creation_identity(pid)[1]


def _check_payload(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in FORBIDDEN_PAYLOAD_KEYS:
                raise QueueError(f"forbidden command-like key {path}.{key}")
            _check_payload(item, f"{path}.{key}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _check_payload(item, f"{path}[{idx}]")


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
    _check_payload(payload)
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


class PipelineQueue:
    def __init__(self, db_path: str | Path, now: Any = time.time):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.now = now
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            str(self.db_path), timeout=30, isolation_level=None, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                candidate_id TEXT NOT NULL UNIQUE,
                stable_key TEXT NOT NULL,
                repo TEXT NOT NULL,
                scope TEXT NOT NULL,
                root_cause TEXT NOT NULL,
                kind TEXT NOT NULL,
                checkout TEXT NOT NULL,
                base_sha TEXT NOT NULL DEFAULT '',
                head_sha TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 2,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                not_before REAL NOT NULL DEFAULT 0,
                output_path TEXT,
                session_id TEXT,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state, not_before, priority);
            CREATE INDEX IF NOT EXISTS tasks_stable ON tasks(stable_key);
            CREATE TABLE IF NOT EXISTS leases (
                token TEXT PRIMARY KEY,
                task_id INTEGER NOT NULL UNIQUE,
                candidate_id TEXT NOT NULL,
                repo TEXT NOT NULL,
                purpose TEXT NOT NULL,
                role TEXT NOT NULL,
                owner_pid INTEGER,
                owner_creation TEXT,
                coordinator_pid INTEGER,
                run_pid INTEGER,
                run_creation TEXT,
                session_id TEXT,
                artifact_path TEXT,
                heartbeat REAL NOT NULL,
                expires REAL NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_heavy_candidate
                ON leases(candidate_id) WHERE purpose = 'heavy';
            CREATE UNIQUE INDEX IF NOT EXISTS one_heavy_repo
                ON leases(repo) WHERE purpose = 'heavy';
            CREATE INDEX IF NOT EXISTS leases_expires ON leases(expires);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                at REAL NOT NULL,
                task_id INTEGER,
                candidate_id TEXT,
                event TEXT NOT NULL,
                detail TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                token TEXT NOT NULL,
                session_id TEXT,
                artifact_path TEXT,
                run_pid INTEGER,
                run_creation TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                exit_code INTEGER,
                detail TEXT
            );
            """
        )

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self) -> PipelineQueue:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _tx(self) -> _Transaction:
        return _Transaction(self.conn, self._lock)

    def _event(
        self,
        task_id: int | None,
        candidate_id: str | None,
        event: str,
        detail: Any = None,
        at: float | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events(at, task_id, candidate_id, event, detail) VALUES (?,?,?,?,?)",
            (
                at if at is not None else self.now(),
                task_id,
                candidate_id,
                event,
                json.dumps(detail, ensure_ascii=False) if detail is not None else None,
            ),
        )

    @staticmethod
    def _task_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        try:
            data["payload"] = json.loads(data.get("payload") or "{}")
        except json.JSONDecodeError:
            pass
        return data

    def get_task(self, task_id: int | None = None, candidate_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            return self._get_task(task_id, candidate_id)

    def _get_task(self, task_id: int | None = None, candidate_id: str | None = None) -> dict[str, Any] | None:
        if task_id is not None:
            row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM tasks WHERE candidate_id=?", (candidate_id,)).fetchone()
        return self._task_dict(row)

    def list_tasks(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tasks ORDER BY state, priority, created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [d for r in rows if (d := self._task_dict(r)) is not None]

    def events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def counts(self, now: float | None = None) -> dict[str, int]:
        with self._lock:
            ts = self.now() if now is None else now
            rows = self.conn.execute("SELECT state, COUNT(*) n FROM tasks GROUP BY state").fetchall()
            out = {r["state"]: r["n"] for r in rows}
            out["active_leases"] = self.conn.execute(
                "SELECT COUNT(*) n FROM leases WHERE expires > ?", (ts,)
            ).fetchone()["n"]
            return out

    def has_claimable(self, kinds: list[str], now: float | None = None) -> bool:
        ts = self.now() if now is None else now
        if not kinds:
            return False
        marks = ",".join("?" * len(kinds))
        row = self.conn.execute(
            "SELECT 1 FROM tasks WHERE state=? AND not_before<=? AND kind IN (%s) "
            "AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.task_id=tasks.id) LIMIT 1" % marks,
            [CLAIMABLE, ts, *kinds],
        ).fetchone()
        return row is not None

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
                    "UPDATE tasks SET state=?, base_sha=?, updated_at=?, "
                    "not_before=0, head_sha='' WHERE id=?",
                    (CLAIMABLE, data["base_sha"] or row["base_sha"], ts, row["id"]),
                )
                self._event(
                    row["id"],
                    row["candidate_id"],
                    "woken_base_change" if base_changed else "woken_cooldown",
                    {"base_sha": data["base_sha"]},
                    ts,
                )
                task = self._task_dict(
                    self.conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
                )
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
                if (
                    cooldown_h
                    and task["state"] == "no_finding"
                    and ts - task["updated_at"] < cooldown_h * 3600
                ):
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
                "SELECT * FROM tasks WHERE stable_key=? AND state NOT IN ('rejected') "
                "ORDER BY id ASC LIMIT 1",
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
                    data["candidate_id"],
                    data["stable_key"],
                    data["repo"],
                    data["scope"],
                    data["root_cause"],
                    data["kind"],
                    data["checkout"],
                    data["base_sha"],
                    data["head_sha"],
                    json.dumps(data["payload"], ensure_ascii=False),
                    CLAIMABLE,
                    data["priority"],
                    ts,
                    ts,
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
        resumable = {
            "investigated",
            "waiting",
            "review_pending",
            "inspect_required",
            "needs_model",
            "failed",
            "rejected",
            "prepared",
            "done",
            "no_finding",
        }
        if expected_state not in resumable:
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
                    data["kind"],
                    data["checkout"],
                    data["base_sha"],
                    json.dumps(data["payload"], ensure_ascii=False),
                    data["priority"],
                    ts,
                    row["id"],
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
            if max_active:
                active = self.conn.execute("SELECT COUNT(*) n FROM leases").fetchone()["n"]
                if active >= int(max_active):
                    return None
            cap = {"read": read_capacity, "heavy": heavy_capacity, "review": review_capacity}.get(purpose)
            if cap:
                used = self.conn.execute(
                    "SELECT COUNT(*) n FROM leases WHERE purpose=?", (purpose,)
                ).fetchone()["n"]
                if used >= int(cap):
                    return None
            if purpose == "heavy" and repo_limit:
                distinct = self.conn.execute(
                    "SELECT COUNT(DISTINCT repo) n FROM leases WHERE purpose='heavy'"
                ).fetchone()["n"]
                if distinct >= int(repo_limit):
                    return None
            kinds = [k for k, v in KINDS.items() if v == purpose]
            if purpose == "review":
                kinds = list(KINDS)
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
            for row in rows:
                if daily_research and row["kind"] in ("audit", "investigate") and purpose != "review":
                    used_ids = {
                        r["task_id"]
                        for r in self.conn.execute(
                            "SELECT DISTINCT e.task_id FROM events e "
                            "WHERE e.event='claimed' AND e.at>=? "
                            "AND (json_extract(e.detail, '$.kind') IN ('audit','investigate') "
                            "OR json_extract(e.detail, '$.kind') IS NULL)",
                            (_day_start(ts),),
                        ).fetchall()
                    }
                    if row["id"] not in used_ids and len(used_ids) >= int(daily_research):
                        continue
                if purpose == "heavy":
                    if self.conn.execute(
                        "SELECT 1 FROM leases WHERE purpose='heavy' AND repo=?", (row["repo"],)
                    ).fetchone():
                        continue
                try:
                    self.conn.execute(
                        """INSERT INTO leases(token, task_id, candidate_id, repo, purpose,
                               role, owner_pid, owner_creation, coordinator_pid,
                               session_id, artifact_path, heartbeat, expires, created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            token,
                            row["id"],
                            row["candidate_id"],
                            row["repo"],
                            purpose,
                            role,
                            owner_pid,
                            str(owner_creation) if owner_creation else None,
                            coordinator_pid,
                            session_id,
                            artifact_path,
                            ts,
                            ts + float(ttl),
                            ts,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                self.conn.execute(
                    "UPDATE tasks SET state='running', updated_at=?, session_id=COALESCE(?, session_id) "
                    "WHERE id=?",
                    (ts, session_id, row["id"]),
                )
                self._event(
                    row["id"],
                    row["candidate_id"],
                    "claimed",
                    {"purpose": purpose, "kind": row["kind"], "token": token[:8], "pid": owner_pid},
                    ts,
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

    def record_run_identity(
        self,
        token: str,
        run_pid: int | None = None,
        run_creation: str | None = None,
        session_id: str | None = None,
        artifact_path: str | None = None,
        now: float | None = None,
    ) -> None:
        ts = self.now() if now is None else now
        with self._tx():
            lease = self.conn.execute("SELECT * FROM leases WHERE token=?", (token,)).fetchone()
            if not lease:
                raise StaleLease("unknown token")
            self.conn.execute(
                "UPDATE leases SET run_pid=?, run_creation=?, "
                "session_id=COALESCE(?, session_id), "
                "artifact_path=COALESCE(?, artifact_path) WHERE token=?",
                (run_pid, str(run_creation) if run_creation else None, session_id, artifact_path, token),
            )
            self.conn.execute(
                "INSERT INTO runs(task_id, token, session_id, artifact_path, run_pid, "
                "run_creation, started_at, detail) VALUES (?,?,?,?,?,?,?,?)",
                (
                    lease["task_id"],
                    token,
                    session_id,
                    artifact_path,
                    run_pid,
                    str(run_creation) if run_creation else None,
                    ts,
                    "{}",
                ),
            )

    def _lease(self, token: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM leases WHERE token=?", (token,)).fetchone()

    def heartbeat(self, token: str, ttl: float = 120.0, now: float | None = None) -> dict[str, Any]:
        ts = self.now() if now is None else now
        with self._tx():
            lease = self._lease(token)
            if not lease:
                raise StaleLease("unknown token")
            if lease["expires"] <= ts:
                raise StaleLease("lease expired")
            expires = ts + float(ttl)
            self.conn.execute("UPDATE leases SET heartbeat=?, expires=? WHERE token=?", (ts, expires, token))
            return {"task_id": lease["task_id"], "expires": expires}

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
            lease = self._lease(token)
            if not lease:
                raise StaleLease("unknown token (already released or replaced)")
            if int(lease["task_id"]) != int(task_id):
                raise StaleLease("token belongs to another task")
            if lease["expires"] <= ts:
                raise StaleLease("lease expired")
            allowed = {"read": READ_RESULTS, "heavy": HEAVY_RESULTS, "review": REVIEW_RESULTS}.get(
                lease["purpose"], set()
            )
            if status not in allowed:
                raise InvalidResult(f"status {status!r} not allowed for {lease['purpose']} lease")
            if status == "prepared":
                if lease["purpose"] != "review" or lease["role"] != "reviewer":
                    raise InvalidResult("prepared requires a reviewer lease")
                if not output_path or not Path(output_path).is_file():
                    raise InvalidResult("prepared requires an existing evidence artifact")
                try:
                    from oss_audit.evidence import runner as evidence_runner

                    bundle = json.loads(Path(output_path).read_text(encoding="utf-8"))
                    card = json.loads(Path(bundle["contract"]).read_text(encoding="utf-8"))
                    task_row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                    if card.get("candidate_id") != task_row["candidate_id"]:
                        raise InvalidResult("prepared evidence candidate_id mismatch")
                    if task_row["base_sha"] and card.get("base_sha") != task_row["base_sha"]:
                        raise InvalidResult("prepared evidence base_sha mismatch")
                    green = json.loads(Path(bundle["receipts"]["green"]).read_text(encoding="utf-8"))
                    actual_head = green["after"]["head"]
                    if head_sha and head_sha != actual_head:
                        raise InvalidResult("prepared head_sha mismatch")
                    head_sha = actual_head
                    validation = evidence_runner.validate_bundle(output_path)
                    if not validation.get("ok"):
                        raise InvalidResult(
                            "prepared evidence rejected: " + "; ".join(validation.get("errors", [])[:4])
                        )
                except InvalidResult:
                    raise
                except Exception as exc:
                    raise InvalidResult(f"prepared evidence unreadable: {exc}") from exc
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
                    state,
                    ts,
                    not_before,
                    head_sha or None,
                    output_path,
                    (detail or {}).get("error") if isinstance(detail, dict) else None,
                    task_id,
                ),
            )
            self.conn.execute("DELETE FROM leases WHERE token=?", (token,))
            self.conn.execute(
                "UPDATE runs SET ended_at=?, exit_code=?, detail=? WHERE token=?",
                (
                    ts,
                    (detail or {}).get("exit_code") if isinstance(detail, dict) else None,
                    json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                    token,
                ),
            )
            self._event(
                task_id,
                lease["candidate_id"],
                f"finished:{status}",
                {"token": token[:8], **({"detail": detail} if detail else {})},
                ts,
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
            lease = self._lease(token)
            if not lease:
                raise StaleLease("unknown token")
            if lease["expires"] <= ts:
                raise StaleLease("lease expired")
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
            inspect = bool(artifact and Path(artifact).exists() and Path(artifact).stat().st_size > 0)
            state = "inspect_required" if inspect else CLAIMABLE
            self.conn.execute(
                "UPDATE tasks SET state=?, not_before=?, updated_at=? WHERE id=?",
                (state, ts if state == CLAIMABLE else 0.0, ts, lease["task_id"]),
            )
            self.conn.execute("DELETE FROM leases WHERE token=?", (token,))
            self._event(
                lease["task_id"],
                lease["candidate_id"],
                "abandoned",
                {"reason": reason, "artifact": artifact},
                ts,
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
                artifact = lease["artifact_path"]
                has_artifact = bool(
                    artifact and Path(artifact).exists() and Path(artifact).stat().st_size > 0
                )
                if status == "alive":
                    if lease["expires"] <= ts:
                        self.conn.execute(
                            "UPDATE leases SET expires=? WHERE token=?", (ts + float(ttl), lease["token"])
                        )
                        self._event(
                            lease["task_id"],
                            lease["candidate_id"],
                            "lease_overdue_alive",
                            {"token": lease["token"][:8]},
                            ts,
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
                        lease["task_id"],
                        lease["candidate_id"],
                        "orphan_child_hold",
                        {"token": lease["token"][:8], "run_pid": lease["run_pid"], "child": child},
                        ts,
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
                        (
                            ts,
                            json.dumps({"recovered": status, "artifact": bool(has_artifact)}),
                            lease["token"],
                        ),
                    )
                    self._event(
                        lease["task_id"],
                        lease["candidate_id"],
                        "recovered_artifact" if has_artifact else "recovered",
                        {"token": lease["token"][:8], "pid": lease["owner_pid"]},
                        ts,
                    )
                    actions.append(
                        {
                            "token": lease["token"],
                            "action": "inspect_artifact" if has_artifact else "requeued",
                        }
                    )
                    continue
                if lease["expires"] <= ts:
                    self.conn.execute(
                        "UPDATE leases SET expires=? WHERE token=?",
                        (ts + float(quarantine), lease["token"]),
                    )
                    self._event(
                        lease["task_id"],
                        lease["candidate_id"],
                        "lease_unknown_recheck",
                        {"token": lease["token"][:8], "pid": lease["owner_pid"]},
                        ts,
                    )
                    actions.append({"token": lease["token"], "action": "recheck_unknown"})
        return actions


class _Transaction:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self.conn = conn
        self.lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self.lock.acquire()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except Exception:
            self.lock.release()
            raise
        return self.conn

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if exc_type:
                self.conn.execute("ROLLBACK")
            else:
                self.conn.execute("COMMIT")
        finally:
            self.lock.release()
        return False


def _day_start(ts: float) -> float:
    tm = time.localtime(ts)
    return time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, -1))


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
