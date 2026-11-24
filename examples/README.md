# Verified Case Studies & Exemplar Artifacts

This directory contains curated, production-verified case studies produced by the `oss-audit` pipeline across major open-source AI agent frameworks. Each case study illustrates how contract provenance, RED-to-GREEN execution receipts, and upstream policy routing prevent speculative or duplicate pull requests.

| Case Study | Upstream Target | Route | Outcome |
|---|---|---|---|
| [deerflow-local-sandbox-path-translation](deerflow-local-sandbox-path-translation/pr-writeup.md) | `bytedance/deer-flow#7462` | `direct_pr` (linked `#7461`) | **Merged** (`228913b6b`) |
| [deerflow-required-secrets-precedence](deerflow-required-secrets-precedence/pr-writeup.md) | `bytedance/deer-flow#7569` | `direct_pr` (linked `#7568`) | **Merged** (`8d3af33f8`) |
| [agentscope-wordparser-blank-runs](agentscope-wordparser-blank-runs/pr-writeup.md) | `agentscope-ai/agentscope#3401` | `issue_first` (`#3400` -> `#3401`) | **Merged** (`ce04210e2`) |
| [haystack-async-retriever-guard](haystack-async-retriever-guard/pr-writeup.md) | `deepset-ai/haystack#13381` | `issue_first` (`#13380` -> `#13381`) | **Merged** (`76fc2d3d7`) |
| [skyvern-split-pdf-token-budget](skyvern-split-pdf-token-budget/issue-report.md) | `Skyvern-AI/skyvern#9098` | `issue_only` (design trade-off) | **Published Issue** (`#9098`) |
