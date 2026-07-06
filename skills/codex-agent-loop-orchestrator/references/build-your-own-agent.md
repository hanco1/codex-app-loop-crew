# Build Your Own Agent

`methodology.md` defines *what must hold*; this file shows *how to instantiate it
for your own needs*. It assumes the reference implementation is in place (the
flat-JSON evidence contract, `--no-default-lanes`, and `record_decision.py` all
exist in `scripts/`).

## 0. Orientation

You are not writing a new tool. You are **re-parameterizing the same file stack +
scripts + presets for a different verification surface**. Keep the nine
invariants; swap the implementation.

## 1. Decide first (write into `goal.md` / `constraints.md`)

- **Goal:** one sentence plus a concrete `Done When`.
- **Verification surface (the pivot question):** "What machine command proves one
  unit of work is acceptable?" If your only answer is "a human nods", you must
  build a machine-checkable proxy - a lint, a link-check, a schema validation, a
  screenshot diff. If you cannot build one, this methodology does not apply -
  stop. (invariant 4)
- **Lanes:** list recurring responsibilities, not personalities. Each needs an
  input / output / write scope / routing rule. (invariant 6)

## 2. SWAP table - which knobs you turn, and where they live

| knob | where it lives | fork needed? |
| --- | --- | --- |
| lane set | `--preset` / `--extra-lane "lane\|role\|write_scope"` / `--no-default-lanes` | no |
| per-lane write scope | the `write_scope` column in `agent-lanes.md` | no |
| message vocabulary | your own `*_REQUEST` / `*_DONE` verbs; **the fixed envelope fields do not change** (`message_type, request_id, iteration, from_lane, to_lane, status, source_docs, acceptance_criteria, expected_reply`) | no |
| request lifecycle intermediate states | the `status` values in `requests.md` | no |
| acceptance_criteria | per request, written into the message | no |
| evidence command | any command that exits 0 on success; one flat JSON record per command | no |
| budget / `max_fix_cycles` | `loop-policy.md` / `loop-budget.md` | no |

**The evidence contract** is a flat, five-field JSON object (`request_id,
checkpoint, command, exit_code, ran_at`) at
`docs/loop/evidence/<request_id>-iter-<n>-<command>.json`. **Do not inherit a
nested or `.txt` template.**

## 3. Must NOT change (with a mechanical check)

| invariant | mechanical check |
| --- | --- |
| files are truth | recovery reads files; nothing depends on chat |
| four planes separate | the registry has no raced-on `status` column; evidence is not in `requests.md` |
| handoff from files + message | a brand-new session can rebuild the task from files alone |
| verification fails closed | the completion gate emits `SHIP_CHECK_OK` only on a recorded exit 0; can't-verify -> BLOCKED |
| memory fails open | drift is a doctor WARNING, and never blocks `handoff_ready` |
| loop is bounded | `max_fix_cycles` + `budget_exhausted` are enforced |

## 4. Self-check (prove your instance is sound)

- **Static:** the doctor is green for your lanes (`--json`).
- **Smoke:** a brand-new session, given only the files, rebuilds the task ->
  produces evidence -> the gate prints `SHIP_CHECK_OK`; tamper with one
  `exit_code` -> the gate prints `SHIP_CHECK_FAIL`.
- If you use the memory layer: change one source doc -> the doctor reports
  `stale_decision` and **does not** block handoff.
- **Proof that you built a different agent, not just re-ran the default:** your
  `agent-lanes.md` **must not** contain `product` / `implementation` / `review`
  unless you intentionally kept them. If those three are still there, you re-ran
  the default team; add `--no-default-lanes` and your own lanes.

## 5. Worked example - a 2-lane research + writing team

```bash
python <skill_dir>/scripts/bootstrap_agent_loop.py --loop-dir docs/loop \
  --no-default-lanes \
  --extra-lane "research|Gather and cite sources|docs/research/**" \
  --extra-lane "writing|Synthesize a sourced briefing|docs/briefing/**"
```

Without `--no-default-lanes`, bootstrap can only **append** lanes onto the
default `product` / `implementation` / `review` trio - it cannot **replace** it.
So building a genuinely different team requires this flag.

- **Verification surface:** `linkcheck` exits 0, **and** every claim carries a
  parseable `source_url`.
- **Message verbs:** `SOURCE_REQUEST / SOURCE_DELIVERED / REVISION_REQUEST /
  BLOCKED`; the fixed envelope fields are unchanged.
- **Evidence:** `docs/loop/evidence/req-001-iter-1-linkcheck.json`, `exit_code`
  0. Delete it -> the gate fails closed immediately.

**Compressed second example - a security-audit team:** `--no-default-lanes` plus
two lanes, `triage` and `audit`; the verification surface is one reproducible,
exit-0 PoC per finding.

## 6. Anti-patterns

- Do not fork the scripts - parameterize them.
- Do not add a lane for a personality (invariant 6).
- Do not merge the four planes (invariant 2).
- Do not let memory become a gate (invariant 5).
- Do not adopt a runtime your sandbox cannot run - borrow the pattern (invariant 9).
