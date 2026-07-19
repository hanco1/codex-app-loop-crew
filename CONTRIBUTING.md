# Contributing

Thanks for contributing! This project is stdlib-only Python and must run on
both Windows and POSIX.

## Prerequisites

- Git on PATH (the memory-layer smoke creates real commits, so a git identity
  must be configured: `git config user.email` / `git config user.name`).
- Python 3.9 or newer (any of `python3` / `python` / `py -3`).

## Running the tests locally

From the repository root:

```sh
# Contract test for the shared state-file parser
python skills/codex-agent-loop-orchestrator/scripts/test_loop_state_parsing.py

# Smoke suites
python skills/codex-agent-loop-orchestrator/scripts/smoke_memory_layer.py
python skills/codex-agent-loop-orchestrator/scripts/smoke_dashboard.py
```

All three must pass before a PR is merged; CI runs them on Ubuntu and Windows.

## Write scopes and leases

Each agent lane declares a `write_scope` (a set of path globs) in
`docs/loop/agent-lanes.md`, and may hold temporary leases on specific globs in
`docs/loop/leases.md`. The pre-commit scope guard enforces both: a lane may
only commit files inside its own write scope, and never files under another
lane's active lease. When editing the guard or anything that writes these
tables, keep the fail-closed posture — an unreadable or malformed table must
block the commit, not be waved through.

## Pull request expectations

- The contract test and both smoke suites pass on your platform (CI verifies
  the other one).
- Keep `README.md` and `README.zh-CN.md` in sync — any user-facing doc change
  lands in both in the same PR.
- Use UTF-8 with `encoding="utf-8"` for all file I/O; standard library only.
