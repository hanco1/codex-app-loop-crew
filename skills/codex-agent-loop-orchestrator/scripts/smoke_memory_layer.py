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

F8/F11 registry checks (tier loop):

  - the agent-lanes.md registry ends with an advisory ``tier`` column whose
    abstract values follow the policy (coding lanes highest, others
    second-highest) and survive template reruns, registration, and
    --set-thread adoption without clobbering a human opt-down;
  - --set-thread fills an EXISTING custom-lane row even in a separate,
    flagless invocation (status registered, thread id set, tier preserved, no
    duplicate row), and fails non-zero with a readable error -- registry left
    byte-identical -- when the named lane has no row on disk.

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
import deliver_message  # noqa: E402
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


def _set_request_status(requests_path: Path, request_id: str, status: str) -> None:
    """Rewrite the status cell (column 2) of ``request_id``'s row in requests.md."""
    lines = requests_path.read_text(encoding="utf-8").splitlines(keepends=True)
    out = []
    for line in lines:
        if line.strip().startswith("|") and "| {0} |".format(request_id) in line:
            suffix = "\n" if line.endswith("\n") else ""
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2:
                cells[1] = status
            out.append("| " + " | ".join(cells) + " |" + suffix)
        else:
            out.append(line)
    requests_path.write_text("".join(out), encoding="utf-8")


def _remove_request_row(requests_path: Path, request_id: str) -> None:
    """Drop ``request_id``'s row from requests.md entirely."""
    lines = requests_path.read_text(encoding="utf-8").splitlines(keepends=True)
    out = [
        line
        for line in lines
        if not (line.strip().startswith("|") and "| {0} |".format(request_id) in line)
    ]
    requests_path.write_text("".join(out), encoding="utf-8")


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


def _find_registry_row(registry: Path, lane: str) -> str:
    """Return the agent-lanes.md data row for ``lane`` (raises if absent)."""
    for line in registry.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("|") and "| {0} |".format(lane) in line:
            return line
    _fail("no registry row for lane {0!r}".format(lane))
    raise AssertionError  # unreachable


def _registry_col_index(registry: Path, column: str) -> int:
    """Return the 0-based index of ``column`` in the agent-lanes.md header.

    Header-driven so the registry can grow trailing columns (e.g. the F8 ``tier``
    column) without breaking cell lookups that used to assume a fixed position.
    """
    for line in registry.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("|") and "lane" in line and "thread_id" in line:
            headers = [c.strip().lower() for c in line.strip().strip("|").split("|")]
            if column.lower() in headers:
                return headers.index(column.lower())
    _fail("agent-lanes.md header has no {0!r} column".format(column))
    raise AssertionError  # unreachable


def _heartbeat_cell(registry: Path, lane: str) -> str:
    """Return ``lane``'s heartbeat cell value by header position (not [-1])."""
    idx = _registry_col_index(registry, "heartbeat")
    row = _find_registry_row(registry, lane)
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    return cells[idx] if idx < len(cells) else ""


def _blank_heartbeat(registry: Path, lane: str) -> None:
    """Reset ``lane``'s heartbeat cell back to '-' in agent-lanes.md.

    Targets the heartbeat column by header index rather than the last cell,
    since the registry now carries a trailing F8 ``tier`` column.
    """
    idx = _registry_col_index(registry, "heartbeat")
    lines = registry.read_text(encoding="utf-8").splitlines(keepends=True)
    out = []
    for line in lines:
        if line.strip().startswith("|") and "| {0} |".format(lane) in line:
            suffix = "\n" if line.endswith("\n") else ""
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if idx < len(cells):
                cells[idx] = "-"
            out.append("| " + " | ".join(cells) + " |" + suffix)
        else:
            out.append(line)
    registry.write_text("".join(out), encoding="utf-8")


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

        # ---- F10: stalled_handoff detector ----------------------------------
        # PASSING_REQUEST is registered IMPLEMENTING and owned by implementation,
        # and its own evidence is SHIP_CHECK_OK -- work done, but the request
        # never advanced to a terminal/next state. The doctor must flag it as a
        # stalled_handoff WARNING naming the lane + request (a genuine your-turn
        # nudge), and it must NEVER be an issue.
        stall_probe = _doctor(loop_a)
        stalls = [w for w in stall_probe["warnings"] if w["code"] == "stalled_handoff"]
        if not stalls:
            _fail("doctor should warn stalled_handoff for a done-but-unadvanced request")
        if not any(PASSING_REQUEST in w["message"] for w in stalls):
            _fail("stalled_handoff should name the stalled request id")
        if not any("implementation" in w["message"] for w in stalls):
            _fail("stalled_handoff should name the owning lane to nudge")
        if any(i["code"] == "stalled_handoff" for i in stall_probe["issues"]):
            _fail("stalled_handoff must be a warning, never an issue")
        if not isinstance(stall_probe.get("stalled_handoffs"), list) or not stall_probe["stalled_handoffs"]:
            _fail("doctor result should expose a non-empty stalled_handoffs list")

        # An archived REVIEW_DONE message is the OTHER stall signal: a request in
        # a pre-acceptance state with a REVIEW_DONE on disk but no evidence of
        # its own. Register such a request and drop a REVIEW_DONE message file.
        reviewed_request = "REQ-20260704-000200-review"
        _append_request_row(requests_path, reviewed_request, "product", "reviewed but not accepted")
        # Force its status to REVIEWING (the _append helper writes IMPLEMENTING).
        _set_request_status(requests_path, reviewed_request, "REVIEWING")
        rd_dir = loop_a / "messages" / reviewed_request
        rd_dir.mkdir(parents=True, exist_ok=True)
        (rd_dir / "REVIEW_DONE-iter-1.md").write_text("# REVIEW_DONE\n\npass\n", encoding="utf-8")
        rd_probe = _doctor(loop_a)
        rd_stalls = [
            w for w in rd_probe["warnings"]
            if w["code"] == "stalled_handoff" and reviewed_request in w["message"]
        ]
        if not rd_stalls:
            _fail("doctor should flag a REVIEWING request with an archived REVIEW_DONE as stalled_handoff")
        if "REVIEW_DONE" not in rd_stalls[0]["message"]:
            _fail("the REVIEW_DONE-based stall should cite archived REVIEW_DONE as its evidence")
        # Clean up so later blocks see only PASSING_REQUEST's stall.
        _remove_request_row(requests_path, reviewed_request)
        import shutil
        shutil.rmtree(rd_dir, ignore_errors=True)

        # ---- Git health (F4): git_present / hook_installed ------------------
        # A bare temp loop has no .git ancestor, so git_present is False and the
        # doctor emits a git_absent WARNING (never an issue: invariant 1 wants
        # version control but a missing repo must not block handoff).
        git_none = _doctor(loop_a)
        if git_none.get("git_present") is not False:
            _fail("git_present should be False for a loop with no .git ancestor")
        if git_none.get("hook_installed") is not False:
            _fail("hook_installed should be False when there is no repo")
        if not any(w["code"] == "git_absent" for w in git_none["warnings"]):
            _fail("doctor should emit a git_absent warning when the loop is not under git")
        if any(i["code"] in ("git_absent", "hook_absent") for i in git_none["issues"]):
            _fail("git health must be a warning, never an issue")

        # Now synthesize a repo: a .git dir directly above the loop dir. With a
        # repo but no hook, git_present is True, hook_installed is False, and the
        # doctor switches to a hook_absent warning (not git_absent).
        git_loop = tmp_path / "repo" / "docs" / "loop"
        _bootstrap(git_loop)
        (tmp_path / "repo" / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
        git_repo = _doctor(git_loop)
        if git_repo.get("git_present") is not True:
            _fail("git_present should be True when a .git dir is above the loop dir")
        if git_repo.get("hook_installed") is not False:
            _fail("hook_installed should be False before the guard is installed")
        if not any(w["code"] == "hook_absent" for w in git_repo["warnings"]):
            _fail("doctor should emit a hook_absent warning when repo present but guard missing")
        if any(w["code"] == "git_absent" for w in git_repo["warnings"]):
            _fail("git_absent must not fire once a repo is present")

        # Install a marker-bearing pre-commit hook: hook_installed flips True and
        # both git-health warnings clear.
        hook_file = tmp_path / "repo" / ".git" / "hooks" / "pre-commit"
        hook_file.write_text(
            "#!/bin/sh\n{0}\nexit 0\n".format(doctor.HOOK_MARKER), encoding="utf-8"
        )
        git_hooked = _doctor(git_loop)
        if git_hooked.get("git_present") is not True:
            _fail("git_present should remain True with the hook installed")
        if git_hooked.get("hook_installed") is not True:
            _fail("hook_installed should be True once a marker-bearing pre-commit hook exists")
        if any(w["code"] in ("git_absent", "hook_absent") for w in git_hooked["warnings"]):
            _fail("no git-health warning should remain once the guard is installed")
        # A pre-commit hook WITHOUT the marker must not read as armed.
        hook_file.write_text("#!/bin/sh\n# some other hook\nexit 0\n", encoding="utf-8")
        git_foreign = _doctor(git_loop)
        if git_foreign.get("hook_installed") is not False:
            _fail("a foreign pre-commit hook (no marker) must not count as installed")

        # ---- F7: mandatory heartbeats ---------------------------------------
        # (a) The doctor warns when a lane OWNS an active non-terminal request
        # but has no heartbeat. Loop A's PASSING_REQUEST is owned by
        # `implementation`, whose registry heartbeat is still the "-" default, so
        # stale_heartbeat_active_owner must fire and name that lane+request. This
        # is a WARNING, never an issue.
        hb_probe = _doctor(loop_a)
        owner_gaps = [w for w in hb_probe["warnings"] if w["code"] == "stale_heartbeat_active_owner"]
        if not owner_gaps:
            _fail("doctor should warn stale_heartbeat_active_owner for an active-request owner with no heartbeat")
        if not any("implementation" in w["message"] for w in owner_gaps):
            _fail("stale_heartbeat_active_owner should name the owning lane 'implementation'")
        if not any(PASSING_REQUEST in w["message"] for w in owner_gaps):
            _fail("stale_heartbeat_active_owner should name the owned request id")
        if any(i["code"] == "stale_heartbeat_active_owner" for i in hb_probe["issues"]):
            _fail("stale_heartbeat_active_owner must be a warning, never an issue")
        if not isinstance(hb_probe.get("heartbeat_gap_owners"), list) or not hb_probe["heartbeat_gap_owners"]:
            _fail("doctor result should expose a non-empty heartbeat_gap_owners list")

        # (b) deliver_message stamps the sender lane's heartbeat on delivery.
        # Deliver a message FROM implementation and assert the registry heartbeat
        # cell and the lane current.md both got a fresh non-"-" timestamp, which
        # then CLEARS the stale_heartbeat_active_owner warning for that lane.
        msg_path = loop_a / "msg_body.md"
        msg_path.write_text("# LOOP_STATUS\n\nstill working\n", encoding="utf-8")
        rc = deliver_message.main(
            [
                "--loop-dir", str(loop_a),
                "--to-lane", "product",
                "--from-lane", "implementation",
                "--request-id", PASSING_REQUEST,
                "--message-type", "LOOP_STATUS",
                "--iteration", "1",
                "--message-file", str(msg_path),
            ]
        )
        if rc != 0:
            _fail("deliver_message returned non-zero: {0}".format(rc))
        # The heartbeat cell must no longer be the "-" placeholder. Read it by
        # header position (the registry now ends with the F8 ``tier`` column, so
        # the heartbeat is no longer the last cell).
        impl_hb = _heartbeat_cell(loop_a / "agent-lanes.md", "implementation")
        if impl_hb in ("-", ""):
            _fail("deliver_message did not stamp the implementation heartbeat cell: {0!r}".format(impl_hb))
        if "2026-" not in impl_hb and "T" not in impl_hb:
            _fail("stamped heartbeat does not look like an ISO timestamp: {0!r}".format(impl_hb))
        current_after = (loop_a / "lanes" / "implementation" / "current.md").read_text(encoding="utf-8")
        if "heartbeat: -" in current_after or "heartbeat:\n" in current_after:
            _fail("deliver_message did not stamp the implementation current.md heartbeat")
        # With a fresh heartbeat, the F7 warning for implementation clears.
        hb_after = _doctor(loop_a)
        if any(
            w["code"] == "stale_heartbeat_active_owner" and "implementation" in w["message"]
            for w in hb_after["warnings"]
        ):
            _fail("stale_heartbeat_active_owner should clear after deliver_message stamps the heartbeat")
        # --no-heartbeat must NOT stamp. Reset the registry heartbeat to "-" by
        # re-bootstrapping a fresh lane in a separate loop is overkill; instead
        # deliver again with --no-heartbeat FROM a lane whose heartbeat we first
        # blank, and confirm it stays blank.
        _blank_heartbeat(loop_a / "agent-lanes.md", "review")
        rc = deliver_message.main(
            [
                "--loop-dir", str(loop_a),
                "--to-lane", "product",
                "--from-lane", "review",
                "--request-id", PASSING_REQUEST,
                "--message-type", "REVIEW_DONE",
                "--iteration", "1",
                "--message-file", str(msg_path),
                "--no-heartbeat",
            ]
        )
        if rc != 0:
            _fail("deliver_message --no-heartbeat returned non-zero: {0}".format(rc))
        review_hb = _heartbeat_cell(loop_a / "agent-lanes.md", "review")
        if review_hb not in ("-", ""):
            _fail("--no-heartbeat must NOT stamp the sender heartbeat; got {0!r}".format(review_hb))

        # ---- F11: workerless_lane_dependency --------------------------------
        # Fresh loop with a VERIFIED product lane (owns an active request) and a
        # WORKERLESS frontend lane (no verified thread) that has a request
        # dispatched to it. Because another (verified) lane's request is active,
        # the workerless dependency ESCALATES to an ERROR.
        loop_d = tmp_path / "loop_d"
        _bootstrap(loop_d, ["--set-thread", "product=codex:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"])
        d_requests = loop_d / "requests.md"
        # product is verified (has a thread) and owns an active request.
        _append_request_row(d_requests, "REQ-D-PRODUCT-1", "product", "product working")
        # frontend is workerless (bootstrap default status needs-thread) and owns
        # a dispatched request AND has a stuck inbox message.
        _append_request_row(d_requests, "REQ-D-FRONTEND-1", "frontend", "dispatched to frontend")
        _set_request_status(d_requests, "REQ-D-FRONTEND-1", "REQUESTED")
        # Add a frontend lane if bootstrap did not (default lanes lack frontend).
        if not (loop_d / "lanes" / "frontend").exists():
            _bootstrap(loop_d, ["--extra-lane", "frontend|Own the UI shell|src/ui/**"])
            _append_request_row(d_requests, "REQ-D-FRONTEND-1b", "frontend", "dispatched")
            _set_request_status(d_requests, "REQ-D-FRONTEND-1b", "REQUESTED")
        # Stick a message in frontend's inbox/new so the pending-inbox signal fires too.
        fe_new = loop_d / "lanes" / "frontend" / "inbox" / "new"
        fe_new.mkdir(parents=True, exist_ok=True)
        (fe_new / "stuck.md").write_text("# IMPLEMENTATION_REQUEST\n\nplease build\n", encoding="utf-8")

        d_probe = doctor.summarize(loop_d, stale_heartbeat_mins=doctor.DEFAULT_STALE_HEARTBEAT_MINS)
        wl_issues = [i for i in d_probe["issues"] if i["code"] == "workerless_lane_dependency"]
        if not wl_issues:
            _fail("doctor should raise workerless_lane_dependency as an ERROR when a verified lane's request is active")
        if not any("frontend" in i["message"] for i in wl_issues):
            _fail("workerless_lane_dependency should name the workerless lane (frontend)")
        if not any("no verified thread" in i["message"] for i in wl_issues):
            _fail("workerless_lane_dependency message should say 'no verified thread'")
        if not isinstance(d_probe.get("workerless_dependencies"), list) or not d_probe["workerless_dependencies"]:
            _fail("doctor result should expose a non-empty workerless_dependencies list")
        # Escalation means ok is False (it is an issue/error).
        if d_probe["ok"] is not False:
            _fail("an escalated workerless_lane_dependency error should make doctor ok=False")

        # WARNING (not error) case: a workerless lane with a dispatched request
        # but NO other verified lane actively working. Fresh loop, no --set-thread,
        # so every lane is workerless; the frontend request has no verified peer.
        loop_d2 = tmp_path / "loop_d2"
        _bootstrap(loop_d2, ["--extra-lane", "frontend|Own the UI shell|src/ui/**"])
        d2_requests = loop_d2 / "requests.md"
        _append_request_row(d2_requests, "REQ-D2-FRONTEND-1", "frontend", "dispatched")
        _set_request_status(d2_requests, "REQ-D2-FRONTEND-1", "REQUESTED")
        d2_probe = doctor.summarize(loop_d2, stale_heartbeat_mins=doctor.DEFAULT_STALE_HEARTBEAT_MINS)
        wl_warns = [w for w in d2_probe["warnings"] if w["code"] == "workerless_lane_dependency"]
        wl_errs = [i for i in d2_probe["issues"] if i["code"] == "workerless_lane_dependency"]
        if not any("frontend" in w["message"] for w in wl_warns):
            _fail("workerless_lane_dependency should appear as a WARNING when no verified peer is active")
        if any("frontend" in i["message"] for i in wl_errs):
            _fail("workerless_lane_dependency must NOT escalate to error without an active verified peer")

        # ---- F16: missing-dependency blocker classification -----------------
        # bootstrap's loop-policy template must carry the dependency_install knob
        # defaulting to ask.
        policy_text = (loop_a / "loop-policy.md").read_text(encoding="utf-8")
        if "dependency_install: ask" not in policy_text:
            _fail("bootstrap loop-policy.md is missing the 'dependency_install: ask' knob")

        # A BLOCKED request whose durable BLOCKED message carries the greppable
        # 'blocker: missing_dependency' marker must be classified by the doctor,
        # with pip vs system dependencies distinguished and install commands
        # surfaced. Register a BLOCKED request and write the marked message.
        dep_request = "REQ-20260704-000300-data"
        # Append a BLOCKED row (helper writes IMPLEMENTING; flip it to BLOCKED).
        _append_request_row(requests_path, dep_request, "product", "blocked on OCR deps")
        _set_request_status(requests_path, dep_request, "BLOCKED")
        dep_msg_dir = loop_a / "messages" / dep_request
        dep_msg_dir.mkdir(parents=True, exist_ok=True)
        (dep_msg_dir / "BLOCKED-iter-1.md").write_text(
            "# BLOCKED\n\n"
            "message_type: BLOCKED\n"
            "request_id: {rid}\n"
            "blocker: missing_dependency\n"
            "dependency: pip | pytesseract | pip install pytesseract\n"
            "dependency: system | tesseract | choco install tesseract\n".format(rid=dep_request),
            encoding="utf-8",
        )
        dep_probe = _doctor(loop_a)
        mdb = dep_probe.get("missing_dependency_blockers") or []
        match = [b for b in mdb if b["request_id"] == dep_request]
        if not match:
            _fail("doctor did not classify the BLOCKED request as a missing_dependency blocker")
        classified = match[0]
        if classified.get("has_pip") is not True:
            _fail("missing_dependency classification should flag has_pip for pytesseract")
        if classified.get("has_system") is not True:
            _fail("missing_dependency classification should flag has_system for tesseract")
        kinds = {d["kind"] for d in classified["dependencies"]}
        if kinds != {"pip", "system"}:
            _fail("missing_dependency dependencies should distinguish pip vs system; got {0}".format(kinds))
        installs = {d["install"] for d in classified["dependencies"]}
        if "pip install pytesseract" not in installs:
            _fail("missing_dependency classification lost the pip install command")
        if "choco install tesseract" not in installs:
            _fail("missing_dependency classification lost the system install command")
        # It must surface as an actionable WARNING (exit ramp), never an issue.
        dep_warns = [w for w in dep_probe["warnings"] if w["code"] == "missing_dependency"]
        if not any(dep_request in w["message"] for w in dep_warns):
            _fail("doctor should emit a missing_dependency warning naming the request")
        if any(i["code"] == "missing_dependency" for i in dep_probe["issues"]):
            _fail("missing_dependency must be a warning (has an exit ramp), never an issue")
        # A plain BLOCKED (no marker) must NOT be classified as missing_dependency.
        plain_request = "REQ-20260704-000301-data"
        _append_request_row(requests_path, plain_request, "product", "blocked, generic")
        _set_request_status(requests_path, plain_request, "BLOCKED")
        plain_dir = loop_a / "messages" / plain_request
        plain_dir.mkdir(parents=True, exist_ok=True)
        (plain_dir / "BLOCKED-iter-1.md").write_text(
            "# BLOCKED\n\nblocker:\n- Missing API key.\n", encoding="utf-8"
        )
        plain_probe = _doctor(loop_a)
        if any(
            b["request_id"] == plain_request
            for b in (plain_probe.get("missing_dependency_blockers") or [])
        ):
            _fail("a generic BLOCKED without the marker must NOT classify as missing_dependency")
        # Clean up the F16 rows/messages so later blocks are unaffected.
        _remove_request_row(requests_path, dep_request)
        _remove_request_row(requests_path, plain_request)
        import shutil as _shutil_f16
        _shutil_f16.rmtree(dep_msg_dir, ignore_errors=True)
        _shutil_f16.rmtree(plain_dir, ignore_errors=True)

        # ---- F8: per-lane advisory model-tier column ------------------------
        # The registry must carry a ``tier`` column whose values are the ABSTRACT
        # tier words (never a model name), assigned by policy: coding lanes
        # (implementation/backend/data-eng/frontend) -> highest; everyone else
        # (product/review/security/research) -> second-highest. And the tier must
        # survive every bootstrap round-trip: the template rerun, a lane
        # registration via --extra-lane, and a --set-thread adoption -- none may
        # clobber a human opt-down.
        loop_t = tmp_path / "loop_t"
        _bootstrap(loop_t)
        registry_t = loop_t / "agent-lanes.md"

        def _tier_of(lane: str) -> str:
            row = _find_registry_row(registry_t, lane)
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            return cells[-1]

        # (a) The header carries the tier column, last.
        header_cells = None
        for line in registry_t.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("|") and "lane" in line and "thread_id" in line:
                header_cells = [c.strip() for c in line.strip().strip("|").split("|")]
                break
        if header_cells is None or header_cells[-1] != "tier":
            _fail("agent-lanes.md header must end with a 'tier' column; got {0}".format(header_cells))

        # (b) Policy assignment for the default lanes.
        if _tier_of("implementation") != bootstrap_agent_loop.HIGHEST_TIER:
            _fail("coding lane 'implementation' should default to the highest tier")
        if _tier_of("product") != bootstrap_agent_loop.SECOND_HIGHEST_TIER:
            _fail("default lane 'product' should default to the second-highest tier")
        if _tier_of("review") != bootstrap_agent_loop.SECOND_HIGHEST_TIER:
            _fail("default lane 'review' should default to the second-highest tier")
        # And the tier words are abstract -- never a concrete model name.
        for word in (bootstrap_agent_loop.HIGHEST_TIER, bootstrap_agent_loop.SECOND_HIGHEST_TIER):
            if "gpt" in word.lower():
                _fail("tier words must be abstract, never a model name; got {0!r}".format(word))

        # (c) recommended_tier_for classifies representative coding/default names.
        for coding in ("data-engineering", "backend", "frontend", "data-eng", "implementation"):
            if bootstrap_agent_loop.recommended_tier_for(coding) != bootstrap_agent_loop.HIGHEST_TIER:
                _fail("recommended_tier_for({0!r}) should be the highest tier".format(coding))
        for default in ("product", "review", "security-privacy", "research", "docs"):
            if bootstrap_agent_loop.recommended_tier_for(default) != bootstrap_agent_loop.SECOND_HIGHEST_TIER:
                _fail("recommended_tier_for({0!r}) should be the second-highest tier".format(default))

        # (d) A new coding lane via --extra-lane gets the highest tier; a new
        # non-coding custom lane gets the second-highest.
        _bootstrap(loop_t, ["--extra-lane", "data-engineering|Own backend|src/core/**"])
        _bootstrap(loop_t, ["--extra-lane", "security-privacy|Own privacy|docs/security/**"])
        if _tier_of("data-engineering") != bootstrap_agent_loop.HIGHEST_TIER:
            _fail("new coding lane 'data-engineering' should register at the highest tier")
        if _tier_of("security-privacy") != bootstrap_agent_loop.SECOND_HIGHEST_TIER:
            _fail("new non-coding lane 'security-privacy' should register at the second-highest tier")

        # (e) OPT-DOWN survives a template rerun. Manually opt data-engineering
        # DOWN to second-highest, rerun bootstrap's template, and confirm the
        # override is preserved (the policy never silently opts a lane back UP).
        def _set_tier(lane: str, tier: str) -> None:
            lines_ = registry_t.read_text(encoding="utf-8").splitlines(keepends=True)
            out_ = []
            for line in lines_:
                if line.strip().startswith("|") and "| {0} |".format(lane) in line:
                    suffix = "\n" if line.endswith("\n") else ""
                    cells = [c.strip() for c in line.strip().strip("|").split("|")]
                    cells[-1] = tier
                    out_.append("| " + " | ".join(cells) + " |" + suffix)
                else:
                    out_.append(line)
            registry_t.write_text("".join(out_), encoding="utf-8")

        _set_tier("data-engineering", bootstrap_agent_loop.SECOND_HIGHEST_TIER)
        _bootstrap(loop_t)  # template rerun
        if _tier_of("data-engineering") != bootstrap_agent_loop.SECOND_HIGHEST_TIER:
            _fail("a human opt-DOWN on 'data-engineering' was clobbered by a template rerun")

        # (f) --set-thread adoption preserves the tier (no clobber). Adopt a
        # thread for implementation (a default lane, so it is in this run's lane
        # set) after opting it down; the tier must survive and status flips to
        # registered.
        _set_tier("implementation", bootstrap_agent_loop.SECOND_HIGHEST_TIER)
        _bootstrap(loop_t, ["--set-thread", "implementation=codex:019f8-smoke"])
        impl_row_t = _find_registry_row(registry_t, "implementation")
        impl_cells_t = [c.strip() for c in impl_row_t.strip().strip("|").split("|")]
        if impl_cells_t[-1] != bootstrap_agent_loop.SECOND_HIGHEST_TIER:
            _fail("--set-thread clobbered the implementation opt-down tier; got {0!r}".format(impl_cells_t[-1]))
        if "registered" not in impl_cells_t:
            _fail("--set-thread should flip the adopted lane's status to registered")
        if "codex:019f8-smoke" not in impl_cells_t:
            _fail("--set-thread should set the adopted lane's thread_id")

        # (g) render_registry is grep-proof: no gpt-* model name in the output.
        rendered = bootstrap_agent_loop.render_registry(
            bootstrap_agent_loop.existing_rows(registry_t)
        )
        if "gpt-" in rendered.lower():
            _fail("render_registry emitted a concrete model name; tiers must stay abstract")

        # ---- F11 completion: --set-thread adopts an existing custom-lane row -
        # data-engineering is a CUSTOM lane (absent from a flagless invocation's
        # default lane set) whose row already exists on disk with an opted-down
        # tier. The documented adoption one-liner run as a SEPARATE bootstrap
        # invocation (no --extra-lane) must fill THAT existing row: thread_id
        # set, status flipped to registered, the opted-down tier preserved, and
        # no duplicate row minted. This was a real dogfood no-op: the old code
        # only applied --set-thread to lanes in the current invocation's
        # lane_defaults, silently ignoring on-disk custom lanes.
        _bootstrap(loop_t, ["--set-thread", "data-engineering=codex:019f11-adopt"])
        de_row = _find_registry_row(registry_t, "data-engineering")
        de_cells = [c.strip() for c in de_row.strip().strip("|").split("|")]
        if "codex:019f11-adopt" not in de_cells:
            _fail("--set-thread did not fill the existing custom-lane row's thread_id: {0!r}".format(de_row))
        if "registered" not in de_cells:
            _fail("--set-thread did not flip the custom lane's status to registered: {0!r}".format(de_row))
        if de_cells[-1] != bootstrap_agent_loop.SECOND_HIGHEST_TIER:
            _fail("custom-lane adoption clobbered the opted-down tier; got {0!r}".format(de_cells[-1]))
        de_count = sum(
            1
            for line in registry_t.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("| data-engineering |")
        )
        if de_count != 1:
            _fail("custom-lane adoption must not duplicate the row; found {0} rows".format(de_count))

        # An adoption line naming a lane with NO row on disk (and not registered
        # by the same invocation) must fail LOUDLY: non-zero SystemExit whose
        # message names the lane, the registry left byte-identical, and no fresh
        # row minted. Never a silent exit 0.
        registry_before_f11 = registry_t.read_text(encoding="utf-8")
        adoption_failed = False
        try:
            _bootstrap(loop_t, ["--set-thread", "no-such-lane=codex:0199dead"])
        except SystemExit as exc:
            adoption_failed = True
            code = exc.code
            if code == 0 or code is None:
                _fail("--set-thread for an unknown lane must exit non-zero, got {0!r}".format(code))
            message = code if isinstance(code, str) else str(code)
            if "no-such-lane" not in message:
                _fail("unknown-lane adoption error should name the lane; got {0!r}".format(message))
        if not adoption_failed:
            _fail("--set-thread for a lane absent from disk must fail, not exit 0")
        if registry_t.read_text(encoding="utf-8") != registry_before_f11:
            _fail("a failed adoption must not modify agent-lanes.md")
        if "| no-such-lane |" in registry_t.read_text(encoding="utf-8"):
            _fail("a failed adoption must never create a row for the unknown lane")

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
