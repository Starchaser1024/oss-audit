"""Low-level SQLite WAL store, immediate transactions, and PID+creation-time lease fencing."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

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
CLAIMABLE = "pending"

SCHEMA_SQL = """
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
    """Check whether ``pid`` with optional creation timestamp is ``alive``, ``dead``, or ``unknown``."""
    status, current = _creation_identity(pid)
    if status != "alive":
        return status
    if creation not in (None, "") and str(creation) != str(current):
        return "dead"
    return "alive"


def my_identity() -> tuple[int, str | None]:
    """Return ``(pid, creation_ticks)`` for the current OS process."""
    pid = os.getpid()
    return pid, _creation_identity(pid)[1]


def check_payload(value: Any, path: str = "payload") -> None:
    """Reject shell or command-execution keys inside serialized task payloads."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in FORBIDDEN_PAYLOAD_KEYS:
                raise QueueError(f"forbidden command-like key {path}.{key}")
            check_payload(item, f"{path}.{key}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            check_payload(item, f"{path}[{idx}]")


def task_row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Deserialize a ``tasks`` SQLite row and decode its JSON ``payload`` column."""
    if row is None:
        return None
    data = dict(row)
    try:
        data["payload"] = json.loads(data.get("payload") or "{}")
    except json.JSONDecodeError:
        pass
    return data


def has_nonempty_artifact(artifact_path: str | Path | None) -> bool:
    """Return True when ``artifact_path`` exists on disk with positive byte size."""
    if not artifact_path:
        return False
    path = Path(artifact_path)
    return path.exists() and path.stat().st_size > 0


def day_start(ts: float) -> float:
    """Return local midnight epoch timestamp enclosing ``ts``."""
    tm = time.localtime(ts)
    return time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, -1))


def open_wal_connection(db_path: Path) -> sqlite3.Connection:
    """Open an autocommit SQLite connection configured with WAL journaling and busy timeout."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


class ImmediateTransaction:
    """Thread-serialized ``BEGIN IMMEDIATE`` SQLite write transaction."""

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


class SQLiteLeaseStore:
    """Low-level SQLite WAL connection, lease fencing primitives, and run/event persistence."""

    def __init__(self, db_path: str | Path, now: Any = time.time):
        self.db_path = Path(db_path)
        self.now = now
        self._lock = threading.RLock()
        self.conn = open_wal_connection(self.db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self) -> SQLiteLeaseStore:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _tx(self) -> ImmediateTransaction:
        return ImmediateTransaction(self.conn, self._lock)

    @staticmethod
    def _task_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return task_row_to_dict(row)

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

    def _lease(self, token: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM leases WHERE token=?", (token,)).fetchone()

    def _require_valid_lease(
        self,
        token: str,
        ts: float,
        task_id: int | None = None,
        missing_msg: str = "unknown token",
    ) -> sqlite3.Row:
        lease = self._lease(token)
        if not lease:
            raise StaleLease(missing_msg)
        if task_id is not None and int(lease["task_id"]) != int(task_id):
            raise StaleLease("token belongs to another task")
        if lease["expires"] <= ts:
            raise StaleLease("lease expired")
        return lease

    def _within_lease_capacity(
        self,
        purpose: str,
        max_active: int | None,
        read_capacity: int | None,
        heavy_capacity: int | None,
        review_capacity: int | None,
        repo_limit: int | None,
    ) -> bool:
        if max_active:
            active = self.conn.execute("SELECT COUNT(*) n FROM leases").fetchone()["n"]
            if active >= int(max_active):
                return False
        cap = {"read": read_capacity, "heavy": heavy_capacity, "review": review_capacity}.get(purpose)
        if cap:
            used = self.conn.execute(
                "SELECT COUNT(*) n FROM leases WHERE purpose=?", (purpose,)
            ).fetchone()["n"]
            if used >= int(cap):
                return False
        if purpose == "heavy" and repo_limit:
            distinct = self.conn.execute(
                "SELECT COUNT(DISTINCT repo) n FROM leases WHERE purpose='heavy'"
            ).fetchone()["n"]
            if distinct >= int(repo_limit):
                return False
        return True

    def _used_daily_research_ids(self, ts: float) -> set[int]:
        return {
            r["task_id"]
            for r in self.conn.execute(
                "SELECT DISTINCT e.task_id FROM events e "
                "WHERE e.event='claimed' AND e.at>=? "
                "AND (json_extract(e.detail, '$.kind') IN ('audit','investigate') "
                "OR json_extract(e.detail, '$.kind') IS NULL)",
                (day_start(ts),),
            ).fetchall()
        }

    def _try_insert_lease(
        self,
        *,
        token: str,
        row: sqlite3.Row,
        purpose: str,
        role: str,
        owner_pid: int | None,
        owner_creation: str | None,
        coordinator_pid: int | None,
        session_id: str | None,
        artifact_path: str | None,
        ts: float,
        ttl: float,
    ) -> bool:
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
            return True
        except sqlite3.IntegrityError:
            return False

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
            lease = self._lease(token)
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

    def heartbeat(self, token: str, ttl: float = 120.0, now: float | None = None) -> dict[str, Any]:
        ts = self.now() if now is None else now
        with self._tx():
            lease = self._require_valid_lease(token, ts)
            expires = ts + float(ttl)
            self.conn.execute("UPDATE leases SET heartbeat=?, expires=? WHERE token=?", (ts, expires, token))
            return {"task_id": lease["task_id"], "expires": expires}

    def _close_run_record(self, token: str, ts: float, detail: dict[str, Any] | None) -> None:
        self.conn.execute(
            "UPDATE runs SET ended_at=?, exit_code=?, detail=? WHERE token=?",
            (
                ts,
                (detail or {}).get("exit_code") if isinstance(detail, dict) else None,
                json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                token,
            ),
        )


def verify_prepared_evidence(
    task_row: sqlite3.Row,
    output_path: str | None,
    head_sha: str | None,
) -> str:
    """Verify reviewer-approved execution bundle integrity before transitioning to ``prepared``."""
    if not output_path or not Path(output_path).is_file():
        raise InvalidResult("prepared requires an existing evidence artifact")
    try:
        from oss_audit.evidence import runner as evidence_runner

        bundle = json.loads(Path(output_path).read_text(encoding="utf-8"))
        card = json.loads(Path(bundle["contract"]).read_text(encoding="utf-8"))
        if card.get("candidate_id") != task_row["candidate_id"]:
            raise InvalidResult("prepared evidence candidate_id mismatch")
        if task_row["base_sha"] and card.get("base_sha") != task_row["base_sha"]:
            raise InvalidResult("prepared evidence base_sha mismatch")
        green = json.loads(Path(bundle["receipts"]["green"]).read_text(encoding="utf-8"))
        actual_head = green["after"]["head"]
        if head_sha and head_sha != actual_head:
            raise InvalidResult("prepared head_sha mismatch")
        validation = evidence_runner.validate_bundle(output_path)
        if not validation.get("ok"):
            raise InvalidResult("prepared evidence rejected: " + "; ".join(validation.get("errors", [])[:4]))
        return str(actual_head)
    except InvalidResult:
        raise
    except Exception as exc:
        raise InvalidResult(f"prepared evidence unreadable: {exc}") from exc
