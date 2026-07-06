---
name: codex-agent-loop-orchestrator
description: Set up or continue a repo-local multi-agent Codex workflow with lane registration, durable request state, handoff readiness gates, send_message_to_thread delivery, checkpoint review/fix loops, and optional Codex Loop Engineering auto-chain. Use when users mention agent-lanes, Product/Implementation/Review agents, session handoff, loop files, requests.md, checkpoint iteration, or sustainable multi-agent collaboration.
---

# Codex Agent Loop Orchestrator

Create a thin orchestration layer around Codex threads. Keep project truth in repo files, keep agent identities in a registry, and use thread tools only as the message bus and session starter.

## Methodology vs Reference Implementation

This skill is two layers. The **methodology** (`references/methodology.md`) is the transferable discipline - nine invariants that stay true even if you change the lanes, the message vocabulary, or the verification surface. Everything in this SKILL.md, the other `references/`, and the `scripts/` is **one reference implementation** that proves it on Codex. Read `references/methodology.md` to understand *why* the loop is shaped this way; read `references/build-your-own-agent.md` to re-parameterize it for a different team or verification surface. If code and methodology ever disagree, the code is the bug.

## Core Model

Use this stack:

```text
docs/loop/{goal,tracker,constraints,handoff}.md  -> project state and loop contract
docs/loop/agent-lanes.md                        -> lane registry and verified thread IDs
docs/loop/requests.md                           -> request lifecycle and current owner
docs/loop/loop-policy.md                        -> fix limits, handoff rules, stop rules
docs/loop/loop-budget.md                        -> cost/iteration budget and budget_exhausted stop gate
docs/loop/loop-run-log.md                       -> append-only transition log (timestamp|request_id|iteration|from_status|to_status|lane|note)
docs/loop/messages/<request_id>/*.md            -> durable message copies
docs/loop/lanes/<lane>/{inbox,outbox,current,worklog}.md
send_message_to_thread                          -> cross-thread delivery
create_thread/read_thread/set_thread_title      -> optional verified continuation sessions
```

Do not treat the registry as a dashboard. It is a small source-of-truth table that lets an agent find another agent's thread ID, role, write scope, and worklog.

## First Move

In every command example below, `<skill_dir>` means the directory that contains this SKILL.md - `~/.codex/skills/codex-agent-loop-orchestrator` for a direct install, or `~/.codex/plugins/<plugin>/skills/codex-agent-loop-orchestrator` when installed as a Codex plugin. Substitute your actual path (an absolute path works everywhere).

1. Inspect whether `docs/loop/goal.md`, `tracker.md`, `constraints.md`, and `handoff.md` exist.
2. If the loop files (`goal.md`, `tracker.md`, `constraints.md`, `handoff.md`) are missing, run the local bootstrap helper below - it now writes starter templates for all four (Status Legend `[ ]`/`[~]`/`[x]`/`[!]`, a `Done When` section, `auto_chain_next_session: false`), plus `loop-budget.md`, `loop-run-log.md`, `leases.md`, and an empty `evidence/` dir. No external skill is required; `$codex-loop-engineering` is optional, not a dependency.
3. Run the local bootstrap helper when the registry, request ledger, or lane files are missing or stale:

```bash
python <skill_dir>/scripts/bootstrap_agent_loop.py --loop-dir docs/loop
```

4. Read `agent-lanes.md`, `requests.md`, `loop-policy.md`, and the relevant lane `current.md` before sending or resuming cross-agent work.
5. Use `tool_search` for thread tools before assuming they exist. Look for `create_thread`, `read_thread`, `send_message_to_thread`, and `set_thread_title`.
6. On a Codex-app host, `tool_search` first for the cross-thread `codex_app.*` tools (the app's own thread/message tools) and prefer them for delivery. When they are absent (headless `codex exec`, sandboxed, or a non-app host), fall back to the durable file inbox (`deliver_message.py` / `inbox/`); the loop works identically either way because delivery is just one plane over repo files.

## Health Check

Before deciding whether to hand off, recover a request, add a lane, or auto-chain, run the read-only doctor:

```bash
python <skill_dir>/scripts/multi_agent_loop_doctor.py --loop-dir docs/loop --json
```

Use the output as orientation. Still read the actual loop files before editing or sending messages.

The doctor reports:

- missing loop files and lane files;
- lane thread status, including `UNVERIFIED` and stale markers;
- non-terminal requests and unknown request owners;
- tracker unchecked and blocked counts;
- whether handoff and auto-chain are currently ready;
- per-lane `heartbeat` and `orphan_suspect` lanes (heartbeat older than `--stale-heartbeat-mins`, default 30);
- `budget` state from `loop-budget.md`, per-request `fix_cycles`/`thrash`, `run_log_present`, and `evidence_dir_present`;
- `evidence_recorded_ok`: true only when every non-terminal request has a non-empty evidence cell in `requests.md` (proves a cell was filled in, not that anything passed);
- `completion_gate_ok`: the real deterministic gate. The doctor imports `completion_gate` in-process, loads `evidence/*.json`, and evaluates every non-terminal request that has at least one evidence record; it is true only when the gate is importable (`gate_available`), no evaluated request failed, and no evidence file is malformed. If the gate cannot be imported, `gate_available` is false and a `gate_unavailable` warning is emitted.
- `decisions`: `{total, active, stale, malformed}` from the optional memory cache (see the Memory Layer below). A `stale_decision`/`missing_source_doc`/`malformed_decision` is a WARNING only; it never flips `handoff_ready` or `auto_chain`. A missing `decisions.jsonl` degrades gracefully (no warning).

## Hardening Scripts

Use these helpers alongside the bootstrap and doctor. All are stdlib-only and idempotent.

- `completion_gate.py` - run before declaring the loop done or emitting `SHIP_CHECK_OK`. It is read-only: it reads the recorded evidence under `docs/loop/evidence/*.json` and prints `SHIP_CHECK_OK` only when every record for the request reports `exit_code` 0. It does not execute any command. The implementation lane must run each verify command itself and record the real exit code in an evidence file; the gate validates those records, it does not run the commands. A missing, malformed, or non-zero record means `BLOCKED`.
- `deliver_message.py` - run instead of writing an inbox by hand. It delivers a saved message atomically (Maildir `tmp` -> `new` rename) so a crash never leaves a half-written inbox entry.
- `install_precommit.py` - run once per repo when leases and write scopes matter. It installs a git pre-commit hook that enforces each lane's `write_scope` and advisory file leases.

```bash
python <skill_dir>/scripts/completion_gate.py --loop-dir docs/loop
python <skill_dir>/scripts/deliver_message.py --loop-dir docs/loop --request-id <request_id> --to-lane <lane> --message-file <message.md>
python <skill_dir>/scripts/install_precommit.py --loop-dir docs/loop
```

Run `completion_gate.py` before any final acceptance, `deliver_message.py` on every cross-lane send, and `install_precommit.py` once during setup or when write scopes change.

## Memory Layer

The loop has one optional memory artifact: an append-only decision cache at `docs/loop/memory/decisions.jsonl`. It is a cache over the source files, never a source of truth. See `references/memory.md` for the schema and Memory Protocol.

- Write one line per checkpoint decision with `record_decision.py` (append-only; supersede by appending a new line whose `supersedes` names the old `decision_id`, never by editing).
- Each record stores a `content_hash` over its `source_docs`; the doctor recomputes it with the same canonical `normalize_then_hash` (defined once in `record_decision.py`, imported in-process) and reports drift as a `stale_decision` WARNING.
- Drift never blocks handoff or auto-chain (memory fails open; verification fails closed). Before trusting a recorded `gate_status`, re-run the completion gate and the doctor.
- A missing `decisions.jsonl` is fine - the loop runs identically without it.

## Dashboard

`scripts/loop_dashboard.py` is a mostly read-only local viewer (binds `127.0.0.1:8765`, stdlib `http.server`) that renders the loop files, the in-process doctor result, a Codex rate-limit usage panel (5h + weekly remaining, read privately from the newest `~/.codex` session JSONL - only rate-limit and token-count numbers, never conversation content), and a `max_fix_cycles` control. It has two human-only write endpoints: `POST /api/lanes` (add one lane) and `POST /api/policy` (set the anti-thrash `max_fix_cycles` cap, 1..10, written to `loop-policy.md`). Agents never read the dashboard and deleting it does not affect the loop.

After the First Move completes (loop bootstrapped, lanes/threads registered), START the dashboard as a background process and report its URL to the user:

```bash
python <skill_dir>/scripts/loop_dashboard.py --loop-dir docs/loop
```

On startup it prints exactly one machine-greppable line, `DASHBOARD_URL=http://127.0.0.1:<port>/`, AFTER binding. Capture that line and report that exact URL to the user - never a guessed one. If port 8765 is busy the script self-selects an ephemeral port and prints the actual bound URL. Run one dashboard per loop dir; before spawning, check whether the URL already responds so you do not start a duplicate.

## Decision Gates

Read `references/loop-state.md` before deciding whether to start loop engineering, close a checkpoint, hand off to another lane, recover a request, or auto-chain a continuation session.

Core rule:

```text
Only switch sessions at a checkpoint boundary.
```

Do not hand off while implementation is half-done, verification has not run, acceptance criteria are unclear, or the next lane would need to guess context from chat history.

## Default Lanes

Start with three lanes unless the user names a different team:

| lane | owns | does not own |
| --- | --- | --- |
| product | goals, specs, milestone decisions, acceptance criteria, final product judgment | implementation details beyond constraints |
| implementation | code changes, tests, implementation notes, verification evidence | product expansion or acceptance rewrites |
| review | independent acceptance review, test review, regression concerns | feature implementation |

Add specialist lanes only when they reduce context pollution or enable real parallelism, for example `visual`, `research`, `security`, or `data`.

## Lane Expansion

Read `references/loop-state.md` for the Lane Expansion Gate before adding a lane. Add lanes to split recurring responsibility, not to role-play. A new lane must have clear input, output, write scope, and routing rules.

Common presets:

| lane | use when | typical output |
| --- | --- | --- |
| research | source-backed investigation or competitive/product research is repeated | findings, citations, evidence notes |
| visual | UI review, design assets, screenshots, or visual QA are repeated | design notes, screenshot findings, asset requests |
| security | security review or threat modeling is repeated | risk findings, fix requests, verification notes |
| data | metrics, analytics, datasets, or experiment review are repeated | data notes, queries, charts, validation notes |
| docs | documentation, changelog, release notes, or user-facing copy are repeated | docs changes, copy review, doc gaps |
| release | packaging, QA checklist, deployment prep, or launch coordination is repeated | release checklist, readiness status, blockers |
| media | script, cover, video, or social-content production is repeated | content briefs, asset requests, publishing notes |

Use the bootstrap helper to add preset lanes:

```bash
python <skill_dir>/scripts/bootstrap_agent_loop.py --loop-dir docs/loop --preset research --preset visual
```

Use `--extra-lane "lane|role|write_scope"` for custom teams.

## Registry Rules

`agent-lanes.md` must include at least:

```md
| lane | thread_id | role | write_scope | worklog | status | heartbeat |
| --- | --- | --- | --- | --- | --- | --- |
```

`heartbeat` is the last ISO-8601 UTC time the lane reported liveness (default `-`); the doctor uses it for orphan/stale-lane recovery. Legacy 6-column registries upgrade automatically on the next bootstrap run.

Use stable lane names. Record thread IDs only after they are verified with `read_thread` or an equivalent current-session tool. If a thread ID cannot be read, mark it stale instead of using it.

Keep write scopes disjoint where possible. Product may update loop planning files; implementation may update code; review may write review notes. Avoid letting multiple lanes casually edit the same loop file.

## Request State

`requests.md` is the durable queue. Use it to resume work instead of re-planning from memory.

Lifecycle:

```text
PLANNED -> REQUESTED -> IMPLEMENTING -> IMPLEMENTATION_DONE -> REVIEWING -> FIX_REQUESTED -> ACCEPTED | BLOCKED
```

Reuse the same `request_id` across fix cycles and increment `iteration`. Product is the default owner of final acceptance; review can recommend pass/fail but product decides whether to mark tracker checkpoints complete unless the user assigns that authority elsewhere.

## Message Protocol

Read `references/protocol.md` when composing or validating cross-agent messages.

Use these message types:

- `IMPLEMENTATION_REQUEST`: product -> implementation
- `IMPLEMENTATION_DONE`: implementation -> product or review
- `REVIEW_REQUEST`: product or implementation -> review
- `REVIEW_DONE`: review -> product
- `FIX_REQUEST`: review or product -> implementation
- `BLOCKED`: any lane -> product
- `LOOP_STATUS`: any lane -> product or coordinator

Every message must include `message_type`, `request_id`, `iteration`, `from_lane`, `to_lane`, `status`, `source_docs`, `scope` or `artifact_scope`, `acceptance_criteria`, and `expected_reply`.

## Dispatch Rules

Before sending to another lane:

1. Pass the handoff readiness gate in `references/loop-state.md`.
2. Update `requests.md` and the sender lane `outbox.md`.
3. Save the exact message under `docs/loop/messages/<request_id>/`.
4. If the target lane has a verified thread ID, use `send_message_to_thread`.
5. Record delivery status in the message copy and sender worklog.
6. If the thread tool is unavailable, append the message to the target lane `inbox.md` and add a pending entry to `handoff.md`.

Do not ask the human to copy/paste unless no thread tool or durable inbox fallback can be used.

## Lane Behavior

When product creates an implementation request, it must define scope, non-goals, acceptance criteria, source docs, and expected reply.

When implementation receives a request, it reads the named source docs and loop files, implements only the requested scope, runs verification, updates its worklog/current state, and returns `IMPLEMENTATION_DONE`.

When review receives a request, it evaluates against acceptance criteria, not implementation intent. It sends `REVIEW_DONE` on pass or `FIX_REQUEST` with exact failed criteria and evidence on fail.

When a fix is requested, implementation reuses the original `request_id`, increments `iteration`, and sends another `IMPLEMENTATION_DONE`.

## Verification Integrity

Verification is a gate, not a formality. A request reaches `ACCEPTED` only when every acceptance criterion has passing evidence from a verification command that actually ran and exited 0.

Core rule:

```text
verification cannot run -> BLOCKED, never ACCEPTED
```

- If a checkpoint's verify command cannot run (missing tooling, credentials, environment, or data), mark the request `BLOCKED` and report what is needed. Do not record "accepted with caveat".
- Do not emit a completion token from unverified state. `SHIP_CHECK_OK` is valid only when every checkpoint verify command was actually run and its recorded exit code in `docs/loop/evidence/*.json` is 0; otherwise the loop is not shippable. The gate reads those recorded exit codes; it never runs the commands for you.
- "Tests not run", "could not build", or "unverified" are blockers, not acceptances. Review must send `FIX_REQUEST` or `BLOCKED`, never `REVIEW_DONE`, when evidence is absent.
- Record the exact command, exit code, and output location in the message and the lane worklog so the next lane can re-run it.

## Continuation And Auto-Chain

Use Codex Loop Engineering for long work:

- Keep `goal.md` durable.
- Keep `tracker.md` as the phase dashboard.
- Keep `constraints.md` as boundaries.
- Keep `handoff.md` as the continuation state.

Only create a continuation thread when `auto_chain_next_session: true`, unchecked work remains, no blocker exists, state files are updated, and the thread health check in `references/loop-state.md` can be completed. Returned thread IDs are provisional until verified.

## Stop Conditions

Stop and report clearly when:

- thread tools are not available and durable fallback cannot be written;
- a target lane has no verified thread ID and thread creation is not allowed;
- the current checkpoint is not closed enough to hand off;
- verification cannot run, or any checkpoint verify command exits non-zero (mark the checkpoint BLOCKED, never accept-with-caveat; the implementation lane runs each verify command and records its real exit code under `docs/loop/evidence/*.json`, and `SHIP_CHECK_OK` is emitted only when the gate reads every such record as exit 0);
- `budget_exhausted: true` is set in `docs/loop/loop-budget.md`;
- a blocker needs credentials, approval, external data, or destructive action;
- lanes would need to write the same files concurrently;
- the next action would violate `constraints.md`;
- `max_fix_cycles` is reached;
- auto-chain would create an unbounded or duplicate continuation.

---

Every rule above is one reference implementation of the discipline in `references/methodology.md#the-nine-invariants`; when in doubt, that file is the contract and this one is the proof.
