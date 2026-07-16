# Skill vs Solo: a controlled build comparison

Two builds of the **same** local-first Canadian expense-analysis web app, from the **same** requirements
and the **same** synthetic bank-statement fixtures, on the **same** model (codex `gpt-5.6-sol`, xhigh effort).

- **Skill arm** — built by the `codex-agent-loop-orchestrator` multi-agent loop: a Product / Implementation /
  Review lane structure over 5 requests and ~15 review iterations, with independent code review and a human
  QA gate on every user-facing slice. Lives at `hanco1/expense-app-loop-built` (the full `docs/loop/` ledger is
  included — the audit trail *is* the demo).
- **Solo arm** — one plain `codex exec` session, no orchestration skill, no built-in playbooks/subagents/
  worktrees, single shot. Lives at `hanco1/expense-app-solo-session-built`.

### How the comparison was kept fair (controlled variables)

| Variable | Both arms |
|---|---|
| Requirements | Identical brief: same objective, same 8 invariants, same operating flow, same fixtures |
| Fixtures | The same synthetic TD-style CSV + text-PDF |
| Model / effort | `gpt-5.6-sol`, xhigh |
| Skill isolation | Solo ran under an isolated `CODEX_HOME` — probed and confirmed the orchestrator skill was **not** visible (evidence recorded) |
| Superpowers | Solo forbidden from invoking any built-in playbook/subagent/worktree — one plain session |

**Honest limitations (stated up front):** n = 1. The skill arm consumed mid-run human QA feedback (it caught a
port-collision and a pie-chart rendering bug during the run); the solo arm got no mid-run feedback — it was
judged exactly as first delivered. Solo's token count is exact; the skill arm's is an order-of-magnitude
estimate reconstructed from a multi-day, multi-session history. The persistence decision the skill's intake
elicited from the human was folded into the solo brief so both arms started from equal information.

---

## Scoreboard (independent same-rubric review, adversarially verified)

Five dimensions, scored 0–10 by independent reviewers, every blocker/major finding re-derived by a second
adversarial verifier before it counts.

| Dimension | Solo | Skill | Who wins |
|---|:---:|:---:|---|
| Correctness on edge input | 6 | 7 | skill (narrow) |
| Invariant enforcement depth | 6 | 8 | **skill** |
| Security | 7 | 9 | **skill** |
| Test quality | 7 | 7 | tie |
| Maintainability | 8 | 8 | tie |
| **Average** | **6.8** | **7.8** | **skill by ~1 point** |

### Cost to produce

| | Solo | Skill |
|---|---|---|
| Wall-clock | ~10 minutes | multiple days, ~15 review iterations |
| Build tokens | **733,070 (exact)** | not precisely reconstructable; 1–2 orders of magnitude higher |
| App code | 1,606 LOC | 13,622 LOC (~8.5×) |
| Tests | 12 | 100 static defs → 299 executed cases (matrices) |
| Human interventions | 0 | 5 QA gates + 7 cap authorizations |

---

## What the loop bought (where skill genuinely wins)

- **Security (9 vs 7).** The skill app is XSS-airtight — every statement-derived string reaches the DOM via
  `textContent`/`createElement`, never `innerHTML`; a merchant literally named `<img src=x onerror=alert(1)>`
  renders as inert text, backed by a `default-src 'none'` CSP. It adds CSRF tokens with constant-time compare,
  Host-header/DNS-rebinding validation, `SO_EXCLUSIVEADDRUSE` loopback pinning, and a 10 MB upload cap enforced
  twice. (Solo is also XSS-safe via an `escapeHtml` helper — credit where due — but has **no CSRF** and a
  crash-on-large-amount path.)
- **Invariant depth (8 vs 6).** The skill app pushes invariants into the **database**: `CHECK
  (typeof(amount_minor)='integer')` rejects a float even on a raw SQL INSERT; `BEFORE DELETE/UPDATE` triggers
  make every fact table append-only against a direct-SQL attacker; a composite foreign key blocks cross-run
  splicing. Solo enforces the same invariants correctly but only in Python — a direct DB handle bypasses them.
- **Test depth.** The skill ships frozen boundary matrices (a 144-case write-boundary matrix cross-checked by
  an independent graph oracle; a 55-case component-state matrix), an amount whitelist covering NaN / overflow /
  unicode digits, and a real-browser test that ray-casts 720 points around the pie to prove it is visually
  contiguous and keyboard-operable.

## What the loop did NOT buy — and where solo holds its own

This is the honest part. The review was equally harsh to both arms, and the skill app is **not** flawless:

- **The skill app silently drops PDF lines it doesn't recognize** (`statement_import.py:106`) — producing no
  record at all for skipped lines. That violates INV-1 ("no silent data loss"), the *exact* invariant the whole
  skill exists to protect. The CSV path is honest; the PDF path is not.
- **Two of its shipped tests fail out of the box**, and the 299-test suite cannot be run with `pytest` at all
  (package-shadowing; no `conftest.py`) — so 286 of 299 tests would never run for anyone following the repo's
  own docs. The depth is real but partly un-exercised as delivered.
- **A quadratic duplicate-link path** hangs the app on a few thousand identical rows within one file.
- A broad `except (TypeError, ValueError) → HTTP 400` masks internal bugs as client errors, with logging
  disabled so there's no server-side trace.

And solo, for **10 minutes and 733K tokens**, delivered:
- A clean 4-module backend, integer-cent money end to end (zero `float()`), and **honest failure surfacing** —
  every malformed row is retained as an explicit error record, never dropped (it passed the same 10/10
  acceptance battery the skill app did).
- **Tied** with the skill app on maintainability (8) and test quality (7), and one point behind on correctness.

Solo's real defects: one **blocker** (a large/typo amount throws `OverflowError → HTTP 500` and leaves a
partial commit + an orphan file on disk), US-first date parsing that mis-buckets a Canadian D/M/Y statement
into the wrong month, no CSRF, and thin tests around its most exactness-critical function.

---

## The verdict

The loop bought roughly **one point of average quality (6.8 → 7.8)**, concentrated exactly where a
review-and-invariants process should concentrate it: **security and defense-in-depth**. It did so at ~8.5× the
code and one-to-two orders of magnitude more time and tokens — and it still shipped real defects, including one
that violates its own headline invariant.

Read that honestly in both directions:

- **For a quick, local, single-user tool**, the solo build is genuinely good — fast, cheap, honest about
  failure, and it passed every functional acceptance check. The loop would be over-engineering.
- **For software that handles real money at scale or faces untrusted input**, the skill's margin is the margin
  that matters: DB-enforced invariants, an airtight XSS/CSRF posture, and frozen regression matrices are worth
  far more than one averaged point suggests — a single silent double-count or stored-XSS in a finance app
  outweighs a lot of speed.
- **And the loop is not magic.** Its advantage is *depth of defense and auditability*, not perfection; this
  comparison found real bugs in it. The right takeaway is not "always use the loop" but "match the machinery to
  the stakes" — which is the same task-size lesson the skill's own methodology already records.

*Both repositories are public so you can read the code and, for the skill arm, the complete decision ledger
that produced it. Every finding above cites a file and line in those repos.*
