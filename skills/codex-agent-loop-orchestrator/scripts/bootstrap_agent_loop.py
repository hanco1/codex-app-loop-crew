#!/usr/bin/env python3
"""Create or update a repo-local multi-agent loop registry.

This helper is intentionally small and deterministic. It creates
docs/loop/agent-lanes.md, durable request state, the loop-engineering state
files (goal/tracker/constraints/handoff.md), loop budget/run-log/lease files,
and per-lane files without changing project code.

All template writes are write-if-missing: existing user content is never
clobbered. The registry table is rebuilt from existing rows so the script is
idempotent across repeated runs.
"""

from __future__ import annotations

import argparse
from pathlib import Path


DEFAULT_LANES = {
    "product": {
        "role": "Plan goals, specs, milestones, acceptance criteria, and product judgment.",
        "write_scope": "docs/loop/tracker.md; docs/loop/handoff.md; docs/product/**",
    },
    "implementation": {
        "role": "Implement scoped requests, run verification, and report evidence.",
        "write_scope": "src/**; tests/**; implementation notes named by request",
    },
    "review": {
        "role": "Review against acceptance criteria and request precise fixes.",
        "write_scope": "docs/loop/lanes/review/**",
    },
}


LANE_PRESETS = {
    "research": {
        "role": "Do source-backed research and summarize findings.",
        "write_scope": "docs/research/**; docs/loop/lanes/research/**",
    },
    "visual": {
        "role": "Review UI/visual output and prepare visual asset requests.",
        "write_scope": "docs/design/**; docs/loop/lanes/visual/**",
    },
    "security": {
        "role": "Review security risks, threat models, and sensitive changes.",
        "write_scope": "docs/security/**; docs/loop/lanes/security/**",
    },
    "data": {
        "role": "Analyze metrics, datasets, experiments, and validation evidence.",
        "write_scope": "docs/data/**; docs/loop/lanes/data/**",
    },
    "docs": {
        "role": "Maintain docs, changelogs, release notes, and user-facing copy.",
        "write_scope": "docs/**; docs/loop/lanes/docs/**",
    },
    "release": {
        "role": "Coordinate release readiness, QA checklist, packaging, and blockers.",
        "write_scope": "docs/release/**; docs/loop/lanes/release/**",
    },
    "media": {
        "role": "Coordinate scripts, covers, videos, and social-content assets.",
        "write_scope": "docs/media/**; docs/loop/lanes/media/**",
    },
}


REQUESTS_TEMPLATE = """# Requests

Use the table below as the durable queue for cross-agent work. Recover from
here instead of from chat memory.

## Schema

Required columns, in order: request_id, status, owner_lane, iteration,
source_docs, last_message, next_action, updated_at.

Rules:

- request_id is stable across fix cycles: REQ-YYYYMMDD-HHMMSS-<lane>.
- status lifecycle: PLANNED -> REQUESTED -> IMPLEMENTING -> IMPLEMENTATION_DONE -> REVIEWING -> FIX_REQUESTED -> ACCEPTED | BLOCKED.
- Terminal states are ACCEPTED and BLOCKED.
- Only the current owner_lane moves a request forward.
- Increment iteration when a request returns to implementation after review.
- next_action must let any lane resume after compaction or a new session.
- updated_at is ISO-8601 UTC, e.g. 2026-06-23T11:00:00Z.

This file must contain exactly one Markdown table (the queue below). Keep the
schema described as prose above so recovery tooling reads only real rows.

## Queue

| request_id | status | owner_lane | iteration | source_docs | last_message | next_action | updated_at |
| --- | --- | --- | --- | --- | --- | --- | --- |
"""


LOOP_POLICY_TEMPLATE = """# Loop Policy

## Handoff Gates

- Start loop engineering only when the goal is clear, work is checkpointable, verification exists, and state must survive across sessions.
- Hand off only at checkpoint boundaries after verification and state updates.
- The next lane must be able to act from repo files plus the message alone.

## Request Policy

<!-- max_fix_cycles bounds token burn per request; humans may edit it here or via the loop dashboard's POST /api/policy control. Keep the "max_fix_cycles: <int>" line format. -->
max_fix_cycles: 3
auto_dispatch: true
auto_chain_next_session: false

## Anti-Thrash Policy

- Cap consecutive FIX_REQUESTED <-> IMPLEMENTING cycles for one request at `max_fix_cycles`.
- When the cap is reached, stop the fix loop and escalate the request to product as BLOCKED.
- Do not reopen an ACCEPTED request without a new request_id.

## Completion Token

- Emit `SHIP_CHECK_OK` only when every checkpoint verify command exited 0 and evidence is recorded under `evidence/`.
- If any verify command cannot run or exits non-zero, the checkpoint is BLOCKED, not accepted-with-caveat.

## Human Gates

Stop before credentials, production deployment, destructive actions, billing changes, private external data, or unclear acceptance criteria.

## Thread Policy

- Returned thread IDs are provisional until read_thread or list_threads verifies them.
- Create at most one replacement continuation for a stale or unreadable thread.
- Do not create a new thread when a verified active thread already owns the same request.
"""


GOAL_TEMPLATE = """# Goal

## Objective

- State the single durable objective in one or two sentences.

## Done When

- [ ] Define the first concrete, verifiable completion condition.
- [ ] Add more conditions until \"done\" is unambiguous.

## Out Of Scope

- List non-goals so later sessions do not drift.

## Verification Surface

- Name how completion is checked: tests, build, screenshots, review checklist,
  rendered artifact, source evidence, or manual acceptance criteria.

## Status Legend

- `[ ]` not started
- `[~]` in progress
- `[x]` done and verified
- `[!]` blocked

## Auto-Chain Permission

auto_chain_next_session: false
"""


TRACKER_TEMPLATE = """# Tracker

Phase dashboard for the loop. Keep checkpoints small and verifiable. Use the
status legend so the doctor and other lanes can read progress mechanically.

## Status Legend

- `[ ]` not started
- `[~]` in progress
- `[x]` done and verified
- `[!]` blocked

## Checkpoints

- [ ] Define the first checkpoint as one coherent, verifiable slice.
- [ ] Add the next checkpoint once the first is scoped.

## Done When

- [ ] Mirror the goal's completion conditions here and check them off only
      after verification evidence exists.

## Notes

- Record verification commands, evidence paths, and blockers next to each
  checkpoint as you close it.
"""


CONSTRAINTS_TEMPLATE = """# Constraints

Boundaries every lane must respect. Read before implementing or reviewing.

## Hard Constraints

- Do not commit secrets, credentials, or tokens.
- Do not run destructive, billing, or production-deployment actions without explicit human approval.
- Stay inside each lane's declared write_scope in `agent-lanes.md`.

## Technical Constraints

- Record language, framework, runtime, and version pins the work must honor.

## Process Constraints

- Only switch sessions at a checkpoint boundary.
- Update `tracker.md`, `handoff.md`, `requests.md`, and lane `current.md` before any handoff.
- Reuse the same `request_id` across fix cycles; increment `iteration`.

## Status Legend

- `[ ]` not started
- `[~]` in progress
- `[x]` done and verified
- `[!]` blocked

## Auto-Chain Permission

auto_chain_next_session: false
"""


HANDOFF_TEMPLATE = """# Handoff

Continuation state for the next session or lane. Keep this current enough that
the next actor can continue from repo files plus the latest message alone.

## Current State

- Summarize what is done and verified right now.

## Next Action

- [ ] Write the single next checkpoint as one clear, bounded request.

## Active Request

- request_id:
- owner_lane:
- iteration:

## Blockers

- None.

## Pending Inbox Deliveries

- None.

## Status Legend

- `[ ]` not started
- `[~]` in progress
- `[x]` done and verified
- `[!]` blocked

## Done When

- [ ] Restate the completion condition this handoff is driving toward.

## Memory Protocol

The decision memory (`memory/decisions.jsonl`) is an append-only cache, never a
source of truth. Follow this protocol so it survives compaction and never lies:

1. Before deciding, grep `memory/decisions.jsonl` for this request_id and
   follow the `supersedes` chain to the newest live decision.
2. Before trusting any recorded `gate_status`, re-run
   `completion_gate.py --request-id <id>` and `multi_agent_loop_doctor.py`.
   The recorded token is only a hint; the live gate is the authority.
3. If the doctor reports a `stale_decision`, discard that cached decision and
   re-read the live source docs before acting on it.
4. At checkpoint close, append EXACTLY one line via `record_decision.py`. Never
   edit or delete an old line. To change a prior decision, append a new line
   whose `supersedes` names the old `decision_id`.

## Auto-Chain Permission

auto_chain_next_session: false
"""


LOOP_BUDGET_TEMPLATE = """# Loop Budget

Cost and effort stop gate. The loop must stop and report when any budget is
exhausted, regardless of remaining tracker work.

## Limits

max_total_tokens: 0
max_total_usd: 0
max_attempts_per_request: 3
max_loop_iterations: 0

A limit of `0` means \"unset / no enforced cap\"; set a positive number to enforce.

## Spent

tokens_spent: 0
usd_spent: 0
loop_iterations: 0

## Stop Flag

budget_exhausted: false

## Rules

- Update `Spent` as work proceeds.
- Set `budget_exhausted: true` and stop when any positive limit is reached or exceeded.
- Do not auto-chain a continuation session while `budget_exhausted: true`.
"""


LOOP_RUN_LOG_TEMPLATE = """# Loop Run Log

Append-only transition log. Add one row per state transition; never edit or
delete prior rows. Use this to reconstruct loop history after compaction.

| timestamp | request_id | iteration | from_status | to_status | lane | note |
| --- | --- | --- | --- | --- | --- | --- |
"""


LEASES_TEMPLATE = """# File Leases

Advisory write leases. A lane should acquire a lease before editing files
outside an obviously exclusive scope, and release it when done. Leases are
advisory: pair them with the git pre-commit write_scope check for enforcement.

| file_glob | lane | request_id | acquired_at | status |
| --- | --- | --- | --- | --- |

Status values: `active` (held/enforced) or `released`/`expired`/`done`/`revoked` (ignored). A blank status is ignored; any other non-blank value is treated as held, so the guard fails closed on a status it does not recognize.
"""


WORKLOG_TEMPLATE = """# {title} Worklog

| Time | Request | Action | Evidence |
| --- | --- | --- | --- |
"""


INBOX_TEMPLATE = """# {title} Inbox

Messages pending this lane's attention.

| Time | Request | From | Message | Status |
| --- | --- | --- | --- | --- |
"""


OUTBOX_TEMPLATE = """# {title} Outbox

Messages sent or queued by this lane.

| Time | Request | To | Message | Delivery |
| --- | --- | --- | --- | --- |
"""


CURRENT_TEMPLATE = """# {title} Current State

current_request_id:
status: idle
iteration:
last_updated:
heartbeat:

## Current Checkpoint

- None.

## Next Action

- Wait for an assigned request or product direction.

## Blockers

- None.
"""


EVIDENCE_README_TEMPLATE = """# Evidence

Completion-gate evidence lives here. Store one JSON record per verification
command so `SHIP_CHECK_OK` can be justified from repo files alone.

## File contract (flat, one JSON object per command)

Write one file per verification command directly in this directory, named:

```text
docs/loop/evidence/<request_id>-iter-<n>-<command>.json
```

`<command>` is slugified: lowercase it and replace every run of non-alphanumeric
characters with a single `-` (for example `npm test` -> `npm-test`,
`pytest -q` -> `pytest-q`). `<n>` is the request's current iteration.

Each file is a single JSON object with exactly these five fields:

```json
{
  "request_id": "REQ-20260623-101500-implementation",
  "checkpoint": "mvp-color-match",
  "command": "npm test",
  "exit_code": 0,
  "ran_at": "2026-06-23T11:00:00Z"
}
```

All five fields are required. Record the real process exit code; never
normalize a non-zero exit to `0`. A checkpoint is only verified when every
record for the request reports `exit_code` 0.

## Warning: the gate reads this directory with a NON-RECURSIVE glob

The completion gate collects evidence with `glob('*.json')` on this directory
only. It does not recurse. Consequences:

- A file in a subdirectory (for example `evidence/<subdir>/foo.json`) is
  INVISIBLE to the gate. Do not nest evidence under per-request folders.
- A non-JSON file (for example a `.txt` transcript) is INVISIBLE to the gate.
  Capture the exit code in the JSON record above, not in a side `.txt` file.

Keep every evidence record as a flat `*.json` file in this directory, or the
gate will not count it.
"""


DECISIONS_README_TEMPLATE = """# Decision Memory

`decisions.jsonl` is the loop's only memory artifact: an append-only decision
log. It is a CACHE derived from the `docs/loop` source files, never a source of
truth. If it disagrees with the live sources, the sources win.

## One line per decision (append-only)

Each line is one JSON object. Append new lines only; NEVER edit or delete a
prior line. To change a decision, append a new line whose `supersedes` names the
old `decision_id`. Write lines with `record_decision.py` (it computes the hash
and appends in `'a'` mode).

Fields (one JSON object per line):

- `decision_id` -- stable id (derived from request_id + a per-request sequence).
- `request_id` -- the request this decision serves.
- `lane` -- which lane decided.
- `decision` -- what was decided (one line).
- `rationale` -- why.
- `alternatives_rejected` -- options considered and dropped.
- `supersedes` -- the `decision_id` this line replaces, or empty.
- `source_docs` -- the source files this decision derives from / depends on.
- `content_hash` -- `normalize_then_hash(source_docs)` at write time (CRLF->LF,
  trailing newlines stripped, sha256 hex).
- `gate_status` -- completion-gate token at write time: `SHIP_CHECK_OK`,
  `SHIP_CHECK_FAIL`, or `none` (so a decision made under FAIL reads as tentative).
- `created_at` -- ISO-8601 UTC.

## Known tradeoff: the hash only covers the listed `source_docs`

`content_hash` is computed over exactly the files named in `source_docs` and
nothing else. Drift detection (in `multi_agent_loop_doctor.py`) can only notice
a change in a file that was listed. If a decision truly depended on a file that
was omitted from `source_docs`, a later change to that file will NOT be flagged
as `stale_decision`. List every source a decision genuinely depends on.

## Drift is advisory, never a gate

The doctor recomputes the hash for every non-superseded decision with the SAME
`normalize_then_hash` helper (imported from `record_decision.py`, the single
canonical definition). A mismatch is a `stale_decision` WARNING; a missing
source doc is `missing_source_doc`; a bad line is `malformed_decision`. None of
these ever affect `handoff_ready` or `auto_chain_ready`. Verification fails
closed; memory fails open.
"""


LANE_WORKSPACE_README_TEMPLATE = """# {title} Workspace

Declared home for this lane's deliverables (files it produces under its
write_scope). Keep working artifacts here so the lane's outputs are easy to find
and review. This placeholder is safe to overwrite or delete once real
deliverables exist.
"""


# Columns of the agent-lanes.md registry table, in order.
REGISTRY_COLUMNS = ["lane", "thread_id", "role", "write_scope", "worklog", "status", "heartbeat"]


def parse_thread_mapping(values: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"--set-thread must be lane=thread_id, got: {value}")
        lane, thread_id = value.split("=", 1)
        lane = lane.strip()
        thread_id = thread_id.strip()
        if not lane or not thread_id:
            raise SystemExit(f"Invalid --set-thread value: {value}")
        mapping[lane] = thread_id
    return mapping


def parse_extra_lanes(values: list[str]) -> dict[str, dict[str, str]]:
    lanes: dict[str, dict[str, str]] = {}
    for value in values:
        parts = [part.strip() for part in value.split("|")]
        lane = parts[0] if parts else ""
        if not lane:
            raise SystemExit(f"--extra-lane must start with a lane name, got: {value}")
        role = parts[1] if len(parts) > 1 and parts[1] else f"Handle scoped {lane} work and report evidence."
        write_scope = parts[2] if len(parts) > 2 and parts[2] else f"docs/loop/lanes/{lane}/**"
        lanes[lane] = {"role": role, "write_scope": write_scope}
    return lanes


def parse_presets(values: list[str]) -> dict[str, dict[str, str]]:
    lanes: dict[str, dict[str, str]] = {}
    for value in values:
        names = [name.strip() for name in value.split(",") if name.strip()]
        for name in names:
            if name not in LANE_PRESETS:
                valid = ", ".join(sorted(LANE_PRESETS))
                raise SystemExit(f"Unknown --preset {name!r}. Valid presets: {valid}")
            lanes[name] = LANE_PRESETS[name]
    return lanes


def existing_rows(path: Path) -> dict[str, dict[str, str]]:
    """Read existing registry rows.

    Backward compatible with the legacy 6-column table (no heartbeat column).
    Missing trailing cells default to '-' so older registries upgrade cleanly.
    """
    if not path.exists():
        return {}

    rows: dict[str, dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        # Skip the header row regardless of column count.
        if cells[0] == "lane" and cells[1] in {"thread_id", "thread id"}:
            continue
        lane = cells[0]
        if not lane:
            continue
        thread_id = cells[1]
        role = cells[2]
        write_scope = cells[3]
        worklog = cells[4]
        status = cells[5]
        heartbeat = cells[6] if len(cells) >= 7 else "-"
        rows[lane] = {
            "thread_id": thread_id,
            "role": role,
            "write_scope": write_scope,
            "worklog": worklog,
            "status": status,
            "heartbeat": heartbeat or "-",
        }
    return rows


def write_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def title_for(lane: str) -> str:
    return lane.replace("-", " ").replace("_", " ").title()


def posix_path(path: str) -> str:
    return path.replace("\\", "/")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop-dir", default="docs/loop")
    parser.add_argument(
        "--set-thread",
        action="append",
        default=[],
        help="Set a lane thread ID, e.g. --set-thread product=codex:019...",
    )
    parser.add_argument(
        "--extra-lane",
        action="append",
        default=[],
        help=(
            "Add a lane as lane or lane|role|write_scope. "
            "Example: --extra-lane research|Source-backed research|docs/research/**"
        ),
    )
    parser.add_argument(
        "--preset",
        action="append",
        default=[],
        help=(
            "Add common lane presets. Repeat the flag or pass comma-separated names. "
            "Valid: " + ", ".join(sorted(LANE_PRESETS))
        ),
    )
    parser.add_argument(
        "--no-default-lanes",
        action="store_true",
        help=(
            "Omit the default product/implementation/review lanes. The registry is "
            "built from --preset and --extra-lane only. Errors out if that leaves "
            "zero lanes. Use this to build a genuinely different agent shape."
        ),
    )
    args = parser.parse_args()

    loop_dir = Path(args.loop_dir)
    lanes_dir = loop_dir / "lanes"
    messages_dir = loop_dir / "messages"
    evidence_dir = loop_dir / "evidence"
    memory_dir = loop_dir / "memory"
    registry = loop_dir / "agent-lanes.md"
    requests = loop_dir / "requests.md"
    loop_policy = loop_dir / "loop-policy.md"
    thread_mapping = parse_thread_mapping(args.set_thread)

    base_lanes = {} if args.no_default_lanes else dict(DEFAULT_LANES)
    lane_defaults = {
        **base_lanes,
        **parse_presets(args.preset),
        **parse_extra_lanes(args.extra_lane),
    }
    if not lane_defaults:
        raise SystemExit(
            "--no-default-lanes leaves zero lanes; add at least one --preset or "
            "--extra-lane."
        )

    loop_dir.mkdir(parents=True, exist_ok=True)
    lanes_dir.mkdir(parents=True, exist_ok=True)
    messages_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)

    # Loop-engineering state files and loop-control files. These were previously
    # expected from an external skill; create starter templates here so the
    # loop is self-contained. All are write-if-missing.
    loop_state_files = [
        (loop_dir / "goal.md", GOAL_TEMPLATE),
        (loop_dir / "tracker.md", TRACKER_TEMPLATE),
        (loop_dir / "constraints.md", CONSTRAINTS_TEMPLATE),
        (loop_dir / "handoff.md", HANDOFF_TEMPLATE),
        (loop_dir / "loop-budget.md", LOOP_BUDGET_TEMPLATE),
        (loop_dir / "loop-run-log.md", LOOP_RUN_LOG_TEMPLATE),
        (loop_dir / "leases.md", LEASES_TEMPLATE),
        (evidence_dir / "README.md", EVIDENCE_README_TEMPLATE),
        # Decision memory cache (append-only). The jsonl starts empty so writers
        # only ever append; the README documents the one-line schema.
        (memory_dir / "decisions.jsonl", ""),
        (memory_dir / "decisions-README.md", DECISIONS_README_TEMPLATE),
    ]

    wrote: list[Path] = []
    if write_if_missing(requests, REQUESTS_TEMPLATE):
        wrote.append(requests)
    if write_if_missing(loop_policy, LOOP_POLICY_TEMPLATE):
        wrote.append(loop_policy)
    for path, template in loop_state_files:
        if write_if_missing(path, template):
            wrote.append(path)

    rows = existing_rows(registry)
    for lane, defaults in lane_defaults.items():
        worklog = f"{posix_path(args.loop_dir)}/lanes/{lane}/worklog.md"
        rows.setdefault(
            lane,
            {
                "thread_id": "UNVERIFIED",
                "role": defaults["role"],
                "write_scope": defaults["write_scope"],
                "worklog": worklog,
                "status": "needs-thread",
                "heartbeat": "-",
            },
        )
        # Ensure any pre-existing row gains a heartbeat default on upgrade.
        rows[lane].setdefault("heartbeat", "-")
        if lane in thread_mapping:
            rows[lane]["thread_id"] = thread_mapping[lane]
            rows[lane]["status"] = "registered"

    for lane in sorted(rows):
        lane_dir = lanes_dir / lane
        lane_dir.mkdir(parents=True, exist_ok=True)
        title = title_for(lane)
        for filename, template in {
            "worklog.md": WORKLOG_TEMPLATE,
            "inbox.md": INBOX_TEMPLATE,
            "outbox.md": OUTBOX_TEMPLATE,
            "current.md": CURRENT_TEMPLATE,
        }.items():
            path = lane_dir / filename
            if write_if_missing(path, template.format(title=title)):
                wrote.append(path)
        # Declared home for this lane's deliverables. A placeholder README keeps
        # the directory tracked and tells the lane where to put its outputs.
        workspace_dir = lane_dir / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        workspace_readme = workspace_dir / "README.md"
        if write_if_missing(workspace_readme, LANE_WORKSPACE_README_TEMPLATE.format(title=title)):
            wrote.append(workspace_readme)

    lines = [
        "# Agent Lanes",
        "",
        "| " + " | ".join(REGISTRY_COLUMNS) + " |",
        "| " + " | ".join("---" for _ in REGISTRY_COLUMNS) + " |",
    ]
    for lane in sorted(rows):
        row = rows[lane]
        lines.append(
            "| {lane} | {thread_id} | {role} | {write_scope} | {worklog} | {status} | {heartbeat} |".format(
                lane=lane,
                thread_id=row["thread_id"],
                role=row["role"],
                write_scope=row["write_scope"],
                worklog=row["worklog"],
                status=row["status"],
                heartbeat=row.get("heartbeat", "-") or "-",
            )
        )
    registry.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"wrote {registry}")
    if wrote:
        for path in wrote:
            print(f"created {path}")
    print(f"ensured {messages_dir}")
    print(f"ensured {evidence_dir}")
    print(f"ensured {memory_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
