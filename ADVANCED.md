# Advanced reference

Deeper mechanics behind the [README](README.md). The plain-language version is there; this is the detail.

## Why this exists

The skill conditionally solves ordinary developers' multi-agent coordination pain. It is a control and audit
layer — not a promise that multiple agents become cheap, fully autonomous, or impossible to stall.

- **Know when the agents need you.** The dashboard raises a "Ready for you" banner, moves the relevant lane to
  the top, and names the conversation to open.
- **Keep project state out of disposable chat history.** Goals, requests, handoffs, messages, decisions, and
  evidence live in the repository, so another session can resume from files.
- **Reduce agents stepping on the same files.** A lane is one ongoing agent job with an explicit write scope;
  the reference workflow requires pairwise-disjoint scopes and can reject out-of-scope commits.
- **Leave a reconstructable history.** Lane-labelled commits, saved message envelopes, an append-only
  transition log, and per-command evidence record what moved and why.
- **Avoid abandoned dirty work.** Every lane closes its turn with a commit; a paused loop is expected to be
  fully committed, and health checks surface leftover in-scope work.

These properties come from the [nine methodology invariants](skills/codex-agent-loop-orchestrator/references/methodology.md),
not from the dashboard UI.

## Core guarantees (and their limits)

- **Machine-checked completion gate.** `SHIP_CHECK_OK` is emitted only when the checker can read passing
  exit-code evidence. Missing, malformed, or non-zero evidence fails closed. The gate validates records; it
  does not pretend to have run the tests itself.
- **Independent review lane.** Review checks unmet criteria, scope creep, and "looks done but is wrong"
  outcomes before product accepts a slice.
- **Human-QA gate for user-facing work.** Machine checks and review happen first; the request then stays in
  `REVIEWING` until a human operates the UI and confirms it.
- **Red-capable acceptance criteria.** Each criterion names a command that can fail when the requirement is
  violated. A check that stays green on garbage output is not evidence.
- **Invariants-first intake.** Data and multi-step systems record the rules that must never break in
  `goal.md`, then carry the applicable invariants into each request.
- **Bounded recovery.** Heartbeats, stalled-handoff findings, explicit fix-cycle caps, and a durable budget
  provide recovery paths — without claiming to wake a stopped conversation automatically.
- **Runtime tier guidance.** Each lane records an abstract model tier, defaults to the host's highest tier, and
  surfaces observed-tier mismatches; a human can opt a lane down.

Implementation and limits live in the skill's
[Health Check](skills/codex-agent-loop-orchestrator/SKILL.md#health-check),
[Verification Integrity](skills/codex-agent-loop-orchestrator/SKILL.md#verification-integrity), and
[Model Tier Policy](skills/codex-agent-loop-orchestrator/SKILL.md#model-tier-policy) sections.

## Lanes and ownership

The default team is `product`, one build lane, and `review`. Add `data-eng`, `frontend`, `security`, or another
specialist only when it owns a recurring responsibility with clear input, output, routing, and a disjoint write
scope. Lanes are disciplines, not personalities or product features.

Product owns the loop ledger under `docs/loop/**`. Build lanes own separate code and test subtrees. Every lane
also owns its `docs/loop/lanes/<lane>/**` worklog area.

## The request lifecycle

`requests.md` is the queue and recovery index. The same `request_id` is reused across a blocker fix cycle,
while `iteration` increments:

```text
PLANNED -> REQUESTED -> IMPLEMENTING -> IMPLEMENTATION_DONE
        -> REVIEWING -> FIX_REQUESTED -> ACCEPTED | BLOCKED
```

Typed messages are saved under `docs/loop/messages/<request_id>/` before cross-conversation delivery. If thread
delivery is unavailable, an atomic file inbox preserves the message — but a file inbox is not an automatic
worker.

## Completion is machine-checked

The implementation lane runs each acceptance command and writes a flat evidence record containing the request,
checkpoint, command, exit code, and timestamp. `completion_gate.py` reads those records. Verification
unavailable means `BLOCKED`, never "accepted with a caveat."

## User-facing work waits for human QA

After machine evidence and review pass, a UI request remains in `REVIEWING`. Product sends one URL and a short
try-it instruction; only an explicit `human_qa: confirmed` record unlocks `ACCEPTED`.

## The dashboard

The dashboard is a local viewer over repo files and the read-only health check. It shows Progress, the current
human gate, lane ownership, requests, evidence, Git/hook health, usage availability, and the run log. The human
stays in the product conversation until the banner says where to act.

## Git model

The reference workflow uses **one shared branch with a linear, lane-labelled commit history** — a convention,
not a branch restriction enforced by the scripts.

- **Commit as the lane on every turn.** A lane finishes its slice, updates its worklog and durable request
  state, then commits before replying or handing off.
- **Arm the scope guard.** `install_precommit.py` installs a Git pre-commit check. With it active, missing
  `CODEX_LANE` fails closed, and staged files outside that lane's declared scope are rejected.
- **Keep write scopes disjoint.** Static lane scopes must be pairwise disjoint; dynamic file leases cover
  bounded exceptions. The guard acts at commit time, so it does not prevent two processes from editing the same
  file before a commit.
- **Pause only from a clean checkpoint.** A paused loop should be fully committed. Product checks
  `git status --porcelain`, and the health check reports attributable in-scope leftovers.
- **Use a private remote only as backup.** It can preserve checkpoint commits for disaster recovery; it is not
  the lane message bus, and sensitive or raw data must never be committed merely because the remote is private.

```bash
CODEX_LANE=frontend git commit -m "frontend: finish request REQ-004"
```

```powershell
$env:CODEX_LANE = 'frontend'
git commit -m 'frontend: finish request REQ-004'
```

### Why not one Git worktree per lane?

This reference implementation depends on all lanes seeing the same request ledger, evidence, and transition log
immediately. It deliberately serializes writes when scopes conflict and does not implement branch creation,
merges, rebases, or cross-worktree reconciliation. Per-lane worktrees would add a second coordination system
and can leave one lane acting on a stale ledger. If you choose worktrees, you are designing a different
implementation and need an explicit merge/reconciliation protocol; the reference scope guard does not provide
one.

## Daily use

Live in the long-running **product conversation** and keep the dashboard open. Product is the durable front
door for new work, acceptance changes, and final product judgment.

For a UI change, ask product in the **same conversation**:

```text
Please tighten the dashboard header spacing and improve the primary button hierarchy. Route this through the existing frontend lane and the normal review + human-QA gates.
```

Do not open an ad-hoc conversation for each change and do not ask a build lane to bypass product. A direct lane
request is routed back into the normal request lifecycle; it is never a shortcut past evidence or review.
Create a replacement lane conversation only when the registered one is genuinely stale or missing, then adopt
the replacement into the existing lane row.

## Repository layout

```text
codex-app-loop-crew/
├── .agents/plugins/marketplace.json
├── .codex-plugin/plugin.json
├── assets/
├── skills/codex-agent-loop-orchestrator/
│   ├── SKILL.md
│   ├── agents/
│   ├── references/
│   └── scripts/
├── install.ps1
├── install.sh
├── COMPARISON.md
├── ADVANCED.md
├── README.zh-CN.md
├── README.md
└── LICENSE
```
