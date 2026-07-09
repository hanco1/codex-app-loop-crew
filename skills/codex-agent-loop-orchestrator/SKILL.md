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

Run the Task-Size Gate first (see Identity And Fit). If the work plausibly fits one session, say so and recommend a direct session rather than a loop.

**Intake is an interview, not a questionnaire.** Grill the objective one step at a time:

- **Ask ONE question at a time and wait for the answer** before the next. A bulk list of questions is bewildering and loses the dependency order between answers.
- **Attach your own RECOMMENDED answer to every question**, so the human edits a proposal instead of authoring cold ("I'd recommend X because Y - does that hold, or do you want Z?").
- **Look up any fact the repo or host can answer - never ask it.** The stack, the test runner, the existing UI surface, whether a tool is installed: read the files or `tool_search`; reserve questions for genuine decisions only the human can make.
- **Stop rule:** when the objective is checkable (a concrete Done-When is in hand), STOP asking and write `goal.md`. Do not over-interview - a checkable objective is the signal to move, not to keep drilling.

The questions to cover, one at a time (each with your recommended answer):

1. **Objective and Done-When.** What is the single durable objective, and what concrete, checkable conditions mean it is done? Write the answers into `goal.md` (Objective + Done When) and mirror the conditions into `tracker.md` before creating any request.
2. **Which fork - build the software, or be the operations team?** Building software (the default) means lanes ship code/tests/docs behind a machine-checkable gate. Being the operations team means the lanes *do the ongoing work* (ingest data, produce content). Only take the operations fork when every checkpoint has a machine-checkable acceptance; otherwise scope the gate to structure and add an explicit human-quality review step (see Identity And Fit). If the work recurs, offer to build a reusable tool instead of standing up a team.
3. **Which cut - by discipline, or by feature?** The default is a **discipline cut**: lanes are development disciplines (product, backend-or-data-eng, frontend, research, security, review), each owning one kind of work across the whole product. A **feature cut** (a lane per product feature) fragments write scopes and re-derives the same discipline in every lane; take it only when features are genuinely independent deliverables with disjoint files. When in doubt, cut by discipline.

**One MANDATORY question for user-facing goals.** If the deliverable is something a human operates (a UI, a dashboard, an interactive tool), you MUST ask: *"walk me through how you'll actually operate this - input method, file selection, what you look at first."* This is the question run 2 never asked: the loop shipped a path textbox while the user wanted a file picker, and nobody found out until after the pause. Answer it and the file-picker-vs-path-textbox gap dies at intake.

**Hybrid intake: cap the serial questions, then batch the rest.** One-at-a-time is the default ONLY for **fork-shaped** decisions - a decision whose answer changes which later questions you ask (the build-vs-operations fork rewrites everything downstream; the redacted-sample questions only exist if the goal names real data). Decisions that are **independent** - their answer changes no other question - do not need the serial drip, and dripping them one message at a time is needless friction. So:

- **Cap at three.** After at most three serial questions you MUST offer questionnaire mode: one message listing every remaining INDEPENDENT decision, each with your own recommended answer, and the human replies once. Phrase it as an offer - *"here are the rest as one list, each with my recommendation; reply once and change anything you want."*
- **Switch on request, at any time.** The human may ask for the whole list at any point - "list them all" (in any language) - and you switch to questionnaire mode immediately, even before the third question.
- **Batch independent items only.** A decision whose answer would change another item is held back, NOT put in the batch: ask it serially AFTER the batch resolves, because its answer may rewrite the questions that follow. The batch carries only the decisions that stand alone.

Questionnaire mode changes the DELIVERY (one message instead of many), never the substance: every batched item still carries your recommended answer, and the stop rule still governs - when the objective is checkable, stop asking and write `goal.md`, whether you got there serially or by batch.

**Invariants-first intake.** For any goal that **handles data or does multi-step processing** (an importer, a pipeline, a dashboard over a store - anything where records flow through steps), RIGHT AFTER the objective is confirmed and before the first request, ask the human for the domain **invariants** - the rules that must never break. Features say what to build; invariants say what must never break, and they become standing acceptance material for every later request. Do not ask cold: DRAFT a recommended set from the goal and have the human edit it, the same grilling style as every other intake question.

Draft from this class of invariant, adapted to the goal:

- **No silent drops** - every parsed record lands in the raw store even if it is unclassifiable or duplicate-suspected; mark it, never let it vanish.
- **Human edits outrank machine re-runs** - a re-import must not overwrite a human correction.
- **Machine ingest is add-only** - never a silent delete or overwrite; dedupe marks or merges, it never deletes.
- **External-effect operations carry a `run_id` and recoverable state** - an import can be inspected and undone as a unit.
- **Every displayed number traces to its source** - each figure ties back to a file, row, or rule.

Write the agreed invariants into `goal.md` under a dedicated **`## Invariants`** section - that is their ONE canonical home; the request template and the review gate both read them there. **Worked example (run-4 expense app):** transactions are never silently dropped; a human's category or duplicate correction survives a re-import; ingest is add-only; each import carries a `run_id` and can be undone as a unit; every dashboard number is traceable to its source row. A data goal's invariants usually rhyme with that set.

Only after the human answers step 1 do you create the first request and append the first run-log row. The intake conversation itself is not a request.

**Redacted-sample ritual (when the goal names human-provided real data).** If any Done-When condition depends on the human's real data (a real bank statement, export, or invoice), get the redacted-sample approval ONCE at intake: ask the human to approve a sanitized excerpt (a few rows with identifying detail scrubbed) or a field-shape spec (which column is the ISO date, which is merchant text, which is the signed amount, and a realistic row count). Later slices assert **field-level correctness** against that approved derivative - row count > 0, valid calendar dates, merchant non-empty and non-numeric, sign convention - and record only counts/booleans as evidence, never a raw row. "Parses without error" alone is never sufficient real-data evidence (run 2 parsed cleanly and still produced `merchant="9"` and 0 imported rows). See `references/protocol.md` "Real-input correctness".

### Proposing Lanes

When you propose the initial team, **default to development disciplines**, not product features and not an operations framing:

- **product** - goals, specs, acceptance criteria, final product judgment.
- **backend or data-eng** - server/data/core code, schema, pipelines, tests.
- **frontend** - UI shell, views, charts, UX, UI tests. Do not omit this for anything with a user-facing surface.
- **research** - source-backed investigation when it recurs.
- **security** - privacy/threat review and the human gate before sensitive data flows.
- **review** - independent acceptance review against the criteria.

Propose only the lanes the goal actually needs (three is the common floor: product, one build lane, review), and add specialists per the Lane Expansion Gate. Do not name a lane after a product feature ("dedupe agent", "merchant agent") - that is a feature cut wearing a lane costume; fold those into the discipline that owns them.

**Write-scope rules (hard, mechanically checked).** A lane team is only well-formed when the write scopes obey all three. The run-4 coordinator broke the first two - it proposed data-eng AND frontend both owning `src/**, tests/**` (overlapping, so the precommit guard could not tell whose commit was legal) and product owning only `tracker.md, handoff.md` (too narrow to commit its own ledger). Do not repeat that:

1. **Pairwise disjoint.** No lane's scope glob may equal or contain another lane's. Two lanes sharing `src/**`, or one owning `src/**` while another owns `src/ui/**`, leaves the precommit scope guard unable to arbitrate between them. Cut the shared tree into disjoint subtrees instead (see the worked example). The doctor flags a violation as `write_scope_overlap`, naming both lanes and the offending globs.
2. **Product owns the ledger.** Product's scope MUST include `docs/loop/**` - it commits the ledger (requests.md, loop-run-log.md, agent-lanes.md, goal.md, tracker.md, handoff.md) - plus `.gitignore`. A product scope narrowed to just tracker.md + handoff.md makes product's own close-the-turn commits fail the hook. The doctor flags a product lane that does not cover `docs/loop/**` as `product_scope_gap`.
3. **Each lane owns its own lane dir.** Every lane's scope includes its own `docs/loop/lanes/<lane>/**` (its worklog/current/inbox/outbox). These per-lane dirs nest under product's `docs/loop/**` by design and are the ONE expected nesting: the disjointness check exempts a lane's own lane dir, exactly as the G19 review carve-out exempts the same ritual writes. (Product's `docs/loop/**` already covers its own `docs/loop/lanes/product/**`, so product needs no separate entry.)

Both `write_scope_overlap` and `product_scope_gap` are WARNING-only; they never block handoff, but a proposed team that trips either is not yet well-formed - fix the cut before dispatching.

**Worked example (a correct disjoint cut).** `src/**` + `tests/**` split into disjoint core-vs-ui subtrees:

| lane | write_scope |
| --- | --- |
| product | `docs/loop/**; docs/product/**; .gitignore` |
| data-eng | `src/core/**; tests/core/**; docs/data/**; docs/loop/lanes/data-eng/**` |
| frontend | `src/ui/**; app/**; tests/ui/**; docs/design/**; docs/loop/lanes/frontend/**` |
| review | `docs/loop/lanes/review/**` |

No two scopes overlap (`src/core/**` vs `src/ui/**`, `tests/core/**` vs `tests/ui/**`, `docs/data/**` vs `docs/design/**` vs `docs/product/**` are all disjoint); product holds the whole ledger; each lane also owns its own `docs/loop/lanes/<lane>/**`.

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

Keep write scopes **pairwise disjoint** - this is a hard rule, not a preference (see "Proposing Lanes" -> "Write-scope rules" for the mandate, the product-ledger rule, and the worked example; the doctor enforces it with `write_scope_overlap` / `product_scope_gap`). Product owns the `docs/loop/**` ledger; each build lane owns its own code subtree; every lane owns its own `docs/loop/lanes/<lane>/**`. Never let two lanes claim the same file.

## Model Tier Policy

Lane threads should not run on whatever light model the host defaults a new thread to. Give each lane the reasoning tier its work needs, per this policy:

- **Every lane defaults to the highest available tier.** Whatever kind of work a lane does - building code, authoring acceptance criteria, deciding ACCEPT in review, research, security - it recommends the **highest** tier the calling host offers. This supersedes the older coding/non-coding split (F8): run-2/run-3 showed the criteria-authoring and review lanes are exactly the quality leverage points that bind the whole loop, so no lane is auto-downgraded. The cost counterweights are the usage panel, the budget stop rule, and the per-lane opt-down below - not a lower default tier.
- **Downgrading is a manual human action.** A person may set any lane DOWN to any lower tier in the registry `tier` column (see the advisory column below) - for example lower a rarely-binding lane to save tokens. The recorded tier column IS the policy for that lane; the skill honors it exactly and **never silently deviates** from the recorded tier - it neither downgrades to a host default nor raises past the recorded value on its own. Whatever tier the registry records is the tier the lane runs; a change is a human edit to that column, never a silent drift.

**Resolve tiers at runtime - never hardcode a model name.** Tiers are expressed abstractly ("highest available", and "second-highest available" for a manually opted-down lane) because the concrete model list is host-specific. When you `tool_search` for `codex_app.create_thread`, read its own `model` parameter description: it embeds the calling host's live model list and each model's supported reasoning efforts, and the host validates the combination when the tool runs. Map "highest available" to the top model in that list (and, for a lane a human has opted down, map the recorded lower tier to the matching model further down the list); pick a high reasoning effort (`thinking`) that the chosen model supports.

**When create_thread accepts model/effort (the common case), pass them.** Create each lane thread with the resolved tier - the highest available by default, or whatever lower tier the registry records for an opted-down lane:

```text
codex_app.create_thread(prompt=<lane kickoff>, target=..., model=<model for the lane's recorded tier>, thinking=<a high effort that model supports>)
```

`model` and `thinking` are optional; omitting them is the safe degradation path (the thread just starts at the host default), so never fail a dispatch because you could not resolve a tier - fall back to the advisory column and tell the human.

**Resolving the create_thread guidance conflict (G14d).** `create_thread`'s own parameter description typically says *"do not specify a model unless the user explicitly requests one."* That does not conflict with this policy: **the loop's recorded tier policy IS the user's explicit request.** The human set up this loop and its tier column (and may manually opt any lane down to a lower tier, per the downgrade rule above), so passing `model` + `thinking` for a lane is honoring an explicit user choice, not overriding a default. Pass them accordingly; do not withhold the tier out of deference to the generic "no model unless asked" default.

**Record the OBSERVED model at creation/adoption (G14a).** As soon as a lane's thread is created or adopted, record what it is ACTUALLY running in that lane's `lanes/<lane>/current.md` `model_observed:` line, as observed DATA with the abstract tier tag in parentheses - for example `model_observed: <model-id> <effort> (<tier>)`. The concrete model id is data (it is fine here because this line is a recorded observation, not policy text); the `(highest)` / `(second-highest)` tag is what the doctor compares to the registry tier. This is what makes the recommended-vs-observed tier visible in the dashboard and lets the doctor flag a `tier_mismatch`, so a human who opens the thread and sees a different model can tell an intentional setting from a silent drift. When you adopt a hand-created thread with `--set-thread`, you can stamp it in the same call: `--observed-model '<lane>=<model-id> <effort> (<tier>)'`.

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

When the human opens the thread, they should pick the lane's recommended model tier from the registry's advisory `tier` column (see Model Tier Policy) - the highest available tier by default, or the lower tier the column records if the lane was manually opted down. Then title the thread with the bare lane name (`set_thread_title(<thread_id>, "<lane>")`) and proceed with the dispatch. `--set-thread` flips the row's `thread_id` and status to `registered` in place and preserves the advisory tier; the doctor's `workerless_lane_dependency` warning (an error when another lane's active request waits on it) clears once the thread is verified.

## Lane Behavior

When product creates an implementation request, it must define scope, non-goals, acceptance criteria, source docs, and expected reply. Each acceptance criterion must name a **red-capable** verify command - one that can FAIL on that criterion's violation, not merely exit 0 when the code runs; a criterion with no command that can go red is a vibe, so sharpen it or drop it (see `references/protocol.md` "Red-capable acceptance criteria").

When implementation receives a request, it reads the named source docs and loop files, implements only the requested scope, runs verification, updates its worklog/current state, and returns `IMPLEMENTATION_DONE`.

When review receives a request, it evaluates against acceptance criteria, not implementation intent, across three named categories: **unmet/partial criteria**; **scope creep** (changed files or behavior outside the request's declared `scope`/`non_goals` - flagged even if it works, using the `scope` globs as the mechanical yardstick); and **looks-done-but-wrong**. Protocol-mandated ritual writes are exempt from the scope-creep comparison - the lane's own heartbeat cell in `agent-lanes.md`, the request's row in `requests.md`, appended `loop-run-log.md` rows, the lane's own `lanes/<lane>/**` files, and the request's `messages/<request_id>/**` / `evidence/**` records are never creep, while writes to OTHER lanes' rows or dirs still are (see `references/loop-state.md` "Review checklist" for the standing list and the run-3 heartbeat-stamp example). It also answers one standing **ease-of-misuse** question ("can a caller/input reach a wrong-but-accepted outcome the criteria did not forbid? name the path or state none found") and records the answer in `REVIEW_DONE`. Each `FIX_REQUEST` finding carries a `severity` (`blocker` | `should-fix` | `nit`): **only a blocker forces a fix cycle and increments `iteration`**; should-fix/nit findings may be accepted-with-notes or batched, so a review that finds only nits sends `REVIEW_DONE` (with the notes), not `FIX_REQUEST`. See `references/loop-state.md` "Review checklist", "Ease-of-misuse question", and "Finding severity".

When a **blocker** fix is requested, implementation reuses the original `request_id`, increments `iteration`, and sends another `IMPLEMENTATION_DONE`. (Only blockers force a fix cycle; should-fix/nit findings are accepted-with-notes or batched and do not increment `iteration` - see the review-lane wording above.)

### In-Turn Report-Back Ritual (hard gate, not advice)

**Close the turn** every time you finish a slice. The full ritual is defined
once in `references/protocol.md` ("Close the turn"); it is a hard gate, not
advice. Finishing the code - even reaching `SHIP_CHECK_OK` - is not the end of
your turn: if your turn ends there, the requester waits forever and a human has
to hand-carry the baton. Read the single source for the exact step list; the
doctor flags a request whose work is done but that never advanced as
`stalled_handoff`, naming the lane to nudge.

### Human Direct-Ask Ritual (hard gate)

Cardinal rule: **no code ships without a request_id and independent review.**

When a human asks a lane directly to do work ("just restyle the header", "add a
pie chart"), the lane's job is to **record the preference and route it into the
normal lifecycle**:

1. Capture the ask as a preference in the lane's `current.md`/`worklog.md` so it
   is durable.
2. Route it: ask **product** to mint an `IMPLEMENTATION_REQUEST` for it, or
   self-mint a request row and cc product. Offer the human the exit in one
   sentence: *"ask product to create the request; it takes one message."*
3. The change then ships the way every change ships - a request row in
   `requests.md`, recorded evidence, and an independent review pass before
   `ACCEPTED`. A direct ask is a shortcut to the request, never a shortcut past
   review.

Routing the ask is the whole job here; it is fast (one message to product) and
it keeps the lineage and the independent review that catch the defects a solo
lane misses.

**Product dispatches; it does not implement.** Even a one-line chart ask becomes
an `IMPLEMENTATION_REQUEST` to the owning lane (frontend owns the UI shell, the
build lane owns the core). Product writing the code itself puts a change outside
its write scope with no request and no review - the exact bypass this gate
exists to close.

### Design Skills For UI Work (G15)

When a lane does UI work - the frontend lane, or any request marked
`user_facing: true` - it applies **both** installed design skills by default,
with a clear division of labor:

- **`han-design-skill-v1` owns the VISUAL STYLE.** Source:
  `https://github.com/hanco1/han-design-skill-v1`. The Han house aesthetic is the
  DEFAULT look - typography, color, layout mood, visual identity. Reach for it for
  anything about how the UI *looks*.
- **`ui-ux-pro-max` owns the UX MECHANICS.** Interaction patterns, accessibility,
  responsive behavior, component usability, and chart/table ergonomics. Reach for
  it for anything about how the UI *works* and how usable/accessible it is.

**Conflict rule.** When the two disagree, the axis decides: a **visual** call
(what it looks like) goes to `han-design-skill-v1`; a **usability or
accessibility** call (how it works, whether everyone can use it) goes to
`ui-ux-pro-max`. This split is stable, so a lane rarely has to arbitrate.

**The human's explicit choice ALWAYS overrides and is recorded.** If the human
names a different design system (or turns one off), that wins over this default.
Record the choice on the request itself: the `IMPLEMENTATION_REQUEST` envelope
carries a `design_system:` line (see `references/protocol.md` "Common Envelope")
naming the design skill(s) in force for that request - the durable, per-request
record. (This request-envelope line is the chosen mechanism over a separate
decision entry: it travels with the request the UI lane actually reads, so the
lane cannot miss it.)

**Lookup by NAME, never by path; absence is never a blocker.** Look each skill up
by its NAME in the host's standard skills locations (for example
`~/.codex/skills/<name>`). Do not hardcode an absolute local path - skills move
between machines. When a skill is **not installed**, note that in the lane's
worklog ("han-design-skill-v1 not found; proceeding with plain good practice")
and proceed - a missing design skill degrades to ordinary good UI practice, it is
never a hard dependency and never blocks the request.

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
- Passing the gate does not end your turn. A green `SHIP_CHECK_OK` with no reply sent, no `requests.md` transition, and no run-log row is a stalled handoff, not a completed one: **close the turn** (the ritual in `references/protocol.md`) in the SAME turn, or the loop does not advance.
- **User-facing slices need a human-QA sign-off before `ACCEPTED`.** A request marked `user_facing: true` (a UI slice, dashboard, or interactive tool - anything a person operates) does not reach `ACCEPTED` on machine evidence alone. Machine evidence still comes FIRST and is unchanged; then, after `REVIEW_DONE`, product asks the human to operate the feature (one message: URL + a 30-second try), the request HOLDS at `REVIEWING` with `next_action: awaiting human sign-off` and a `human_qa_requested` run-log row, and only a `human_qa: confirmed` run-log row unlocks the `ACCEPTED` transition. The tracker checkpoint stays `[~]` while it holds. This hold is normal waiting, not a stall (see `references/loop-state.md` "Human-QA Gate").

## Continuation And Auto-Chain

Use Codex Loop Engineering for long work:

- Keep `goal.md` durable.
- Keep `tracker.md` as the phase dashboard.
- Keep `constraints.md` as boundaries.
- Keep `handoff.md` as the continuation state.
- **Reference sensitive material, never quote it into a handoff or auto-chain seed.** `handoff.md` is re-read (and can be pasted into a fresh thread) on every continuation, so an account number or a full path into a private-sample directory (`data/`, `uploads/`, `private_samples/`, or any dir `constraints.md` marks sensitive) sitting in it leaks across sessions. Name the artifact ("the TD statement", "the redacted sample") instead; the doctor flags obvious leaks with a `handoff_sensitive_content` WARNING.

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
- a lane is asked to change code with no backing request row (route it through the Human Direct-Ask Ritual first; its cardinal rule applies);
- the next action would violate `constraints.md`;
- `max_fix_cycles` is reached;
- auto-chain would create an unbounded or duplicate continuation.

---

Every rule above is one reference implementation of the discipline in `references/methodology.md#the-nine-invariants`; when in doubt, that file is the contract and this one is the proof.
