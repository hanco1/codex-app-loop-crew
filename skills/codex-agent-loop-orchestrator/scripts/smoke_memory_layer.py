#!/usr/bin/env python3
"""In-process smoke test for the memory layer + team-shape flags.

Exercises the reference implementation end to end WITHOUT spawning any
subprocess (this box's sandbox cannot shell out): everything is a direct
module-level import of ``bootstrap_agent_loop``, ``completion_gate``,
``record_decision``, and ``multi_agent_loop_doctor``.

It asserts, in one temp loop:

  1. bootstrap creates the loop (default lanes) with the memory cache;
  2. a passing and a failing evidence record are written per the flat JSON
     contract, and completion_gate.evaluate reports ok=False for the failing
     request;
  3. the doctor's completion_gate_ok (real gate) and evidence_recorded_ok
     (cell-present only) diverge as designed;
  4. recording one decision makes the doctor report decisions.total == 1,
     stale == 0;
  5. mutating a source doc makes stale == 1 while handoff_ready is UNCHANGED
     (memory fails open, never blocks handoff);
  6. restoring the source doc returns stale == 0;
  7. an ORPHAN failing evidence record -- a failing record whose request_id is
     NOT registered in requests.md -- still flips completion_gate_ok to False
     and names that request in gate_failed_requests (P0-2); removing the record
     returns completion_gate_ok to True.

Negative checks:

  - an evidence file written in the OLD nested ``.txt`` layout is invisible to
    the gate (the request still reports no evidence);
  - normalize_then_hash gives IDENTICAL digests for the LF and CRLF variants of
    the same content.

Team-shape check (second temp loop):

  - bootstrap with --no-default-lanes plus one --extra-lane registers only that
    lane, never product/implementation/review, and every lane has a workspace/
    directory.

Prints ``SMOKE_OK`` and exits 0 only if every assertion passes.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import bootstrap_agent_loop  # noqa: E402
import completion_gate  # noqa: E402
import multi_agent_loop_doctor as doctor  # noqa: E402
import record_decision  # noqa: E402


PASSING_REQUEST = "REQ-20260704-000001-implementation"
FAILING_REQUEST = "REQ-20260704-000002-implementation"


def _fail(message: str) -> None:
    raise AssertionError(message)


def _bootstrap(loop_dir: Path, extra_argv=None) -> None:
    """Run bootstrap main in-process with a controlled argv."""
    argv = ["bootstrap_agent_loop", "--loop-dir", str(loop_dir)]
    if extra_argv:
        argv.extend(extra_argv)
    saved = sys.argv
    sys.argv = argv
    try:
        rc = bootstrap_agent_loop.main()
    finally:
        sys.argv = saved
    if rc != 0:
        _fail("bootstrap returned non-zero: {0}".format(rc))


def _write_evidence(evidence_dir: Path, request_id: str, iteration: int, command: str, exit_code: int) -> None:
    """Write one flat evidence JSON file per the README contract."""
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in command)
    # Collapse runs of '-' to match the documented slug rule.
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")
    name = "{0}-iter-{1}-{2}.json".format(request_id, iteration, slug)
    record = {
        "request_id": request_id,
        "checkpoint": "smoke-checkpoint",
        "command": command,
        "exit_code": exit_code,
        "ran_at": "2026-07-04T00:00:00Z",
    }
    import json

    (evidence_dir / name).write_text(json.dumps(record), encoding="utf-8")


def _append_request_row(requests_path: Path, request_id: str, owner_lane: str, note: str) -> None:
    """Append one non-terminal request row with a filled evidence/last_message cell.

    Columns: request_id, status, owner_lane, iteration, source_docs,
    last_message, next_action, updated_at. A non-empty last_message makes the
    doctor's evidence_recorded_ok treat the request as having a recorded cell.
    """
    row = "| {rid} | IMPLEMENTING | {owner} | 1 | goal.md | {note} | continue | 2026-07-04T00:00:00Z |\n".format(
        rid=request_id, owner=owner_lane, note=note
    )
    with requests_path.open("a", encoding="utf-8") as handle:
        handle.write(row)


def _record_one_decision(loop_dir: Path, request_id: str, source_doc: Path, gate_status: str) -> None:
    argv = [
        "--loop-dir",
        str(loop_dir),
        "--request-id",
        request_id,
        "--lane",
        "implementation",
        "--decision",
        "Ship the smoke checkpoint.",
        "--rationale",
        "Evidence recorded and gate consulted.",
        "--alternatives",
        "Block pending manual review.",
        "--source-doc",
        str(source_doc),
        "--gate-status",
        gate_status,
    ]
    rc = record_decision.main(argv)
    if rc != 0:
        _fail("record_decision returned non-zero: {0}".format(rc))


def _doctor(loop_dir: Path) -> dict:
    return doctor.summarize(loop_dir, stale_heartbeat_mins=doctor.DEFAULT_STALE_HEARTBEAT_MINS)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # ---- Loop A: full memory-layer flow with default lanes ---------------
        loop_a = tmp_path / "loop_a"
        _bootstrap(loop_a)

        evidence_dir = loop_a / "evidence"
        requests_path = loop_a / "requests.md"

        # Sanity: bootstrap laid down the memory cache and per-lane workspaces.
        if not (loop_a / "memory" / "decisions.jsonl").exists():
            _fail("bootstrap did not create memory/decisions.jsonl")
        if (loop_a / "memory" / "decisions.jsonl").read_text(encoding="utf-8") != "":
            _fail("decisions.jsonl should start empty")
        for lane in ("product", "implementation", "review"):
            if not (loop_a / "lanes" / lane / "workspace" / "README.md").exists():
                _fail("bootstrap did not create workspace for lane {0}".format(lane))
        if "## Memory Protocol" not in (loop_a / "handoff.md").read_text(encoding="utf-8"):
            _fail("handoff.md is missing the pinned Memory Protocol block")

        # One passing request, one failing request. Both have a filled evidence
        # cell in requests.md (so evidence_recorded_ok is true), but the failing
        # request's actual evidence JSON reports a non-zero exit code.
        _write_evidence(evidence_dir, PASSING_REQUEST, 1, "pytest -q", 0)
        _write_evidence(evidence_dir, FAILING_REQUEST, 1, "npm test", 1)
        _append_request_row(requests_path, PASSING_REQUEST, "implementation", "evidence: pytest ok")
        _append_request_row(requests_path, FAILING_REQUEST, "implementation", "evidence: npm test recorded")

        # (2) completion_gate.evaluate is ok=False for the failing request.
        records, load_errors = completion_gate.load_evidence(evidence_dir)
        fail_result = completion_gate.evaluate(records, load_errors, FAILING_REQUEST)
        if fail_result["ok"]:
            _fail("gate should report ok=False for the failing request")
        pass_result = completion_gate.evaluate(records, load_errors, PASSING_REQUEST)
        if not pass_result["ok"]:
            _fail("gate should report ok=True for the passing request")

        # (3) doctor completion_gate_ok and evidence_recorded_ok diverge.
        result = _doctor(loop_a)
        if result["evidence_recorded_ok"] is not True:
            _fail(
                "evidence_recorded_ok should be True (both requests have a filled "
                "evidence cell), got {0}".format(result["evidence_recorded_ok"])
            )
        if result["completion_gate_ok"] is not False:
            _fail(
                "completion_gate_ok should be False (a recorded command exited "
                "non-zero), got {0}".format(result["completion_gate_ok"])
            )
        if FAILING_REQUEST not in result["gate_failed_requests"]:
            _fail("doctor did not flag the failing request as gate_failed")
        # This divergence is the whole point of P0-2: a cell was filled in, but
        # the real gate still fails.
        if result["evidence_recorded_ok"] == result["completion_gate_ok"]:
            _fail("evidence_recorded_ok and completion_gate_ok did not diverge")

        # (4) record one decision; doctor sees total==1, stale==0.
        source_doc = loop_a / "goal.md"
        original_goal_bytes = source_doc.read_bytes()
        _record_one_decision(loop_a, PASSING_REQUEST, source_doc, "SHIP_CHECK_OK")

        result = _doctor(loop_a)
        decisions = result.get("decisions") or {}
        if decisions.get("total") != 1:
            _fail("expected decisions.total == 1, got {0}".format(decisions.get("total")))
        if decisions.get("stale") != 0:
            _fail("expected decisions.stale == 0, got {0}".format(decisions.get("stale")))
        if decisions.get("active") != 1:
            _fail("expected decisions.active == 1, got {0}".format(decisions.get("active")))
        handoff_ready_baseline = result["handoff_ready"]

        # (5) mutate the source doc -> stale == 1, handoff_ready UNCHANGED.
        source_doc.write_text(
            source_doc.read_text(encoding="utf-8") + "\nDrifted line added after the decision.\n",
            encoding="utf-8",
        )
        result = _doctor(loop_a)
        decisions = result.get("decisions") or {}
        if decisions.get("stale") != 1:
            _fail("expected decisions.stale == 1 after mutating source, got {0}".format(decisions.get("stale")))
        if result["handoff_ready"] != handoff_ready_baseline:
            _fail(
                "handoff_ready changed due to drift ({0} -> {1}); memory drift must "
                "never affect handoff readiness".format(handoff_ready_baseline, result["handoff_ready"])
            )
        stale_codes = [w["code"] for w in result["warnings"] if w["code"] == "stale_decision"]
        if not stale_codes:
            _fail("doctor did not emit a stale_decision warning after mutation")
        # And it must appear only as a warning, never as an issue.
        if any(issue["code"] == "stale_decision" for issue in result["issues"]):
            _fail("stale_decision must be a warning, never an issue")

        # (6) restore the source doc -> stale == 0 again.
        source_doc.write_bytes(original_goal_bytes)
        result = _doctor(loop_a)
        decisions = result.get("decisions") or {}
        if decisions.get("stale") != 0:
            _fail("expected decisions.stale == 0 after restoring source, got {0}".format(decisions.get("stale")))

        # (7) P0-2: an ORPHAN failing evidence record -- a failing record for a
        # request_id that is NOT registered in requests.md -- must still flip the
        # doctor's completion_gate_ok to False and name that request in
        # gate_failed_requests. The doctor must agree with the gate on failing
        # evidence regardless of registration; otherwise a failing checkpoint can
        # hide simply by never registering (or by going terminal) in requests.md.
        # First confirm the baseline is clean now that only passing evidence
        # remains registered.
        orphan_request = "REQ-20260704-000099-orphan"
        for stale_json in evidence_dir.glob("{0}-*.json".format(FAILING_REQUEST)):
            stale_json.unlink()
        baseline = _doctor(loop_a)
        if baseline["completion_gate_ok"] is not True:
            _fail(
                "baseline completion_gate_ok should be True once only passing "
                "evidence remains, got {0}".format(baseline["completion_gate_ok"])
            )
        if orphan_request in [r for r in requests_path.read_text(encoding="utf-8").split()]:
            _fail("orphan request must NOT be registered in requests.md for this check")

        _write_evidence(evidence_dir, orphan_request, 1, "pytest", 1)
        orphan_result = _doctor(loop_a)
        if orphan_result["completion_gate_ok"] is not False:
            _fail(
                "completion_gate_ok must be False for an orphan failing evidence "
                "record (request not registered in requests.md), got {0}".format(
                    orphan_result["completion_gate_ok"]
                )
            )
        if orphan_request not in orphan_result["gate_failed_requests"]:
            _fail(
                "gate_failed_requests must name the orphan request {0}; got {1}".format(
                    orphan_request, orphan_result["gate_failed_requests"]
                )
            )
        # And it must surface as a gate_failed ISSUE, never merely a warning.
        if not any(
            issue["code"] == "gate_failed" and orphan_request in issue["message"]
            for issue in orphan_result["issues"]
        ):
            _fail("orphan gate failure must appear as a gate_failed issue")

        # Removing the orphan record returns completion_gate_ok to True.
        for orphan_json in evidence_dir.glob("{0}-*.json".format(orphan_request)):
            orphan_json.unlink()
        restored = _doctor(loop_a)
        if restored["completion_gate_ok"] is not True:
            _fail(
                "completion_gate_ok must return to True after the orphan failing "
                "record is removed, got {0}".format(restored["completion_gate_ok"])
            )
        if orphan_request in restored["gate_failed_requests"]:
            _fail("orphan request must no longer appear in gate_failed_requests after removal")

        # ---- Negative: OLD nested .txt evidence layout is invisible ----------
        # The pre-P0 layout nested a .txt transcript under a per-request folder.
        # The gate uses a non-recursive glob('*.json'), so this file must not
        # count as evidence for its request.
        legacy_request = "REQ-20260704-000003-implementation"
        legacy_dir = evidence_dir / legacy_request
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "checkpoint-npm-test.txt").write_text(
            "npm test\nexit 0\n", encoding="utf-8"
        )
        records2, load_errors2 = completion_gate.load_evidence(evidence_dir)
        legacy_result = completion_gate.evaluate(records2, load_errors2, legacy_request)
        if legacy_result["ok"]:
            _fail("gate must NOT accept a request whose only evidence is a nested .txt file")
        if legacy_result["total_records"] != 0:
            _fail(
                "nested .txt evidence must be invisible to the gate; got "
                "{0} records for the legacy request".format(legacy_result["total_records"])
            )

        # ---- Negative: CRLF and LF hash identically -------------------------
        lf_file = tmp_path / "content_lf.txt"
        crlf_file = tmp_path / "content_crlf.txt"
        lf_file.write_bytes(b"first line\nsecond line\nthird line\n")
        crlf_file.write_bytes(b"first line\r\nsecond line\r\nthird line\r\n")
        lf_hash = record_decision.normalize_then_hash([str(lf_file)])
        crlf_hash = record_decision.normalize_then_hash([str(crlf_file)])
        if lf_hash != crlf_hash:
            _fail(
                "normalize_then_hash must give identical digests for LF and CRLF "
                "variants; got {0} vs {1}".format(lf_hash, crlf_hash)
            )

        # ---- Loop B: team-shape flags ---------------------------------------
        loop_b = tmp_path / "loop_b"
        _bootstrap(
            loop_b,
            [
                "--no-default-lanes",
                "--extra-lane",
                "research|Gather and cite sources|docs/research/**",
            ],
        )
        registry_text = (loop_b / "agent-lanes.md").read_text(encoding="utf-8")
        if "| research |" not in registry_text:
            _fail("--extra-lane research was not registered")
        for lane in ("product", "implementation", "review"):
            if "| {0} |".format(lane) in registry_text:
                _fail("default lane {0} leaked in despite --no-default-lanes".format(lane))
            if (loop_b / "lanes" / lane).exists():
                _fail("default lane dir {0} was created despite --no-default-lanes".format(lane))
        if not (loop_b / "lanes" / "research" / "workspace" / "README.md").exists():
            _fail("research lane is missing its workspace/ directory")

    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
