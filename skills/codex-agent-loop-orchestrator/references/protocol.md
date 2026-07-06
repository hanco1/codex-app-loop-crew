# Cross-Agent Protocol

This file is the reference implementation of the plane-separation and verification-gate invariants (`references/methodology.md`): the message envelope, evidence records, and deterministic gate below are one concrete shape of those invariants. The shape is swappable; the discipline is not.

Use this reference when an agent records, sends, receives, or reviews work from another Codex thread.

## Request ID

Use a stable request ID:

```text
REQ-YYYYMMDD-HHMMSS-<lane>
```

Reuse the same `request_id` across fix cycles. Increment `iteration` when a request returns to implementation after review or product feedback.

## Request Lifecycle

```text
PLANNED -> REQUESTED -> IMPLEMENTING -> IMPLEMENTATION_DONE -> REVIEWING -> FIX_REQUESTED -> ACCEPTED | BLOCKED
```

Rules:

- Only product or the assigned coordinator should create a new request.
- Only the current owner lane should move a request forward.
- Review failures keep the same `request_id` and move to `FIX_REQUESTED`.
- Product makes final tracker completion decisions unless the loop files delegate that authority.
- Terminal states are `ACCEPTED` and `BLOCKED`.

## Deterministic Completion Gate

A request may move to `ACCEPTED` (and auto-chain may proceed) only after a
machine check confirms every verification command for that request exited `0`.
Completion is never asserted from a self-report such as "tests passed" or
"verification looks good". If verification could not run, the request stays
`BLOCKED`, not `ACCEPTED-with-caveat`.

### Evidence Records

The implementation lane writes one evidence file per verification command it
runs, under `docs/loop/evidence/`. Each file is a JSON object:

```json
{
  "request_id": "REQ-20260623-101500-implementation",
  "checkpoint": "mvp-color-match",
  "command": "npm test",
  "exit_code": 0,
  "ran_at": "2026-06-23T11:00:00Z"
}
```

All five fields are required. Record the real process exit code; do not
normalize a non-zero exit to `0`. Use one file per command (for example
`evidence/REQ-20260623-101500-implementation-iter-1-npm-test.json`) so each
checkpoint command is independently auditable and files never collide.

Keep every evidence file as a flat, non-nested `.json` directly under
`docs/loop/evidence/`: the gate collects records with a non-recursive
`glob('*.json')`, so a file in a subdirectory or with any other extension is
invisible to it and does not count.

### Running The Gate

Before product or review marks a request `ACCEPTED`, run the gate:

```bash
python <skill_dir>/scripts/completion_gate.py --loop-dir docs/loop --request-id REQ-20260623-101500-implementation
```

- The gate prints `SHIP_CHECK_OK <request_id>` and exits `0` only when every
  evidence record for that request reports `exit_code == 0`.
- Otherwise it prints `SHIP_CHECK_FAIL`, lists the failing or malformed records,
  and exits non-zero.

The gate fails closed: an unreadable or malformed evidence file, a record whose
`exit_code` is missing or not a clean integer, no evidence found for the
requested id, or an empty `docs/loop/evidence/` all produce `SHIP_CHECK_FAIL`.
Omit `--request-id` to evaluate every request found; in that mode the gate
fails if any single request fails. Add `--json` for a structured report
(`passing`, `failing`, `load_errors`, `reasons`).

### Gate Rule

```text
ACCEPTED and auto-chain require: completion_gate.py prints SHIP_CHECK_OK <request_id> and exits 0.
```

When the gate prints `SHIP_CHECK_FAIL`, treat the request as not done:

- if a checkpoint command exited non-zero, send `FIX_REQUEST` with the failing
  record(s) as evidence and keep the same `request_id` with an incremented
  `iteration`;
- if verification could not run at all (no evidence records, or only malformed
  ones), send `BLOCKED` to product rather than accepting with a caveat.

Record the gate result (the token line and exit code) in the review or product
worklog and in the `evidence`/`verification` field of the `REVIEW_DONE` or
acceptance message, so acceptance is traceable to a concrete machine check
rather than to a claim.

## Anti-Thrash

A request must not bounce between fix and implement forever. Count completed `FIX_REQUESTED` -> `IMPLEMENTING` rounds for one `request_id` from the run log.

Core rule:

```text
fix cycles >= max_fix_cycles -> escalate to BLOCKED + human, do not re-request
```

- `max_fix_cycles` comes from `loop-policy.md` (default 3).
- On the round that would exceed the cap, move the request to `BLOCKED`, append the transition to the run log with a note naming the recurring failed criterion, and send `BLOCKED` to product.
- Do not silently raise the cap or rewrite acceptance criteria to force a pass. A human decides whether to re-scope, raise the cap, or abandon the request.

## requests.md Schema

`docs/loop/requests.md` is the durable queue and recovery index.

```md
| request_id | status | owner_lane | iteration | source_docs | last_message | next_action | updated_at |
| --- | --- | --- | --- | --- | --- | --- | --- |
```

Use `owner_lane` to prevent concurrent writers from claiming the same request. Use `next_action` to make recovery possible after context compaction or a new session.

## Message Storage

Save every outbound message before or immediately after delivery:

```text
docs/loop/messages/<request_id>/<message_type>-iter-<n>.md
```

Also append a one-line summary to:

- sender `lanes/<lane>/outbox.md`
- target `lanes/<lane>/inbox.md` when delivery is pending or tool delivery is unavailable
- sender `lanes/<lane>/worklog.md`

## Atomic Message Delivery

Cross-agent messages are delivered into a per-lane Maildir-style inbox so a
concurrent reader never sees a torn or partial message. Use
`scripts/deliver_message.py` rather than appending directly to a shared file.

### Inbox layout

```text
docs/loop/lanes/<lane>/inbox/
  tmp/   staging area for in-flight writes (never read by consumers)
  new/   fully written, undelivered-to-reader messages
  cur/   messages the reader has already picked up
  index.md  append-only delivery log (one row per message)
```

Each message is one file named `<message_id>.md`, where the id is derived
deterministically from `request_id`, `message_type`, and `iteration`
(`<request_id>--<MESSAGE_TYPE>--iter-<n>`). The file body is a normal message
envelope as defined below.

### tmp -> new rename contract

Delivery is a strict two-step sequence:

1. The full message body is written to `inbox/tmp/<unique>.tmp`, flushed, and
   fsynced.
2. The temp file is atomically renamed (`os.replace`) to
   `inbox/new/<message_id>.md`. `os.replace` is atomic on the same filesystem
   on both POSIX and Windows, so the final name appears only once the entire
   message is durably on disk.

Because the final path is published by an atomic rename, a reader scanning
`inbox/new` either does not see the file yet or sees it complete -- never a
half-written file. Writers must never create or edit a file directly inside
`new`; always stage in `tmp` and rename.

### Reader contract

A consumer (the receiving lane):

1. Lists `inbox/new`, sorting by name for deterministic order.
2. Processes each message file (reads the envelope, acts on it, updates
   `requests.md` / `current.md` / `worklog.md`).
3. Moves the processed file from `inbox/new` to `inbox/cur` (a rename within
   the same inbox). A file in `cur` is "already seen"; a file in `new` is
   "pending".

Delivery is idempotent: if a message id already exists in `new` or `cur`,
re-running the deliver helper is a no-op (use `--force` only to intentionally
republish, which writes a fresh `new/<id>.md` and appends another index row).
Readers should treat a message already in `cur` as handled and not reprocess
it.

### index.md

Every delivery appends one row to `inbox/index.md`:

```md
| delivered_at | message_id | request_id | iteration | from_lane | message_type | state |
| --- | --- | --- | --- | --- | --- | --- |
```

The index is append-only and is for recovery/audit. It is not the source of
truth for pending work -- the presence of a file in `inbox/new` is. `state`
is written as `new` at delivery time; the reader does not have to rewrite the
index when it moves a file to `cur`.

### Delivering a message

```bash
python <skill_dir>/scripts/deliver_message.py \
  --loop-dir docs/loop \
  --to-lane implementation \
  --from-lane product \
  --request-id REQ-20260624-101500-implementation \
  --message-type IMPLEMENTATION_REQUEST \
  --iteration 1 \
  --message-file docs/loop/messages/REQ-20260624-101500-implementation/IMPLEMENTATION_REQUEST-iter-1.md
```

The body may also be piped on stdin (omit `--message-file` or pass `-`). This
is the same envelope you save under `docs/loop/messages/<request_id>/`; deliver
the saved copy so the durable message store and the inbox stay consistent.

### Migration from the flat inbox.md fallback

The original fallback appended a one-line summary row to a single
`docs/loop/lanes/<lane>/inbox.md` table. That flat file is still valid as a
low-fidelity, human-readable fallback when no script runtime is available, but
it is not torn-write safe and stores only a summary, not the full message.

The `inbox/` Maildir tree is an upgrade, not a replacement of the protocol:

- Prefer `deliver_message.py` (full message body, atomic, idempotent) when a
  Python runtime is available.
- When falling back to a single line in `inbox.md`, keep doing so -- a lane may
  legitimately have both `inbox.md` (legacy summary rows) and `inbox/`
  (atomic full messages) during migration.
- A lane is fully migrated once its consumers read from `inbox/new` and move to
  `inbox/cur`; at that point `inbox.md` becomes an optional human log only.
- Do not delete `inbox.md` automatically; leave existing summary rows in place
  so recovery from older sessions still works.

## Append-Only Run Log

Every lifecycle transition appends one row to `docs/loop/loop-run-log.md`. Never edit or delete prior rows; the log is the audit trail for recovery and anti-thrash counting.

```md
| timestamp | request_id | iteration | from_status | to_status | lane | note |
| --- | --- | --- | --- | --- | --- | --- |
```

Rules:

- Append a row whenever a request changes status in `requests.md`, including `BLOCKED` and `ACCEPTED`.
- `timestamp` is UTC ISO 8601 (`YYYY-MM-DDTHH:MM:SSZ`); `iteration` is the request's current fix iteration; `lane` is the lane that made the transition; `note` is a short reason or evidence pointer.
- Append the row in the same step that updates `requests.md`, so the log and the queue never diverge.
- Count FIX_REQUESTED <-> IMPLEMENTING transitions for the same `request_id` from this log when applying the Anti-Thrash rule.

## Decision Log

The optional memory cache is an append-only decision log at
`docs/loop/memory/decisions.jsonl`, written one line per checkpoint decision by
`scripts/record_decision.py`. It is a cache over the source files, never a
source of truth; drift is a doctor WARNING and never blocks handoff. See
`references/memory.md` for the full schema, the single canonical
`normalize_then_hash` contract, and the Memory Protocol.

Supersession is expressed by appending a new line whose `supersedes` names the
old `decision_id` - never by editing a prior line. The `supersedes` field draws
from a reserved relation vocabulary of seven verbs, of which only `supersedes`
is implemented today (`superseded_by` is its read-back):

```text
supersedes, superseded_by, conflicts_with, related, compatible, scoped, not_conflict
```

The remaining verbs are reserved values so the schema can grow with zero
migration; do not build a conflict-detection engine on top of them.

## Common Envelope

Every message uses this envelope before message-specific fields:

```md
# <MESSAGE_TYPE>

message_type: <MESSAGE_TYPE>
request_id: REQ-YYYYMMDD-HHMMSS-<lane>
parent_request_id:
iteration: 1
from_lane: product
to_lane: implementation
status: REQUESTED
created_at: YYYY-MM-DDTHH:MM:SSZ
source_docs:
- docs/loop/goal.md
- docs/loop/tracker.md
delivery:
- channel: send_message_to_thread | lane_inbox | manual
- target_thread_id:
- delivery_status: pending | sent | failed | stale
- sent_at:
```

## IMPLEMENTATION_REQUEST

```md
# IMPLEMENTATION_REQUEST

message_type: IMPLEMENTATION_REQUEST
request_id: REQ-20260623-101500-implementation
parent_request_id:
iteration: 1
from_lane: product
to_lane: implementation
status: REQUESTED
created_at: 2026-06-23T10:15:00Z
source_docs:
- docs/loop/goal.md
- docs/loop/tracker.md
- docs/product/specs/mvp-color-match.md
goal: Implement the MVP color matching flow from the approved spec.
scope:
- src/**
- tests/**
non_goals:
- Do not redesign the UI shell.
- Do not add authentication.
acceptance_criteria:
- User can choose foundation and lipstick shades.
- App returns three compatible color recommendations.
- Existing tests pass and one regression test covers the recommendation rule.
expected_reply:
- changed_files
- verification commands and results
- blockers, if any
```

## IMPLEMENTATION_DONE

```md
# IMPLEMENTATION_DONE

message_type: IMPLEMENTATION_DONE
request_id: REQ-20260623-101500-implementation
parent_request_id:
iteration: 1
from_lane: implementation
to_lane: product
status: IMPLEMENTATION_DONE
created_at: 2026-06-23T11:00:00Z
source_docs:
- docs/loop/requests.md
- docs/product/specs/mvp-color-match.md
changed_files:
- src/recommend.ts
- tests/recommend.test.ts
verification:
- npm test: passed
notes:
- Implemented the rule-based MVP only.
needs_review_by: review
expected_reply:
- REVIEW_REQUEST or ACCEPTANCE_DECISION
```

## REVIEW_REQUEST

```md
# REVIEW_REQUEST

message_type: REVIEW_REQUEST
request_id: REQ-20260623-101500-implementation
parent_request_id:
iteration: 1
from_lane: product
to_lane: review
status: REVIEWING
created_at: 2026-06-23T11:10:00Z
source_docs:
- docs/loop/tracker.md
- docs/product/specs/mvp-color-match.md
artifact_scope:
- src/recommend.ts
- tests/recommend.test.ts
acceptance_criteria:
- <copy exact criteria>
expected_reply:
- pass/fail per criterion
- evidence
- REVIEW_DONE or FIX_REQUEST
```

## REVIEW_DONE

```md
# REVIEW_DONE

message_type: REVIEW_DONE
request_id: REQ-20260623-101500-implementation
parent_request_id:
iteration: 1
from_lane: review
to_lane: product
status: ACCEPTED
created_at: 2026-06-23T11:30:00Z
source_docs:
- docs/loop/tracker.md
review_result: pass
criteria_results:
- Criterion 1: pass, verified by tests/recommend.test.ts
- Criterion 2: pass, verified by manual code review
evidence:
- npm test: passed
remaining_risks:
- None known.
expected_reply:
- Product marks the tracker checkpoint complete or sends ACCEPTANCE_DECISION.
```

## FIX_REQUEST

```md
# FIX_REQUEST

message_type: FIX_REQUEST
request_id: REQ-20260623-101500-implementation
parent_request_id:
iteration: 2
from_lane: review
to_lane: implementation
status: FIX_REQUESTED
created_at: 2026-06-23T11:35:00Z
source_docs:
- docs/product/specs/mvp-color-match.md
failed_criteria:
- Criterion 2 failed: recommendations do not include undertone compatibility.
evidence:
- tests/recommend.test.ts lacks undertone coverage.
requested_fix:
- Add undertone compatibility to the recommendation rule and cover it with tests.
expected_reply:
- changed_files
- verification commands and results
- blockers, if any
```

## BLOCKED

```md
# BLOCKED

message_type: BLOCKED
request_id: REQ-20260623-101500-implementation
parent_request_id:
iteration: 1
from_lane: implementation
to_lane: product
status: BLOCKED
created_at: 2026-06-23T11:20:00Z
source_docs:
- docs/loop/constraints.md
blocker:
- Missing API key for the production color catalog.
needed_from_human:
- Confirm whether to use a local mock catalog for MVP.
expected_reply:
- Product updates scope, provides input, or marks request blocked.
```

## LOOP_STATUS

```md
# LOOP_STATUS

message_type: LOOP_STATUS
request_id: REQ-20260623-101500-implementation
parent_request_id:
iteration: 1
from_lane: implementation
to_lane: product
status: IMPLEMENTING
created_at: 2026-06-23T10:45:00Z
source_docs:
- docs/loop/requests.md
current_state:
- Implementing src/recommend.ts.
last_verification:
- Not run yet.
next_action:
- Finish test coverage, run npm test, send IMPLEMENTATION_DONE.
blockers:
- None.
```
