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
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loop_lock import atomic_replace, loop_file_lock


# Default lane write scopes obey the G22 "Proposing Lanes" rules: pairwise
# disjoint, product owns the whole docs/loop/** ledger (plus .gitignore) so its
# close-the-turn commits never fail the guard, and every lane also owns its own
# docs/loop/lanes/<lane>/** dir (product's docs/loop/** already covers its own).
DEFAULT_LANES = {
    "product": {
        "role": "Plan goals, specs, milestones, acceptance criteria, and product judgment.",
        "write_scope": "docs/loop/**; docs/product/**; .gitignore",
    },
    "implementation": {
        "role": "Implement scoped requests, run verification, and report evidence.",
        "write_scope": "src/**; tests/**; docs/loop/lanes/implementation/**",
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
        "write_scope": "docs/user/**; CHANGELOG.md; docs/loop/lanes/docs/**",
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
- status lifecycle: PLANNED -> REQUESTED -> IMPLEMENTING -> IMPLEMENTATION_DONE -> REVIEWING; REVIEWING -> FIX_REQUESTED | ACCEPTED | BLOCKED; FIX_REQUESTED -> IMPLEMENTING; BLOCKED -> FIX_REQUESTED | ABANDONED.
- BLOCKED is a human-gate pause, not a terminal state. It has exactly one legal edge back into work: BLOCKED -> FIX_REQUESTED, carrying a recorded human authorization. The authorizing run-log note is exactly `human_authorization: approved` or starts with `human_authorization: approved | ` followed by an evidence pointer.
- ACCEPTED is the success terminal. ABANDONED is the explicit human-declared dead-end terminal. BLOCKED -> ABANDONED requires a recorded human decision; this terminal disposition is not a resume edge, and ABANDONED rows keep their evidence.
- Only the current owner_lane moves a request forward.
- Increment iteration when a request returns to implementation after review.
- next_action must let any lane resume after compaction or a new session.
- updated_at is ISO-8601 UTC, e.g. 2026-06-23T11:00:00Z.
- loop-run-log.md is the authoritative transition history.
- requests.md is a coarse current-state snapshot at checkpoint granularity.
  PLANNED and IMPLEMENTATION_DONE may never appear in it - that is legal.

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

<!-- dependency_install controls the missing-dependency exit ramp (F16): ask = always ask the human before installing anything (default); auto-pip-only = a lane may auto-install a pip-installable Python package but must still ask before installing a system binary; never = never install, always stay BLOCKED for a human to resolve. Keep the "dependency_install: <value>" line format. -->
dependency_install: ask

## Anti-Thrash Policy

- Cap consecutive FIX_REQUESTED <-> IMPLEMENTING cycles for one request at `max_fix_cycles`.
- When the cap is reached, stop the fix loop and escalate the request to product as BLOCKED.
- Do not reopen an ACCEPTED request without a new request_id.

## Overrides

Overrides are append-only from the first change. Record each state change as a
new line: Active -> Superseded/Completed; older lines are never rewritten in
place. The record format is `override: max_fix_cycles | value: <n> | status:
Active | authorized_at: <UTC timestamp> | decision: <evidence pointer>`. A
non-default `max_fix_cycles` value requires one matching Active record.

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
- Reserved loop infrastructure ports: 8765 (dashboard default; append any later manual dashboard port choice to this line).

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

The `lane` column is the lane that performed the transition - the acting lane,
not the new owner. Human-gate transitions are always recorded by product.

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
model_observed:

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

Each file is a single JSON object with these five required fields and optional
per-command metadata:

```json
{
  "request_id": "REQ-20260623-101500-implementation",
  "checkpoint": "mvp-color-match",
  "command": "npm test",
  "exit_code": 0,
  "ran_at": "2026-06-23T11:00:00Z",
  "started_at": "2026-06-23T10:59:57Z",
  "finished_at": "2026-06-23T11:00:00Z",
  "result": "PASS"
}
```

All five original fields are required. `started_at`, `finished_at`, and
`result` are OPTIONAL; records that omit them remain valid. Record the real
process exit code; never normalize a non-zero exit to `0`. A checkpoint is only
verified when every record for the request reports `exit_code` 0.

The implementing lane first writes the record under
`docs/loop/lanes/<lane>/evidence/`; product mirrors the exact bytes into this
directory. Both copies are byte-for-byte identical. The lane copy preserves
producer lineage and this flat root copy is the completion gate's input.

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


# Columns of the agent-lanes.md registry table, in order. ``tier`` is the
# advisory model-tier column (last, so header-driven readers and any code that
# treats ``heartbeat`` as a fixed position keep working). Its values are the
# ABSTRACT tier words below, never a concrete model name.
REGISTRY_COLUMNS = ["lane", "thread_id", "role", "write_scope", "worklog", "status", "heartbeat", "tier"]

# Per-lane model-tier policy (advisory; the actual tiers are host-specific and
# resolved at runtime from the create_thread tool's own model-parameter
# description). Tiers are expressed ABSTRACTLY here and NEVER as model names:
#   HIGHEST_TIER        -> the highest tier the calling host offers.
#   SECOND_HIGHEST_TIER -> the next tier down (a valid MANUAL downgrade target).
# Policy (G16, supersedes the F8 coding/non-coding split): EVERY lane defaults
# to the highest tier. Run-2/run-3 showed the criteria-authoring and review
# lanes are the quality leverage points, so no lane is auto-downgraded.
# Downgrading is a manual human action: a person may set any lane DOWN to any
# lower tier in the registry ``tier`` cell, and the skill honors that recorded
# tier exactly -- it never silently deviates from it.
HIGHEST_TIER = "highest"
SECOND_HIGHEST_TIER = "second-highest"
REGISTRY_QUARANTINE_PREFIX = "<!-- bootstrap-quarantined-registry-row: "
REGISTRY_QUARANTINE_SUFFIX = " -->"


class RegistryRows(dict[str, dict[str, str]]):
    """Lane rows plus malformed raw rows that must survive normalization."""

    def __init__(self) -> None:
        super().__init__()
        self.malformed_rows: list[tuple[int, str]] = []


class RegistryUnreadableError(ValueError):
    """agent-lanes.md exists but cannot be decoded as UTF-8.

    A corrupt registry must NEVER read as an empty "no lanes" registry (the
    same silent-empty trap the dashboard's duplicate guard just fixed): a
    caller that treated it as empty would rebuild the table and clobber every
    real row. Raised as a ValueError subclass -- not SystemExit -- because
    ``existing_rows`` is also called in-process (the dashboard's add_lane);
    bootstrap's CLI ``main`` converts it to a clean SystemExit.
    """


def recommended_tier_for(lane: str) -> str:
    """Return the advisory tier word for ``lane`` per the G16 policy.

    EVERY lane -- default or custom-named -- defaults to the highest available
    tier (G16 supersedes the old F8 coding/non-coding split, so there is no
    per-lane classification any more). Downgrading is a manual human edit to the
    registry ``tier`` cell, never an automatic decision here. Abstract tier
    words only -- never a model name. The ``lane`` argument is kept for API
    stability (callers pass the lane name).
    """
    return HIGHEST_TIER


def render_registry(rows: dict[str, dict[str, str]]) -> str:
    """Render the full agent-lanes.md text from a lane->row mapping.

    Single source of truth for the registry table layout (columns + cell order),
    so every writer -- bootstrap's template/registration, --set-thread adoption,
    and the dashboard's add_lane -- emits the SAME columns and can never clobber
    a trailing column (like the F8 ``tier``) by rebuilding with a short row.
    Missing cells fall back to sensible defaults; ``tier`` defaults to the
    policy recommendation for the lane so an upgraded legacy row gains one.
    """
    lines = [
        "# Agent Lanes",
        "",
        "| " + " | ".join(REGISTRY_COLUMNS) + " |",
        "| " + " | ".join("---" for _ in REGISTRY_COLUMNS) + " |",
    ]
    for lane in sorted(rows):
        row = rows[lane]
        lines.append(
            "| {lane} | {thread_id} | {role} | {write_scope} | {worklog} | {status} | {heartbeat} | {tier} |".format(
                lane=lane,
                thread_id=row.get("thread_id", "UNVERIFIED"),
                role=row.get("role", ""),
                write_scope=row.get("write_scope", ""),
                worklog=row.get("worklog", ""),
                status=row.get("status", "needs-thread"),
                heartbeat=row.get("heartbeat", "-") or "-",
                tier=(row.get("tier") or "").strip() or recommended_tier_for(lane),
            )
        )
    malformed_rows = getattr(rows, "malformed_rows", [])
    if malformed_rows:
        lines.extend(
            [
                "",
                "## Quarantined malformed registry rows",
                "",
                "Bootstrap preserved these rows verbatim because they had fewer than six cells.",
                "Fix them manually; they are not active lane registrations while quarantined.",
                "",
            ]
        )
        lines.extend(
            REGISTRY_QUARANTINE_PREFIX
            + json.dumps(raw_line, ensure_ascii=True)
            + REGISTRY_QUARANTINE_SUFFIX
            for _line_number, raw_line in malformed_rows
        )
    return "\n".join(lines) + "\n"


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
        # D5: the lane name becomes lanes_dir/<lane>; reject traversal here.
        safe_name(lane, "--set-thread")
        mapping[lane] = thread_id
    return mapping


def parse_observed_models(values: list[str]) -> dict[str, str]:
    """Parse --observed-model lane=<observed model+effort> pairs (G14(a)).

    The value is observed DATA written verbatim into the lane's current.md
    model_observed line, e.g. ``implementation=gpt-5.5 xhigh (highest)``. The
    only split is on the FIRST ``=`` so the value may itself contain spaces and
    parentheses. Empty lane or value is a loud error.
    """
    mapping: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"--observed-model must be lane=<observed>, got: {value}")
        lane, observed = value.split("=", 1)
        lane = lane.strip()
        observed = observed.strip()
        if not lane or not observed:
            raise SystemExit(f"Invalid --observed-model value: {value}")
        mapping[lane] = observed
    return mapping


def stamp_observed_model(current_path: Path, observed: str) -> bool:
    """Write the ``model_observed:`` line in a lane's current.md (G14(a)).

    Rewrites the existing ``model_observed:`` line in place (never adds other
    fields), or inserts one right after the ``heartbeat:`` header line if the
    file predates this template. The value is observed DATA, written verbatim.
    Returns True if the file changed. Best-effort: a missing/unreadable file is a
    no-op (returns False), never an exception.
    """
    if not observed or not current_path.exists():
        return False
    try:
        original = current_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    lines = original.splitlines(keepends=True)
    replacement = "model_observed: " + observed
    out: list[str] = []
    changed = False
    found = False
    for line in lines:
        suffix = "\n" if line.endswith("\n") else ""
        body = line[: -len(suffix)] if suffix else line
        low = body.strip().lower()
        if low.startswith("model_observed:"):
            found = True
            if body != replacement:
                changed = True
            out.append(replacement + suffix)
        else:
            out.append(line)
    if not found:
        # Insert after the heartbeat: header line for older current.md files.
        rebuilt: list[str] = []
        inserted = False
        for line in out:
            rebuilt.append(line)
            if not inserted and line.strip().lower().startswith("heartbeat:"):
                rebuilt.append(replacement + "\n")
                inserted = True
        if inserted:
            out = rebuilt
            changed = True
    if not changed:
        return False
    try:
        current_path.write_text("".join(out), encoding="utf-8")
    except OSError:
        return False
    return True


def parse_extra_lanes(values: list[str]) -> dict[str, dict[str, str]]:
    lanes: dict[str, dict[str, str]] = {}
    for value in values:
        parts = [part.strip() for part in value.split("|")]
        lane = parts[0] if parts else ""
        if not lane:
            raise SystemExit(f"--extra-lane must start with a lane name, got: {value}")
        # D5: the lane name becomes lanes_dir/<lane>; reject traversal here.
        safe_name(lane, "--extra-lane")
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


def existing_rows(path: Path) -> RegistryRows:
    """Read existing registry rows.

    Backward compatible with the legacy 6-column table (no heartbeat column) and
    the 7-column table (no tier column). Missing trailing cells default so older
    registries upgrade cleanly: a missing ``heartbeat`` becomes '-', and a
    missing ``tier`` is left blank here so render_registry fills it with the
    policy recommendation. An EXISTING tier cell is preserved verbatim, so a
    human opt-DOWN survives every round-trip (template rerun, --set-thread
    adoption, dashboard add_lane).
    """
    rows = RegistryRows()
    if not path.exists():
        return rows

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        # Never let a corrupt registry look like "no lanes" -- fail loudly.
        raise RegistryUnreadableError(
            "agent-lanes.md is not valid UTF-8: {0}; re-save the file as "
            "UTF-8".format(posix_path(str(path)))
        ) from exc

    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.startswith(REGISTRY_QUARANTINE_PREFIX) and line.endswith(
            REGISTRY_QUARANTINE_SUFFIX
        ):
            payload = line[
                len(REGISTRY_QUARANTINE_PREFIX) : -len(REGISTRY_QUARANTINE_SUFFIX)
            ]
            try:
                raw_line = json.loads(payload)
            except (TypeError, ValueError):
                raw_line = line
            if not isinstance(raw_line, str):
                raw_line = line
            rows.malformed_rows.append((line_number, raw_line))
            sys.stderr.write(
                "warning: preserving malformed registry row in {0} line {1} "
                "(expected at least 6 cells): {2}\n".format(
                    posix_path(str(path)), line_number, raw_line
                )
            )
            continue
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        # Skip the header row regardless of column count.
        if len(cells) >= 2 and cells[0] == "lane" and cells[1] in {
            "thread_id",
            "thread id",
        }:
            continue
        if len(cells) < 6:
            rows.malformed_rows.append((line_number, line))
            sys.stderr.write(
                "warning: preserving malformed registry row in {0} line {1} "
                "(expected at least 6 cells): {2}\n".format(
                    posix_path(str(path)), line_number, line
                )
            )
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
        # tier is the 8th cell when present; blank means "not yet set" so the
        # renderer supplies the policy default without overriding an opt-down.
        tier = cells[7] if len(cells) >= 8 else ""
        rows[lane] = {
            "thread_id": thread_id,
            "role": role,
            "write_scope": write_scope,
            "worklog": worklog,
            "status": status,
            "heartbeat": heartbeat or "-",
            "tier": tier,
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


# D5 path-traversal guard: a lane name becomes a single path segment under
# docs/loop/lanes/. The leading character class rejects empty values and
# dot-leading segments ('.', '..', hidden files); the body class rejects '/',
# '\\', ':', NUL, and every other separator or metacharacter. Every
# DEFAULT_LANES and LANE_PRESETS name matches this pattern.
# fullmatch (not match) so a trailing newline cannot ride along; trailing
# dots/spaces and DOS device names are rejected separately because Windows
# aliases them onto other paths ('lane.' resolves to 'lane'; NUL/COM1 are
# devices regardless of extension). Keep in sync with deliver_message.py.
SAFE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

_DOS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {"com{0}".format(i) for i in range(1, 10)}
    | {"lpt{0}".format(i) for i in range(1, 10)}
)


def is_safe_name(value: str) -> bool:
    """True when ``value`` is a safe single path segment on POSIX and Windows."""

    if not SAFE_NAME_RE.fullmatch(value):
        return False
    if value.endswith(".") or value.endswith(" "):
        return False
    if value.split(".", 1)[0].lower() in _DOS_RESERVED_NAMES:
        return False
    return True


def safe_name(value: str, flag: str) -> str:
    """Validate ``value`` as a safe single path segment; exit loudly otherwise."""

    if not is_safe_name(value):
        raise SystemExit(
            "{0} value {1!r} is not a safe name: use only letters, digits, "
            "'.', '_' and '-', starting with a letter or digit (no path "
            "separators, no leading/trailing dot, no DOS device names).".format(
                flag, value
            )
        )
    return value


def assert_within(base: Path, child: Path) -> None:
    """Belt-and-suspenders containment check: ``child`` must resolve under ``base``."""

    base_resolved = str(base.resolve())
    child_resolved = str(child.resolve())
    try:
        contained = os.path.commonpath([base_resolved, child_resolved]) == base_resolved
    except ValueError:
        # Different drives / mixed absolute-relative on Windows: not contained.
        contained = False
    if not contained:
        raise SystemExit(
            "refusing to write outside {0}: {1}".format(
                posix_path(base_resolved), posix_path(child_resolved)
            )
        )


# --- G22 write-scope overlap advisory (registration-time, advisory only) ------
# These mirror the authoritative normalization in multi_agent_loop_doctor.py
# (``_normalize_scope_entry`` / ``_scope_entries_overlap`` /
# ``_substantive_scope_entries``). They are re-implemented here so bootstrap
# stays self-contained (stdlib-only, no cross-script import); the smoke exercises
# both sides so a divergence would be caught. A lane's OWN
# docs/loop/lanes/<lane>/** dir is exempt (the by-design nesting under product's
# docs/loop/** ledger). This is ADVISORY: it only prints; registration proceeds.
_LEDGER_LANE_PREFIX = "docs/loop/lanes/"


def _scope_posix(value: str) -> str:
    return value.replace("\\", "/").strip()


def _looks_like_glob(token: str) -> bool:
    if any(ch in token for ch in "*?[]"):
        return True
    if "/" in token:
        return True
    if "." in token and " " not in token:
        return True
    return False


def _split_scope_globs(write_scope: str) -> list:
    globs = []
    for raw in (write_scope or "").split(";"):
        token = _scope_posix(raw)
        if token and _looks_like_glob(token):
            globs.append(token)
    return globs


def _normalize_scope_entry(glob: str):
    token = _scope_posix(glob)
    if not token:
        return None
    if token in ("**", "*"):
        return ("prefix", "")
    if token.endswith("/**"):
        return ("prefix", token[:-2])
    if token.endswith("/*"):
        return ("prefix", token[:-1])
    if token.endswith("/"):
        return ("prefix", token)
    if any(ch in token for ch in "*?["):
        first = min(token.find(ch) for ch in "*?[" if ch in token)
        head = token[:first]
        slash = head.rfind("/")
        return ("prefix", head[: slash + 1] if slash >= 0 else "")
    return ("file", token)


def _scope_entries_overlap(e1, e2) -> bool:
    k1, v1 = e1
    k2, v2 = e2
    if k1 == "prefix" and k2 == "prefix":
        return v1 == v2 or v1.startswith(v2) or v2.startswith(v1)
    if k1 == "prefix":
        return v2 == v1.rstrip("/") or v2.startswith(v1)
    if k2 == "prefix":
        return v1 == v2.rstrip("/") or v1.startswith(v2)
    return v1 == v2


def _substantive_scope_entries(lane: str, write_scope: str) -> list:
    own_prefix = _LEDGER_LANE_PREFIX + (lane or "").strip() + "/"
    entries = []
    for original in _split_scope_globs(write_scope):
        norm = _normalize_scope_entry(original)
        if norm is None:
            continue
        _, value = norm
        if value == own_prefix or value.startswith(own_prefix):
            continue
        entries.append((original, norm))
    return entries


def scope_overlap_advisories(rows: dict, new_lanes: list) -> list:
    """Advisory strings for NEW lanes whose scopes collide with an existing row.

    Only compares each newly registered lane against the other rows (a
    pre-existing overlap between two old rows is not re-announced). Deterministic
    order; each colliding pair reported once.
    """
    entries_by_lane = {
        lane: _substantive_scope_entries(lane, (row.get("write_scope", "") or ""))
        for lane, row in rows.items()
    }
    advisories = []
    seen = set()
    for new_lane in new_lanes:
        if new_lane not in entries_by_lane:
            continue
        for other in sorted(entries_by_lane):
            if other == new_lane:
                continue
            for glob_a, norm_a in entries_by_lane[new_lane]:
                for glob_b, norm_b in entries_by_lane[other]:
                    if not _scope_entries_overlap(norm_a, norm_b):
                        continue
                    pair = tuple(sorted((new_lane, other)))
                    key = (pair, tuple(sorted((glob_a, glob_b))))
                    if key in seen:
                        continue
                    seen.add(key)
                    advisories.append(
                        "advisory: lane {a!r} write_scope overlaps lane {b!r}: "
                        "{ga} vs {gb} -- the precommit scope guard cannot arbitrate "
                        "between them; cut them into disjoint subtrees".format(
                            a=new_lane, b=other, ga=glob_a, gb=glob_b
                        )
                    )
    return advisories


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop-dir", default="docs/loop")
    parser.add_argument(
        "--set-thread",
        action="append",
        default=[],
        help=(
            "Set a lane thread ID, e.g. --set-thread product=codex:019... "
            "The lane must already have a registry row on disk (or be "
            "registered by this same invocation); adoption fills the EXISTING "
            "row in place and never creates a new one. Naming a lane with no "
            "row is a loud error, not a silent no-op."
        ),
    )
    parser.add_argument(
        "--observed-model",
        action="append",
        default=[],
        help=(
            "G14(a): stamp a lane's OBSERVED model+effort into its current.md "
            "model_observed line at adoption, e.g. --observed-model "
            "'implementation=gpt-5.5 xhigh (highest)'. This is observed DATA, not "
            "policy: the value is written verbatim (the '(highest)'/'(second-highest)' "
            "tier tag lets the doctor compare it to the registry tier). Optional; "
            "the lane can also fill the line itself."
        ),
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
    observed_models = parse_observed_models(args.observed_model)

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

    # F11 completion: --set-thread adoption fills an EXISTING registry row. The
    # on-disk registry is the source of truth for which lanes exist, so a lane
    # already on disk is adoptable even when this invocation's lane_defaults do
    # not mention it (the real dogfood case: a custom lane adopted in a later,
    # flagless run). A lane with NO row on disk and NOT registered by this
    # invocation is an error: fail loudly BEFORE any writes -- a silently
    # ignored adoption line is worse than no ritual, and creating a fresh row
    # here would mint exactly the duplicate the adoption ritual exists to
    # prevent.
    if thread_mapping:
        try:
            known_lanes = set(existing_rows(registry)) | set(lane_defaults)
        except RegistryUnreadableError as exc:
            raise SystemExit(str(exc))
        unknown = sorted(set(thread_mapping) - known_lanes)
        if unknown:
            raise SystemExit(
                "--set-thread names lane(s) with no registry row: {0}. "
                "Known lanes: {1}. Fix the lane name, or register the lane "
                "first (--extra-lane/--preset); adoption fills an EXISTING "
                "row and never creates one.".format(
                    ", ".join(unknown),
                    ", ".join(sorted(known_lanes)) or "(none)",
                )
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
    stamped_observed: list[str] = []
    if write_if_missing(requests, REQUESTS_TEMPLATE):
        wrote.append(requests)
    if write_if_missing(loop_policy, LOOP_POLICY_TEMPLATE):
        wrote.append(loop_policy)
    for path, template in loop_state_files:
        if write_if_missing(path, template):
            wrote.append(path)

    # Serialize the registry read-modify-write across concurrent writers
    # (another bootstrap, or deliver_message's heartbeat stamp). Re-read the
    # on-disk rows INSIDE the lock so a racing writer's rows are merged, not
    # clobbered, and replace the file atomically so no reader sees a torn write.
    with loop_file_lock(registry.parent, "registry"):
        try:
            rows = existing_rows(registry)
        except RegistryUnreadableError as exc:
            raise SystemExit(str(exc))
        # G22: which lanes are NEW this run (absent from the on-disk registry).
        # Only these get an overlap advisory below; a pre-existing overlap is not
        # re-announced on every flagless rerun.
        preexisting_lanes = set(rows)
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
                    # F8 advisory tier: policy default for a brand-new lane.
                    "tier": recommended_tier_for(lane),
                },
            )
            # Ensure any pre-existing row gains a heartbeat default on upgrade.
            rows[lane].setdefault("heartbeat", "-")
            # Ensure any pre-existing (legacy) row gains a tier on upgrade WITHOUT
            # overriding a human opt-down: only fill it when it is missing or blank.
            if not (rows[lane].get("tier") or "").strip():
                rows[lane]["tier"] = recommended_tier_for(lane)

        # Apply --set-thread adoption over the FULL row set -- existing on-disk
        # rows included -- not just this invocation's lane_defaults (F11
        # completion: the documented adoption one-liner must fill a custom lane's
        # EXISTING row even in a later, flagless run). Every lane here passed the
        # validation above, so rows[lane] is guaranteed to exist: flip thread_id
        # and status in place; tier and every other cell are preserved.
        for lane, thread_id in thread_mapping.items():
            rows[lane]["thread_id"] = thread_id
            rows[lane]["status"] = "registered"

        # D5: rows may include lanes loaded from an on-disk registry, and
        # containment alone accepts an in-base alias like 'x/../victim'. Every
        # lane name must be a plain segment BEFORE it is rewritten into the
        # registry or used to create directories below -- a corrupt or hostile
        # registry fails closed here with an actionable message.
        for lane in rows:
            safe_name(lane, "registry lane")

        atomic_replace(registry, render_registry(rows))

    for lane in sorted(rows):
        lane_dir = lanes_dir / lane
        # D5 belt-and-suspenders: rows may come from an on-disk registry, so
        # re-check containment even though CLI lane names were validated above.
        assert_within(lanes_dir, lane_dir)
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
        # G14(a): stamp the OBSERVED model+effort into current.md at adoption
        # when provided. Written verbatim (observed data, not policy).
        if lane in observed_models:
            if stamp_observed_model(lane_dir / "current.md", observed_models[lane]):
                stamped_observed.append(lane)

    print(f"wrote {registry}")
    if wrote:
        for path in wrote:
            print(f"created {path}")
    print(f"ensured {messages_dir}")
    print(f"ensured {evidence_dir}")
    print(f"ensured {memory_dir}")
    for lane in stamped_observed:
        print(f"stamped model_observed for {lane}")
    # G22: advisory overlap warning at registration time. If a newly registered
    # lane's write_scope collides with an existing row's scope, print an advisory
    # line (no behavior change -- registration already succeeded above). The doctor
    # is the authoritative, always-on check; this print catches the collision the
    # moment a human runs --extra-lane, before a colliding team is even proposed.
    new_lanes = [lane for lane in lane_defaults if lane not in preexisting_lanes]
    for advisory in scope_overlap_advisories(rows, new_lanes):
        print(advisory)
    if thread_mapping:
        # Thread-title guidance (F1): title each Codex thread with the BARE lane
        # name only. Do not prefix a project name or a "loop lane:" boilerplate
        # (threads came out "<project> loop lane: review"); the bare lane name is
        # what the loop refers to. Project context, if needed, lives in the
        # dashboard project name, not the thread title.
        for lane in sorted(thread_mapping):
            print(
                "next: set_thread_title({thread!r}, {lane!r})  # bare lane name, no project/'loop lane:' prefix".format(
                    thread=thread_mapping[lane], lane=lane
                )
            )
            # F8 tier hint: print the advisory recommended tier alongside the
            # create_thread/set_thread_title hints (abstract tier word, never a
            # model name). When the host's create_thread accepts a model param,
            # pass this tier's resolved model; otherwise the human picks this
            # tier by hand when opening the thread.
            print(
                "  tier: {tier} available  # advisory: pass model={tier}-available tier to create_thread, or pick it by hand".format(
                    tier=(rows.get(lane, {}).get("tier") or recommended_tier_for(lane)).strip()
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
