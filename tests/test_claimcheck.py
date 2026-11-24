import json
import subprocess
import sys
import unittest
from pathlib import Path

from oss_audit.policy import claimcheck

ROOT = Path(__file__).resolve().parents[1]
REPO = "bytedance/deer-flow"
NUMBER = 7598


def issue(**overrides):
    value = {
        "number": NUMBER,
        "state": "open",
        "title": "sample issue",
        "body": "",
        "html_url": f"https://github.com/{REPO}/issues/{NUMBER}",
        "user": {"login": "reporter", "type": "User"},
        "assignees": [],
        "labels": [],
    }
    value.update(overrides)
    return value


def comment(login, body, user_type="User"):
    return {"user": {"login": login, "type": user_type}, "body": body}


def timeline_pr(number, state="open"):
    return {
        "event": "cross-referenced",
        "source": {
            "type": "issue",
            "issue": {
                "number": number,
                "state": state,
                "pull_request": {"url": f"https://api.github.com/repos/{REPO}/pulls/{number}"},
            },
        },
    }


class BodyEvidenceTests(unittest.TestCase):
    def test_compare_link_in_body_is_not_free(self):
        client = FakeClient(
            main_issue=issue(body="Patch: https://github.com/reporter/deer-flow/compare/main...fix")
        )
        self.assertEqual(claimcheck.check(REPO, NUMBER, client=client)["status"], "UNKNOWN")

    def test_ignored_malformed_label_cannot_be_free(self):
        client = FakeClient(main_issue=issue(labels=[{}]))
        self.assertEqual(claimcheck.check(REPO, NUMBER, client=client)["status"], "UNKNOWN")

    def test_fixed_on_dev_comment_is_not_free(self):
        client = FakeClient(comments=[comment("maintainer", "Fixed in dev, not released yet")])
        self.assertEqual(claimcheck.check(REPO, NUMBER, client=client)["status"], "UNKNOWN")


class FakeClient:
    def __init__(
        self,
        main_issue=None,
        comments=None,
        timeline=None,
        search=None,
        refs=None,
        comments_error=None,
        timeline_error=None,
        search_error=None,
        object_error=None,
    ):
        self.main_issue = main_issue if main_issue is not None else issue()
        self.comments = comments or []
        self.timeline = timeline or []
        self.search = search or []
        self.refs = refs or {}
        self.comments_error = comments_error
        self.timeline_error = timeline_error
        self.search_error = search_error
        self.object_error = object_error
        self.calls = []

    def get_object(self, path, params=None):
        self.calls.append(("object", path))
        if self.object_error is not None:
            raise self.object_error
        if path in self.refs:
            value = self.refs[path]
            if isinstance(value, Exception):
                raise value
            return value
        if path.startswith(f"repos/{REPO}/issues/"):
            return self.main_issue
        raise claimcheck.GitHubReadError("not found", status=404)

    def get_list(self, path, params=None, headers=None):
        self.calls.append(("list", path))
        if path.endswith("/comments"):
            if self.comments_error is not None:
                raise self.comments_error
            return self.comments
        if path.endswith("/timeline"):
            if self.timeline_error is not None:
                raise self.timeline_error
            return self.timeline
        raise claimcheck.GitHubReadError("unexpected list path", status=404)

    def search_issues(self, query):
        self.calls.append(("search", query))
        if self.search_error is not None:
            raise self.search_error
        return self.search


class ClaimCheckTests(unittest.TestCase):
    def test_distinguishes_pr_number_from_issue(self):
        pr = issue(pull_request={"url": "https://api.github.com/repos/x/y/pulls/7600"}, body="Fixes #7598")
        result = claimcheck.check(REPO, 7600, client=FakeClient(main_issue=pr))
        self.assertEqual(result["object_type"], "pull_request")
        self.assertEqual(result["status"], "TAKEN")
        self.assertEqual(result["evidence"]["related_issues"], [7598])

    def test_closed_pr_is_not_free(self):
        pr = issue(state="closed", pull_request={"url": "https://api.github.com/repos/x/y/pulls/7600"})
        result = claimcheck.check(REPO, 7600, client=FakeClient(main_issue=pr))
        self.assertEqual(result["object_type"], "pull_request")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("closed_pr_not_current_claim", result["reasons"])

    def test_free_issue(self):
        result = claimcheck.check(REPO, NUMBER, actor="tester", client=FakeClient())
        self.assertEqual(result["status"], "FREE")
        self.assertEqual(result["object_type"], "issue")
        self.assertIn("no_active_claim_found", result["reasons"])

    def test_assigned_to_other_is_taken(self):
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(main_issue=issue(assignees=[{"login": "other"}])),
        )
        self.assertEqual(result["status"], "TAKEN")
        self.assertIn("assigned_to_other_actor", result["reasons"])

    def test_own_assignment_is_permitted(self):
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(main_issue=issue(assignees=[{"login": "tester"}])),
        )
        self.assertEqual(result["status"], "FREE")
        self.assertIn("own_assignment_permitted", result["reasons"])

    def test_open_linked_pr_is_taken(self):
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(timeline=[timeline_pr(7600, "open")]),
        )
        self.assertEqual(result["status"], "TAKEN")
        self.assertIn("open_linked_pull_request", result["reasons"])

    def test_own_open_pr_is_taken_not_free(self):
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(timeline=[timeline_pr(7600, "open")]),
        )
        self.assertEqual(result["status"], "TAKEN")
        self.assertIn("open_linked_pull_request", result["reasons"])

    def test_search_incomplete_pagination_is_unknown(self):
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(search_error=claimcheck.IncompletePagination("cap exceeded")),
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("search_failed:IncompletePagination", result["reasons"])

    def test_closed_unrelated_pr_is_not_automatically_taken(self):
        unrelated = {"number": 7000, "state": "closed", "body": "Related to another area"}
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(search=[unrelated]),
        )
        self.assertEqual(result["status"], "FREE")

    def test_closed_linked_pr_is_unknown(self):
        closed = {"number": 7000, "state": "closed", "body": f"Fixes #{NUMBER}"}
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(search=[closed]),
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("closed_pr_not_automatically_taken", result["reasons"])

    def test_genuine_claim_comment_is_taken(self):
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(comments=[comment("other", "I'll take this and open a PR.")]),
        )
        self.assertEqual(result["status"], "TAKEN")
        self.assertIn("claim_comment_by_other", result["reasons"])

    def test_look_only_comment_is_not_a_claim(self):
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(comments=[comment("other", "I'll take a look at this tomorrow.")]),
        )
        self.assertEqual(result["status"], "FREE")

    def test_bot_comment_is_ignored(self):
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(comments=[comment("dependabot[bot]", "I will work on this", "Bot")]),
        )
        self.assertEqual(result["status"], "FREE")

    def test_ambiguous_reference_is_unknown(self):
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(comments=[comment("other", "I'll take this; see #9999.")]),
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("ambiguous_reference", result["reasons"])

    def test_claim_comment_reference_to_pr_is_taken(self):
        ref = {"number": 7000, "state": "open", "pull_request": {"url": "x"}}
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(
                comments=[comment("other", "I'll take this; PR #7000 is up.")],
                refs={f"repos/{REPO}/issues/7000": ref},
            ),
        )
        self.assertEqual(result["status"], "TAKEN")

    def test_api_failure_is_unknown_not_free(self):
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(comments_error=claimcheck.GitHubReadError("rate limited")),
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("comments_failed:GitHubReadError", result["reasons"])

    def test_incomplete_pagination_is_unknown(self):
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(timeline_error=claimcheck.IncompletePagination("incomplete")),
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("timeline_failed:IncompletePagination", result["reasons"])

    def test_search_failure_is_unknown(self):
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(search_error=claimcheck.GitHubReadError("search unavailable")),
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("search_failed:GitHubReadError", result["reasons"])

    def test_missing_assignees_is_unknown(self):
        broken = issue()
        del broken["assignees"]
        result = claimcheck.check(REPO, NUMBER, actor="tester", client=FakeClient(main_issue=broken))
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("issue_missing_assignees_or_labels", result["reasons"])

    def test_malformed_comment_is_unknown(self):
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(comments=[{"body": "I'll take this"}]),
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("ambiguous_reference", result["reasons"])

    def test_closed_issue_is_unknown(self):
        result = claimcheck.check(
            REPO, NUMBER, actor="tester", client=FakeClient(main_issue=issue(state="closed"))
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("issue_not_open", result["reasons"])

    def test_complete_pagination_counts_are_reported(self):
        comments = [comment("a", "hello"), comment("b", "me too"), comment("c", "same here")]
        result = claimcheck.check(
            REPO,
            NUMBER,
            actor="tester",
            client=FakeClient(comments=comments),
        )
        reads = {item["path"]: item for item in result["evidence"]["reads"]}
        self.assertEqual(reads[f"repos/{REPO}/issues/{NUMBER}/comments"]["count"], 3)
        self.assertEqual(reads[f"repos/{REPO}/issues/{NUMBER}/timeline"]["count"], 0)

    def test_result_is_json_safe(self):
        result = claimcheck.check(REPO, NUMBER, actor="tester", client=FakeClient())
        payload = json.dumps(result, ensure_ascii=False)
        self.assertIsInstance(payload, str)
        for key in ("status", "object_type", "repo", "number", "checked_at", "reasons", "evidence"):
            self.assertIn(key, result)
        self.assertIn(result["status"], claimcheck.STATUSES)

    def test_gh_client_rejects_write_like_paths(self):
        with self.assertRaises(ValueError):
            claimcheck.GhClient._validate_path("/repos/x/y")
        with self.assertRaises(ValueError):
            claimcheck.GhClient._validate_path("repos/x/y/../z")
        with self.assertRaises(ValueError):
            claimcheck.GhClient._validate_path("user/repos")

    def test_cli_help(self):
        completed = subprocess.run(
            [sys.executable, "-m", "oss_audit", "claimcheck", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(ROOT),
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("FREE/TAKEN/UNKNOWN", completed.stdout)


if __name__ == "__main__":
    unittest.main()
