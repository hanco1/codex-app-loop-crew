<p align="center">
  <strong>Codex Agent Loop Orchestrator</strong>
</p>

<p align="center">
  Durable, review-gated multi-agent Codex work that survives context loss—and tells you exactly when a human is needed.
</p>

<p align="center">
  <img alt="Codex skill" src="https://img.shields.io/badge/Codex-skill-E5DFD2">
  <img alt="Public repository" src="https://img.shields.io/badge/repo-public-6B8A6F">
  <img alt="Local-first state" src="https://img.shields.io/badge/state-repo--local-3D6F59">
  <img alt="License MIT" src="https://img.shields.io/badge/license-MIT-1A1A1A">
</p>

English | [简体中文](README.zh-CN.md)

<p align="center">
  <a href="#quick-start">Quick Start</a>
  |
  <a href="#screenshots">Screenshots</a>
  |
  <a href="#how-it-works">How It Works</a>
  |
  <a href="#install">Install</a>
  |
  <a href="#git-model">Git Model</a>
  |
  <a href="#daily-use">Daily Use</a>
</p>

![Mock Codex-style host showing the product, data-eng, frontend, and review conversations](assets/mock-codex-ui.png)

`codex-agent-loop-orchestrator` is a repo-local operating protocol for long-running Codex projects. It gives each ongoing agent job a named lane, keeps goals and requests in files instead of disposable chat history, requires machine-readable verification evidence, and routes every shipped slice through independent review.

The image above is a mock of a generic Codex-style desktop host (disclosed here rather than on the image). It contains no OpenAI or ChatGPT branding, account identity, or real project data.

## Quick Start

Start in the project folder and paste this into one Codex conversation:

```text
Use $codex-agent-loop-orchestrator for this project.

Build <one-sentence objective with a concrete output and checkable done condition>.

Real data stays local. Never upload, quote, log, commit, or copy raw private data into loop files or handoffs; use only an approved redacted sample or a field-shape description.

Ask one intake question at a time, include your recommended answer, and stop asking when the objective and Done-When are checkable.

Before creating any conversations, propose the smallest useful discipline-based lane team with pairwise-disjoint write scopes, and wait for my approval.

Do not invoke or add any other skill unless I explicitly request it.

After the First Move, report the exact dashboard URL.
```

The orchestrator should first apply its task-size gate. If the work fits one focused session, expect it to recommend a direct session instead of building a loop.

## Why This Exists

The skill conditionally solves ordinary developers' multi-agent coordination pain. It is a control and audit layer—not a promise that multiple agents become cheap, fully autonomous, or impossible to stall.

- **Know when the agents need you.** The local dashboard raises a "Ready for you" banner, moves the relevant lane to the top, and names the conversation to open.
- **Keep project state out of disposable chat history.** Goals, requests, handoffs, messages, decisions, and verification evidence live in the repository, so another session can resume from files.
- **Reduce agents stepping on the same files.** A lane is one ongoing agent job with an explicit write scope. The reference workflow requires pairwise-disjoint scopes and can reject out-of-scope commits.
- **Leave a reconstructable history.** Lane-labelled commits, saved message envelopes, an append-only transition log, and per-command evidence record what moved and why.
- **Avoid abandoned dirty work.** Every lane closes its turn with a commit; a paused loop is expected to be fully committed, and health checks surface leftover in-scope work.

These properties come from the [nine methodology invariants](skills/codex-agent-loop-orchestrator/references/methodology.md), not from the dashboard UI.

## Core Highlights

- **Machine-checked completion gate.** `SHIP_CHECK_OK` is emitted only when the completion checker can read passing exit-code evidence. Missing, malformed, or non-zero evidence fails closed. The gate validates records; it does not pretend to have run the tests itself.
- **Independent review lane.** Review checks unmet criteria, scope creep, and “looks done but is wrong” outcomes before product accepts a slice.
- **Human-QA gate for user-facing work.** Machine checks and independent review happen first. The request then stays in `REVIEWING` until a human operates the UI and confirms it.
- **Red-capable acceptance criteria.** Each criterion names a command that can fail when that requirement is violated. A check that stays green on garbage output is not evidence.
- **Invariants-first intake.** Data and multi-step systems record the rules that must never break in `goal.md`, then carry the applicable invariants into each request.
- **Bounded recovery.** Heartbeats, stalled-handoff findings, explicit fix-cycle caps, and a durable budget provide recovery paths without claiming to wake a stopped conversation automatically.
- **Runtime tier guidance.** Each lane records an abstract model tier, defaults to the host's highest available tier, and surfaces observed-tier mismatches; a human can opt a lane down.

The implementation details and their limits are documented in the skill's [Health Check](skills/codex-agent-loop-orchestrator/SKILL.md#health-check), [Verification Integrity](skills/codex-agent-loop-orchestrator/SKILL.md#verification-integrity), and [Model Tier Policy](skills/codex-agent-loop-orchestrator/SKILL.md#model-tier-policy) sections.

## When Not to Use It

Do not use this loop for a small, low-risk task that one agent can finish in one session—roughly under two hours—when auditability, handoff recovery, sensitive-data gates, and genuine parallel lanes do not matter. Use a direct Codex session instead.

The cost is real. In one same-spec, same-host `n=1` dogfood comparison, the loop took **7.2× the active wall time**, **10.6× the output tokens**, and **36× the total tokens** of the direct session. The loop's review caught correctness defects that the solo build shipped, but one comparison is not a universal benchmark. This protocol buys traceability and independent verification; it does not make multi-agent work free.

It is also a poor fit when the work has no meaningful machine-checkable acceptance surface, or when you need hands-off multi-thread execution but the host cannot create or deliver to long-lived conversations. For recurring operations, prefer using the loop once to build a reusable tool instead of keeping a standing agent team alive.

## Screenshots

These light-theme images come from the real local dashboard served against an archived loop state. The public capture copy redacts local sample paths, account/usage identity, and conversation IDs. The dark image at the top of this README is the mock host UI; the HTML source is preserved at [`assets/mock-codex-ui.html`](assets/mock-codex-ui.html).

### "Ready for you" banner

![Dashboard banner telling the human that data-eng is ready for confirmation](assets/dashboard-your-turn.png)

### Lane card

![Dashboard close-up of the data-eng lane card](assets/dashboard-lane-card.png)

### Progress

![Dashboard Progress section showing three of four checkpoints complete](assets/dashboard-progress.png)

<details>
<summary>Open the full-page dashboard overview</summary>

![Full-page overview of the real loop dashboard](assets/dashboard-overview.png)

</details>

## How It Works

### 1. Lanes split recurring responsibility

The default team is `product`, one build lane, and `review`. Add `data-eng`, `frontend`, `security`, or another specialist only when it owns a recurring responsibility with clear input, output, routing, and a disjoint write scope. Lanes are disciplines, not personalities or product features.

Product owns the loop ledger under `docs/loop/**`. Build lanes own separate code and test subtrees. Every lane also owns its own `docs/loop/lanes/<lane>/**` worklog area.

### 2. Requests move through a durable lifecycle

`requests.md` is the queue and recovery index. The same `request_id` is reused across a blocker fix cycle, while `iteration` increments:

```text
PLANNED -> REQUESTED -> IMPLEMENTING -> IMPLEMENTATION_DONE
        -> REVIEWING -> FIX_REQUESTED -> ACCEPTED | BLOCKED
```

Typed messages are saved under `docs/loop/messages/<request_id>/` before cross-conversation delivery. If thread delivery is unavailable, an atomic file inbox preserves the message—but a file inbox is not an automatic worker.

### 3. Completion is machine checked

The implementation lane runs each acceptance command and writes a flat evidence record containing the request, checkpoint, command, exit code, and timestamp. `completion_gate.py` reads those records. Verification unavailable means `BLOCKED`, never “accepted with a caveat.”

### 4. Review is independent

The review lane checks the request's declared criteria and scope rather than the implementer's intent. Blockers return to the owning build lane under the same request ID. Should-fix and nit findings can be recorded without forcing an endless fix loop.

### 5. User-facing work waits for human QA

After machine evidence and review pass, a UI request remains in `REVIEWING`. Product sends one URL and a short try-it instruction; only an explicit `human_qa: confirmed` record unlocks `ACCEPTED`.

### 6. The dashboard routes human attention

The dashboard is a local viewer over repo files and the read-only health check. It shows Progress, the current human gate, lane ownership, requests, evidence, Git/hook health, usage availability, and the run log. The human stays in the product conversation until the banner says where to act.

## Install

### Ask Codex to install from the repository URL

Open a fresh folder, start one Codex conversation there, and paste this exact message:

```text
Install the Codex skill from https://github.com/hanco1/multi-loops-agents into my personal Codex skills directory. Clone the repository into this fresh folder, run the repository's installer for my operating system (install.ps1 on Windows or install.sh on macOS/Linux), verify that codex-agent-loop-orchestrator is present under my Codex skills directory, and tell me to open a new Codex session so the skill can be rediscovered. Do not modify or push the cloned repository.
```

### Run the installer yourself

Both scripts locate `skills/codex-agent-loop-orchestrator` relative to the repository root and replace the existing installed copy, so rerunning the installer refreshes it cleanly.

Windows PowerShell:

```powershell
git clone https://github.com/hanco1/multi-loops-agents.git
cd .\multi-loops-agents
.\install.ps1
```

If local script execution is blocked:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

macOS or Linux:

```bash
git clone https://github.com/hanco1/multi-loops-agents.git
cd multi-loops-agents
chmod +x install.sh
./install.sh
```

Default destinations:

- Windows: `%USERPROFILE%\.codex\skills\codex-agent-loop-orchestrator`
- macOS/Linux: `~/.codex/skills/codex-agent-loop-orchestrator`

Override the destination with `-SkillsDir <path>` in PowerShell or `CODEX_SKILLS_DIR=<path>` in bash. Open a new Codex session after installation.

<details>
<summary>Optional: install as a Codex plugin marketplace entry</summary>

```bash
codex plugin marketplace add hanco1/multi-loops-agents
codex plugin add codex-agent-loop-orchestrator@multi-loops-agents
```

The plugin manifest is at [`.codex-plugin/plugin.json`](.codex-plugin/plugin.json), and the marketplace manifest is at [`.agents/plugins/marketplace.json`](.agents/plugins/marketplace.json).

</details>

## Git Model

The reference workflow uses **one shared branch with a linear, lane-labelled commit history**. That is a workflow convention, not a branch restriction enforced by the scripts.

- **Commit as the lane on every turn.** A lane finishes its slice, updates its worklog and durable request state, then commits before replying or handing off.
- **Arm the scope guard.** `install_precommit.py` installs a Git pre-commit check. With the guard active, missing `CODEX_LANE` fails closed, and staged files outside that lane's declared scope are rejected.
- **Keep write scopes disjoint.** Static lane scopes must be pairwise disjoint; dynamic file leases cover bounded exceptions. The guard acts at commit time, so it does not prevent two processes from editing the same file before a commit.
- **Pause only from a clean checkpoint.** A paused loop should be fully committed. Product checks `git status --porcelain`, and the health check reports attributable in-scope leftovers.
- **Use a private remote only as backup.** A private remote can preserve checkpoint commits and enable disaster recovery. It is not the lane message bus, and sensitive or raw data must never be committed merely because the remote is private.

Example commit identity:

```bash
CODEX_LANE=frontend git commit -m "frontend: finish request REQ-004"
```

PowerShell:

```powershell
$env:CODEX_LANE = 'frontend'
git commit -m 'frontend: finish request REQ-004'
```

### Why not one Git worktree per lane?

This reference implementation depends on all lanes seeing the same request ledger, evidence, and transition log immediately. It deliberately serializes writes when scopes conflict and does not implement branch creation, merges, rebases, or cross-worktree state reconciliation. Per-lane worktrees would add a second coordination system and can leave one lane acting on a stale ledger. If you choose worktrees, you are designing a different implementation and need an explicit merge/reconciliation protocol; the reference scope guard does not provide one.

## Daily Use

Live in the long-running **product conversation** and keep the dashboard open. Product is the durable front door for new work, acceptance changes, and final product judgment.

For a UI change, ask product in the **same conversation**:

```text
Please tighten the dashboard header spacing and improve the primary button hierarchy. Route this through the existing frontend lane and the normal review + human-QA gates.
```

Do not open an ad-hoc conversation for each change and do not ask `frontend` to bypass product. A direct lane request is routed back into the normal request lifecycle; it is never a shortcut past evidence or review. Create a replacement lane conversation only when the registered one is genuinely stale or missing, then adopt the replacement into the existing lane row.

## Repository Layout

```text
multi-loops-agents/
├── .agents/plugins/marketplace.json
├── .codex-plugin/plugin.json
├── assets/
│   ├── dashboard-overview.png
│   ├── dashboard-your-turn.png
│   ├── dashboard-lane-card.png
│   ├── dashboard-progress.png
│   ├── mock-codex-ui.html
│   └── mock-codex-ui.png
├── skills/codex-agent-loop-orchestrator/
│   ├── SKILL.md
│   ├── agents/
│   ├── references/
│   └── scripts/
├── install.ps1
├── install.sh
├── LICENSE
├── README.zh-CN.md
└── README.md
```

## License

MIT. See [LICENSE](LICENSE).
