"""Read-only upstream claim and active-PR checker for one GitHub issue or pull request.

``check`` returns ``FREE``, ``TAKEN`` or ``UNKNOWN``. It distinguishes issues
from pull requests, reads comments/timeline/search with complete pagination,
and never treats an API error, a missing field, or an ambiguous reference as
FREE. The GitHub adapter is GET-only and can be injected for deterministic
unit tests.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Protocol

DEFAULT_ALIASES = {"modelscope/agentscope": "agentscope-ai/agentscope"}
STATUSES = ("FREE", "TAKEN", "UNKNOWN")

_CLAIM_PATTERNS = (
    r"i(?:'|\u2019)?ll\s+take",
    r"i\s+will\s+take",
    r"i\s+can\s+take",
    r"i(?:'|\u2019)?d\s+like\s+to\s+(?:take|work\s+on|fix)",
    r"i\s+would\s+like\s+to\s+(?:take|work\s+on|fix)",
    r"i(?:'|\u2019)?ll\s+work\s+on",
    r"i\s+will\s+work\s+on",
    r"i(?:'|\u2019)?m\s+working\s+on",
    r"i\s+am\s+working\s+on",
    r"working\s+on\s+(?:a\s+)?(?:fix|pr|pull\s+request)",
    r"assign\s+me",
    r"please\s+assign",
    r"can\s+i\s+be\s+assigned",
    r"can\s+i\s+take",
    r"claim(?:ing)?\s+(?:this|the\s+issue)",
    r"i\s+claim\s+this",
    r"opened\s+(?:a\s+)?(?:pr|pull\s+request)",
    r"submitted\s+(?:a\s+)?(?:pr|pull\s+request)",
    r"i\s+have\s+(?:a\s+)?(?:pr|pull\s+request)",
    r"i(?:'|\u2019)?ll\s+submit",
    r"i\s+will\s+submit",
    r"pr\s+is\s+up",
    r"fix\s+is\s+up",
)
_CLAIM_RE = re.compile("|".join(f"(?:{pattern})" for pattern in _CLAIM_PATTERNS), re.IGNORECASE)
_LOOK_ONLY_RE = re.compile(r"take\s+(?:a\s+)?(?:quick\s+)?look", re.IGNORECASE)
_NEGATION_RE = re.compile(r"\b(?:do\s+not|don't|dont|no\s+need|not\s+going\s+to)\b", re.IGNORECASE)
_ASSIGN_RE = re.compile(r"assign(?:ed|ing)?\s+(?:to\s+)?@([A-Za-z0-9][A-Za-z0-9-]*)", re.IGNORECASE)
_REF_RE = re.compile(r"#(\d+)\b")
_CLOSE_RE_TEMPLATE = r"\b(?:fix|fixes|fixed|close|closes|closed|resolve|resolves|resolved)\s*:?\s*#{number}\b"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PATCH_REFERENCE = re.compile(
    r"https://github\.com/[^\s/]+/[^\s/]+/(?:compare|tree|commit)/|\b(?:fixed|resolved)\s+(?:on|in)\s+(?:dev|next)\b",
    re.IGNORECASE,
)


class ClaimCheckError(Exception):
    """Base class for a read failure that must become UNKNOWN."""


class GitHubReadError(ClaimCheckError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class IncompletePagination(ClaimCheckError):
    pass


class ReadClient(Protocol):
    """Minimal GET-only GitHub adapter used by :func:`check`."""

    def get_object(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def get_list(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> list[Any]: ...

    def search_issues(self, query: str) -> list[dict[str, Any]]: ...


def _canonical_repo(repo: Any) -> str | None:
    if not isinstance(repo, str):
        return None
    cleaned = repo.strip()
    if not _REPO_RE.match(cleaned):
        return None
    return DEFAULT_ALIASES.get(cleaned.lower(), cleaned)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_text(value: Any, limit: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.replace("\x00", "").split())
    return collapsed[:limit]


def _excerpt(value: Any, limit: int = 160) -> str | None:
    return _safe_text(value, limit)


def _is_bot(user: Any) -> bool:
    if not isinstance(user, dict):
        return True
    login = user.get("login")
    if not isinstance(login, str):
        return True
    return user.get("type") == "Bot" or login.endswith("[bot]")


def _login(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    login = user.get("login")
    return login if isinstance(login, str) and login else None


def _claim_in_text(text: Any) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    if _LOOK_ONLY_RE.search(text):
        return False
    negation = _NEGATION_RE.search(text)
    if negation and not _CLAIM_RE.search(text[: negation.start()]):
        return False
    return bool(_CLAIM_RE.search(text))


def _closing_reference(text: Any, number: int) -> bool:
    if not isinstance(text, str):
        return False
    return bool(re.search(_CLOSE_RE_TEMPLATE.format(number=number), text, re.IGNORECASE))


def _related_issue_numbers(text: Any) -> list[int]:
    if not isinstance(text, str):
        return []
    found = {int(match) for match in _REF_RE.findall(text)}
    return sorted(found)


class GhClient:
    """GET-only ``gh api`` adapter with complete pagination."""

    def __init__(self, timeout: int = 45):
        self.timeout = timeout

    @staticmethod
    def _validate_path(path: str) -> str:
        if not isinstance(path, str) or not path or path.startswith("/") or path.startswith("-"):
            raise ValueError("invalid GitHub API path")
        if "://" in path or " " in path or "\n" in path or "\t" in path:
            raise ValueError("invalid GitHub API path")
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("invalid GitHub API path")
        if parts[0] not in ("repos", "search"):
            raise ValueError("only read-only repos/search endpoints are allowed")
        return path

    def _run(self, args: list[str]) -> str:
        command = ["gh", "api", *args]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitHubReadError(f"gh_api_unavailable:{type(exc).__name__}") from exc
        if completed.returncode != 0:
            detail = _safe_text(completed.stderr, 240) or "no stderr"
            status_match = re.search(r"HTTP\s+(\d{3})", detail)
            status = int(status_match.group(1)) if status_match else None
            raise GitHubReadError(f"gh_api_failed:{detail}", status=status)
        return completed.stdout

    @staticmethod
    def _json(text: str) -> Any:
        try:
            return json.loads(text)
        except ValueError as exc:
            raise GitHubReadError(f"gh_api_malformed_json:{type(exc).__name__}") from exc

    def get_object(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        args = ["--method", "GET", self._validate_path(path)]
        for key, value in (params or {}).items():
            args.extend(["--raw-field", f"{key}={value}"])
        payload = self._json(self._run(args))
        if not isinstance(payload, dict):
            raise GitHubReadError("gh_api_expected_object")
        return payload

    def get_list(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> list[Any]:
        args = ["--method", "GET", "--paginate", "--slurp", self._validate_path(path)]
        for key, value in (params or {}).items():
            args.extend(["--raw-field", f"{key}={value}"])
        for key, value in (headers or {}).items():
            args.extend(["--header", f"{key}: {value}"])
        payload = self._json(self._run(args))
        if not isinstance(payload, list):
            raise GitHubReadError("gh_api_expected_paginated_array")
        flattened: list[Any] = []
        for page in payload:
            if not isinstance(page, list):
                raise IncompletePagination("pagination_page_not_a_list")
            flattened.extend(page)
        return flattened

    def search_issues(self, query: str) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        total: int | None = None
        for page in range(1, 11):
            payload = self.get_object(
                "search/issues",
                {"q": query, "per_page": 100, "page": page},
            )
            items = payload.get("items")
            count = payload.get("total_count")
            if not isinstance(items, list) or not isinstance(count, int):
                raise GitHubReadError("search_response_malformed")
            if total is None:
                total = count
                if total > 1000:
                    raise IncompletePagination("search_result_cap_exceeded")
            collected.extend(item for item in items if isinstance(item, dict))
            if len(collected) >= total or not items:
                break
        if total is None or len(collected) < total:
            raise IncompletePagination("search_pagination_incomplete")
        return collected


def _unknown(
    repo: str | None,
    number: int | None,
    reasons: list[str],
    evidence: dict[str, Any] | None = None,
    object_type: str = "unknown",
) -> dict[str, Any]:
    return {
        "status": "UNKNOWN",
        "object_type": object_type,
        "repo": repo,
        "number": number,
        "checked_at": _now_iso(),
        "reasons": reasons,
        "evidence": evidence or {},
    }


def _read_error(exc: Exception, label: str) -> list[str]:
    return [f"{label}_failed:{type(exc).__name__}"]


def check(
    repo: str,
    number: int,
    actor: str | None = None,
    client: ReadClient | None = None,
) -> dict[str, Any]:
    """Check whether an upstream issue or PR is FREE, TAKEN, or UNKNOWN."""
    canonical = _canonical_repo(repo)
    if canonical is None:
        return _unknown(repo if isinstance(repo, str) else None, number, ["invalid_repo"])
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return _unknown(canonical, None, ["invalid_number"])

    reader = client if client is not None else GhClient()
    evidence: dict[str, Any] = {"reads": []}

    def record(path: str, count: int | None, status: str = "ok") -> None:
        evidence["reads"].append({"path": path, "status": status, "count": count})

    try:
        issue = reader.get_object(f"repos/{canonical}/issues/{number}")
    except Exception as exc:  # noqa: BLE001 - API failures are UNKNOWN
        record(f"repos/{canonical}/issues/{number}", None, "error")
        return _unknown(canonical, number, _read_error(exc, "object"), evidence)

    if not isinstance(issue, dict):
        return _unknown(canonical, number, ["object_malformed"], evidence)
    record(f"repos/{canonical}/issues/{number}", 1)

    object_number = issue.get("number")
    state = issue.get("state")
    if not isinstance(object_number, int) or not isinstance(state, str):
        return _unknown(canonical, number, ["object_missing_number_or_state"], evidence)

    evidence.update(
        {
            "state": state,
            "title": _safe_text(issue.get("title"), 160),
            "url": _safe_text(issue.get("html_url"), 240),
        }
    )

    if issue.get("pull_request") is not None:
        evidence["object_type"] = "pull_request"
        evidence["related_issues"] = _related_issue_numbers(issue.get("body"))
        if state == "open":
            return {
                "status": "TAKEN",
                "object_type": "pull_request",
                "repo": canonical,
                "number": object_number,
                "checked_at": _now_iso(),
                "reasons": ["queried_object_is_open_pull_request", "existing_work"],
                "evidence": evidence,
            }
        return {
            "status": "UNKNOWN",
            "object_type": "pull_request",
            "repo": canonical,
            "number": object_number,
            "checked_at": _now_iso(),
            "reasons": ["queried_object_is_closed_pull_request", "closed_pr_not_current_claim"],
            "evidence": evidence,
        }

    if state != "open":
        return _unknown(canonical, object_number, ["issue_not_open"], evidence, object_type="issue")

    assignees = issue.get("assignees")
    labels = issue.get("labels")
    if not isinstance(assignees, list) or not isinstance(labels, list):
        return _unknown(
            canonical, object_number, ["issue_missing_assignees_or_labels"], evidence, object_type="issue"
        )
    if any(not isinstance(item, dict) or not isinstance(item.get("login"), str) for item in assignees):
        return _unknown(canonical, object_number, ["issue_assignee_malformed"], evidence, object_type="issue")
    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in labels):
        return _unknown(canonical, object_number, ["issue_label_malformed"], evidence, object_type="issue")
    if "body" not in issue:
        return _unknown(canonical, object_number, ["issue_body_missing"], evidence, object_type="issue")
    body = issue.get("body") or ""
    if _PATCH_REFERENCE.search(body):
        evidence["body_patch_reference"] = _excerpt(body)
        return _unknown(
            canonical,
            object_number,
            ["body_patch_or_integration_branch_needs_inspection"],
            evidence,
            object_type="issue",
        )
    if _claim_in_text(body):
        evidence["body_claim"] = _excerpt(body)
        return _unknown(
            canonical, object_number, ["issue_author_claim_needs_inspection"], evidence, object_type="issue"
        )

    assignee_logins = [item["login"] for item in assignees]
    label_names = [
        item["name"] for item in labels if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    evidence.update({"assignees": assignee_logins, "labels": label_names, "object_type": "issue"})

    actor_key = actor.strip().casefold() if isinstance(actor, str) and actor.strip() else None
    assignee_keys = {item.casefold() for item in assignee_logins}
    own_assignment = actor_key is not None and actor_key in assignee_keys
    other_assignment = bool(assignee_keys) and not own_assignment
    if other_assignment:
        return {
            "status": "TAKEN",
            "object_type": "issue",
            "repo": canonical,
            "number": object_number,
            "checked_at": _now_iso(),
            "reasons": ["assigned_to_other_actor"],
            "evidence": evidence,
        }

    comments_path = f"repos/{canonical}/issues/{object_number}/comments"
    timeline_path = f"repos/{canonical}/issues/{object_number}/timeline"
    try:
        comments = reader.get_list(comments_path)
        record(comments_path, len(comments))
    except Exception as exc:  # noqa: BLE001
        record(comments_path, None, "error")
        return _unknown(canonical, object_number, _read_error(exc, "comments"), evidence, object_type="issue")

    try:
        timeline = reader.get_list(
            timeline_path,
            headers={"Accept": "application/vnd.github+json"},
        )
        record(timeline_path, len(timeline))
    except Exception as exc:  # noqa: BLE001
        record(timeline_path, None, "error")
        return _unknown(canonical, object_number, _read_error(exc, "timeline"), evidence, object_type="issue")

    try:
        search_items = reader.search_issues(f"repo:{canonical} is:pr {object_number} in:body")
        record("search/issues", len(search_items))
    except Exception as exc:  # noqa: BLE001
        record("search/issues", None, "error")
        return _unknown(canonical, object_number, _read_error(exc, "search"), evidence, object_type="issue")

    open_linked: dict[int, dict[str, Any]] = {}
    closed_linked: dict[int, dict[str, Any]] = {}
    claim_comments: list[dict[str, Any]] = []
    ambiguous: list[str] = []
    self_claims: list[str] = []

    def add_linked(pr_number: Any, pr_state: Any, source_name: str) -> None:
        if not isinstance(pr_number, int):
            ambiguous.append(f"ambiguous_linked_pr_number:{source_name}")
            return
        target = open_linked if pr_state == "open" else closed_linked
        target.setdefault(
            pr_number,
            {
                "number": pr_number,
                "state": pr_state if isinstance(pr_state, str) else None,
                "source": source_name,
            },
        )

    for item in comments:
        if not isinstance(item, dict):
            ambiguous.append("malformed_comment")
            continue
        user = item.get("user")
        login = _login(user)
        if login is None or "body" not in item:
            ambiguous.append("comment_missing_user_or_body")
            continue
        if _is_bot(user):
            continue
        text = item.get("body")
        if _PATCH_REFERENCE.search(text or ""):
            ambiguous.append("comment_patch_or_integration_branch_needs_inspection")
        if _claim_in_text(text):
            excerpt = _excerpt(text)
            claim_comments.append({"login": login, "excerpt": excerpt})
            if actor_key is not None and login.casefold() == actor_key:
                self_claims.append("own_claim_comment")
            else:
                self_claims.append("other_claim_comment")
            for ref in _REF_RE.findall(text if isinstance(text, str) else ""):
                ref_number = int(ref)
                if ref_number == object_number:
                    continue
                try:
                    ref_obj = reader.get_object(f"repos/{canonical}/issues/{ref_number}")
                except Exception as exc:  # noqa: BLE001
                    if isinstance(exc, GitHubReadError) and exc.status == 404:
                        ambiguous.append(f"unresolved_reference:{ref_number}")
                    else:
                        ambiguous.append(f"unreadable_reference:{ref_number}")
                    continue
                if not isinstance(ref_obj, dict):
                    ambiguous.append(f"malformed_reference:{ref_number}")
                    continue
                ref_state = ref_obj.get("state")
                if ref_obj.get("pull_request") is not None:
                    add_linked(ref_obj.get("number", ref_number), ref_state, "claim_comment")
                else:
                    ambiguous.append(f"reference_is_issue_not_pr:{ref_number}")
        assign_match = _ASSIGN_RE.search(text if isinstance(text, str) else "")
        if assign_match:
            target_login = assign_match.group(1)
            if actor_key is not None and target_login.casefold() == actor_key:
                self_claims.append("maintainer_assignment_comment")
            else:
                claim_comments.append({"login": target_login, "excerpt": _excerpt(text)})
                self_claims.append("other_assignment_comment")

    for event in timeline:
        if not isinstance(event, dict):
            ambiguous.append("malformed_timeline_event")
            continue
        event_name = event.get("event")
        if event_name not in ("cross-referenced", "connected", "referenced"):
            continue
        source = event.get("source")
        source_issue = source.get("issue") if isinstance(source, dict) else None
        if not isinstance(source_issue, dict):
            ambiguous.append("cross_reference_without_source_issue")
            continue
        if source_issue.get("pull_request") is None:
            continue
        add_linked(source_issue.get("number"), source_issue.get("state"), "timeline")

    for item in search_items:
        if not isinstance(item, dict):
            ambiguous.append("malformed_search_item")
            continue
        pr_number = item.get("number")
        pr_state = item.get("state")
        if not isinstance(pr_number, int) or not isinstance(pr_state, str):
            ambiguous.append("search_item_missing_number_or_state")
            continue
        if not _closing_reference(item.get("body"), object_number):
            continue
        add_linked(pr_number, pr_state, "search")

    evidence["linked_open_prs"] = sorted(open_linked.values(), key=lambda item: item["number"])
    evidence["linked_closed_prs"] = sorted(closed_linked.values(), key=lambda item: item["number"])
    evidence["claim_comments"] = claim_comments[:10]
    evidence["ambiguous_references"] = sorted(set(ambiguous))[:10]
    if own_assignment:
        evidence["own_assignment"] = True

    has_other_claim = "other_claim_comment" in self_claims or "other_assignment_comment" in self_claims
    if open_linked:
        return {
            "status": "TAKEN",
            "object_type": "issue",
            "repo": canonical,
            "number": object_number,
            "checked_at": _now_iso(),
            "reasons": ["open_linked_pull_request"],
            "evidence": evidence,
        }

    if ambiguous:
        return _unknown(
            canonical,
            object_number,
            ["ambiguous_reference"] + sorted(set(ambiguous))[:5],
            evidence,
            object_type="issue",
        )

    if has_other_claim:
        reasons = []
        if "other_claim_comment" in self_claims:
            reasons.append("claim_comment_by_other")
        if "other_assignment_comment" in self_claims:
            reasons.append("assignment_comment_for_other")
        return {
            "status": "TAKEN",
            "object_type": "issue",
            "repo": canonical,
            "number": object_number,
            "checked_at": _now_iso(),
            "reasons": reasons,
            "evidence": evidence,
        }
    if closed_linked:
        return _unknown(
            canonical,
            object_number,
            ["closed_linked_pull_request", "closed_pr_not_automatically_taken"],
            evidence,
            object_type="issue",
        )

    reasons = ["issue_open", "no_active_claim_found"]
    if own_assignment:
        reasons.append("own_assignment_permitted")
    if self_claims:
        reasons.append("own_claim_recorded")
    return {
        "status": "FREE",
        "object_type": "issue",
        "repo": canonical,
        "number": object_number,
        "checked_at": _now_iso(),
        "reasons": reasons,
        "evidence": evidence,
    }


def parse_target(value: str) -> tuple[str, int]:
    match = re.match(r"^([^#\s]+)#(\d+)$", value.strip())
    if not match:
        raise ValueError("target must be owner/repo#number")
    return match.group(1), int(match.group(2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only FREE/TAKEN/UNKNOWN claim check for a GitHub issue or PR."
    )
    parser.add_argument("target", help="owner/repo#number")
    parser.add_argument("--actor", default=None, help="GitHub login of the would-be contributor")
    args = parser.parse_args(argv)

    try:
        repo, number = parse_target(args.target)
    except ValueError as exc:
        print(json.dumps({"status": "UNKNOWN", "reasons": [str(exc)]}, ensure_ascii=False, indent=2))
        return 2

    result = check(repo, number, actor=args.actor)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
