"""Bounded local scheduler: claim queue tasks, execute isolated AST audits, and gate heavy work."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from oss_audit.analyzers import contract_audit
from oss_audit.core.common import git
from oss_audit.core.config import load_pipeline_config
from oss_audit.core.paths import REPO_ROOT, WorkspacePaths
from oss_audit.core.resource_limits import admission
from oss_audit.policy import router as policy_router
from oss_audit.scheduling.queue import (
    PRODUCTION_DB,
    InvalidResult,
    PipelineQueue,
    QueueError,
    StaleLease,
    _creation_identity,
    my_identity,
)

DEFAULT_AGENT_PROVIDER = "magpie"
DEFAULT_AGENT_MODEL = "opencode-go/deepseek-v4.1-flash"


class SchedulerError(RuntimeError):
    pass


class Cancelled(SchedulerError):
    """The coordinator deadline or lease loss stopped this task child."""


def load_config(path: str | Path | None = None, workspace: str | Path | None = None) -> dict[str, Any]:
    cfg = load_pipeline_config(path, workspace)
    cfg.setdefault("pi", {})
    return cfg


def _resolve_agent_binary() -> Path:
    override = os.environ.get("OSS_AUDIT_AGENT_BIN", "").strip()
    if override:
        return Path(override)
    found = shutil.which("pi") or shutil.which("pi.cmd")
    if found:
        return Path(found)
    return Path("pi.cmd" if os.name == "nt" else "pi")


def _resolve_session_root(workspace: str | Path | None = None) -> Path:
    override = os.environ.get("OSS_AUDIT_SESSION_ROOT", "").strip()
    if override:
        return Path(override)
    return WorkspacePaths.from_workspace(workspace).state_dir / "agent-sessions"


def resolve_db(
    db_path: str | Path | None,
    dry_run: bool,
    workspace: str | Path | None = None,
) -> Path:
    paths = WorkspacePaths.from_workspace(workspace)
    prod_db = paths.queue_db.resolve() if workspace else PRODUCTION_DB.resolve()
    if db_path:
        db = Path(db_path)
    elif dry_run:
        db = paths.scratch_dir / "pipeline-dryrun" / f"tick-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.db"
    else:
        db = prod_db
    db = db.resolve()
    if dry_run and (db == prod_db or prod_db.parent in db.parents):
        raise SchedulerError("dry-run must use an isolated DB, not the production queue")
    return db


def resolve_artifacts(
    artifact_dir: str | Path | None,
    dry_run: bool,
    db_path: str | Path | None = None,
    workspace: str | Path | None = None,
) -> Path:
    if artifact_dir:
        return Path(artifact_dir).resolve()
    paths = WorkspacePaths.from_workspace(workspace)
    prod_db = paths.queue_db.resolve() if workspace else PRODUCTION_DB.resolve()
    if db_path:
        resolved_db = Path(db_path).resolve()
        if resolved_db != prod_db and prod_db.parent not in resolved_db.parents:
            return (
                resolved_db.parent
                / ("artifacts-dryrun" if dry_run else "artifacts")
                / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
            ).resolve()
    base = paths.scratch_dir / ("pipeline-dryrun" if dry_run else "pipeline-runs")
    return (base / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}").resolve()


def pq_identity(pid: int) -> tuple[str, str | None]:
    return _creation_identity(pid)


def _remaining(ctx: dict[str, Any], default: float) -> float:
    remaining = default
    deadline = ctx.get("deadline")
    if deadline is not None:
        remaining = min(remaining, deadline - time.monotonic())
    return max(1.0, remaining)


def _terminate_tree(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except OSError:
            proc.kill()


def _wait_child(proc: subprocess.Popen[Any], timeout: float, cancel: threading.Event | None) -> None:
    end = time.monotonic() + timeout
    while True:
        if cancel is not None and cancel.is_set():
            _terminate_tree(proc)
            proc.wait(timeout=10)
            raise Cancelled("lease lost/deadline: task child stopped")
        if proc.poll() is not None:
            return
        if time.monotonic() >= end:
            _terminate_tree(proc)
            proc.wait(timeout=10)
            raise SchedulerError(f"task exceeded budget {timeout:.0f}s")
        time.sleep(0.2)


def run_audit(task: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Run ``oss_audit.analyzers.contract_audit`` against a Git-validated checkout + scope."""
    checkout = Path(task["checkout"]).resolve()
    if not checkout.is_dir():
        raise SchedulerError(f"checkout missing: {checkout}")
    base = (task.get("base_sha") or "").strip()
    if base:
        proc_head = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc_head.returncode != 0:
            raise SchedulerError(f"cannot verify checkout HEAD: {proc_head.stderr.strip()[:200]}")
        if proc_head.stdout.strip() != base:
            raise SchedulerError(f"checkout HEAD {proc_head.stdout.strip()[:12]} != base_sha {base[:12]}")
    scope = (task.get("scope") or ".").strip() or "."
    scope_path = (checkout / scope).resolve()
    if not scope_path.is_relative_to(checkout) or not scope_path.is_dir():
        raise SchedulerError(f"scope does not resolve beneath checkout: {scope}")
    token = task["lease_token"][:8]
    out = Path(ctx["artifact_dir"]) / f"task-{task['id']}-{token}"
    out.mkdir(parents=True, exist_ok=True)
    audit_out = out / "audit-out"
    audit_out.mkdir(exist_ok=True)
    logs = out / "audit.log"
    started = time.time()
    audit_module_file = Path(contract_audit.__file__).resolve()
    command = [
        sys.executable,
        "-m",
        "oss_audit",
        "audit",
        "--repo-path",
        str(scope_path),
        "--output-dir",
        str(audit_out),
        "--max",
        str(int(ctx["max_groups"])),
    ]
    marker = out / "attempt.json"
    tmp_marker = out / f"attempt.{token}.tmp"
    tmp_marker.write_text(
        json.dumps(
            {
                "candidate_id": task["candidate_id"],
                "repo": task["repo"],
                "repo_root": str(checkout),
                "scope_path": str(scope_path),
                "checkout": str(checkout),
                "command": command,
                "session_id": task.get("session_id"),
                "planned_output_dir": str(audit_out),
                "token": task["lease_token"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp_marker, marker)
    ctx["queue"].record_run_identity(
        task["lease_token"], session_id=task.get("session_id"), artifact_path=str(marker)
    )
    timeout = _remaining(ctx, float(ctx["audit_timeout"]))
    env = os.environ.copy()
    existing_py = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + existing_py if existing_py else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.Popen(
        command,
        cwd=str(out),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    ctx["children"][task["lease_token"]] = proc
    ctx["queue"].record_run_identity(
        task["lease_token"],
        run_pid=proc.pid,
        run_creation=pq_identity(proc.pid)[1],
        artifact_path=str(marker),
    )
    try:
        _wait_child(proc, timeout, ctx.get("cancel"))
        stdout, stderr = proc.communicate()
    finally:
        ctx["children"].pop(task["lease_token"], None)
    logs.write_text(
        f"$ {' '.join(command)}\nexit={proc.returncode}\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}",
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SchedulerError(f"contract audit failed rc={proc.returncode}: {stderr.strip()[:400]}")
    outputs = sorted(audit_out.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not outputs:
        raise SchedulerError("contract audit produced no JSON artifact")
    artifact = outputs[-1]
    try:
        groups = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchedulerError(f"unreadable audit artifact: {exc}") from exc
    if not isinstance(groups, list):
        raise SchedulerError("audit artifact is not a list")
    status = "investigated" if groups else "no_finding"
    return {
        "status": status,
        "artifact": str(artifact),
        "detail": {
            "groups": len(groups),
            "repo": task["repo"],
            "repo_root": str(checkout),
            "audit_tool_sha256": hashlib.sha256(audit_module_file.read_bytes()).hexdigest(),
            "interpretation": "Navigation leads only; zero leads does not establish absence of bugs",
            "scope_path": str(scope_path),
            "checkout": str(scope_path),
            "log": str(logs),
            "duration_seconds": round(time.time() - started, 3),
        },
    }


def pi_session_id(task: dict[str, Any]) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"oss-audit-pipeline:{task['stable_key']}"))


def verify_pi(ctx: dict[str, Any]) -> dict[str, Any]:
    """Verify the configured external agent runner and model catalog per batch."""
    cached = ctx.get("pi_verification")
    if cached is not None:
        return cached
    agent_bin = _resolve_agent_binary()
    result: dict[str, Any] = {"ok": False, "reasons": []}
    if not agent_bin.is_file():
        result["reasons"].append(f"missing:{agent_bin}")
        ctx["pi_verification"] = result
        return result
    try:
        version = subprocess.run(
            [str(agent_bin), "--version"], capture_output=True, text=True, timeout=60, check=False
        )
        models = subprocess.run(
            [str(agent_bin), "--list-models", "deepseek"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["reasons"].append(f"pi_verify_failed:{exc}")
        ctx["pi_verification"] = result
        return result
    result["version"] = (version.stdout or "").strip()[:200]
    listing = (models.stdout or "") + (models.stderr or "")
    result["model_listed"] = DEFAULT_AGENT_MODEL in listing
    if version.returncode != 0:
        result["reasons"].append("pi_version_failed")
    if models.returncode != 0 or not result["model_listed"]:
        result["reasons"].append("allowed_model_not_listed")
    result["ok"] = not result["reasons"]
    ctx["pi_verification"] = result
    return result


def heavy_policy_gate(task: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Upstream policy + worktree authorization gate for heavy implementation."""
    workspace = ctx.get("workspace")
    worktree_root = WorkspacePaths.from_workspace(workspace).worktrees_dir.resolve()
    payload = task.get("payload") or {}
    candidate = payload.get("contract")
    context = payload.get("context")
    reasons: list[str] = []
    if not isinstance(candidate, dict) or not isinstance(context, dict):
        reasons.append("missing_contract_or_claim_context")
    worktree = payload.get("worktree")
    if not worktree:
        reasons.append("missing_authorized_worktree")
    else:
        path = Path(worktree).resolve()
        if not path.is_dir() or not path.is_relative_to(worktree_root):
            reasons.append("worktree_not_under_worktrees")
        elif (
            subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            != 0
        ):
            reasons.append("worktree_not_git_root")
        else:
            if Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path:
                reasons.append("worktree_path_is_not_root")
            if task.get("base_sha") and git(path, "rev-parse", "HEAD") != task["base_sha"]:
                reasons.append("worktree_base_sha_mismatch")
            for allowed in payload.get("allowed_paths", [str(path)]):
                if not Path(allowed).resolve().is_relative_to(path):
                    reasons.append("allowed_edit_path_escapes_worktree")
    route_result = None
    if not reasons:
        try:
            route_result = policy_router.route(
                candidate,
                policy_router.load_policy(task["repo"], workspace=workspace),
                context,
            )
            if not route_result.get("implementation_allowed"):
                reasons.append("policy_hold:" + ",".join(route_result.get("reasons", [])[:4]))
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"policy_error:{exc}")
    return {"allowed": not reasons, "reasons": reasons, "route": route_result, "worktree": worktree}


def _observed_identity(stdout: str) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    observed: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        if isinstance(message, dict):
            provider = message.get("provider")
            model = message.get("model")
            if provider or model:
                observed.append({"provider": provider, "model": model})
    if not observed:
        return None, None, []
    last = observed[-1]
    return last.get("provider"), last.get("model"), observed


def _pi_prompt(task: dict[str, Any], ctx: dict[str, Any], worktree: Path) -> str:
    payload = task.get("payload") or {}
    allowed = payload.get("allowed_paths") or [str(worktree)]
    return (
        "You are one bounded pipeline worker. Use ONLY the explicitly invoked "
        f"{DEFAULT_AGENT_PROVIDER}/{DEFAULT_AGENT_MODEL} model. Do not delegate to other agents/models, "
        "do not install tools, and do not run another coding agent.\n"
        f"candidate_id: {task['candidate_id']}\nrepo: {task['repo']}\n"
        f"scope: {task['scope']}\nbase_sha: {task.get('base_sha') or 'unknown'}\n"
        f"authorized worktree (only writable checkout): {worktree}\n"
        f"main repository checkout is READ-ONLY: {task['checkout']}\n"
        f"artifact output directory: {ctx['task_artifact_dir']}\n"
        f"allowed edit paths: {json.dumps(allowed, ensure_ascii=False)}\n"
        f"budget: {int(ctx['task_minutes'])} minutes; stop at the deadline.\n"
        "Investigate the root cause with read-only evidence first. Never push, "
        "post, open issues/PRs, send notifications, or edit outside allowed paths. "
        "Never claim prepared; report investigated/no_finding/review_pending and "
        "cite artifact/evidence paths. If a product/design decision is needed, stop. "
        "Reply in at most 20 lines.\n"
        f"root_cause: {task['root_cause']}\n"
    )


def run_pi(task: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Run the pinned external worker model; identity mismatch holds for inspection."""
    queue: PipelineQueue = ctx["queue"]
    token = task["lease_token"]
    pi_cfg = ctx["config"].get("pi") or ctx["config"].get("agent") or {}
    provider = pi_cfg.get("provider", DEFAULT_AGENT_PROVIDER)
    model = pi_cfg.get("model", DEFAULT_AGENT_MODEL)
    if provider != DEFAULT_AGENT_PROVIDER or model != DEFAULT_AGENT_MODEL:
        raise SchedulerError("config attempted to override the approved worker provider/model")
    verification = verify_pi(ctx)
    if not verification.get("ok"):
        return {"unavailable": "pi verification failed: " + ",".join(verification.get("reasons", []))}
    gate = heavy_policy_gate(task, ctx)
    if not gate["allowed"]:
        return {
            "status": "waiting",
            "artifact": None,
            "detail": {"gate": "heavy_policy", "reasons": gate["reasons"], "route": gate["route"]},
        }
    worktree = Path(gate["worktree"]).resolve()
    checkout = Path(task["checkout"]).resolve()
    if worktree == checkout:
        raise SchedulerError("heavy edits may not write the main repository checkout")
    session_id = pi_session_id(task)
    session_dir = _resolve_session_root(ctx.get("workspace")) / session_id
    out = Path(ctx["artifact_dir"]) / f"task-{task['id']}-{token[:8]}"
    out.mkdir(parents=True, exist_ok=True)
    prompt_path = out / "prompt.md"
    prompt_path.write_text(_pi_prompt(task, ctx, worktree), encoding="utf-8")
    artifact = out / "pi-result.json"
    ctx["task_artifact_dir"] = str(out)
    queue.record_run_identity(token, session_id=session_id, artifact_path=str(artifact))
    session_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = session_dir / f"{token}-stdout.jsonl"
    stderr_path = session_dir / f"{token}-stderr.log"
    artifact.write_text(
        json.dumps(
            {"state": "spawn_intent", "session_id": session_id, "prompt": str(prompt_path), "token": token}
        ),
        encoding="utf-8",
    )
    agent_bin = _resolve_agent_binary()
    command = [
        str(agent_bin),
        "--provider",
        provider,
        "--model",
        model,
        "--print",
        "--session-id",
        session_id,
        "--session-dir",
        str(session_dir),
        "--mode",
        "json",
        f"@{prompt_path}",
    ]
    started = time.time()
    runner = ctx.get("pi_runner")
    if runner is not None:
        proc = runner(command, task, ctx)
        rc = getattr(proc, "returncode", 1)
        stdout = getattr(proc, "stdout", "") or ""
        stderr = getattr(proc, "stderr", "") or ""
        stdout_path.write_text(stdout, encoding="utf-8", errors="replace")
        stderr_path.write_text(stderr, encoding="utf-8", errors="replace")
    else:
        with (
            open(stdout_path, "w", encoding="utf-8", errors="replace") as out_f,
            open(stderr_path, "w", encoding="utf-8", errors="replace") as err_f,
        ):
            proc = subprocess.Popen(
                command, cwd=str(worktree), stdin=subprocess.DEVNULL, stdout=out_f, stderr=err_f, text=True
            )
            ctx["children"][token] = proc
            queue.record_run_identity(
                token,
                run_pid=proc.pid,
                run_creation=pq_identity(proc.pid)[1],
                artifact_path=str(artifact),
                session_id=session_id,
            )
            try:
                _wait_child(proc, _remaining(ctx, ctx["task_minutes"] * 60 + 30), ctx.get("cancel"))
            finally:
                ctx["children"].pop(token, None)
        rc = proc.returncode
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    observed_provider, observed_model, events = _observed_identity(stdout)
    record = {
        "provider": provider,
        "model": model,
        "observed_provider": observed_provider,
        "observed_model": observed_model,
        "observed_events": events[-5:],
        "pi_version": verification.get("version"),
        "session_id": session_id,
        "session_dir": str(session_dir),
        "command": command[:-1],
        "exit_code": rc,
        "started": started,
        "ended": time.time(),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }
    artifact.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    identity_ok = bool(events) and all(e["provider"] == provider and e["model"] == model for e in events)
    if not identity_ok:
        return {
            "status": "inspect_required",
            "artifact": str(artifact),
            "detail": {
                "reason": "pi_identity_mismatch_or_missing",
                "observed_provider": observed_provider,
                "observed_model": observed_model,
                "exit_code": rc,
            },
        }
    if rc != 0:
        raise SchedulerError(f"pi exited rc={rc}: {stderr.strip()[:400]}")
    return {
        "status": "investigated",
        "artifact": str(artifact),
        "detail": {
            "provider": observed_provider,
            "model": observed_model,
            "session_id": session_id,
            "exit_code": rc,
            "stdout": str(stdout_path),
        },
    }


def _finish_success(
    queue: PipelineQueue, task: dict[str, Any], result: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    status = result.get("status")
    if not status:
        raise SchedulerError("worker returned no status")
    return queue.finish(
        task["id"],
        task["lease_token"],
        status,
        output_path=result.get("artifact") or result.get("output_path"),
        head_sha=result.get("head_sha"),
        detail=result.get("detail"),
    )


def _run_one(task: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    queue: PipelineQueue = ctx["queue"]
    token = task["lease_token"]
    cancel = threading.Event()
    run_ctx = dict(ctx)
    run_ctx["cancel"] = cancel
    run_ctx["task_artifact_dir"] = str(Path(ctx["artifact_dir"]) / f"task-{task['id']}-{token[:8]}")
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(max(1.0, ctx["heartbeat_seconds"])):
            try:
                queue.heartbeat(token, ttl=ctx["lease_ttl"])
            except (StaleLease, QueueError):
                cancel.set()
                return

    heartbeat_thread = threading.Thread(target=_beat, daemon=True, name=f"heartbeat-{task['id']}")
    heartbeat_thread.start()
    try:
        worker = ctx.get("worker")
        if worker is not None:
            result = worker(task, run_ctx)
        elif task["kind"] == "audit":
            result = run_audit(task, run_ctx)
        else:
            result = run_pi(task, run_ctx)
        if not isinstance(result, dict):
            raise SchedulerError("worker result must be a dict")
        if result.get("unavailable"):
            updated = queue.requeue(
                token, result["unavailable"], backoff=float(ctx["config"].get("heavy_backoff_seconds", 600))
            )
            return {
                "task": task["id"],
                "outcome": "requeued",
                "state": updated["state"],
                "reason": result["unavailable"],
            }
        updated = _finish_success(queue, task, result, run_ctx)
        return {
            "task": task["id"],
            "outcome": "finished",
            "state": updated["state"],
            "artifact": updated.get("output_path"),
        }
    except Cancelled as exc:
        try:
            updated = queue.abandon(token, str(exc))
            return {"task": task["id"], "outcome": "abandoned", "state": updated["state"], "error": str(exc)}
        except StaleLease:
            return {"task": task["id"], "outcome": "lease_lost", "error": str(exc)}
    except StaleLease as exc:
        return {"task": task["id"], "outcome": "lease_lost", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        try:
            updated = queue.finish(
                task["id"],
                token,
                "failed",
                detail={"error": error, "traceback": traceback.format_exc()[-2000:]},
                retry_after=float(ctx["config"].get("retry_backoff_seconds", 60)),
            )
            return {"task": task["id"], "outcome": "failed", "state": updated["state"], "error": error}
        except (StaleLease, InvalidResult) as lease_exc:
            return {"task": task["id"], "outcome": "lease_lost", "error": f"{error} / {lease_exc}"}
    finally:
        stop.set()


def _print_tasks(queue: PipelineQueue) -> None:
    for task in queue.list_tasks():
        print(
            f"{task['id']:>4} {task['state']:<16} {task['kind']:<11} "
            f"{task['repo']:<28} {task['candidate_id']} {task['scope']}"
        )


def run_tick(
    *,
    db_path: str | Path | None = None,
    dry_run: bool = False,
    workers: int | None = None,
    repos: int | None = None,
    max_minutes: float | None = None,
    enqueue: Any = None,
    list_only: bool = False,
    execute_pi: bool = False,
    config: dict[str, Any] | None = None,
    config_path: str | Path | None = None,
    artifact_dir: str | Path | None = None,
    workspace: str | Path | None = None,
    worker: Any = None,
    pi_runner: Any = None,
    poll_interval: float = 0.2,
    grace_seconds: float = 5.0,
    clock: Any = time.monotonic,
    sleep: Any = time.sleep,
    logger: Any = print,
) -> dict[str, Any]:
    """Run one bounded scheduling pass and return a summary dictionary."""
    paths = WorkspacePaths.from_workspace(workspace)
    cfg = config or load_config(config_path, workspace)
    if workers is not None:
        cfg["read_workers"] = max(1, int(workers))
    if repos is not None:
        cfg["repos"] = max(1, int(repos))
    total_minutes = float(max_minutes if max_minutes is not None else cfg.get("max_task_minutes", 25))
    db = resolve_db(db_path, dry_run, workspace)
    art = resolve_artifacts(artifact_dir, dry_run, db_path=db_path, workspace=workspace)
    queue = PipelineQueue(db)
    lease_cfg = cfg["lease"]
    ttl = float(lease_cfg["ttl_seconds"])
    identity = my_identity()
    summary: dict[str, Any] = {
        "db": str(db),
        "dry_run": dry_run,
        "claimed": 0,
        "finished": 0,
        "failed": 0,
        "requeued": 0,
        "lease_lost": 0,
        "recovered": [],
        "results": [],
        "artifact_dir": str(art),
    }
    try:
        if list_only:
            _print_tasks(queue)
            summary["tasks"] = queue.list_tasks()
            return summary
        if enqueue is not None:
            envelopes = enqueue if isinstance(enqueue, list) else [enqueue]
            summary["enqueued"] = [queue.enqueue(env, cfg)["id"] for env in envelopes]
            for task_id in summary["enqueued"]:
                logger(f"enqueued task={task_id}")
        summary["recovered"] = queue.recover(
            ttl=ttl, quarantine=float(lease_cfg.get("quarantine_seconds", 60))
        )
        for action in summary["recovered"]:
            logger(f"recovery: {action['action']} token={action['token'][:8]}")
        executable_purposes = ["read", "heavy"] if (execute_pi and not dry_run) else ["read"]
        if execute_pi and dry_run:
            logger("dry-run: model execution disabled; only AST audits run")
        if "heavy" not in executable_purposes and queue.has_claimable(["investigate", "followup"]):
            logger("heavy tasks remain pending (model execution disabled; use --execute-pi)")
        ctx: dict[str, Any] = {
            "queue": queue,
            "config": cfg,
            "workspace": workspace,
            "artifact_dir": str(art),
            "dry_run": dry_run,
            "execute_pi": execute_pi and not dry_run,
            "worker": worker,
            "pi_runner": pi_runner,
            "audit_timeout": float(cfg["audit"]["timeout_seconds"]),
            "max_groups": int(cfg["audit"]["max_groups"]),
            "task_minutes": total_minutes,
            "lease_ttl": ttl,
            "heartbeat_seconds": float(lease_cfg.get("heartbeat_seconds", 30)),
            "children": {},
            "active_tasks": {},
        }
        deadline = clock() + total_minutes * 60
        ctx["deadline"] = deadline
        if execute_pi and not dry_run:
            verification = verify_pi(ctx)
            if not verification.get("ok"):
                logger(
                    "PI verification failed: "
                    + ",".join(verification.get("reasons", []))
                    + "; heavy tasks stay pending"
                )
        limits = {"read": int(cfg["read_workers"]), "heavy": int(cfg["heavy_workers"])}
        executors = {
            p: ThreadPoolExecutor(max_workers=limits[p], thread_name_prefix=f"tick-{p}")
            for p in executable_purposes
        }
        futures: dict[Any, str] = {}
        stop_claiming = False
        try:
            while clock() < deadline:
                if not stop_claiming:
                    summary["resources"] = admission(cfg, paths.root)
                    if not summary["resources"]["ok"]:
                        stop_claiming = True
                        summary["deferred"] = "Resource admission paused: " + ",".join(
                            summary["resources"]["reasons"]
                        )
                for purpose in [] if stop_claiming else executable_purposes:
                    running = sum(1 for value in futures.values() if value == purpose)
                    while running < limits[purpose] and clock() < deadline:
                        task = queue.claim(
                            purpose,
                            owner_pid=identity[0],
                            owner_creation=identity[1],
                            ttl=ttl,
                            repo_limit=int(cfg["repos"]),
                            max_active=int(cfg["max_active"]),
                            read_capacity=int(cfg["read_workers"]),
                            heavy_capacity=int(cfg["heavy_workers"]),
                            review_capacity=int(cfg["review_workers"]),
                            daily_research=int(cfg["daily_research"]),
                        )
                        if task is None:
                            break
                        summary["claimed"] += 1
                        logger(f"claimed task={task['id']} {task['kind']} {task['repo']} purpose={purpose}")
                        future = executors[purpose].submit(_run_one, task, ctx)
                        futures[future] = purpose
                        running += 1
                allowed_kinds = (
                    ["audit"] if "heavy" not in executable_purposes else ["audit", "investigate", "followup"]
                )
                if not futures and not queue.has_claimable(allowed_kinds):
                    break
                if not futures:
                    summary.setdefault(
                        "deferred", "No immediately claimable slot within capacity or daily budget"
                    )
                    break
                done, _ = wait(futures, timeout=poll_interval, return_when=FIRST_COMPLETED)
                for future in done:
                    futures.pop(future, None)
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
                        result = {"outcome": "failed", "error": repr(exc)}
                    summary["results"].append(result)
                    summary[result.get("outcome", "failed")] = (
                        summary.get(result.get("outcome", "failed"), 0) + 1
                    )
            if futures:
                done, _ = wait(futures, timeout=grace_seconds, return_when=FIRST_COMPLETED)
                for future in done:
                    futures.pop(future, None)
                    summary["results"].append(future.result())
                remaining = list(futures)
                for future in remaining:
                    futures.pop(future, None)
                    summary["lease_lost"] += 1
                logger("bounded stop: stopping task child trees")
                for child in list(ctx["children"].values()):
                    try:
                        _terminate_tree(child)
                    except Exception:  # noqa: BLE001
                        pass
                wait(remaining, timeout=2.0)
        finally:
            for executor in executors.values():
                executor.shutdown(wait=True, cancel_futures=False)
        summary["counts"] = queue.counts()
        return summary
    finally:
        queue.close()


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded pipeline tick: AST audits and gated worker execution.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Isolated DB/artifacts; never writes production queue"
    )
    parser.add_argument("--workers", type=int, help="Override read/audit worker count")
    parser.add_argument("--repos", type=int, help="Max concurrent heavy repos")
    parser.add_argument("--max-minutes", type=float, help="Hard wall-clock budget")
    parser.add_argument("--db", help="SQLite queue path")
    parser.add_argument("--enqueue", help="JSON file: envelope or array of envelopes")
    parser.add_argument("--list", action="store_true", help="List tasks and exit")
    parser.add_argument(
        "--execute-pi", action="store_true", help="Allow heavy tasks with the configured worker model"
    )
    parser.add_argument("--config", help="config/pipeline.json path")
    parser.add_argument("--artifact-dir", help="Artifact output directory")
    parser.add_argument("--workspace", type=Path, default=None, help="Workspace root (or OSS_AUDIT_HOME)")
    args = parser.parse_args(argv)
    enqueue = None
    if args.enqueue:
        enqueue = json.loads(Path(args.enqueue).read_text(encoding="utf-8"))
    try:
        summary = run_tick(
            db_path=args.db,
            dry_run=args.dry_run,
            workers=args.workers,
            repos=args.repos,
            max_minutes=args.max_minutes,
            enqueue=enqueue,
            list_only=args.list,
            execute_pi=args.execute_pi,
            config_path=args.config,
            artifact_dir=args.artifact_dir,
            workspace=args.workspace,
            logger=lambda *a, **k: print(*a, file=sys.stderr),
        )
    except (SchedulerError, QueueError) as exc:
        print(f"tick: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(cli())
