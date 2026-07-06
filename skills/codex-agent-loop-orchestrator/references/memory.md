# Memory Layer (Reference Implementation)

This is the reference implementation of methodology **invariant 5** ("derived
memory is an auditable cache"). It depends on `record_decision.py` (the writer)
and the drift check in `multi_agent_loop_doctor.py`.

## What this is

The loop's only memory artifact: an append-only decision log. It is a **cache
over the `docs/loop` source files, never a source of truth.** (invariant 5)
Cut to the bone: one jsonl + one writer + one shared hash + one doctor WARNING.
No DB, no FTS, no index, no new lane.

## `decisions.jsonl`

Location: `docs/loop/memory/decisions.jsonl`. One JSON object per line,
**append-only** (never edit or rewrite an old line; supersession is expressed by
appending a new line).

Fields:

- `decision_id` - stable id.
- `request_id` - the request this decision serves.
- `lane` - who decided.
- `decision` - what was decided (one line).
- `rationale` - why.
- `alternatives_rejected` - what was considered and dropped.
- `supersedes` - the `decision_id` this one replaces, or blank.
- `source_docs[]` - the source files this decision derives from / depends on.
- `content_hash` - `normalize_then_hash()` over the current bytes of `source_docs` at write time.
- `gate_status` - the completion-gate token at write time: `SHIP_CHECK_OK` / `SHIP_CHECK_FAIL` / `none` (so a decision made under FAIL is **visibly tentative**).
- `created_at` - ISO-8601 UTC.

## `normalize_then_hash()` - the single shared helper (the one fatal trap)

**Contract:** for each `source_doc`, read its bytes -> normalize CRLF to LF ->
strip trailing newlines -> concatenate the per-doc texts with a fixed separator
-> `sha256` the result.
**Defined in exactly one place** (in `record_decision.py`), imported by the
doctor. **Two implementations = false drift on every read from CRLF on Windows**,
which is the only fatal trap in the whole layer. The doctor self-checks that the
helper it imported is the canonical one.

## `record_decision.py` (the writer)

CLI: `--loop-dir / --request-id / --lane / --decision / --rationale /
--alternatives / --supersedes / --source-doc (repeatable) / --gate-status`.
It computes `content_hash` with `normalize_then_hash()` and appends one line in
`'a'` mode. **It never reads, edits, or rewrites an old line.**

## Drift detection (the doctor)

For each decision **not superseded by any later `supersedes:`**, the doctor
recomputes `normalize_then_hash(source_docs)` and compares it to the stored
`content_hash`:

- `stored != live` -> WARNING `stale_decision` (naming which source_docs changed);
- a `source_doc` no longer exists -> WARNING `missing_source_doc`;
- a bad or blank line -> WARNING `malformed_decision`;
- `decisions.jsonl` is absent -> graceful degradation: no warning, **never an ERROR**.

**Drift is always a WARNING. It never blocks `handoff_ready` or `auto_chain`.**
(Memory fails open; verification fails closed - invariant 5 vs 4.)
The doctor's `--json` exposes `decisions: {total, active, stale, malformed}`.

## Memory Protocol (pinned into `handoff.md`)

Bootstrap appends this block to the handoff template so it survives compaction:

1. Before deciding, grep `decisions.jsonl` for this `request_id` and follow the
   `supersedes` chain to the newest live decision.
2. Before trusting any recorded `gate_status`, **re-run** the completion gate and
   the doctor. The recorded token is only a hint; the live gate is the authority.
3. If the doctor reports a `stale_decision`, discard that cached decision and
   re-read the live source docs.
4. At checkpoint close, append **exactly one line** via `record_decision.py`;
   never edit an old line. Supersession is expressed by appending a new line
   whose `supersedes` names the old `decision_id`.

## Relation vocabulary (reserved; only `supersedes` is implemented)

Reserved for future cross-linking (taken from the verified Gentleman-engram
reference design):
`supersedes`, `superseded_by`, `conflicts_with`, `related`, `compatible`,
`scoped`, `not_conflict`.
Only **`supersedes` is implemented today** (`superseded_by` is its read-back).
The rest are reserved values so the schema can grow with zero migration.
**Do not build a conflict-detection engine** - that would need an LLM judge / FTS
the file layer does not have.

## Provenance - borrow patterns, not parts (invariant 9)

- **Gentleman-Programming/engram** -> a stable `topic_key`-style key, progressive
  disclosure, the relation vocabulary above, the What/Why/Where/Learned frame,
  and compaction-surviving pins. **Not copied:** SQLite/FTS5, ranked retrieval,
  automatic dedup, the LLM-judge pipeline - those need a running binary.
- **Cartridges (arXiv 2506.06266)** -> amortize (write once, reuse many times)
  plus distill into a derived record rather than a raw transcript. **Not copied:**
  KV-cache training, any throughput metric.
- **DeepSeek-ai/Engram (arXiv 2601.07372)** -> deterministic content-addressed
  keys plus normalize-before-hash. **Analogy only**; no code or weights. (It is a
  model-internal architecture, **not** agent memory.)
