# Upstream Policy Routing & Claim Collision Detection

`oss_audit.policy` ensures every candidate respects upstream maintainer governance and active community work before implementation or publication planning. Neither `router.py`, `claimcheck.py`, `claim_collision.py`, nor `submit.py` performs any GitHub write operations.

---

## 1. Pinned Policy Snapshots (`policies/repos.json`)

Upstream `CONTRIBUTING.md`, PR templates, and AI disclosure policies are recorded in `policies/repos.json` with commit SHAs, Git blob hashes, timestamps, and explicit expiration windows (`max_policy_age_hours`, default 168h).

| Canonical Repository | Verified Upstream Policy Summary |
|---|---|
| `livekit/agents` | Scoped bugfixes allow `direct_pr` (CLA required); features require prior issue & design discussion (`issue_then_wait`). |
| `PrefectHQ/fastmcp` | External PRs must link an issue; author must be assigned or issue labeled `prs welcome`; linked PRs may wait at the gate before assignment (`issue_first`). |
| `agno-agi/agno` | Issue required first; implementation and PRs are blocked until a maintainer applies the `ready` label (`issue_then_wait`). |
| `agentscope-ai/agentscope` | Issue-first workflow with explicit claim comment (`/assign`) required before PR submission (`issue_first`). |
| `deepset-ai/haystack` | Issue required with `good first issue` or `Contributions wanted!`; max **1 open PR** per external contributor; CLA + AI disclosure (`issue_first` / capacity `hold`). |
| `microsoft/agent-framework` | Non-trivial changes require `ready-for-implementation` label or provenance-bearing maintainer approval (`issue_then_wait`). |
| `bytedance/deer-flow` | Verified bugfixes with RED/GREEN evidence allow `direct_pr`; features require prior scope alignment; AI assistance disclosure required. |

---

## 2. Deterministic Routing (`oss_audit.policy.route`)

```bash
python -m oss_audit policy --check-samples
```

Evaluation precedence (fail-closed):

1. Missing or malformed `candidate` / `context` fields -> `hold`
2. Unknown repository, `source_status != "ok"`, `paused`, or `closed` -> `hold`
3. Expired `policy.checked_at` or `context.checked_at` -> `hold`
4. `defect_proven != True` -> `hold`
5. `claim` is `TAKEN` or `UNKNOWN`, assigned to another contributor, or `open_pr_count > 0` -> `hold` (`stage: "claim_collision"`)
6. `issue_only` policy or `report_only == True` -> `issue_only`
7. Non-empty `design_uncertainties` -> `hold` (`stage: "design"`)
8. Contributor open PR count reaches `max_open_prs_per_contributor` -> `hold` (`stage: "capacity"`)
9. Direct bugfix permitted -> `direct_pr` (`pr_allowed: True` only after `fix_verified == True`)
10. Issue-gated workflow -> `issue_then_wait` or `issue_first` depending on issue presence, `ready` labels, maintainer approval provenance, and assignment rules

---

## 3. Claim & Collision Detection (`claimcheck` & `collision`)

### Issue / PR Claim Check

```bash
python -m oss_audit claimcheck bytedance/deer-flow#7598 --actor sample-user
```

Returns `FREE`, `TAKEN`, or `UNKNOWN`:

- Distinguishes PR numbers (`pull_request` field present) from issue numbers so an open PR is never mistaken for an unclaimed issue.
- Paginates comments and timeline events completely (`--paginate --slurp`). Any HTTP error, rate limit, or incomplete search page returns `UNKNOWN` (never `FREE`).
- Detects assignees, cross-referenced PRs, closing-keyword PRs, claim phrases (`"I'll take this"`, `"working on a PR"`), and inline branch/compare references while ignoring `"I'll take a look"` and bot comments.

### Code-Audit Duplicate Collision Check

```bash
python -m oss_audit collision deepset-ai/haystack \
  --query "filter_documents_async" \
  --query "MockedDocumentStore"
```

When a defect is found via static/runtime code auditing without a pre-existing issue number, `search_claim_collision` requires at least two distinct symptom/root-cause phrases and returns `UNKNOWN` whenever matching open or historical issues/PRs exist until reviewed.
