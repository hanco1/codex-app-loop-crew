# multi-loops-agents

**A Codex skill that fuses multi-agent lane orchestration with loop engineering.**

`codex-agent-loop-orchestrator` is a thin, repo-local orchestration layer for
running durable multi-agent Codex workflows. It keeps **project truth in repo
files**, keeps **agent identities in a registry**, and treats thread tools as
just a **message bus** - with a file-based inbox/outbox fallback when
cross-thread tools are not available.

The result: Product, Implementation, and Review agents (plus optional
specialist lanes) can hand work back and forth across sessions, survive context
compaction or an app restart, and resume from files instead of re-planning from
chat memory.

---

## Why this exists

Ad-hoc multi-agent setups break the moment a session is compacted, a thread ID
goes stale, or two agents edit the same file. This skill makes the workflow
**deterministic and recoverable**:

- **Multi-agent lanes** - a registry of agents (`agent-lanes.md`), each with a
  verified thread ID, role, disjoint write scope, and worklog.
- **Typed messages** - a fixed envelope and message vocabulary
  (`IMPLEMENTATION_REQUEST`, `IMPLEMENTATION_DONE`, `REVIEW_REQUEST`,
  `REVIEW_DONE`, `FIX_REQUEST`, `BLOCKED`, `LOOP_STATUS`).
- **Durable request lifecycle** - `requests.md` is the queue and recovery index;
  the same `request_id` is reused across fix cycles with an incrementing
  `iteration`.
- **Loop engineering** - `goal.md`, `tracker.md`, `constraints.md`, and
  `handoff.md` hold the durable loop contract so long work can checkpoint and
  auto-chain across sessions.

---

## File model

Everything lives under `docs/loop/` in the target project repo:

```text
docs/loop/
  goal.md          # durable objective + Done When
  tracker.md       # phase/checkpoint dashboard (checkbox list)
  constraints.md   # boundaries and non-goals
  handoff.md       # continuation state for the next session
  agent-lanes.md   # lane registry: lane | thread_id | role | write_scope | worklog | status | heartbeat
  requests.md      # durable request queue and current owner
  loop-policy.md   # fix limits, handoff rules, stop rules
  loop-budget.md   # cost/iteration budget and stop gate
  loop-run-log.md  # append-only transition log
  leases.md        # advisory file leases (enforced by the pre-commit hook)
  evidence/        # per-checkpoint verify records read by completion_gate.py
  messages/<request_id>/<message_type>-iter-<n>.md   # durable message copies
  lanes/<lane>/{inbox,outbox,current,worklog}.md      # inbox.md = manual fallback
  lanes/<lane>/inbox/{tmp,new,cur}/<id>.md            # atomic Maildir delivery (deliver_message.py)
```

Heartbeat is a column in `agent-lanes.md` (not a per-lane file).

The registry is **not a dashboard** - it is a small source-of-truth table that
lets one agent find another agent's thread ID, role, write scope, and worklog.

### Request lifecycle

```text
PLANNED -> REQUESTED -> IMPLEMENTING -> IMPLEMENTATION_DONE -> REVIEWING -> FIX_REQUESTED -> ACCEPTED | BLOCKED
```

Terminal states are `ACCEPTED` and `BLOCKED`. Only the current owner lane moves a
request forward; only Product (or the assigned coordinator) opens a new one.

### Default lanes

| lane | owns | does not own |
| --- | --- | --- |
| product | goals, specs, milestone decisions, acceptance criteria, final product judgment | implementation details beyond constraints |
| implementation | code changes, tests, implementation notes, verification evidence | product expansion or acceptance rewrites |
| review | independent acceptance review, test review, regression concerns | feature implementation |

Add specialist lanes (`research`, `visual`, `security`, `data`, `docs`,
`release`, `media`) only when they reduce context pollution or enable real
parallelism.

---

## Install

### Recommended: as a Codex plugin

This repo is a Codex plugin marketplace. Add the marketplace, then install the
plugin - two commands, no clone required:

```bash
codex plugin marketplace add hanco1/multi-loops-agents
codex plugin add codex-agent-loop-orchestrator@multi-loops-agents
```

`codex plugin list` shows it as `installed, enabled`. The skill lands under
`~/.codex/plugins/cache/multi-loops-agents/codex-agent-loop-orchestrator/<version>/skills/codex-agent-loop-orchestrator/`,
and Codex records the marketplace and plugin in `~/.codex/config.toml`.

To pull later updates, refresh the marketplace snapshot and re-add:

```bash
codex plugin marketplace upgrade
codex plugin add codex-agent-loop-orchestrator@multi-loops-agents
```

`marketplace add` also accepts an HTTPS Git URL
(`https://github.com/hanco1/multi-loops-agents`) or a local path to a clone.

### Fallback: copy the skill directly

If you are not using Codex plugins, clone this repo and run the installer for
your OS. Both installers are **self-locating** (they find the skill under
`skills/`) and **idempotent** (re-running refreshes the installed copy so
it never lags the source).

Windows (PowerShell) - installs to
`%USERPROFILE%\.codex\skills\codex-agent-loop-orchestrator`:

```powershell
.\install.ps1
```

If script execution is blocked, run it for the current process only:
`powershell -ExecutionPolicy Bypass -File .\install.ps1`.

macOS / Linux (bash) - installs to
`~/.codex/skills/codex-agent-loop-orchestrator`
(override with `CODEX_SKILLS_DIR=/custom/path ./install.sh`):

```bash
chmod +x install.sh
./install.sh
```

Each installer prints a success line, for example:

```text
Installed codex-agent-loop-orchestrator -> /home/you/.codex/skills/codex-agent-loop-orchestrator
```

---

## Usage in Codex

Invoke the skill by name from a Codex session:

```text
Use $codex-agent-loop-orchestrator to set up a Product / Implementation / Review loop for this project.
```

```text
Use $codex-agent-loop-orchestrator to continue the multi-agent loop and hand the open request to the review lane.
```

Typical first moves the skill performs (`<skill_dir>` is the directory
containing `SKILL.md` - `~/.codex/skills/codex-agent-loop-orchestrator` for a
direct install, or the `.../skills/codex-agent-loop-orchestrator` path printed
by `codex plugin add` when installed as a plugin):

1. Check whether `goal.md`, `tracker.md`, `constraints.md`, and `handoff.md`
   exist; create them from templates if missing.
2. Bootstrap the registry, request ledger, and lane files:

   ```bash
   python <skill_dir>/scripts/bootstrap_agent_loop.py --loop-dir docs/loop
   ```

   Add preset lanes with `--preset research --preset visual`, custom lanes with
   `--extra-lane "lane|role|write_scope"`, and verified thread IDs with
   `--set-thread product=codex:019...`.
3. Run the read-only health check before any handoff or auto-chain:

   ```bash
   python <skill_dir>/scripts/multi_agent_loop_doctor.py --loop-dir docs/loop --json
   ```

   The doctor reports missing files, unverified/stale lane threads, non-terminal
   requests, unknown owners, tracker unchecked/blocked counts, and whether
   handoff and auto-chain are currently ready.

---

## Safety and stop gates

The skill **only switches sessions at a checkpoint boundary** and stops with a
clear report rather than guessing. It stops and reports when:

- thread tools are unavailable **and** durable fallback cannot be written;
- a target lane has no verified thread ID and thread creation is not allowed;
- the current checkpoint is not closed enough to hand off;
- **verification cannot run** - this is treated as `BLOCKED`, never as
  accepted-with-caveat;
- a blocker needs credentials, approval, external data, or a destructive action;
- lanes would write the same files concurrently;
- the next action would violate `constraints.md`;
- `max_fix_cycles` is reached;
- auto-chain would create an unbounded or duplicate continuation;
- the configured budget in `loop-budget.md` is exhausted.

Thread IDs returned by `create_thread` are **provisional** until verified with
`read_thread` / `list_threads`; unverifiable IDs are marked stale and at most one
replacement continuation is created.

---

## Hardening features

This build adds engineering safeguards on top of the base orchestration layer:

- **Deterministic completion gate** - a single completion token
  (`SHIP_CHECK_OK`) is emitted only when every checkpoint verify command exited
  `0`. "It looks done" never counts as done.
- **Atomic, crash-safe delivery** - messages are written Maildir-style
  (`tmp/` then atomic rename into `new/`) so a crash mid-write never leaves a
  half-delivered message in a lane inbox.
- **Advisory file leases + write-scope enforcement** - lanes take an advisory
  lease before editing shared files, and a git pre-commit hook rejects commits
  that touch files outside a lane's declared `write_scope`, preventing
  concurrent clobbering.
- **Budget / cost stop gate** - `loop-budget.md` caps iterations/cost; when the
  budget is exhausted the loop halts and reports instead of running unbounded.
- **Heartbeat and orphan recovery** - each lane writes a heartbeat; a stalled or
  orphaned lane is detected and its open request can be safely recovered.
- **Append-only run log** - `loop-run-log.md` records every state transition so
  the full history is auditable and recovery never depends on chat memory.
- **Anti-thrash cap** - a bounded limit on `FIX_REQUESTED <-> IMPLEMENTING`
  ping-pong stops two lanes from looping forever on the same request.

---

## Repository layout

This repo doubles as a Codex plugin marketplace (`.agents/plugins/marketplace.json`)
and the plugin itself: the repo root is the plugin root
(`.codex-plugin/plugin.json`) and ships the skill under `skills/`:

```text
multi-loops-agents/
  .agents/plugins/marketplace.json     # marketplace manifest -> "." (this repo)
  .codex-plugin/plugin.json            # plugin manifest (repo root is the plugin root)
  skills/codex-agent-loop-orchestrator/
    SKILL.md
    agents/openai.yaml
    references/{protocol,loop-state,methodology,memory,build-your-own-agent}.md
    scripts/{bootstrap_agent_loop,multi_agent_loop_doctor,completion_gate,deliver_message,install_precommit,precommit_scope_guard,record_decision,loop_dashboard,...}.py
  install.ps1                          # fallback Windows installer
  install.sh                           # fallback macOS / Linux installer
  README.md                            # this file
```

---

## License

MIT. See `LICENSE` if present in this repository.
