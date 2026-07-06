# Loop State Gates

This file is the reference implementation of the handoff, lanes, coordinated-writes, and bounded-loop invariants (`references/methodology.md`): the specific gates below are one concrete shape of those invariants. Any individual gate is swappable; "every boundary must have some gate" is not.

Use this reference to decide when to start loop engineering, when to hand off, and when to create or continue another Codex session.

## Loop Start Gate

Start a repo-local loop only when all required conditions are true:

```text
clear goal + checkpointable work + verification surface + durable state need
```

Required:

- The objective has a concrete `Done When`.
- The work can be split into checkpoints.
- There is a verification surface: tests, build, screenshots, review checklist, rendered artifact, source evidence, or manual acceptance criteria.
- State needs to survive compaction, app restart, or a different lane/thread.
- Constraints and non-goals are known enough to prevent drift.

Do not start loop engineering for a tiny one-off edit, a vague idea without acceptance criteria, or a task that requires credentials, production actions, deletion, billing changes, or private external data without approval.

## Checkpoint Close Gate

A checkpoint is closed enough to hand off only when:

- The current lane completed one coherent slice.
- Changed files or produced artifacts are listed.
- Verification ran, or the reason it could not run is explicit.
- `tracker.md`, `handoff.md`, `requests.md`, and lane `current.md` reflect the latest state.
- The next action can be written as one clear request.
- Blockers and risks are documented.

If any item is missing, continue in the current session until the checkpoint can be audited.

## Handoff Readiness Gate

Use this decision tree before sending work to another lane:

```text
Is there a concrete next actor?
  no -> continue current session or ask product to decide
  yes -> continue

Is the current checkpoint closed?
  no -> verify/update state first
  yes -> continue

Can the next actor act from repo files + message alone?
  no -> improve docs/handoff first
  yes -> continue

Is the target lane registered and verified?
  yes -> send_message_to_thread
  no -> create/verify/register thread, or write inbox fallback

Would this cause concurrent writes to the same files?
  yes -> serialize through product or stop
  no -> hand off
```

Handoff is ready when the next agent does not need hidden chat history to continue.

## Lane Expansion Gate

Add a lane only when all are true:

- It owns a recurring responsibility, not a one-off task.
- It has clear inputs and outputs.
- Its write scope can be separated from active lanes.
- It reduces context pollution or enables real parallel work.
- Product can route work to it with a concrete message and acceptance criteria.
- Its worklog will be useful for later recovery or accountability.

Do not add a lane when the proposed agent overlaps an existing lane, has no durable output, needs to edit the same files as another active lane, or exists only as a personality label.

Common lane presets:

| lane | role | default write scope |
| --- | --- | --- |
| research | Do source-backed research and summarize findings. | `docs/research/**; docs/loop/lanes/research/**` |
| visual | Review UI/visual output and prepare visual asset requests. | `docs/design/**; docs/loop/lanes/visual/**` |
| security | Review security risks, threat models, and sensitive changes. | `docs/security/**; docs/loop/lanes/security/**` |
| data | Analyze metrics, datasets, experiments, and validation evidence. | `docs/data/**; docs/loop/lanes/data/**` |
| docs | Maintain docs, changelogs, release notes, and user-facing copy. | `docs/**; docs/loop/lanes/docs/**` |
| release | Coordinate release readiness, QA checklist, packaging, and blockers. | `docs/release/**; docs/loop/lanes/release/**` |
| media | Coordinate scripts, covers, videos, and social-content assets. | `docs/media/**; docs/loop/lanes/media/**` |

If a preset write scope is too broad for the project, add a custom lane with a narrower `--extra-lane` entry instead.

## Lease Gate

Lanes coordinate writes two ways: a static `write_scope` per lane in
`agent-lanes.md`, and dynamic, short-lived **advisory leases** in
`docs/loop/leases.md`. Use a lease when a lane needs temporary exclusive
ownership of files that overlap, or could overlap, another lane's scope for the
lifetime of one request.

### leases.md schema

```md
| file_glob | lane | request_id | acquired_at | status |
| --- | --- | --- | --- | --- |
| src/payments/** | implementation | REQ-20260624-101500-implementation | 2026-06-24T10:15:00Z | active |
```

- `file_glob`: a single glob (fnmatch syntax; `**` for recursion) the lease
  covers. One glob per row; add multiple rows for multiple paths.
- `lane`: the lane that holds the lease. Must be a registered lane.
- `request_id`: the request the lease serves, so recovery can tie a lease back
  to live work in `requests.md`.
- `acquired_at`: ISO-8601 UTC timestamp the lease was taken.
- `status`: `active`/`held` is enforced; `released`/`expired`/`done`/`revoked`/`stale`
  or a blank status is ignored; any other non-blank value is treated as held, so
  the guard fails closed on a status it does not recognize.

Leases are advisory: they live in a repo file and are only as honest as the
lanes that write them. Acquire a lease by appending an `active` row before you
start editing; release it by setting the row's `status` to `released` (do not
delete the row, so the history stays auditable). Never edit another lane's
active lease row except to mark a verifiably stale one and only after
coordinating through product.

### How write_scope is enforced

`write_scope` is enforced mechanically by a git **pre-commit hook**, not by
trust. Install it once per repo:

```bash
python <skill_dir>/scripts/install_precommit.py --repo . --loop-dir docs/loop
```

The hook runs `precommit_scope_guard.py`, which reads the committing lane from
the `CODEX_LANE` environment variable, then rejects the commit (exit 1) if any
staged file is either:

- outside the committing lane's `write_scope` globs in `agent-lanes.md`, or
- covered by an **active lease held by a different lane** in `leases.md`.

Commit with the lane set, e.g. `CODEX_LANE=implementation git commit -m "..."`.
With no lane set the guard fails closed (the commit is blocked) unless
`CODEX_PRECOMMIT_SKIP=1` is exported, or `precommit_scope_guard.py` is invoked
directly with `--allow-unscoped` (`install_precommit.py` itself does not accept
that flag). Free-text notes in a `write_scope` cell (for example
`implementation notes named by request`) are ignored for matching; only
path/glob tokens constrain the commit.

### Lease Gate checklist

Before editing files that another lane could touch:

- Acquire a lease in `leases.md` with `status: active` tied to your
  `request_id`, or confirm the files are already inside your `write_scope` and
  unclaimed.
- Do not edit files held by another lane's active lease; route the change
  through that lane via `requests.md` instead.
- Release the lease (`status: released`) at checkpoint close, in the same
  update that moves `requests.md` and lane `current.md`.
- Treat a pre-commit rejection as a coordination signal, not a tooling error:
  restage only in-scope files, or hand the file to its owning lane.

## Product To Implementation Gate

Send `IMPLEMENTATION_REQUEST` only after product has:

- named source docs;
- defined scope and non-goals;
- copied acceptance criteria;
- confirmed the implementation write scope does not conflict with another active lane;
- created or updated the request row in `requests.md`.

## Implementation To Review Gate

Send `REVIEW_REQUEST` only after implementation has:

- completed a runnable or reviewable slice;
- listed changed files and artifacts;
- run the requested verification or explained why it could not run;
- sent `IMPLEMENTATION_DONE`;
- updated its worklog/current state.

## Review To Fix Gate

Send `FIX_REQUEST` only when review can state:

- exact failed criteria;
- evidence path, command, screenshot, or artifact;
- a bounded requested fix;
- the same `request_id` with incremented `iteration`.

Do not rewrite product scope during review. Send `BLOCKED` to product if criteria are ambiguous.

## Auto-Chain Gate

Create a continuation session only when all are true:

- `auto_chain_next_session: true` is present in `handoff.md` or `loop-policy.md`.
- The tracker has unchecked work.
- The next checkpoint is specific and scoped.
- The current checkpoint is verified or explicitly blocked with a safe next action.
- State files and message ledgers are updated before session creation.
- No blocker needs human input, credentials, external data, destructive action, or production deployment.
- No active verified thread already owns the same `request_id + lane + checkpoint`.
- Replacement has not already been attempted for this continuation.

If any condition fails, stop and report the next required human or repo-state action.

## Budget Stop Gate

Before implementing a request, auto-chaining, or starting a new checkpoint, check `docs/loop/loop-budget.md`.

Core rule:

```text
budget_exhausted: true -> stop, mark BLOCKED, report remaining work
```

- `loop-budget.md` tracks the iteration/cost budget and a `budget_exhausted` flag.
- When `budget_exhausted: true`, do not send new `IMPLEMENTATION_REQUEST`s, do not auto-chain, and do not open a continuation thread. Move the active request to `BLOCKED` and append the transition to the run log.
- Report spent versus budget and the next action a human can authorize (raise budget, re-scope, or stop).
- Treat budget exhaustion like any other human gate: it requires explicit approval to resume.

## Heartbeat / Orphan Recovery

Each lane reports a heartbeat while it owns an active request. The authoritative heartbeat the doctor reads is the `heartbeat` column in `agent-lanes.md`; a lane's `current.md` `last_updated` is a per-lane mirror of the same value. A stale heartbeat means the owning thread crashed, compacted, or was abandoned.

Core rule:

```text
IMPLEMENTING request + stale lane heartbeat -> revert to REQUESTED for reassignment
```

- Refresh the heartbeat (the `heartbeat` column in `agent-lanes.md`, and the `last_updated` mirror in `current.md`) when claiming a request and at each checkpoint while implementing.
- A heartbeat is stale when it is older than the staleness window in `loop-policy.md` (or no later than the request's `updated_at`).
- On recovery, if a request is `IMPLEMENTING` (or `REVIEWING`) but its owner lane's heartbeat is stale, revert the request to `REQUESTED`, clear `owner_lane`, append the transition to the run log with a note, and let product reassign it.
- Do not reassign a request whose heartbeat is fresh; assume the owner is still working.
- Reverting for reassignment does not increment `iteration` and does not count against `max_fix_cycles`.

## Continuation Thread Health Check

Treat a returned thread ID as provisional until:

1. `create_thread` returns a thread ID.
2. `list_threads` or `read_thread` can find that exact ID or exact title.
3. `set_thread_title` succeeds, or `read_thread` confirms the title is already correct.
4. `read_thread` shows the first turn exists.
5. The first turn is `inProgress` or completed normally.
6. Recent items show the agent started reading the handoff, skill docs, or project files.

Only then write the ID into `agent-lanes.md`, `requests.md`, `handoff.md`, or the final response.

If the ID cannot be found, title updates fail repeatedly, or the thread becomes unreadable or unopenable:

- do not report it as the next session;
- mark any already-written ID as stale;
- create at most one replacement from the current handoff;
- verify the replacement before recording it.

## Recovery Gate

On resume, do not start by inventing a new plan. First read:

1. `goal.md`
2. `tracker.md`
3. `constraints.md`
4. `handoff.md`
5. `agent-lanes.md`
6. `requests.md`
7. this lane's `current.md` and `inbox.md`

Continue the oldest non-terminal request owned by this lane. If ownership is unclear, ask product or mark `BLOCKED`.

If the optional decision cache is in use, follow the Memory Protocol pinned in `handoff.md` before acting on any recorded decision: grep `docs/loop/memory/decisions.jsonl` for the request, follow the `supersedes` chain to the newest live decision, and re-run the completion gate and doctor before trusting a recorded `gate_status`. A `stale_decision` warning means discard that cached decision and re-read the live source docs. (See `references/memory.md`.)

## Stop Conditions

Stop instead of handing off or auto-chaining when:

- `Done When` is satisfied;
- tracker has no unchecked work;
- verification cannot run and no safe fallback exists;
- target lane lacks verified thread ID and thread creation is not allowed;
- required action needs credentials, approval, private external data, destructive changes, billing changes, or production deployment;
- write scopes conflict;
- `max_fix_cycles` is reached;
- the next request would be vague or unbounded.
