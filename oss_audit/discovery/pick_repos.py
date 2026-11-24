"""Screen candidate open-source repositories for external contributor merge activity."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from oss_audit.core.common import write_json
from oss_audit.core.paths import WorkspacePaths

CANDIDATE_REPOS = [
    "bytedance/deer-flow",
    "huggingface/smolagents",
    "agno-agi/agno",
    "run-llama/llama_index",
    "livekit/agents",
    "microsoft/autogen",
    "microsoft/agent-framework",
    "crewAIInc/crewAI",
    "google/adk-python",
    "pydantic/pydantic-ai",
    "langchain-ai/langgraph",
    "langgenius/dify",
    "infiniflow/ragflow",
    "QwenLM/Qwen-Agent",
    "agentscope-ai/agentscope",
    "deepset-ai/haystack",
    "stanfordnlp/dspy",
    "geekan/MetaGPT",
    "n8n-io/n8n",
    "FlowiseAI/Flowise",
    "openai/openai-agents-python",
    "mem0ai/mem0",
    "Arize-ai/phoenix",
    "browser-use/browser-use",
    "Skyvern-AI/skyvern",
    "All-Hands-AI/OpenHands",
    "cline/cline",
    "continuedev/continue",
    "sourcegraph/amp",
    "block/goose",
]

EXTERNAL_ASSOCIATIONS = {"CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE"}


def _gh(args: list[str]) -> str | None:
    proc = subprocess.run(
        ["gh", "--method", "GET", *args] if args[:1] == ["api"] else ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def evaluate_candidates(repos: list[str] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for repo in repos or CANDIDATE_REPOS:
        base = _gh(
            [
                "api",
                f"repos/{repo}",
                "--jq",
                "{s: .stargazers_count, lang: .language, arch: .archived, fork: .fork}",
            ]
        )
        if not base:
            rows.append({"repo": repo, "error": "not found"})
            continue
        info = json.loads(base)
        if info.get("arch") or info.get("fork"):
            rows.append({"repo": repo, **info, "skip": "archived/fork"})
            continue
        assoc = _gh(
            [
                "api",
                f"repos/{repo}/pulls?state=closed&per_page=60&sort=updated&direction=desc",
                "--jq",
                "[.[] | select(.merged_at != null) | .author_association] | group_by(.) | map({key: .[0], value: length}) | from_entries",
            ]
        )
        counts = json.loads(assoc) if assoc else {}
        ext = sum(v for k, v in counts.items() if k in EXTERNAL_ASSOCIATIONS)
        total = sum(counts.values())
        rows.append(
            {
                "repo": repo,
                "stars": info["s"],
                "lang": info["lang"],
                "merged_recent": total,
                "external_merged": ext,
                "assoc": counts,
            }
        )
    rows.sort(key=lambda r: (-(r.get("external_merged") or 0), -(r.get("stars") or 0)))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    paths = WorkspacePaths.from_workspace(args.workspace)
    output = args.output or (paths.state_dir / "candidate-repos.json")
    rows = evaluate_candidates()
    print(f"{'repo':38s} {'stars':>7s} {'lang':10s} {'merged':>6s} {'ext':>4s}  verdict")
    for row in rows:
        if "error" in row:
            print(f"{row['repo']:38s} {'-':>7s} {'-':10s} {'-':>6s} {'-':>4s}  {row['error']}")
            continue
        if row.get("skip"):
            print(
                f"{row['repo']:38s} {row['stars']:>7d} {str(row['lang'])[:10]:10s} {'-':>6s} {'-':>4s}  {row['skip']}"
            )
            continue
        ext = row["external_merged"]
        total = row["merged_recent"]
        if ext >= 10:
            verdict = "active external contributions"
        elif ext >= 3:
            verdict = "moderate external contributions"
        elif total == 0:
            verdict = "no recent merged PR"
        else:
            verdict = "mostly internal"
        print(
            f"{row['repo']:38s} {row['stars']:>7d} {str(row['lang'])[:10]:10s} {total:>6d} {ext:>4d}  {verdict}"
        )
    write_json(output, rows)
    print(f"\n-> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
