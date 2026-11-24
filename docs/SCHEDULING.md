# Bounded Queue & Lease-Fenced Scheduling

`oss_audit.scheduling` coordinates repository audits, worker execution, crash recovery, and host resource admission using Python's standard library (`sqlite3`, `threading`, `subprocess`, `ctypes`).

---

## 1. SQLite Coordinator (`oss_audit.scheduling.queue`)

State is persisted in `<workspace>/queue/pipeline.db` across four tables:

| Table | Responsibility |
|---|---|
| `tasks` | Deduplicated candidates (`stable_key = sha256(repo + root_cause + scope)`), lifecycle state, retry backoff, session ID, artifact path |
| `leases` | Active atomic leases with random fencing `token`, `owner_pid` + OS process creation timestamp, `run_pid` + child creation timestamp, heartbeat, and TTL |
| `events` | Append-only audit log (`enqueued`, `claimed`, `finished:*`, `advanced`, `recovered*`) |
| `runs` | Spawn-intent and child-process execution records |

### Concurrency & Safety Invariants

- **Payload Injection Prevention**: Envelopes containing shell-like keys (`command`, `argv`, `shell`, `bash`, `powershell`, `exec`, etc.) are rejected at ingestion (`QueueError`).
- **Writer Mutual Exclusion**: Partial unique indexes enforce at most one `heavy` lease per candidate and per repository, while `read` (`audit`) leases may run concurrently.
- **Fenced Finalization**: Every `heartbeat`, `finish`, `requeue`, and `abandon` call validates the active `token` and `expires` timestamp inside a `BEGIN IMMEDIATE` transaction; stale workers raise `StaleLease`.
- **Reviewer-Only `prepared` Gate**: Workers cannot self-approve (`InvalidResult`). Only a `review` lease can transition a task to `prepared`, and `finish()` validates the SHA-256 evidence bundle (`validate_bundle`) before committing.

### Conservative Crash Recovery (`recover()`)

| Owner Process State | Child Process State | Recovery Action |
|---|---|---|
| `alive` | any | Never steal; extend overdue TTL (`kept_alive`) |
| `dead` (or PID reused) | `alive` / `unknown` | Hold lease (`hold_orphan_child`) until child exits |
| `dead` | `dead` (non-empty artifact exists) | Move task to `inspect_required` (preserve partial evidence) |
| `dead` | `dead` (no artifact) | Requeue task to `pending` |
| `unknown` | any | Extend by quarantine window (`recheck_unknown`); never steal blindly |

---

## 2. Running the Bounded Scheduler (`tick` & `queue`)

```bash
# Execute read-only AST audits in an isolated dry-run SQLite database
python -m oss_audit tick --dry-run --enqueue discovered.json --workers 4 --repos 2 --max-minutes 15

# Inspect tasks and append-only events
python -m oss_audit queue --db ./queue/pipeline.db --list
python -m oss_audit queue --db ./queue/pipeline.db --events 20

# Explicitly resume a waiting candidate after upstream label/review updates (CAS state check)
python -m oss_audit queue \
  --db ./queue/pipeline.db \
  --advance refreshed-envelope.json \
  --expected-state waiting \
  --reason "Maintainer added ready label; claimcheck verified FREE"
```

### Host Resource Admission (`oss_audit.core.resource_limits`)

Before claiming each task, `tick` probes available disk space and physical memory (`GlobalMemoryStatusEx` on Windows, `/proc/meminfo` on Linux, `sysconf` on macOS) against `config/pipeline.json` watermarks (default: `5 GiB` disk, `2 GiB` RAM). If either watermark fails or is unreadable, new task claims pause cleanly while in-flight tasks finish within their wall-clock budget.
