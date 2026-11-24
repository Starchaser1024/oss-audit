"""Read-only GitHub notification/PR review watcher and agent session event inspector."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oss_audit.core.paths import WorkspacePaths


def _gh_json(args: list[str]) -> Any:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", check=False)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.strip() + "\n")
        return None
    txt = proc.stdout.strip()
    return json.loads(txt) if txt else None


def _human_time(iso: str | None) -> str:
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:16]
    mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    if mins < 60 * 24:
        return f"{mins // 60}h ago"
    return f"{mins // (60 * 24)}d ago"


def _state_file(workspace: str | Path | None = None) -> Path:
    return WorkspacePaths.from_workspace(workspace).state_dir / "gh-watch-state.json"


def load_state(workspace: str | Path | None = None) -> dict[str, Any]:
    path = _state_file(workspace)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def save_state(state: dict[str, Any], workspace: str | Path | None = None) -> None:
    path = _state_file(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _max_id(items: list[dict[str, Any]]) -> int:
    return max((int(i.get("id") or 0) for i in items), default=0)


def _login_of(item: dict[str, Any]) -> str:
    user = item.get("user") or item.get("author") or {}
    return str(user.get("login") or "?")


def agent_session_status(directory: str | Path) -> dict[str, Any]:
    """Read agent event metadata without exposing prompts, tool output, or credentials."""
    models = set()
    last_type = None
    completed = False
    base = Path(directory)
    events = base / "events.jsonl"
    if events.is_file():
        with events.open(encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                last_type = row.get("type")
                msg = row.get("message", {})
                if isinstance(msg, dict) and msg.get("model"):
                    models.add((msg.get("provider"), msg["model"]))
                if last_type == "agent_end":
                    completed = True
    exit_path = base / "exit-code.txt"
    return {
        "directory": str(base),
        "models_observed": [list(m) for m in sorted(models)],
        "last_event": last_type,
        "agent_end": completed,
        "exit_code": exit_path.read_text(encoding="utf-8").strip() if exit_path.exists() else None,
    }


def cmd_notifications(include_read: bool, limit: int) -> int:
    query = f"notifications?per_page={limit}" + ("&all=true" if include_read else "")
    data = _gh_json(["api", "--method", "GET", query])
    if data is None:
        return 1
    if not data:
        print("No notifications.")
        return 0
    print(f"Notifications ({'all' if include_read else 'unread'}): {len(data)}\n")
    for item in data:
        subj = item["subject"]
        repo = item["repository"]["full_name"]
        flag = "" if item["unread"] else " (read)"
        print(f"* [{subj['type']}] {repo} - {subj['title'][:72]}{flag}")
        print(f"    {item['reason']} · {_human_time(item.get('updated_at'))}")
        url = (
            (subj.get("latest_comment_url") or subj.get("url") or "")
            .replace("api.github.com/repos/", "github.com/")
            .replace("/pulls/", "/pull/")
        )
        if url:
            print(f"    {url}")
        print()
    return 0


def _me() -> str | None:
    data = _gh_json(["api", "--method", "GET", "user", "--jq", "{login: .login}"])
    return (data or {}).get("login")


def _open_prs(me: str) -> list[dict[str, Any]]:
    return (
        _gh_json(
            [
                "search",
                "prs",
                "--author",
                me,
                "--state",
                "open",
                "--limit",
                "30",
                "--json",
                "repository,number,title,updatedAt",
            ]
        )
        or []
    )


def _detail(repo: str, num: int) -> tuple[dict[str, Any], list[Any], list[Any], list[Any]]:
    info = (
        _gh_json(
            [
                "pr",
                "view",
                str(num),
                "--repo",
                repo,
                "--json",
                "state,title,url,headRefName,headRefOid,isDraft,reviewDecision",
            ]
        )
        or {}
    )
    conv = _gh_json(["api", "--method", "GET", f"repos/{repo}/issues/{num}/comments?per_page=100"]) or []
    reviews = [
        r
        for r in (
            _gh_json(["api", "--method", "GET", f"repos/{repo}/pulls/{num}/reviews?per_page=100"]) or []
        )
        if r.get("state") != "PENDING"
    ]
    inline = _gh_json(["api", "--method", "GET", f"repos/{repo}/pulls/{num}/comments?per_page=100"]) or []
    return info, conv, reviews, inline


def cmd_prs() -> int:
    me = _me()
    if not me:
        return 1
    prs = _open_prs(me)
    if not prs:
        print("No open pull requests.")
        return 0
    print(f"Open pull requests: {len(prs)}\n")
    for pr in prs:
        repo, num = pr["repository"]["nameWithOwner"], pr["number"]
        info, conv, reviews, inline = _detail(repo, num)
        print(f"* {repo} #{num} - {pr['title'][:70]}")
        print(
            f"    updated {_human_time(pr['updatedAt'])} · comments {len(conv)} · "
            f"reviews {len(reviews)} · inline {len(inline)} · {info.get('reviewDecision') or 'PENDING'}"
        )
        print(f"    {info.get('url') or f'https://github.com/{repo}/pull/{num}'}\n")
    return 0


def cmd_new(ack: bool = False, workspace: str | Path | None = None) -> int:
    me = _me()
    if not me:
        return 1
    prs = _open_prs(me)
    if not prs:
        print("No open pull requests.")
        return 0
    state = load_state(workspace)
    total = 0
    out: list[str] = []
    for pr in prs:
        repo, num = pr["repository"]["nameWithOwner"], pr["number"]
        key = f"{repo}#{num}"
        seen = state.get(key, {})
        info, conv, reviews, inline = _detail(repo, num)
        new_c = [c for c in conv if int(c.get("id") or 0) > seen.get("comments", 0) and _login_of(c) != me]
        new_r = [r for r in reviews if int(r.get("id") or 0) > seen.get("reviews", 0) and _login_of(r) != me]
        new_i = [c for c in inline if int(c.get("id") or 0) > seen.get("inline", 0) and _login_of(c) != me]
        if ack:
            state[key] = {"comments": _max_id(conv), "reviews": _max_id(reviews), "inline": _max_id(inline)}
        if not (new_c or new_r or new_i):
            continue
        total += len(new_c) + len(new_r) + len(new_i)
        out.append(f"## {repo} #{num} - {(info.get('title') or pr['title'])[:80]}")
        for c in new_c:
            out.append(f"   [comment id={c.get('id')}] {_login_of(c)}: {' '.join((c.get('body') or '').split())}")
        for r in new_r:
            out.append(
                f"   [review id={r.get('id')}] {_login_of(r)} {r.get('state')}: "
                f"{' '.join((r.get('body') or '').split())}"
            )
        for c in new_i:
            out.append(f"   [inline id={c.get('id')}] {_login_of(c)}: {' '.join((c.get('body') or '').split())}")
        out.append("")
    if ack:
        save_state(state, workspace)
    if total == 0:
        print("No new reviews or comments since last watermark.")
        return 0
    print("\n".join(out))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="Include read notifications")
    ap.add_argument("--prs", action="store_true", help="Inspect open PR review activity")
    ap.add_argument("--new", action="store_true", help="Show incremental review activity since watermark")
    ap.add_argument("--ack", action="store_true", help="Advance local watermark after inspection")
    ap.add_argument("--json", action="store_true", help="Emit JSON notifications")
    ap.add_argument("--sessions", nargs="*", help="Inspect local agent session directories")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--workspace", type=Path, default=None)
    args = ap.parse_args(argv)

    if args.sessions is not None:
        print(json.dumps([agent_session_status(d) for d in args.sessions], indent=2))
        return 0
    if args.ack:
        return cmd_new(ack=True, workspace=args.workspace)
    if args.new:
        return cmd_new(ack=False, workspace=args.workspace)
    if args.json:
        query = f"notifications?per_page={args.limit}" + ("&all=true" if args.all else "")
        data = _gh_json(["api", "--method", "GET", query])
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    return cmd_prs() if args.prs else cmd_notifications(args.all, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
