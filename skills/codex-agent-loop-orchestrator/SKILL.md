---
name: codex-agent-loop-orchestrator
description: Set up or continue a repo-local multi-agent Codex workflow with lane registration, durable request state, handoff readiness gates, send_message_to_thread delivery, checkpoint review/fix loops, and optional Codex Loop Engineering auto-chain. Use when users mention agent-lanes, Product/Implementation/Review agents, session handoff, loop files, requests.md, checkpoint iteration, or sustainable multi-agent collaboration.
---

# Codex Agent Loop Orchestrator

Create a thin orchestration layer around Codex threads. Keep project truth in repo files, keep agent identities in a registry, and use thread tools only as the message bus and session starter.

## Identity And Fit

This is a **software-development orchestrator**. Its native job is building and changing software: code, tests, docs, and the machine-checkable evidence that proves a checkpoint is done. Read that as your default before proposing any team shape.

- **Operations-domain use is conditional.** Only run an operations team (data ingestion, content production, ongoing monitoring, human workflows) through this loop when *every* checkpoint has a machine-checkable acceptance command. Where a step's quality can only be judged by a human, do not pretend the gate covers it: scope the completion gate to **structural completeness** (the artifact exists, is well-formed, passes what can be checked) and hand the **quality** judgment to an explicit human review step. Never let a green gate imply human-quality sign-off it did not perform.
- **Recurring work favors a tool, not a standing team.** If the task will repeat, the highest-leverage move is usually to have the loop *build a reusable tool/script* that does it, then run that tool - not to keep a multi-lane team alive doing the work by hand. Say this during intake when you notice repetition.

### Task-Size Gate (do not over-build the loop)

Before spinning up a loop at all, size the task. The loop's process machinery (evidence trail, independent review, durable handoff state) only pays for itself on work that is multi-day, multi-agent, sensitive-data-gated, or must survive compaction/handoff. Measured on a real one-shot MVP, a single direct session delivered *more* product for roughly one-eighth the tokens and one-fifteenth the wall time of the loop.

So, during intake, **if the work plausibly fits one session** - about **under two hours, one agent, low audit/recoverability need, and no sensitive-data gates** - say so plainly and **recommend a direct session instead of a loop**. Spinning up a loop anyway, for a task that does not need it, is the skill making the task more complicated than it is. Only build the loop when at least one of these is true: the work spans multiple sessions/days, needs genuine parallel lanes, touches credentials or private/financial data behind a human gate, or must be reconstructable and independently re-verifiable by an outsider from files alone.

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

4. **Version-control the loop, then arm the scope guard.** The loop's write-scope and lease enforcement is a git pre-commit hook, and invariant 1 requires the loop live under version control. Check first:

```bash
git rev-parse --is-inside-work-tree
```

If that fails (not a repo), initialize one before anything else depends on it: `git init`, make an initial commit that captures the bootstrapped loop, and append a run-log row recording the init. Then offer to arm the guard:

```bash
python <skill_dir>/scripts/install_precommit.py --loop-dir docs/loop
```

CAUTION: the guard **fails closed without `CODEX_LANE` set** - a commit with no lane is rejected. So pair the install with commit-as-lane guidance and use it on every commit: `CODEX_LANE=<lane> git commit -m "..."`. If a lane cannot reliably export `CODEX_LANE` yet (for example the initial bootstrap commit before any lane exists), keep the guard warn-first by invoking `precommit_scope_guard.py --allow-unscoped` for that commit rather than skipping enforcement wholesale. Without a git repo the guard cannot install and write_scope/leases silently degrade to the honor system - so the git check is not optional.

5. Read `agent-lanes.md`, `requests.md`, `loop-policy.md`, and the relevant lane `current.md` before sending or resuming cross-agent work.
6. Use `tool_search` for thread tools before assuming they exist. Look for `create_thread`, `read_thread`, `send_message_to_thread`, and `set_thread_title`.
7. On a Codex-app host, `tool_search` first for the cross-thread `codex_app.*` tools (the app's own thread/message tools) and prefer them for delivery. When they are absent (headless `codex exec`, sandboxed, or a non-app host), fall back to the durable file inbox (`deliver_message.py` / `inbox/`); the loop works identically either way because delivery is just one plane over repo files.

## Intake (before the first request)

A freshly bootstrapped `goal.md`/`tracker.md` contains only placeholder `Done When` lines. **Placeholders mean there is no objective yet - and "no objective yet" is the ABSENCE of a request, not a blocked request.** Do not mint a `PLANNED -> BLOCKED` "no goal yet" placeholder request; a first-contact loop that shows a red BLOCKED row before the human has spoken looks like failure. Instead, run a short intake conversation and create the first real request only after the human answers.

Run the Task-Size Gate first (see Identity And Fit). If the work plausibly fits one session, say so and recommend a direct session rather than a loop. If a loop is warranted, ask the human, in plain language:

1. **Objective and Done-When.** What is the single durable objective, and what concrete, checkable conditions mean it is done? Write the answers into `goal.md` (Objective + Done When) and mirror the conditions into `tracker.md` before creating any request.
2. **Which fork - build the software, or be the operations team?** Building software (the default) means lanes ship code/tests/docs behind a machine-checkable gate. Being the operations team means the lanes *do the ongoing work* (ingest data, produce content). Only take the operations fork when every checkpoint has a machine-checkable acceptance; otherwise scope the gate to structure and add an explicit human-quality review step (see Identity And Fit). If the work recurs, offer to build a reusable tool instead of standing up a team.
3. **Which cut - by discipline, or by feature?** The default is a **discipline cut**: lanes are development disciplines (product, backend-or-data-eng, frontend, research, security, review), each owning one kind of work across the whole product. A **feature cut** (a lane per product feature) fragments write scopes and re-derives the same discipline in every lane; take it only when features are genuinely independent deliverables with disjoint files. When in doubt, cut by discipline.

Only after the human answers step 1 do you create the first request and append the first run-log row. The intake conversation itself is not a request.

### Proposing Lanes

When you propose the initial team, **default to development disciplines**, not product features and not an operations framing:

- **product** - goals, specs, acceptance criteria, final product judgment.
- **backend or data-eng** - server/data/core code, schema, pipelines, tests.
- **frontend** - UI shell, views, charts, UX, UI tests. Do not omit this for anything with a user-facing surface.
- **research** - source-backed investigation when it recurs.
- **security** - privacy/threat review and the human gate before sensitive data flows.
- **review** - independent acceptance review against the criteria.

Propose only the lanes the goal actually needs (three is the common floor: product, one build lane, review), and add specialists per the Lane Expansion Gate. Do not name a lane after a product feature ("dedupe agent", "merchant agent") - that is a feature cut wearing a lane costume; fold those into the discipline that owns them.

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
- `deliver_message.py` - run instead of writing an inbox by hand. It delivers a saved message atomically (Maildir `tmp` -> `new` rename) so a crash never leaves a half-written inbox entry. When you pass `--from-lane`, it also **stamps that lane's heartbeat** (the `heartbeat` column in `agent-lanes.md` and, if present, `last_updated`/`heartbeat` in `lanes/<lane>/current.md`) - delivering a message is proof the sender is alive (F7). Pass `--no-heartbeat` to opt out.
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

**The human operating model.** The human lives in the **product thread** and watches the **dashboard**; they are pulled into the loop only at gates. They do not babysit every lane. The dashboard's "your turn" banner is what names the moment and the window to go to - a human gate, a stalled handoff, a workerless lane, or a missing-dependency approval - so the human can stay in the product thread until the banner (and the rank-1 lane card) tells them exactly where to act.

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

Title each Codex thread with the **bare lane name** and nothing else - `set_thread_title(<thread_id>, "review")`, not `"<project> loop lane: review"`. Drop the project name and the "loop lane:" boilerplate; the loop refers to a lane by its bare name, so the title must match it exactly. Project context, when a human needs it to tell dashboards apart, comes from the dashboard project name, never the thread title.

Keep write scopes disjoint where possible. Product may update loop planning files; implementation may update code; review may write review notes. Avoid letting multiple lanes casually edit the same loop file.

## Model Tier Policy

Lane threads should not run on whatever light model the host defaults a new thread to. Give each lane the reasoning tier its work needs, per this policy:

- **Coding lanes get the highest available tier.** A coding lane is one that *builds code* - implementation, backend, data-eng, frontend. These carry the hardest reasoning load, so they recommend the **highest** tier the calling host offers.
- **Every other lane gets the second-highest available tier.** Product, review, security, research, docs, and the like recommend the **second-highest** tier.
- **The human only ever opts DOWN.** A person may lower a lane's tier in the registry (see the advisory column below); the skill honors that and never silently raises a lane back up past this policy.

**Resolve tiers at runtime - never hardcode a model name.** Tiers are expressed abstractly ("highest available", "second-highest available") because the concrete model list is host-specific. When you `tool_search` for `codex_app.create_thread`, read its own `model` parameter description: it embeds the calling host's live model list and each model's supported reasoning efforts, and the host validates the combination when the tool runs. Map "highest available" to the top model in that list and "second-highest available" to the next one down; pick a high reasoning effort (`thinking`) that the chosen model supports.

**When create_thread accepts model/effort (the common case), pass them.** Create each lane thread with the resolved tier:

```text
codex_app.create_thread(prompt=<lane kickoff>, target=..., model=<resolved highest|second-highest model>, thinking=<a high effort that model supports>)
```

`model` and `thinking` are optional; omitting them is the safe degradation path (the thread just starts at the host default), so never fail a dispatch because you could not resolve a tier - fall back to the advisory column and tell the human.

**The advisory `tier` column records the recommendation on disk.** `bootstrap_agent_loop.py` writes a `tier` column in `agent-lanes.md` (abstract words `highest` / `second-highest`) and assigns it by the policy above when it registers a lane. It prints the tier hint alongside its `set_thread_title` hint on `--set-thread`, and `--set-thread` adoption preserves an existing tier (a human opt-down is never clobbered). The dashboard renders the recommended tier as a small neutral chip on each lane card, read from this column.

**Degradation when create_thread lacks the model parameter.** Some hosts validate model/effort only at run time or do not expose the parameter at all. When `create_thread` cannot take a tier, the advisory column IS the recommendation: tell the human to pick that tier by hand when they open the thread. This is the same hand-created-thread path as the adoption ritual - when a human opens a thread for a lane, they set the recommended tier from the registry, then run the `--set-thread` adoption line.

**Cost counterweight.** Higher tiers burn more tokens, so tier policy is paired with the loop's existing self-calibration: the dashboard's usage panel shows how much of the Codex quota this workspace has spent, and the `max_fix_cycles` slider caps how many fix-retry rounds one request gets before it is flagged BLOCKED. Those two are the throttle; the `max_fix_cycles` default is right-sized and should not be changed to compensate for tier cost.

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

### Threadless Lane: Adopt A Hand-Created Thread

A lane with no verified thread has no worker. Dispatching a request into its file inbox and waiting is a deadlock: nothing processes the message. This happens when `create_thread` is unavailable at dispatch time (a real mid-run host regression). Two rules:

- **Do not silently wait.** When `create_thread` is unavailable and a lane needs a thread, product must tell the human in plain language: *"lane X needs a thread - open one in the Codex app and paste the adoption line below."* Surface the halt; do not leave a request rotting in an inbox.
- **Adopt the hand-created thread into the EXISTING row.** Once the human opens a thread, adopt it by filling the lane's existing registry row - never by hand-editing `agent-lanes.md`, which duplicates the row:

```bash
python <skill_dir>/scripts/bootstrap_agent_loop.py --loop-dir docs/loop --set-thread <lane>=<thread_id>
```

When the human opens the thread, they should pick the lane's recommended model tier from the registry's advisory `tier` column (see Model Tier Policy) - coding lanes at the highest available tier, everything else at the second-highest. Then title the thread with the bare lane name (`set_thread_title(<thread_id>, "<lane>")`) and proceed with the dispatch. `--set-thread` flips the row's `thread_id` and status to `registered` in place and preserves the advisory tier; the doctor's `workerless_lane_dependency` warning (an error when another lane's active request waits on it) clears once the thread is verified.

## Lane Behavior

When product creates an implementation request, it must define scope, non-goals, acceptance criteria, source docs, and expected reply.

When implementation receives a request, it reads the named source docs and loop files, implements only the requested scope, runs verification, updates its worklog/current state, and returns `IMPLEMENTATION_DONE`.

When review receives a request, it evaluates against acceptance criteria, not implementation intent. It sends `REVIEW_DONE` on pass or `FIX_REQUEST` with exact failed criteria and evidence on fail.

When a fix is requested, implementation reuses the original `request_id`, increments `iteration`, and sends another `IMPLEMENTATION_DONE`.

### In-Turn Report-Back Ritual (hard gate, not advice)

Codex threads run one turn and stop, so a handoff MUST complete inside the same turn as the work. Finishing the code, or even getting `SHIP_CHECK_OK`, is not the end of your turn - if your turn ends there, the requester waits forever and a human has to hand-carry the baton.

**Your turn is NOT finished until you have, in this same turn:**

1. **Sent the reply message** to the next lane - `send_message_to_thread` (or the `codex_app.*` equivalent), or the durable file-inbox fallback (`deliver_message.py`) when no thread tool is available.
2. **Updated `requests.md`** - move the request to its next status and owner (do not leave it parked in `IMPLEMENTING`/`REVIEWING` after the work is done).
3. **Appended the `loop-run-log.md` row** for that transition, in the same step that updated `requests.md`.
4. **Refreshed your heartbeat** - the `heartbeat` column in `agent-lanes.md` and the `last_updated`/`heartbeat` mirror in your lane `current.md`. (`deliver_message.py --from-lane <you>` does this for you.)

All four are mandatory every time you close a slice. Do not stop after step 1's work is done and leave the reply, the ledger, the log, or the heartbeat for "next turn" - there is no guaranteed next turn. The doctor flags a request whose work is done but that never advanced as `stalled_handoff`, naming the lane to nudge.

## Verification Integrity

Verification is a gate, not a formality. A request reaches `ACCEPTED` only when every acceptance criterion has passing evidence from a verification command that actually ran and exited 0.

Core rule:

```text
verification cannot run -> BLOCKED, never ACCEPTED
```

- If a checkpoint's verify command cannot run (missing tooling, credentials, environment, or data), mark the request `BLOCKED` and report what is needed. Do not record "accepted with caveat".
- A `BLOCKED` on a **missing dependency** is a distinct blocker type with a built-in exit ramp, not a dead end: record exactly what is missing and the exact install command (distinguishing a `pip` package from a `system` binary), mark the message with the greppable `blocker: missing_dependency` format, and ask the human per `dependency_install` in `loop-policy.md` (`ask` / `auto-pip-only` / `never`). On approval, install, re-run the exact failed verify, record fresh evidence, and unblock the SAME `request_id` (increment `iteration`). See `references/protocol.md` "Missing-Dependency Blocker".
- Do not emit a completion token from unverified state. `SHIP_CHECK_OK` is valid only when every checkpoint verify command was actually run and its recorded exit code in `docs/loop/evidence/*.json` is 0; otherwise the loop is not shippable. The gate reads those recorded exit codes; it never runs the commands for you.
- "Tests not run", "could not build", or "unverified" are blockers, not acceptances. Review must send `FIX_REQUEST` or `BLOCKED`, never `REVIEW_DONE`, when evidence is absent.
- Record the exact command, exit code, and output location in the message and the lane worklog so the next lane can re-run it.
- Passing the gate does not end your turn. A green `SHIP_CHECK_OK` with no reply sent, no `requests.md` transition, and no run-log row is a stalled handoff, not a completed one: complete the In-Turn Report-Back Ritual above in the SAME turn, or the loop does not advance.

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
