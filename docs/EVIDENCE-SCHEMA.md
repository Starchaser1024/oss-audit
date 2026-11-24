# Execution Evidence & Bundle Schema

`oss-audit` separates **hypothesis generation** from **execution proof**. Every repair bundle (`bundle.json`) binds a semantic contract card (`contract.json`), six deterministic subprocess execution receipts (`red`, `green`, `adjacent`, `lint`, `format`, `whitespace`), and an independent review attestation (`review.json`).

---

## 1. Contract Card (`contract.json`)

Required fields for all repair candidates:

- `candidate_id` — Stable identifier (`owner--repo--hash`)
- `repo` — Canonical `owner/repo`
- `base_sha` — Full 40-character upstream Git SHA where `RED` was reproduced
- `user_scenario` — Concrete user-visible workflow affected
- `expected_behavior` / `observed_behavior` / `root_cause`
- `preserved_behaviors` — Non-empty list of existing behaviors that must remain green on both `base_sha` and `head_sha`
- `entry_path` — Caller-to-callee chain proving production reachability (`reachability == "proven"`)
- `contract_sources` — List of `{url_or_path, revision, location}` citations (upstream docs, protocol definitions, maintainer comments, or existing tests)
- `design_uncertainties` — Must be `[]` for `prepared` PR bundles; non-empty uncertainties force `hold` or `issue_only`
- `counterexample_cases` — List of `{category, nodeid, basis}` mappings:
  - Standard bugfixes require at least: `original_bug`, `preservation`, `adjacent_boundary`
  - `defect_class == "parser"` requires all 7 categories: `original_bug`, `explicit_valid`, `ordinary_similar`, `unknown`, `adjacent_boundary`, `metamorphic`, `preservation`

---

## 2. Subprocess Execution Spec (`spec.json`)

All relative paths resolve inside `checkout`; the output directory must reside outside `checkout` so receipt artifacts never pollute the tested Git worktree:

```json
{
  "candidate_id": "owner--repo--stable-cause-scope",
  "phase": "red",
  "checkout": "<workspace>/worktrees/example-base",
  "revision": "68c9fce5c424b0e96986a0ca365db73a1eef8196",
  "cwd": ".",
  "python": "<target-venv-python>",
  "sources": ["src/example/selector.py"],
  "tests": ["tests/test_selector.py"],
  "locks": ["uv.lock"],
  "imports": {"example.selector": "src/example/selector.py"},
  "pythonpath": ["src"],
  "pytest_args": ["tests/test_selector.py"],
  "timeout_seconds": 300
}
```

### Phase Validation Rules

| Phase | Runner | Acceptance Criteria |
|---|---|---|
| `red` | `pytest_harness.py` | Unmodified production sources; `exit_code == 1`; `failed >= 1`; all failures are `assert` mismatches; `0` errors/skips/xfails; `preservation` nodes pass |
| `green` | `pytest_harness.py` | Identical test & lockfile SHA-256 as `red`; identical collected node set; `exit_code == 0`; `passed >= 1`; `0` failed/errors/skips/xfails |
| `adjacent` | `pytest_harness.py` | Same candidate & post-fix tree fingerprint as `green`; `exit_code == 0`; `0` failed/errors/skips/xfails |
| `lint` / `format` / `whitespace` | Explicit `check_argv` list (no shell interpolation) | Same candidate & post-fix tree fingerprint as `green`; `exit_code == 0` |

---

## 3. Bundle & Review Binding (`bundle.json` / `review.json`)

```json
{
  "contract": "<workspace>/evidence/example/contract.json",
  "receipts": {
    "red": "<workspace>/evidence/example/red/receipt.json",
    "green": "<workspace>/evidence/example/green/receipt.json",
    "adjacent": "<workspace>/evidence/example/adjacent/receipt.json",
    "lint": "<workspace>/evidence/example/lint/receipt.json",
    "format": "<workspace>/evidence/example/format/receipt.json",
    "whitespace": "<workspace>/evidence/example/whitespace/receipt.json"
  },
  "review": "<workspace>/evidence/example/review.json"
}
```

`review.json` binds `oss_audit.evidence.bundle_binding(bundle)` (SHA-256 digests of `contract.json` and every `receipt.json`). Modifying any source file, test file, log file, or contract field after review invalidates the bundle (`review_stale` or `checkout_changed_since_run`).

For `issue_only` reports (`validate_report`), only the `red` receipt and an `approved_report` review with `semantic_basis` and `causal_checks` are required — `oss-audit` never fabricates a speculative `GREEN` patch when upstream design trade-offs remain open.
