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
PLANNED -> REQUESTED -> IMPLEMENTING -> IMPLEMENTATION_DONE -> REVIEWING
REVIEWING -> FIX_REQUESTED | ACCEPTED | BLOCKED
FIX_REQUESTED -> IMPLEMENTING
BLOCKED -> FIX_REQUESTED | ABANDONED
```

Rules:

- Only product or the assigned coordinator should create a new request.
- Only the current owner lane should move a request forward.
- Review failures keep the same `request_id` and move to `FIX_REQUESTED`.
- Product makes final tracker completion decisions unless the loop files delegate that authority.
- **BLOCKED is a human-gate pause, not a terminal state.** It has exactly one
  legal edge back into work: `BLOCKED -> FIX_REQUESTED`, carrying a recorded
  human authorization in the run log. The authorizing row's note is exactly
  `human_authorization: approved` or starts with
  `human_authorization: approved | ` followed by an evidence pointer. No lane
  may resume directly into `REQUESTED`, `IMPLEMENTING`, or `REVIEWING`.
- **ACCEPTED is the success terminal.** `ABANDONED` is the explicit
  human-declared dead-end terminal. A request may move `BLOCKED -> ABANDONED`
  only on a recorded human decision; this terminal disposition is not a resume
  edge, and `ABANDONED` rows keep their evidence.

### State surfaces and run completion

**loop-run-log.md is the authoritative transition history.** **requests.md is a
coarse current-state snapshot at checkpoint granularity;** intermediate states
such as `PLANNED` and `IMPLEMENTATION_DONE` may never appear in it - that is
legal. Reconstruct ordering and lifecycle edges from the append-only run log,
not from historical copies of the snapshot.

When every request is terminal and the goal's Done-When holds, product appends
this exact run-completion marker:

```md
| <ts> | - | - | RUN_COMPLETE | RUN_COMPLETE | product | every request is terminal; goal Done-When holds |
```

The marker uses `request_id` `-`, `iteration` `-`, both status cells
`RUN_COMPLETE`, acting lane `product`, and the note shown above. Dashboards and
audits may key off this row; absence means the files do not yet assert that the
whole run is complete.

### Human-QA gate for user-facing slices

A request whose deliverable is **human-operated** does not reach `ACCEPTED` on
machine evidence alone. Machine evidence is still required FIRST (the completion
gate and independent review are unchanged); the human sign-off is an ADDITIVE
step layered on top, and only for user-facing slices.

**Marking a request user-facing.** A request is user-facing when its
`IMPLEMENTATION_REQUEST` envelope carries `user_facing: true` (mirrored from a
`user_facing: true` marker on the goal/tracker checkpoint it serves). This one
line - not a new status token, not a new schema column - is the whole marker. A
UI slice, a dashboard, an interactive CLI, anything a person operates by hand is
user-facing; a pure library or internal data-core slice is not.

**The gate (after `REVIEW_DONE`, before `ACCEPTED`).** For a user-facing
request, once review passes:

1. Product archives and sends one `HUMAN_QA_REQUEST` using the fixed template
   below. It names the exact artifact or entry point, includes fresh SELF-PROBE
   evidence for the first requested steps, and spends the human only on the
   judgment that automation cannot make.
2. The request **HOLDS**: it stays at `REVIEWING` (the existing token - do NOT
   invent a new status), its `next_action` cell reads
   `awaiting human sign-off: <try>`, and a run-log row records the ask:

   ```md
   | <ts> | <request_id> | <iter> | REVIEWING | REVIEWING | product | human_qa_hold: <try> |
   ```

   The note starts with `human_qa_hold:` so stall detection and audits can grep
   the gate without interpreting prose. The tracker checkpoint stays `[~]` (in
   progress), never `[x]`, while it holds.
3. When the human confirms, product records the sign-off as a run-log row BEFORE
   the `ACCEPTED` transition:

   ```md
   | <ts> | <request_id> | <iter> | REVIEWING | REVIEWING | product | human_qa: confirmed <who/when> |
   ```

   Only then does product move the request to `ACCEPTED`, append the acceptance
   run-log row, and mark the tracker checkpoint `[x]`.

The hold is **normal waiting, not a stall**: the doctor detects the
`human_qa_hold:` run-log row without a matching `human_qa: confirmed` row for the
same `request_id` and suppresses `stalled_handoff` for it (a held request is
your-turn for the HUMAN, not a lane to nudge). Older free-text
`human_qa_requested:` notes remain recognized as an additive compatibility
path. See `references/loop-state.md` "Human-QA Gate" and the doctor's
`stalled_handoff` exclusion.

### HUMAN_QA_REQUEST (fixed durable template)

Each human-QA round has its own durable message file under
`docs/loop/messages/<request_id>/`, archived before send and addressed with
`to_lane: human`. The requirement is one file per round, including repeat
rounds for the same request. The doctor recognizes both the structured
`human_qa_hold:` token and legacy `human_qa_requested:` prose in the run log; a
hold with no matching durable human-addressed file emits the warning
`human_qa_message_missing`.

**SELF-PROBE invariant (the rule for every artifact type): never advertise an
entry point to the human that you have not just proven live yourself.** Before
sending, the sender executes, against the exact artifact or entry point being
advertised, the first steps the human is being asked to take and pastes the REAL
evidence into the durable message. This is project-agnostic: "entry point" may
be a command, import, file, package, executable, local service, rendered
artifact, or another operator-visible starting point.

Worked examples below are examples, not the rule:

- **Served-app example:** probe the exact advertised URL and paste its HTTP
  status plus page title; probe one app-identifying endpoint (for example a
  session or health route) and paste its status; record the listener PID; then
  prove exclusive bind by showing a second bind attempt fails. A generic 200
  from some process on the port is not proof that the advertised app is live.
- **CLI-deliverable example:** run the advertised command verbatim and paste its
  exit code plus the first output lines that identify the expected program and
  operation.
- **File/library-deliverable example:** open, validate, or import the exact
  advertised path and paste the result, including the validator/importer used
  and whether it resolved the expected artifact.

**Illustrative run-5 example (not the rule):** the first QA ask advertised a URL
that served the loop dashboard instead of the application after a silent
co-bind, and the resulting hold lost ~10h. A self-probe of the exact URL, its
page title, and an app-identifying route would have exposed the mismatch before
the human was asked.

#### What only you can judge

Name the exact visual or interaction surface where human perception is the
acceptance oracle - for example charts, layout, focus behavior, legibility, or
interaction feel. Never spend the human on machine-verifiable facts; settle
those in SELF-PROBE and gate evidence before sending.

The fixed five-element template is:

```md
# HUMAN_QA_REQUEST

message_type: HUMAN_QA_REQUEST
request_id: REQ-20260623-101500-implementation
iteration: 1
from_lane: product
to_lane: human
status: REVIEWING
created_at: 2026-06-23T11:40:00Z
source_docs:
- docs/loop/goal.md
- docs/loop/requests.md
self_probe:
- exact_artifact_or_entry_point: <the exact path, command, URL, import, or executable advertised>
- first_steps_executed: <the first steps below, executed by the sender immediately before send>
- real_evidence: <real command/probe output, status, title, PID, validation, or import result>
what_only_you_can_judge:
- <the visual or interaction surface: for example charts, layout, focus behavior, clarity, or feel>
- Never spend the human on machine-verifiable facts; keep those in self_probe and gate evidence.
human_try:
- <a short operation that reaches the named human-only judgment>
reply_contract:
- Reply PASS, or the first concrete problem.
repeat_round_cache:
- <for the same origin, paste the Cache-Control: no-store header probe for static assets OR instruct a hard refresh with Ctrl+F5>
needed_from_human:
- <one decision or judgment only the human can provide>
recommended_answer:
- <the sender's concrete recommended answer, normally PASS if the named surface is acceptable>
expected_reply:
- PASS, or the first concrete problem
```

The five elements are mandatory: (1) fresh `self_probe` evidence; (2)
`what_only_you_can_judge`, naming the specific visual/interaction surface such
as charts, layout, or focus behavior; (3) the exact reply contract **"Reply
PASS, or the first concrete problem."**; (4) for repeat rounds on the same
origin, a pasted `Cache-Control: no-store` static-asset header probe OR an
explicit hard-refresh instruction (`Ctrl+F5`); and (5) the one-decision
`needed_from_human` plus `recommended_answer` pair.

**Illustrative run-5 evidence (not the rule):** two of three human-caught
defects were invisible to every green machine gate. That is why the template
names the human-only surface explicitly and never spends the human on a status,
exit code, header, bind, or file-validity fact a probe can settle.

**Every human-addressed message type carries `needed_from_human` and
`recommended_answer`.** This includes a cap raise, QA request, intake question,
dependency approval, scope choice, and any later human gate. Ask for one
decision and attach the sender's recommended answer so the human edits a
proposal instead of authoring from a blank page. Do not ask the human for facts
the repo, runtime, or a probe can establish. **Illustrative run-5 measurement
(not the rule):** the one decision + recommendation pattern produced roughly
three-minute turnarounds, while open-ended multi-decision asks took multi-hour
turnarounds.

### Runtime entry-point integrity and reserved infrastructure identifiers

**Project-agnostic invariant:** any lane that opens a shared runtime resource
must choose an identifier that infrastructure has not reserved and must acquire
that resource exclusively. A port, local socket, named pipe, lock, temporary
endpoint, or similar shared name must never resolve to a different process or
artifact while the lane reports its own deliverable as live. Projects that do
not launch a server still apply the same no-shadowing rule to the shared runtime
resources they do use. A reserved infrastructure identifier belongs only to the
infrastructure component that reserved it.

The loop's concrete port registry is the `Reserved loop infrastructure ports:`
line in `constraints.md`. The generated line starts with the dashboard default;
if a human later chooses another dashboard port, append it to that same line so
future lanes see the complete infrastructure reservation during "open the
turn".

For a TCP/HTTP deliverable, a **lane-launched server must not default to a
reserved port** and must bind exclusively. It must fail loudly at startup when
the port is occupied. SO_REUSEADDR-style co-binds are forbidden: reuse flags,
framework defaults, or platform-specific socket options may not turn an
occupied endpoint into a reported successful start. The SELF-PROBE ritual then
proves the advertised listener is the intended artifact, but self-probe is not
a substitute for exclusive acquisition.

The doctor emits `reserved_port_advertised` when a URL in `goal.md` or
`docs/product/*.md` uses a port listed on the constraints line. The check is
warning-only and skips cleanly for older loops with no reserved-ports line.

**Illustrative run-5 example (not the rule):** an application defaulted to
8765, silently co-bound while the loop dashboard was already listening, and the
advertised URL served the wrong application. Declaring the infrastructure port,
choosing an unreserved application default, and failing the second bind would
have prevented the false entry point.

## Deterministic Completion Gate

A request may move to `ACCEPTED` (and auto-chain may proceed) only after a
machine check confirms every verification command for that request exited `0`.
Completion is never asserted from a self-report such as "tests passed" or
"verification looks good". If verification could not run, the request stays
`BLOCKED`, not `ACCEPTED-with-caveat`.

### Evidence Records

**Standing dual-write rule, from request 1.** The implementing lane writes one
evidence file per verification command it runs under
`docs/loop/lanes/<lane>/evidence/`. Product then mirrors the exact bytes into
`docs/loop/evidence/`. The two copies are **byte-for-byte** identical; matching
names, parsed fields, or semantically equivalent JSON are not substitutes for
matching content. The lane copy preserves who produced the evidence, while the
flat root copy remains the deterministic gate's canonical input. This rule
applies to every project and every request from the first request onward, not
only after a fix cycle exposes an audit gap.

Each file is a JSON object:

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

All five fields are required. `request_id`, `checkpoint`, `command`, and
`ran_at` must be non-empty strings, and `ran_at` must be a valid timezone-aware
ISO-8601 timestamp. The `started_at` and `finished_at` execution-window fields
are OPTIONAL per-command timezone-aware timestamps; `result` is OPTIONAL
command-result metadata already tolerated by the gate. Records that omit any or
all of these three optional fields remain valid. These fields are additive:
they never replace `ran_at`, `exit_code`, or another required field, and the
gate verdict continues to depend on the required contract and the real exit
code. Record the real process exit code; do not normalize a non-zero exit to
`0`. Use one file per command (for example
`evidence/REQ-20260623-101500-implementation-iter-1-npm-test.json`) so each
checkpoint command is independently auditable and files never collide.

The archived `messages/<request_id>/IMPLEMENTATION_REQUEST*.md` files are the
request's expected VERIFY-command manifest. The doctor compares every
backticked `VERIFY` command in that manifest with the request's evidence-record
`command` values. A missing archive, an empty VERIFY manifest, or any uncovered
command emits `evidence_manifest_gap`; a message path never counts as evidence.

Keep every product mirror as a flat, non-nested `.json` directly under
`docs/loop/evidence/`: the gate collects records with a non-recursive
`glob('*.json')`, so a file in a subdirectory or with any other extension is
invisible to it and does not count. The implementing lane's byte-identical
source copy stays flat under `docs/loop/lanes/<lane>/evidence/`; the doctor emits
`evidence_mirror_gap` for a root record at `IMPLEMENTATION_DONE` or beyond with
no byte-identical twin in that implementing-lane directory.

### Running The Gate

Before review issues a pass verdict or product marks a request `ACCEPTED`, run
the gate:

```bash
python <skill_dir>/scripts/completion_gate.py --loop-dir docs/loop --request-id REQ-20260623-101500-implementation
```

The evidence contract pins this exact request-scoped invocation for every
accepted iteration (substitute only the placeholder):

```bash
python <skill_dir>/scripts/completion_gate.py --loop-dir docs/loop --request-id <request_id>
```

Record that command, its real exit code, and its output token as a completion-
gate evidence JSON for the same request and iteration, then dual-write it by the
standing rule above. Every `ACCEPTED` iteration has at least one such record;
running the gate without `--request-id <request_id>` does not satisfy this
contract. The doctor reports an accepted iteration without it as
`gate_evidence_missing`.

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

For auto-chain, the doctor's in-process view must also report
`gate_available: true`, `completion_gate_ok: true`, and no
`evidence_manifest_gap`. A green subset of the declared VERIFY manifest is not
complete enough to seed a continuation session.

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

### Frozen-artifact ritual

The invariant is artifact-agnostic: whenever any acceptance artifact is
declared frozen, its exact bytes become part of the review contract. A test,
matrix, scenario set, fixture, rendered baseline, or other artifact declared
frozen records its SHA-256 in **BOTH the review document** and the
**review-lane evidence JSON** at freeze time. Before PASS, the accepting
reviewer re-hashes the exact artifact and compares the digest with both recorded
copies. A missing digest or mismatch blocks the pass; a commit reference alone
does not prove the reviewed bytes stayed frozen.

**Illustrative run-5 example (not the rule):** an early matrix was treated as
frozen by commit reference only. Later rounds recorded the digest at freeze
time, which made re-hashing and byte-level comparison possible. The rule above
applies equally to a CLI golden file, a library conformance fixture, a pipeline
matrix, a desktop interaction baseline, or a web rendering scenario set.

## Anti-Thrash

A request must not bounce between fix and implement forever. Count completed `FIX_REQUESTED` -> `IMPLEMENTING` rounds for one `request_id` from the run log.

Core rule:

```text
fix cycles >= max_fix_cycles -> escalate to BLOCKED + human, do not re-request
```

- `max_fix_cycles` comes from `loop-policy.md` (default 3).
- On the round that would exceed the cap, move the request to `BLOCKED`, append the transition to the run log with a note naming the recurring failed criterion, and send `BLOCKED` to product.
- Do not silently raise the cap or rewrite acceptance criteria to force a pass. A human decides whether to re-scope, raise the cap, or abandon the request.
- **Same-class-twice trigger:** a second blocker with the same `defect_class` on
  one `request_id` mandates an immediate class-closure round. Review freezes the
  implementation-independent red-capable enumeration first, without waiting for
  cap exhaustion. **Illustrative run-5 economics (not the rule):** measured human
  escalation latency was 10-21h, while a class-closure implementation took
  16-25 min.

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

When `--from-lane` is given, `deliver_message.py` also **stamps the sender
lane's heartbeat** (F7): it sets the `heartbeat` cell of that lane's row in
`agent-lanes.md` and the `heartbeat:`/`last_updated:` lines in
`lanes/<lane>/current.md` when that file exists. Delivering a message is proof
the sender is live, so a lane that sends its handoff can never look orphaned for
lack of a heartbeat. It is write-if-present (it never adds a lane or creates a
`current.md`) and best-effort (a heartbeat write never blocks the delivery).
Pass `--no-heartbeat` to suppress it.

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
- `timestamp` is UTC ISO 8601 (`YYYY-MM-DDTHH:MM:SSZ`); `iteration` is the request's current fix iteration; `note` is a short reason or evidence pointer.
- The `lane` column is **the lane that performed the transition** - the acting
  lane, not the new owner. Human-gate transitions are always recorded by
  product, so their `lane` cell is `product` even when the next owner is review
  or implementation.
- Append the row in the same step that updates `requests.md`, so the log and the queue never diverge.
- Count FIX_REQUESTED <-> IMPLEMENTING transitions for the same `request_id` from this log when applying the Anti-Thrash rule.

### Two-phase cap raises

Every cap raise is two checkpoints so authorization-before-effect is provable
from files alone:

1. Product first records the human authorization as a same-status
   `BLOCKED -> BLOCKED` run-log row whose note uses the
   `human_authorization: approved` marker, and appends an Active override in
   `loop-policy.md` in the same checkpoint.
2. The counted `BLOCKED -> FIX_REQUESTED` dispatch occurs in a **separate later
   checkpoint**. The resumed-round `FIX_REQUEST` carries a non-empty
   `cap_authorization:` line pointing to the authorization row and override.

Combining authorization, policy change, and dispatch in one checkpoint is not a
two-phase raise. The doctor reports an unauthorized exit when phase 2 lacks the
preceding row, and reports a resumed `FIX_REQUEST` whose
`cap_authorization:` line is missing.

### Open the turn (single source of truth)

This section is the ONE authoritative definition of the turn-start ritual.
`SKILL.md` and `references/loop-state.md` point here by the leading token
**"open the turn"** and do not restate the rule.

**A lane's turn STARTS by re-reading `agent-lanes.md`, `requests.md`, `goal.md`
(including `## Invariants`), `constraints.md`, and `loop-policy.md` before
acting; anything added since your last turn is binding.**

### Close the turn (the in-turn report-back ritual - single source of truth)

This section is the ONE authoritative definition of the in-turn report-back
ritual. `SKILL.md` and `references/loop-state.md` point here by the leading
token **"close the turn"** and do not restate the step list; when the ritual
changes, change it here and nowhere else.

A Codex thread runs one turn and stops, so a handoff MUST complete inside the
same turn as the work. Finishing the code - even reaching `SHIP_CHECK_OK` - is
not the end of your turn: if your turn ends there, the requester waits forever
and a human has to hand-carry the baton.

**To close the turn you must, in this same turn:**

1. **Send the reply message** to the next lane - `send_message_to_thread` (or
   the `codex_app.*` equivalent), or the durable file-inbox fallback
   (`deliver_message.py`) when no thread tool is available.
2. **Advance `requests.md`** - move the request to its next status and owner (do
   not leave it parked in `IMPLEMENTING`/`REVIEWING` after the work is done).
3. **Append the `loop-run-log.md` row** for that transition, in the same step
   that updated `requests.md`.
4. **Refresh your heartbeat** - the `heartbeat` column in `agent-lanes.md` and
   the `last_updated`/`heartbeat` mirror in lane `current.md`.
   (`deliver_message.py --from-lane <you>` does this for you.)
5. **Commit your slice as your lane** - `CODEX_LANE=<lane> git commit -m "..."`.
   A closed slice that is not committed leaves the scope guard inert (it only
   runs at commit time) and the next lane builds on uncommitted state.

All steps are mandatory every time you close a slice. Do not stop after the
work is done and leave the reply, the ledger, the log, the heartbeat, or the
commit for "next turn" - there is no guaranteed next turn. The doctor reports a
request whose work is done but that never advanced as `stalled_handoff`, naming
the lane to nudge.

**Product's accept/pause path: a paused loop is a fully committed loop.** Before
product accepts a slice or pauses the loop, run `git status --porcelain` and
commit every non-exempt dirty or untracked file as its owning lane
(`CODEX_LANE=<lane> git commit ...`) so the tree is clean at the pause. Data and
DB artifacts stay exempt per `constraints.md` (for example `data/`, `uploads/`,
`private_samples/`, `*.sqlite`/`*.sqlite3`/`*.db`); everything else must be
committed. A pause with an in-scope dirty file is not a paused loop - it is an
un-guarded one.

**UI addendum: re-smoke the LIVE instance.** If your change affects a running
serving process (a localhost dashboard, an app server), restart that process and
re-run the smoke against the LIVE instance before reporting DONE. A green smoke
against a stale process that never picked up your change is not evidence the
change works.

**Handoff hygiene: reference sensitive material, never quote it.** `handoff.md`
and any auto-chain/continuation seed are re-read on every continuation and can be
pasted into a fresh thread, so a raw account number or a full path into a
constraint-marked sensitive directory (`data/`, `uploads/`, `private_samples/`,
or any dir `constraints.md` marks sensitive) left in them leaks across sessions.
Name the artifact ("the TD statement", "the redacted sample") instead of pasting
its contents; the doctor scans the seed and emits a `handoff_sensitive_content`
WARNING when it finds an obvious leak.

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
design_system:
- han-design-skill-v1 (visual style), ui-ux-pro-max (UX mechanics)
delivery:
- channel: send_message_to_thread | lane_inbox | manual
- target_thread_id:
- delivery_status: pending | sent | failed | stale
- sent_at:
```

**`design_system` (G15).** For UI work (the frontend lane, or any request marked
`user_facing: true`) this line records the design skills in force for the
request. The DEFAULT for UI work is both skills with a division of labor:
`han-design-skill-v1` owns the VISUAL STYLE (the Han house aesthetic - typography,
color, layout mood), and `ui-ux-pro-max` owns the UX MECHANICS (interaction,
accessibility, responsive behavior, component/chart/table usability). On a
conflict, a visual call goes to `han-design-skill-v1` and a usability/
accessibility call goes to `ui-ux-pro-max`. The human's explicit choice ALWAYS
overrides this default and is recorded here, on the request the UI lane reads
(this per-request envelope line is the chosen record over a separate decision
entry). Skills are looked up by NAME in the host's standard skills locations
(e.g. `~/.codex/skills/<name>`); an absent skill is noted in the worklog and the
lane proceeds with plain good practice - never a blocker. Omit or leave this line
blank for non-UI requests. See `SKILL.md` "Design Skills For UI Work".

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
user_facing: false
scope:
- src/**
- tests/**
non_goals:
- Do not redesign the UI shell.
- Do not add authentication.
invariants:
- none apply: this color-match slice persists no data and has no external effect; see goal.md ## Invariants.
acceptance_criteria:
- User can choose foundation and lipstick shades. VERIFY `python -m unittest tests.ui.test_shade_picker` (fails if either selector is missing).
- App returns three compatible color recommendations. VERIFY `python -m unittest tests.core.test_recommend` asserts exactly three results and undertone compatibility (fails on zero, four, or an incompatible pick).
- Existing tests pass and one regression test covers the recommendation rule. VERIFY `python -m unittest discover -s tests` (fails on any regression).
expected_reply:
- changed_files
- verification commands and results
- blockers, if any
```

### Red-capable acceptance criteria (each criterion names a command that can FAIL)

Every acceptance criterion in an `IMPLEMENTATION_REQUEST` must name the exact
command that proves it, and that command must be **red-capable**: able to FAIL
on that specific criterion's violation, not merely exit `0` because the code
ran. **A criterion with no command that can go red is a vibe: sharpen it or drop
it.** Name the command inline on the criterion (the `VERIFY <command>` suffix
above); the implementation lane records its real exit code as evidence, and the
completion gate reads that exit code.

Exemplar pair - the difference between a vibe and a red-capable criterion:

- **Bad (a vibe):** "parsing works." The obvious check - "run the parser, it
  exits 0" - passes on garbage output: it cannot distinguish a correct parse
  from a parser that emits `merchant="9"` and `date="2026-06-00"` and still
  returns 0.
- **Good (red-capable):** "`python -m unittest tests.core.test_parse_fields`
  asserts merchant is non-numeric text, dates are valid ISO calendar days, and
  amounts carry a sign - and FAILS on any garbage field." The command goes red
  precisely on the failure the user would see.

**Canonical counterexample (run 2).** The request
`REQ-20260707-073729-data-eng` shipped an accepted slice whose evidence
`REQ-20260707-073729-data-eng-iter-1-td-pdf-smoke.json` recorded `exit_code: 0`
- yet the running app produced `merchant="9"` and `date="2026-06-00"` and
imported 0 rows from the user's real statement. The smoke command exercised the
plumbing and exited 0 without asserting a single field value, so it could not go
red on exactly the defect the user hit. That is tautological evidence: a command
whose success is indistinguishable from garbage. Red-capable criteria exist to
make this impossible - see the review gate's tautological-evidence guard in
`references/loop-state.md`.

### Real-input correctness (field-level, not "parses without error")

When `goal.md` names **human-provided real data** (for example a real TD bank
statement PDF, an exported CSV, a real invoice), at least one acceptance
criterion must assert **field-level correctness** on that data - not merely that
the parser ran. "Parses without error" alone is NEVER sufficient real-data
evidence: run 2's importer parsed without error and still produced `merchant="9"`
and 0 imported rows.

The field-level criterion must assert, against the real input or a human-approved
redacted derivative, at minimum:

- **row count > 0** - the real file actually produced transactions;
- **valid calendar dates** - every parsed date is a real ISO calendar day (no
  `2026-06-00`, no month 13, no day 32);
- **merchant/payee non-empty and non-numeric** - the merchant field is real text,
  not a stray digit like `9`;
- **sign convention holds** - amounts carry the sign the document uses (debits
  and credits/refunds land on the correct side).

The verify command must be red-capable on each of these (it FAILS if row count is
0, a date is invalid, a merchant is empty/numeric, or a sign is flipped).

#### The redacted-sample ritual (privacy holds; evidence records only counts/booleans)

Real financial input must never be copied into prompts, committed files, run
logs, or evidence records. So the correctness check runs against a
**human-approved redacted derivative**, approved ONCE at intake:

1. **At intake**, the human approves either a sanitized excerpt (a few rows with
   account numbers and identifying detail scrubbed) OR a field-shape spec (for
   example: "column 1 is an ISO date, column 3 is merchant text, column 4 is a
   signed amount; a real month has 20-60 rows"). This one approval is the whole
   privacy gate.
2. **Lanes verify against that approved derivative** and its field shape, never
   against raw un-approved contents.
3. **Evidence records only counts and booleans** - `rows_parsed: 42`,
   `all_dates_valid: true`, `merchant_nonempty: true`, `signs_ok: true`,
   `exit_code: 0` - and never a raw merchant string, account number, or
   transaction line. The redacted excerpt itself lives outside the repo (or in a
   constraints-exempted `private_samples/` path), never in `docs/loop/evidence/`.

This keeps the never-upload constraint intact while still making the field-level
criterion red-capable: the assertion runs on approved shape, the record carries
only the pass/fail counts.

### Applicable invariants per request (each applicable one is red-capable)

`goal.md`'s `## Invariants` section (the domain rules the human approved at
intake - see `SKILL.md` "Invariants-first intake") is standing acceptance
material, so every `IMPLEMENTATION_REQUEST` carries an `invariants:` line that
either names **which invariants apply** to this slice (by number or name) or
**states that none apply and why**. Silence is not allowed: an unstated
`invariants:` line is an incomplete request, the same way an empty `non_goals`
is not ready to send.

The red-capable rule extends to invariants: **each APPLICABLE invariant names a
verify command that can FAIL on its violation**, exactly like an acceptance
criterion. "INV-1 no silent drops" is a vibe until a command asserts every
parsed row reached the raw store and goes RED when one is dropped; an applicable
invariant with no red-capable check is the same defect as a criterion with no
red-capable command - sharpen it or the request is not ready. Name the command
inline, the same `VERIFY <command>` suffix the criteria use:

```md
invariants:
- INV-1 no silent drops: VERIFY `python -m unittest tests.core.test_no_drops`
  (fails if any parsed row is missing from the raw store).
- INV-4 per-import run_id + undo: VERIFY `python -m unittest tests.core.test_undo`
  (fails if an import cannot be inspected and undone as a unit).
- none of the display-tracing invariants apply: this slice renders nothing.
```

The review gate checks the changed behavior against the same `## Invariants`
section, and an invariant violation is ALWAYS a blocker-severity finding (see
`references/loop-state.md` "Invariant check").

## PRE_IMPLEMENTATION_TEST_REQUEST

A class-closure round has a first-class pre-implementation contract-freeze
phase. During that phase **FIX_REQUESTED may be owned by review** while review
freezes the implementation-independent red-capable enumeration. Ownership by
review means the contract is being authored and proven red; it does not mean
implementation has started. Only after the enumeration artifact and SHA-256 are
recorded does review issue the resumed `FIX_REQUEST` to implementation.

```md
# PRE_IMPLEMENTATION_TEST_REQUEST

message_type: PRE_IMPLEMENTATION_TEST_REQUEST
request_id: REQ-20260623-101500-implementation
parent_request_id:
iteration: 2
from_lane: product
to_lane: review
status: FIX_REQUESTED
created_at: 2026-06-23T11:32:00Z
source_docs:
- docs/loop/goal.md
- docs/loop/requests.md
defect_class: an output is valid if and only if the declared contract holds
sibling_scan:
- input_domain: the positively defined domain and rejected complement classes
- state_reachability: every public entry point, writer, and alias for the state
- boundaries: extremes and simultaneous worst cases
enumeration_artifact: docs/loop/evidence/<request_id>-iter-2-class-matrix.md
enumeration_sha256: <sha256>
test_contract:
- Freeze an implementation-independent red-capable enumeration before implementation.
- Record the red command, exit code, artifact path, and SHA-256.
expected_reply:
- frozen enumeration artifact and SHA-256
- red-capable command and failing result
- FIX_REQUEST dispatch-ready or BLOCKED
```

The durable envelope separates contract authoring from implementation and makes
the recurring review-owned `FIX_REQUESTED` phase recoverable without chat.

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
status: REVIEWING
created_at: 2026-06-23T11:30:00Z
source_docs:
- docs/loop/tracker.md
verdict: PASS
criteria_results:
- Criterion 1: pass, verified by tests/recommend.test.ts
- Criterion 2: pass, verified by manual code review
scope_creep: none - all changed files inside the request's scope globs
ease_of_misuse: none found - inputs cannot reach a wrong-but-accepted outcome the criteria did not forbid
evidence:
- npm test: passed
remaining_risks:
- None known. (any should-fix/nit findings accepted-with-notes go here)
expected_reply:
- Product performs the ACCEPTED transition when all gates allow, or records the
  next action; user-facing requests first hold for the Human-QA Gate.
```

`REVIEW_DONE` returns review's pass/fail `verdict`; it keeps the request at
`REVIEWING` and never performs the `ACCEPTED` transition. Product performs that
transition after evaluating the verdict and machine evidence, and, for a
user-facing request, only after the Human-QA Gate. It also records the
three-category review result: the per-criterion
`criteria_results`, a `scope_creep` line (changed files vs the request's `scope`
globs - flag creep even if it works), and a mandatory `ease_of_misuse` line
answering *"can a caller/input reach a wrong-but-accepted outcome the criteria
did not forbid? Name the path or state none found."* See
`references/loop-state.md` "Review checklist" and "Ease-of-misuse question".

The `scope_creep` comparison exempts **protocol-mandated ritual writes**: the
close-the-turn ritual requires them on every slice, so they are never creep -
the lane's own heartbeat cell in `agent-lanes.md`, the request's row in
`requests.md`, appended `loop-run-log.md` rows, the lane's own `lanes/<lane>/**`
files, `messages/<request_id>/**` envelopes for the request, and `evidence/**`
records for the request. Writes to OTHER lanes' rows or directories remain
creep. Canonical non-creep example (run 3): a review blocked data-eng for
stamping its own `agent-lanes.md` heartbeat cell - the exact write the "Close
the turn" heartbeat step mandates.

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
severity: blocker
defect_class: an output is compatible if and only if every declared compatibility constraint holds
sibling_scan:
- input_domain: enumerate the positively defined allowed input domain and every rejected complement class
- state_reachability: enumerate every public entry point, writer, and alias that can reach the output state
- boundaries: enumerate boundary and extreme values, including simultaneous worst cases
cap_authorization: not applicable; this request did not resume from BLOCKED
evidence:
- tests/recommend.test.ts lacks undertone coverage.
requested_fix:
- Enforce one centralized compatibility rule and cover the frozen sibling enumeration.
expected_reply:
- changed_files
- verification commands and results
- blockers, if any
```

Each `FIX_REQUEST` finding carries a `severity`: `blocker`, `should-fix`, or
`nit`. **Only a `blocker` forces a fix cycle and increments `iteration`.**
`should-fix`/`nit` findings may be accepted-with-notes (recorded in
`REVIEW_DONE`'s `remaining_risks`) or batched into a follow-up request - they do
NOT bounce the request back and do NOT increment `iteration`. A review that finds
only nits sends `REVIEW_DONE` with the notes, not `FIX_REQUEST`. See
`references/loop-state.md` "Finding severity".

Every `FIX_REQUEST` has two additional MANDATORY fields:

- `defect_class` states the violated rule as an **invariant over inputs/states**
  in the form "X if and only if Y". Defining a defect class as a **list of named
  failure symptoms** is explicitly forbidden because an unlisted sibling can
  still escape. **Illustrative run-5 example (not the rule):** "catch every
  Decimal exception" still missed silent underflow. The successful positive
  whitelist invariant was: "a nonblank raw token is valid if and only if its
  whole token matches the frozen decimal grammar".
- `sibling_scan` enumerates known same-class siblings across exactly three axes:
  (1) the **input domain defined positively** with a whitelist, grammar, range,
  or equivalent allow-rule; (2) **every public entry point**, writer, and alias
  that can reach the same state; and (3) **boundary and extreme values**,
  including simultaneous worst cases. All three axes matter. **Illustrative
  run-5 example (not the rule):** the two observed class escapes were exactly an
  unenumerated write door and unenumerated extreme values.

### Defect class closure

Before any class-closure fix round, review freezes an
**implementation-independent red-capable enumeration** as a matrix or scenario
set and records its SHA-256. The enumeration describes externally observable
inputs, states, and outcomes without depending on the proposed implementation,
so it can go red before the fix and remain stable while the fix is written.

The `FIX_REQUEST` demands **one centralized rule** that covers the frozen
enumeration and its positively defined domain. **Scattered operation-specific
patches do not satisfy this request.** The same `request_id`, `defect_class`,
enumeration SHA-256, and sibling scan remain attached to the round so review can
re-hash and re-run the complete class contract.

A `FIX_REQUEST` for a resumed round (one dispatched by
`BLOCKED -> FIX_REQUESTED`) has a non-empty `cap_authorization:` line pointing
to the preceding product-authored `BLOCKED -> BLOCKED` human-authorization row
and the matching Active override in `loop-policy.md`. A fix request that did not
resume from `BLOCKED` records `not applicable` as in the template; it must not
invent an authorization.

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
recommended_answer:
- Use the bundled local mock catalog for the MVP; wire the production key in a later slice.
expected_reply:
- Product updates scope, provides input, or marks request blocked.
```

**Every BLOCKED / approval-needed message carries a `recommended_answer`.** The
lane that raises the block already knows the situation best, so it proposes the
resolution it would pick -- the human edits a proposal instead of authoring a
decision cold. This generalizes the missing-dependency blocker's exact install
command (below): there the recommended answer is literally the install line;
here it is the lane's proposed scope call, default, or config. Keep it to one
concrete, actionable line (a command, a value, or a "do X" sentence), not a
menu. The human always overrides; the recommendation just removes the blank
page. The dashboard renders it inline on the your-turn item so seeing the block
and seeing the proposed answer are the same glance.

### Missing-Dependency Blocker (a blocker with a built-in exit ramp)

A `BLOCKED` caused by a missing tool or package is not a dead end. It is a
distinct, machine-classifiable blocker type that carries its own exit ramp:
record exactly what is missing and the exact install command, ask the human for
a one-line approval, and on approval install, re-run the failed verification,
record fresh evidence, and unblock the **same** `request_id` (increment
`iteration`, do not mint a new request).

Mark it in the `BLOCKED` message with this greppable, flat format so the doctor
and dashboard can classify it (rather than painting a generic red dead-end):

```md
blocker: missing_dependency
dependency: pip | pytesseract | pip install pytesseract
dependency: system | tesseract | choco install tesseract
```

- The single `blocker: missing_dependency` line is the marker the doctor greps.
- Each `dependency:` line is `kind | name | install-command`, pipe-separated.
- `kind` is exactly `pip` (a pip-installable Python package) or `system` (a
  system binary that needs an installer/choco). Distinguish them: `pip`
  packages are cheap and reversible; `system` binaries mutate the machine and
  always need explicit human approval.

Behavior is governed by `dependency_install` in `loop-policy.md`:

- `ask` (default) - always ask the human before installing anything.
- `auto-pip-only` - a lane may auto-install a `pip` dependency and re-run the
  verify, but must still ask before any `system` binary.
- `never` - never install; stay `BLOCKED` for a human to resolve out of band.

On the retry after an approved install, reuse the same `request_id`, increment
`iteration`, re-run the exact verify command, and record a fresh evidence file -
the completion gate then accepts the request on real re-verified evidence, never
on the assumption that the install fixed it.

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
