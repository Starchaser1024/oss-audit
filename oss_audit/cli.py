"""Unified command-line interface for the ``oss-audit`` engineering pipeline."""

from __future__ import annotations

import argparse
import sys
from typing import Callable

from oss_audit import __version__


def _dispatch_map() -> dict[str, tuple[str, Callable[[list[str] | None], int]]]:
    from oss_audit.analyzers.contract_audit import main as audit_main
    from oss_audit.analyzers.issue_scanner import main as scan_main
    from oss_audit.discovery.discover import main as discover_main
    from oss_audit.discovery.pick_repos import main as pick_repos_main
    from oss_audit.discovery.reach import main as reach_main
    from oss_audit.discovery.score import main as score_main
    from oss_audit.evidence.runner import main as evidence_main
    from oss_audit.policy.claim_collision import main as collision_main
    from oss_audit.policy.claimcheck import main as claimcheck_main
    from oss_audit.policy.router import main as policy_main
    from oss_audit.policy.submit import main as submit_main
    from oss_audit.scheduling.gate import main as gate_main
    from oss_audit.scheduling.prep import main as prep_main
    from oss_audit.scheduling.queue import cli as queue_main
    from oss_audit.scheduling.tick import cli as tick_main
    from oss_audit.scheduling.watch import main as watch_main
    from oss_audit.verification.metrics import main as metrics_main
    from oss_audit.verification.render import main as render_main
    from oss_audit.verification.verify import main as verify_main

    return {
        "discover": ("Discover and assess candidate repositories into bounded module audit tasks", discover_main),
        "tick": ("Run one bounded SQLite lease-fenced scheduling pass", tick_main),
        "queue": ("Inspect or advance tasks in the SQLite pipeline queue", queue_main),
        "gate": ("Display coordinator capacity and lease status", gate_main),
        "policy": ("Route candidates against pinned upstream CONTRIBUTING policies", policy_main),
        "claimcheck": ("Check FREE/TAKEN/UNKNOWN claim status for owner/repo#number", claimcheck_main),
        "collision": ("Detect upstream duplicate issues/PRs for code-audit candidates", collision_main),
        "scan": ("Scan open upstream issues or run AST guard-asymmetry analyzers", scan_main),
        "audit": ("Generate AST multi-implementation contract audit leads", audit_main),
        "reach": ("Extract static call-site reachability leads for a target symbol", reach_main),
        "score": ("Compute explainable evidence-gated candidate shadow rankings", score_main),
        "prep": ("Create an isolated Git worktree for a candidate branch", prep_main),
        "evidence": ("Execute isolated pytest receipts or validate SHA-256 bundles", evidence_main),
        "verify": ("Validate evidence bundles or replay historical counterexample suites", verify_main),
        "render": ("Render issue or PR drafts strictly from verified execution receipts", render_main),
        "submit": ("Run fail-closed preflight gate and emit a dry-run publication plan", submit_main),
        "metrics": ("Compute read-only local SQLite funnel or remote cohort metrics", metrics_main),
        "watch": ("Poll read-only GitHub PR review notifications or session metadata", watch_main),
        "pick-repos": ("Screen candidate repositories for external contributor merge activity", pick_repos_main),
    }


def _format_help(commands: dict[str, tuple[str, Callable[[list[str] | None], int]]]) -> str:
    lines = [
        "usage: oss-audit <command> [options]",
        "",
        "Evidence-driven open-source defect verification & policy-aware triage pipeline.",
        "",
        "commands:",
    ]
    for name, (summary, _) in commands.items():
        lines.append(f"  {name:<14} {summary}")
    lines += [
        "",
        "options:",
        "  -h, --help     Show this help message and exit",
        "  -V, --version  Show program version and exit",
        "",
        "Run `oss-audit <command> --help` for subcommand-specific options.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    args = list(sys.argv[1:] if argv is None else argv)
    commands = _format_commands()

    if not args or args[0] in ("-h", "--help"):
        print(_format_help(commands))
        return 0
    if args[0] in ("-V", "--version"):
        print(f"oss-audit {__version__}")
        return 0

    cmd = args[0]
    rest = args[1:]
    if cmd not in commands:
        parser = argparse.ArgumentParser(prog="oss-audit", add_help=False)
        parser.error(f"unknown command {cmd!r}; choose from {', '.join(commands)}")
    _, handler = commands[cmd]
    saved_argv = sys.argv[:]
    try:
        sys.argv = [f"oss-audit {cmd}", *rest]
        try:
            rc = handler(rest)
            return int(rc) if rc is not None else 0
        except SystemExit as exc:
            if isinstance(exc.code, int):
                return exc.code
            return 0 if exc.code is None else 1
    finally:
        sys.argv = saved_argv


def _format_commands() -> dict[str, tuple[str, Callable[[list[str] | None], int]]]:
    return _dispatch_map()


if __name__ == "__main__":
    raise SystemExit(main())
