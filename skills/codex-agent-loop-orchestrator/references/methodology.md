# Methodology: Memory-Disciplined Multi-Agent Loops

This skill has two layers. **This file is the methodology** - the transferable
discipline that stays true even if you change the lanes, the message vocabulary,
or the verification surface. Everything else (`SKILL.md`, `protocol.md`,
`loop-state.md`, `memory.md`, and the executable hardening it points to) is
**one reference implementation** that proves the methodology on Codex. To build
your own agent, keep the invariants below and swap the implementation; see
`build-your-own-agent.md`.

**In one sentence:** a memory-disciplined multi-agent loop is durable because
truth lives in repo files (not chat) across four never-merged planes - identity
registry, durable request state, message bus, machine-checked verification
evidence - plus an auditable memory cache; handoff is valid only from files plus
one message, completion is gated by recorded exit codes and fails closed, and
every loop is bounded.

## The nine invariants

Each: the rule, what breaks without it, what you may swap vs must keep, and where
the reference implementation shows it.

**1. Repo files are the source of truth; chat is disposable.**
All durable state lives in version-controlled files; on resume you read files,
never recall chat.
- *Breaks if:* a session compacts or another actor takes over - anything only in chat is gone.
- *Swap:* layout, file names, format. *Keep:* state is file-backed; recovery starts by reading files.
- *Ref:* SKILL.md `Core Model`, `Request State`; loop-state.md `Recovery Gate`.

**2. Four planes, kept separate, never merged.**
Identity registry (who/scope/address), request state (queue + lifecycle), message
bus (typed envelopes), and verification evidence (machine-checked) are distinct
concerns with distinct files. The registry is a lookup table, not a dashboard.
- *Breaks if:* you merge them - one mutable file every lane races on, conflating identity, status, and proof.
- *Swap:* each plane's schema/columns/addressing. *Keep:* the four stay distinct; registry never becomes a dashboard.
- *Ref:* SKILL.md `Core Model`, `Registry Rules`, `Message Protocol`, `Verification Integrity`.

**3. Handoff is valid only from files + message, at checkpoint boundaries.**
Work transfers only when the next actor could act correctly from repo files plus
the one delivered message, with zero hidden chat. Switch sessions only at a
closed checkpoint.
- *Breaks if:* the handoff needs context that exists only in the sender's conversation - the next actor guesses and drifts.
- *Swap:* checkpoint definition, close-checklist, message shape. *Keep:* the files-plus-message self-containment test; switch only at a closed checkpoint.
- *Ref:* loop-state.md `Handoff Readiness Gate`, `Checkpoint Close Gate`.

**4. Completion is gated by recorded exit codes, and fails closed.**
A unit is accepted only when its verification evidence shows exit 0; if
verification cannot run, it is BLOCKED, never accepted-with-caveat. The
completion token is emitted only by the gate, never self-reported.
- *Breaks if:* "tests passed" is asserted instead of recorded - completion becomes hallucinable and regressions ship.
- *Swap:* the verification surface, evidence schema, token string, the checker program. *Keep:* a machine reads recorded results and fails closed; can't-verify -> BLOCKED; the writer and checker must agree on one evidence path and format.
- *Ref:* SKILL.md `Verification Integrity`; protocol.md `Deterministic Completion Gate`.

**5. Derived memory is an auditable cache - detectable, never blocking.**
Any summary/index/decision log is a cache over the source files, not a second
source of truth. Each derived record links its sources and stores a content hash
so staleness is detectable. Staleness is a WARNING; it never blocks handoff.
- *Breaks if:* memory becomes authoritative (it goes stale and lies), or staleness blocks handoff (a disposable cache turns load-bearing).
- *Swap:* what you remember, the format, the relation vocabulary, the hash algorithm. *Keep:* memory is derived, source-linked, hash-checkable; drift only warns; one shared normalizer is used by both the writer and the checker.
- *Ref:* memory.md (reference implementation of this invariant).

**6. Lanes split recurring responsibility, not personality.**
A lane has inputs, outputs, a write scope, and routing rules. Add one only when
it reduces context pollution or enables real parallelism.
- *Breaks if:* lanes are personality labels - you get role-play overhead with no isolation or parallelism.
- *Swap:* the whole lane set, inter-lane message types, write-scope globs. *Keep:* every lane has the four elements; each is justified by isolation or parallelism.
- *Ref:* SKILL.md `Default Lanes`, `Lane Expansion`; loop-state.md `Lane Expansion Gate`.

**7. Writes are coordinated mechanically, and fail closed.**
Static per-lane write scope plus dynamic advisory leases, enforced by a mechanism
(not trust); with no asserted identity the mechanism denies by default.
- *Breaks if:* coordination is convention only - two lanes silently overwrite the same files.
- *Swap:* the enforcement mechanism, lease schema, how identity is asserted. *Keep:* static scope + dynamic lease, mechanically enforced, ambiguity fails closed.
- *Ref:* SKILL.md `Registry Rules`, `Hardening Scripts`; loop-state.md `Lease Gate`.

**8. Every loop is bounded - no unbounded auto-chain.**
Capped fix cycles, a budget stop gate, an explicit closed set of stop conditions;
continuation requires an allow-flag plus a checklist.
- *Breaks if:* a loop can fix->implement forever or auto-chain without limit - it burns budget and never escalates to a human.
- *Swap:* the numeric caps, budget units, the specific stop conditions. *Keep:* fix and spend are both capped; chaining needs an allow-flag + checklist; counts come from a durable log; stop conditions are a closed set.
- *Ref:* SKILL.md `Stop Conditions`, `Continuation And Auto-Chain`; protocol.md `Anti-Thrash`.

**9. Borrow patterns, not parts.**
When adopting an idea from an external system, copy the access pattern and the
discipline, then rebuild it on your own file-based, dependency-light foundation.
Do not import an artifact that needs a runtime your environment can't honor.
- *Breaks if:* you adopt the substrate instead of the pattern - your loop now depends on a server/binary it can't run.
- *Swap:* which systems you mine, which patterns you borrow. *Keep:* only the discipline, re-hosted locally.
- *Runnable check:* if adopting it requires a runtime dependency your sandbox can't satisfy (e.g. an external MCP/HTTP memory server, a GPU, a trained weight), you borrowed a part - reject it or reduce it to a pattern.
- *Ref:* memory.md footer (how the engram/cartridge ideas were reduced to patterns here).

---

To instantiate these for your own agent, see `build-your-own-agent.md`. Every
reference-implementation doc - and the executable hardening they point to -
implements an invariant above and must not contradict it.
Put plainly: if code and methodology disagree, the code is the bug.
