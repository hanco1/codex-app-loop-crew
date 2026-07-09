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

G1/G2/G3 criteria-and-gate checks (doc wording + a synthetic doctor loop):

  - G1: protocol.md teaches red-capable acceptance criteria (each names the
    exact command that proves it; a criterion with no red-capable command is a
    vibe), carries the good/bad exemplar pair and the run-2 canonical
    counterexample, and loop-state.md carries the tautological-evidence guard;
  - G2: protocol.md defines field-level real-input correctness + the
    redacted-sample ritual (human approves a sanitized excerpt/field-shape spec
    ONCE at intake; evidence records only counts/booleans);
  - G3: a request held awaiting human QA (a user-facing slice at REVIEWING with
    a ``human_qa_requested`` run-log row and no confirmation) is NORMAL WAITING,
    so the doctor suppresses ``stalled_handoff`` for it; the same request with
    no marker (or after ``human_qa: confirmed``) still stalls.
  - G20: a REVIEWING request whose OWN implementation evidence is gate-green
    only stalls once the reviewer (owner) heartbeat is stale/missing (the grace
    window; a fresh heartbeat = healthy review = no stall), with an HONEST
    reason (``implementation_evidence_green_no_verdict``) and wording that never
    claims the review "finished"; an archived REVIEW_DONE stalls IMMEDIATELY
    regardless of heartbeat (reason ``work_done_unreported``).

G16/F11 registry checks (tier loop):

  - the agent-lanes.md registry ends with an advisory ``tier`` column whose
    abstract values follow the G16 policy (EVERY lane -- default or custom --
    defaults to highest; downgrading is a manual human action) and survive
    template reruns, registration, and --set-thread adoption without clobbering
    a human opt-down;
  - --set-thread fills an EXISTING custom-lane row even in a separate,
    flagless invocation (status registered, thread id set, tier preserved, no
    duplicate row), and fails non-zero with a readable error -- registry left
    byte-identical -- when the named lane has no row on disk.

Prints ``SMOKE_OK`` and exits 0 only if every assertion passes.
"""

from __future__ import annotations

import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
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

# The skill root (one level above scripts/) holds SKILL.md and references/.
_SKILL_DIR = Path(__file__).resolve().parent.parent
_SKILL_MD = _SKILL_DIR / "SKILL.md"
_PROTOCOL_MD = _SKILL_DIR / "references" / "protocol.md"
_LOOP_STATE_MD = _SKILL_DIR / "references" / "loop-state.md"


def _fail(message: str) -> None:
    raise AssertionError(message)


def _read_doc(path: Path) -> str:
    if not path.exists():
        _fail("expected skill doc missing: {0}".format(path))
    return path.read_text(encoding="utf-8")


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


def _set_heartbeat(registry: Path, lane: str, value: str) -> None:
    """Write ``value`` into ``lane``'s heartbeat cell in agent-lanes.md.

    Targets the heartbeat column by header index (the registry carries a trailing
    F8 ``tier`` column, so writing the last cell would clobber the wrong column).
    """
    idx = _registry_col_index(registry, "heartbeat")
    lines = registry.read_text(encoding="utf-8").splitlines(keepends=True)
    out = []
    for line in lines:
        if line.strip().startswith("|") and "| {0} |".format(lane) in line:
            suffix = "\n" if line.endswith("\n") else ""
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if idx < len(cells):
                cells[idx] = value
            out.append("| " + " | ".join(cells) + " |" + suffix)
        else:
            out.append(line)
    registry.write_text("".join(out), encoding="utf-8")


def _make_terminal_request(requests_path: Path, request_id: str, owner_lane: str) -> None:
    """Append a request row already in a terminal (ACCEPTED) state."""
    _append_request_row(requests_path, request_id, owner_lane, "accepted")
    _set_request_status(requests_path, request_id, "ACCEPTED")


def _check_g7_doctor(tmp_path: Path) -> None:
    """G7: the three WARNING-only doctor lineage/hygiene checks.

    Positive AND negative cases for orphan_evidence, evidence_naming, and
    uncommitted_work. orphan_evidence/evidence_naming are pure filesystem
    checks; uncommitted_work needs a real tiny git repo, built in %TEMP% here.
    """
    import subprocess

    # ---- (a) orphan_evidence / (b) evidence_naming (filesystem-only) --------
    loop_g7 = tmp_path / "loop_g7"
    _bootstrap(loop_g7)
    evidence_dir = loop_g7 / "evidence"
    requests_path = loop_g7 / "requests.md"
    real_request = "REQ-20260707-090000-implementation"
    _make_terminal_request(requests_path, real_request, "implementation")

    # A well-named evidence file for a REGISTERED request: no warning.
    _write_evidence(evidence_dir, real_request, 1, "pytest", 0)
    # A well-named evidence file whose request_id is NOT registered: orphan.
    orphan_request = "REQ-20260707-091111-frontend"
    _write_evidence(evidence_dir, orphan_request, 1, "pytest", 0)
    # A SETUP-* record: legitimate, never flagged.
    (evidence_dir / "SETUP-20260707-first-move-doctor.json").write_text(
        '{"note": "setup record"}', encoding="utf-8"
    )
    # A malformed name (the canonical stray from run 2): evidence_naming.
    stray_name = "frontend-ui-ux-pro-max-20260707-verification.json"
    (evidence_dir / stray_name).write_text('{"note": "stray"}', encoding="utf-8")

    probe = doctor.summarize(loop_g7, stale_heartbeat_mins=doctor.DEFAULT_STALE_HEARTBEAT_MINS)

    # orphan_evidence fires for the unregistered request, names file + id.
    orphans = [w for w in probe["warnings"] if w["code"] == "orphan_evidence"]
    if not orphans:
        _fail("doctor should warn orphan_evidence for an evidence file naming an unregistered request")
    if not any(orphan_request in w["message"] for w in orphans):
        _fail("orphan_evidence should name the unregistered request_id")
    # It must NOT flag the registered request's evidence or the SETUP record.
    if any(real_request in w["message"] for w in orphans):
        _fail("orphan_evidence must not fire for a registered request's evidence")
    if any("SETUP-" in w["message"] for w in orphans):
        _fail("orphan_evidence must not flag a SETUP-* record")
    # Machine-readable list mirrors it.
    if not any(o["request_id"] == orphan_request for o in probe.get("orphan_evidence", [])):
        _fail("doctor result should expose orphan_evidence with the request_id")

    # evidence_naming fires for the malformed name, and only that.
    naming = [w for w in probe["warnings"] if w["code"] == "evidence_naming"]
    if not any(stray_name in w["message"] for w in naming):
        _fail("evidence_naming should flag the malformed stray filename")
    if any(real_request in w["message"] or orphan_request in w["message"] for w in naming):
        _fail("evidence_naming must not flag a well-named REQ-* evidence file")
    if not any(n["file"] == stray_name for n in probe.get("evidence_naming", [])):
        _fail("doctor result should expose evidence_naming with the file name")
    # All three G7 codes are WARNING-only, never issues.
    for code in ("orphan_evidence", "evidence_naming", "uncommitted_work"):
        if any(i["code"] == code for i in probe["issues"]):
            _fail("{0} must be a warning, never an issue".format(code))

    # ---- (c) uncommitted_work: real tiny git repo ---------------------------
    # Negative first: with NO git repo above the loop, the check is skipped
    # silently even though a request is terminal and files are "dirty".
    if probe.get("git_present") is not False:
        _fail("loop_g7 must have no git ancestor for the git-absent negative case")
    if probe.get("uncommitted_work"):
        _fail("uncommitted_work must NOT fire when git is absent")

    # Positive: build a real repo with the loop under it, an in-scope tracked
    # file left dirty, all requests terminal. Guard on git being runnable so the
    # smoke stays green where subprocess/git is unavailable.
    def _git(repo: Path, *args: str) -> bool:
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return out.returncode == 0

    repo = tmp_path / "g7_repo"
    repo.mkdir(parents=True, exist_ok=True)
    git_usable = _git(repo, "init")
    if git_usable:
        _git(repo, "config", "user.email", "smoke@example.com")
        _git(repo, "config", "user.name", "smoke")
        loop_c = repo / "docs" / "loop"
        # A single custom lane owning src/** (no default lanes) so attribution
        # is unambiguous: the default 'implementation' lane also owns src/**,
        # which would win first-match and make the owner assertion flaky.
        _bootstrap(loop_c, ["--no-default-lanes", "--extra-lane", "data-eng|Own core|src/**"])
        c_requests = loop_c / "requests.md"
        _make_terminal_request(c_requests, "REQ-20260707-092222-data-eng", "data-eng")
        # An in-scope source file, committed, then modified so it is dirty.
        src_dir = repo / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        src_file = src_dir / "core.py"
        src_file.write_text("x = 1\n", encoding="utf-8")
        # A data/DB artifact that must stay EXEMPT even when dirty.
        data_dir = repo / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "expenses.sqlite3").write_text("db\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "seed")
        # Now dirty the in-scope tracked source file.
        src_file.write_text("x = 2\n", encoding="utf-8")
        # And dirty the exempt data artifact.
        (data_dir / "expenses.sqlite3").write_text("db2\n", encoding="utf-8")

        c_probe = doctor.summarize(loop_c, stale_heartbeat_mins=doctor.DEFAULT_STALE_HEARTBEAT_MINS)
        if c_probe.get("git_present") is not True:
            _fail("loop_c must have a git ancestor for the uncommitted_work positive case")
        uw = [w for w in c_probe["warnings"] if w["code"] == "uncommitted_work"]
        if not uw:
            _fail("uncommitted_work should fire for a dirty in-scope file at a paused/idle loop")
        if not any("src/core.py" in w["message"] for w in uw):
            _fail("uncommitted_work should name the dirty in-scope path src/core.py")
        if not any("data-eng" in w["message"] for w in uw):
            _fail("uncommitted_work should name the owning lane (data-eng)")
        # The exempt data artifact must NOT be flagged.
        if any("expenses.sqlite3" in w["message"] for w in uw):
            _fail("uncommitted_work must exempt data/DB artifacts (*.sqlite3)")
        if not any("src/core.py" in item["path"] for item in c_probe.get("uncommitted_work", [])):
            _fail("doctor result should expose uncommitted_work with the in-scope path")

        # Negative: while a request is still NON-terminal (mid-flight), the same
        # dirty file must NOT warn -- work in progress is normal.
        _set_request_status(c_requests, "REQ-20260707-092222-data-eng", "IMPLEMENTING")
        mid_probe = doctor.summarize(loop_c, stale_heartbeat_mins=doctor.DEFAULT_STALE_HEARTBEAT_MINS)
        if any(w["code"] == "uncommitted_work" for w in mid_probe["warnings"]):
            _fail("uncommitted_work must NOT fire while a request is still non-terminal (mid-flight)")


def _append_run_log_row(loop_dir: Path, request_id: str, from_status: str, to_status: str, lane: str, note: str) -> None:
    """Append one row to loop-run-log.md (the append-only transition log).

    Columns: timestamp, request_id, iteration, from_status, to_status, lane, note.
    """
    row = "| 2026-07-07T10:00:00Z | {rid} | 1 | {frm} | {to} | {lane} | {note} |\n".format(
        rid=request_id, frm=from_status, to=to_status, lane=lane, note=note
    )
    with (loop_dir / "loop-run-log.md").open("a", encoding="utf-8") as handle:
        handle.write(row)


def _check_g14(tmp_path: Path) -> None:
    """G14: tier observability (model_observed line + tier_mismatch doctor).

    (a) ``bootstrap --observed-model 'lane=<observed>'`` stamps the lane's
    current.md ``model_observed:`` line verbatim, and a fresh current.md carries
    the (blank) model_observed field.

    (b) The doctor emits a WARNING-only ``tier_mismatch`` for a lane whose
    OBSERVED tier tag differs from the registry ``tier`` column, names both
    tiers, and does NOT flag a lane whose observed tier MATCHES nor one whose
    model_observed line is blank (not-yet-observed is not a mismatch). Never an
    issue; the concrete model id is DATA (allowed in the observed value).
    """
    loop = tmp_path / "g14_loop"
    # (a) adoption-time stamping. Under G16 every lane's registry tier defaults
    # to highest, so a mismatch now comes from a lane OBSERVED running LOWER than
    # the recorded tier: implementation MATCHES (both highest); product MISMATCHES
    # (registry highest, observed second-highest -- i.e. the thread was opened on
    # a lower model than policy records); review is left blank (not-yet-observed).
    _bootstrap(loop, extra_argv=[
        "--observed-model", "implementation=gpt-5.5 xhigh (highest)",
        "--observed-model", "product=gpt-5.4 xhigh (second-highest)",
    ])
    # The template carries a (blank) model_observed field for every lane.
    review_cur = (loop / "lanes" / "review" / "current.md").read_text(encoding="utf-8")
    if "model_observed:" not in review_cur:
        _fail("G14(a): current.md template must carry a model_observed field")
    impl_cur = (loop / "lanes" / "implementation" / "current.md").read_text(encoding="utf-8")
    if "model_observed: gpt-5.5 xhigh (highest)" not in impl_cur:
        _fail("G14(a): --observed-model must stamp the current.md model_observed line verbatim")

    # The doctor's tag extractor pulls the abstract tag from the observed value.
    if doctor.observed_tier_tag(impl_cur) != "highest":
        _fail("G14(b): observed_tier_tag must extract 'highest' from the model_observed line")
    if doctor.observed_tier_tag("current_request_id:\nmodel_observed:\n") != "":
        _fail("G14(b): a blank model_observed line must yield no observed tier tag")

    res = _doctor(loop)
    tms = [w for w in res["warnings"] if w["code"] == "tier_mismatch"]
    if not tms:
        _fail("G14(b): the doctor must warn tier_mismatch for product (observed second-highest vs recommended highest)")
    if not any("product" in w["message"] for w in tms):
        _fail("G14(b): tier_mismatch must name the mismatched lane 'product'")
    if not any("highest" in w["message"] and "second-highest" in w["message"] for w in tms):
        _fail("G14(b): tier_mismatch must name both the observed and recommended tiers")
    # The MATCHING lane (implementation) must NOT be flagged.
    if any("lane implementation" in w["message"] for w in tms):
        _fail("G14(b): a lane whose observed tier matches must not be flagged")
    # A not-yet-observed lane (review, blank model_observed) must NOT be flagged.
    if any("lane review" in w["message"] for w in tms):
        _fail("G14(b): a blank (not-yet-observed) lane must not be a tier_mismatch")
    if any(i["code"] == "tier_mismatch" for i in res["issues"]):
        _fail("G14(b): tier_mismatch must be a warning, never an issue")
    # Machine-readable passthrough.
    tm_list = res.get("tier_mismatches") or []
    if not any(t["lane"] == "product" and t["observed"] == "second-highest"
               and t["recommended"] == "highest" for t in tm_list):
        _fail("G14(b): doctor result must expose the tier_mismatches list with both tiers")
    # A tier_mismatch must NOT block handoff (WARNING-only contract).
    if res.get("handoff_ready") is not True:
        _fail("G14(b): a tier_mismatch warning must not flip handoff_ready to False")

    # NEGATIVE: the human opts product DOWN in the registry to second-highest,
    # matching what the thread is observed running -> the mismatch clears (the
    # recorded tier is now the honest tier; a downgrade is a deliberate human
    # edit, not a silent drift).
    reg = loop / "agent-lanes.md"
    idx = _registry_col_index(reg, "tier")
    lines = reg.read_text(encoding="utf-8").splitlines(keepends=True)
    out = []
    for line in lines:
        if line.strip().startswith("|") and "| product |" in line:
            suffix = "\n" if line.endswith("\n") else ""
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if idx < len(cells):
                cells[idx] = "second-highest"
            out.append("| " + " | ".join(cells) + " |" + suffix)
        else:
            out.append(line)
    reg.write_text("".join(out), encoding="utf-8")
    res2 = _doctor(loop)
    if any(w["code"] == "tier_mismatch" and "product" in w["message"]
           for w in res2["warnings"]):
        _fail("G14(e): opting the registry tier down to match the observed tier must clear the mismatch")


def _check_g12(tmp_path: Path) -> None:
    """G12: handoff/auto-chain seed sensitive-content scan (WARNING-only).

    NEGATIVE: a clean handoff that references sensitive material by name (and
    carries only ordinary short numbers -- ports, dates, iterations) produces NO
    ``handoff_sensitive_content`` warning.

    POSITIVE: a handoff that quotes an account-number-like digit run and a full
    path into a constraint-marked sensitive directory produces the warning for
    each, as a WARNING (never an issue), and the doctor's own message MASKS the
    account number (last 4 only) so it never re-leaks the material. A
    project-specific sensitive dir named on a constraints.md sensitive line is
    detected too.
    """
    loop = tmp_path / "g12_loop"
    _bootstrap(loop)
    (loop / "constraints.md").write_text(
        "# Constraints\n\n"
        "- The user's TD statement is highly sensitive; never upload or commit it.\n"
        "- Keep `data/`, `uploads/`, `private_samples/`, and `client_files/` "
        "untracked (private).\n",
        encoding="utf-8",
    )

    # NEGATIVE.
    (loop / "handoff.md").write_text(
        "# Handoff\n\n"
        "Continue R4: import the TD statement (referenced by name only).\n"
        "Server on port 8011; last touched 2026-07-07; iteration 2.\n"
        "Context: docs/loop/goal.md and src/core/parse.py.\n",
        encoding="utf-8",
    )
    clean = doctor.check_handoff_sensitive_content(loop)
    if clean:
        _fail("G12: a clean handoff (references only, short numbers) must not flag; got {0}".format(clean))
    res_clean = _doctor(loop)
    if any(w["code"] == "handoff_sensitive_content" for w in res_clean["warnings"]):
        _fail("G12: no handoff_sensitive_content warning may fire on a clean handoff")

    # POSITIVE.
    (loop / "handoff.md").write_text(
        "# Handoff\n\n"
        "Continue R4. The account is 4123 5678 9012 3456 on the TD statement.\n"
        "Redacted sample at private_samples/td_2026_06.pdf; also "
        "client_files/customer_list.csv.\n"
        "Server on port 8011.\n",
        encoding="utf-8",
    )
    leaky = doctor.check_handoff_sensitive_content(loop)
    kinds = {f["kind"] for f in leaky}
    if "account_number" not in kinds:
        _fail("G12: an account-number-like digit run in handoff.md must be flagged")
    if "sensitive_path" not in kinds:
        _fail("G12: a path into a sensitive directory in handoff.md must be flagged")
    # The account sample must be MASKED (no full run), and a project-specific
    # sensitive dir named in constraints must be caught.
    acct = [f for f in leaky if f["kind"] == "account_number"][0]
    if "4123" in acct["sample"] or "5678" in acct["sample"]:
        _fail("G12: the account-number finding must be masked, not carry the leading digits")
    if not acct["sample"].endswith("3456"):
        _fail("G12: the masked account finding should keep the last 4 digits")
    paths = {f["sample"] for f in leaky if f["kind"] == "sensitive_path"}
    if not any(p.startswith("private_samples/") for p in paths):
        _fail("G12: the private_samples path must be flagged")
    if not any(p.startswith("client_files/") for p in paths):
        _fail("G12: a project-specific sensitive dir named on a constraints sensitive line must be flagged")

    res = _doctor(loop)
    warns = [w for w in res["warnings"] if w["code"] == "handoff_sensitive_content"]
    if not warns:
        _fail("G12: the doctor must emit handoff_sensitive_content warnings for a leaky handoff")
    if any(i["code"] == "handoff_sensitive_content" for i in res["issues"]):
        _fail("G12: handoff_sensitive_content must be a warning, never an issue")
    # The raw account run must NEVER appear in the doctor's own output.
    for w in warns:
        if "4123 5678 9012 3456" in w["message"] or "4123567890123456" in w["message"]:
            _fail("G12: the doctor re-leaked the raw account number in its own warning")
    # A leaky handoff must NOT block handoff/auto-chain (WARNING-only contract).
    if res.get("handoff_ready") is not True:
        _fail("G12: a sensitive-content warning must not flip handoff_ready to False")
    # Machine-readable passthrough present.
    if not isinstance(res.get("handoff_sensitive_content"), list) or not res["handoff_sensitive_content"]:
        _fail("G12: doctor result should expose a non-empty handoff_sensitive_content list")


def _check_g11(tmp_path: Path) -> None:
    """G11: no pre-minted message dirs + timestamp-sorted reconstruction.

    (a) ``deliver_message.archive_message`` creates the durable
    ``messages/<request_id>/`` dir ONLY alongside a real write: called with no
    request_id it creates nothing (the run-2 empty-stray-dir failure mode), and
    called with a final id it leaves a message file, never an empty dir.

    (b) The doctor's run-log reconstruction is timestamp-ordered: feeding the
    SAME rows in chronological order and in a SHUFFLED order (late-append
    recovery rows out of file order -- which run 2 legally produced) yields
    identical fix-cycle counts and human-QA-hold conclusions, and
    ``parse_run_log_sorted`` returns rows in chronological order.
    """
    loop = tmp_path / "g11_loop"
    _bootstrap(loop)

    # ---- (a) archive_message never leaves an empty messages/<rid>/ dir -------
    msgs = loop / "messages"
    # No id -> no request-scoped store, and crucially no empty directory.
    if deliver_message.archive_message(loop, "", "IMPLEMENTATION_DONE", "1", "x\n") is not None:
        _fail("G11: archive_message with no request_id should return None (no dir)")
    empties = [p for p in msgs.iterdir() if p.is_dir() and not any(p.iterdir())]
    if empties:
        _fail("G11: archive_message with no id created an empty dir: {0}".format(
            [p.name for p in empties]))
    # Final id -> dir minted WITH a message file inside it.
    final_rid = "REQ-20260707-120000-implementation"
    archived = deliver_message.archive_message(
        loop, final_rid, "IMPLEMENTATION_DONE", "1", "# IMPLEMENTATION_DONE\n\ndone\n")
    if not archived:
        _fail("G11: archive_message with a final id must return the archived path")
    final_dir = msgs / final_rid
    if not any(final_dir.glob("*.md")):
        _fail("G11: archive_message must write a message file into messages/<rid>/")
    still_empty = [p for p in msgs.iterdir() if p.is_dir() and not any(p.iterdir())]
    if still_empty:
        _fail("G11: no empty messages/<rid>/ dir may remain; got {0}".format(
            [p.name for p in still_empty]))

    # ---- (b) shuffled-log invariance ----------------------------------------
    runlog = loop / "loop-run-log.md"
    header = (
        "# Loop Run Log\n\n"
        "| timestamp | request_id | iteration | from_status | to_status | lane | note |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
    )
    rid = "REQ-20260707-073729-data-eng"
    rows = [
        ("2026-07-07T07:37:29Z", rid, "1", "REQUESTED", "IMPLEMENTING", "data-eng", "start"),
        ("2026-07-07T07:45:00Z", rid, "1", "IMPLEMENTING", "REVIEWING", "review", "impl done"),
        ("2026-07-07T07:50:00Z", rid, "1", "REVIEWING", "FIX_REQUESTED", "review", "reject"),
        ("2026-07-07T07:55:00Z", rid, "2", "FIX_REQUESTED", "IMPLEMENTING", "data-eng", "fix"),
        ("2026-07-07T08:00:00Z", rid, "2", "IMPLEMENTING", "REVIEWING", "review", "impl done 2"),
        # A human_qa hold on a second (user-facing) request; its confirmation is
        # a LATER row placed out of order in the shuffle below.
        ("2026-07-07T08:05:00Z", "REQ-20260707-080500-frontend", "1", "IMPLEMENTATION_DONE",
         "REVIEWING", "frontend", "human_qa_requested: try the UI"),
    ]

    def _render(order):
        return header + "".join("| " + " | ".join(r) + " |\n" for r in order)

    runlog.write_text(_render(rows), encoding="utf-8")
    fix_inorder = doctor.count_fix_cycles_from_log(loop)
    held_inorder = doctor.requests_held_for_human_qa(loop)

    # Shuffle: put recovery rows out of chronological order (legal append-only).
    shuffled = [rows[2], rows[5], rows[0], rows[4], rows[1], rows[3]]
    runlog.write_text(_render(shuffled), encoding="utf-8")
    fix_shuffled = doctor.count_fix_cycles_from_log(loop)
    held_shuffled = doctor.requests_held_for_human_qa(loop)

    if fix_inorder != fix_shuffled:
        _fail("G11: fix-cycle counts must be identical on a shuffled log; "
              "{0} vs {1}".format(fix_inorder, fix_shuffled))
    if fix_inorder.get(rid) != 3:
        _fail("G11: the data-eng request should count 3 thrash transitions; got {0}".format(
            fix_inorder.get(rid)))
    if held_inorder != held_shuffled:
        _fail("G11: human-QA hold conclusions must be identical on a shuffled log; "
              "{0} vs {1}".format(held_inorder, held_shuffled))
    if "REQ-20260707-080500-frontend" not in held_shuffled:
        _fail("G11: the held user-facing request must be detected on the shuffled log")

    # parse_run_log_sorted yields chronological order even on the shuffled log.
    srt = doctor.parse_run_log_sorted(loop)
    ts = [r.get("timestamp", "") for r in srt]
    if ts != sorted(ts):
        _fail("G11: parse_run_log_sorted must return rows in chronological order; got {0}".format(ts))


def _check_g3_doctor(tmp_path: Path) -> None:
    """G3: a request held awaiting human QA is NORMAL WAITING, not stalled_handoff.

    Positive (exclusion works): a user-facing request at REVIEWING with an
    archived REVIEW_DONE (work done) AND a human_qa_requested run-log row but NO
    confirmation must NOT emit stalled_handoff -- the doctor recognizes the hold.

    Negative (exclusion is specific): the SAME request, with the human_qa marker
    removed but still REVIEWING with an archived REVIEW_DONE, MUST still emit
    stalled_handoff. And once a human_qa: confirmed row is appended, the hold is
    released (so if the request were still parked it would stall again) -- proving
    the confirmation, not merely any human_qa mention, releases the exclusion.
    """
    loop_g3 = tmp_path / "loop_g3"
    _bootstrap(loop_g3)
    requests_path = loop_g3 / "requests.md"
    uf_request = "REQ-20260707-100000-frontend"
    # A user-facing request parked at REVIEWING (work done, awaiting the human).
    _append_request_row(requests_path, uf_request, "product", "awaiting human sign-off: try import")
    _set_request_status(requests_path, uf_request, "REVIEWING")
    # Archive a REVIEW_DONE so the stall detector's done-by-review signal fires.
    rd_dir = loop_g3 / "messages" / uf_request
    rd_dir.mkdir(parents=True, exist_ok=True)
    (rd_dir / "REVIEW_DONE-iter-1.md").write_text("# REVIEW_DONE\n\npass\n", encoding="utf-8")

    # --- Negative baseline: no human_qa marker yet -> the hold is absent, so a
    # done-but-unadvanced REVIEWING request DOES stall (the exclusion is not a
    # blanket suppression of REVIEWING requests).
    base = doctor.summarize(loop_g3, stale_heartbeat_mins=doctor.DEFAULT_STALE_HEARTBEAT_MINS)
    base_stalls = [w for w in base["warnings"] if w["code"] == "stalled_handoff" and uf_request in w["message"]]
    if not base_stalls:
        _fail("without a human_qa_requested row, a done REVIEWING request must still emit stalled_handoff")
    if uf_request in base.get("held_for_human_qa", []):
        _fail("a request with no human_qa_requested row must NOT be reported as held_for_human_qa")

    # --- Positive: append human_qa_requested (no confirmation) -> HELD, no stall.
    _append_run_log_row(loop_g3, uf_request, "REVIEWING", "REVIEWING", "product", "human_qa_requested: import your statement")
    held = doctor.summarize(loop_g3, stale_heartbeat_mins=doctor.DEFAULT_STALE_HEARTBEAT_MINS)
    held_stalls = [w for w in held["warnings"] if w["code"] == "stalled_handoff" and uf_request in w["message"]]
    if held_stalls:
        _fail("a request held awaiting human QA must NOT emit stalled_handoff (normal waiting)")
    if uf_request not in held.get("held_for_human_qa", []):
        _fail("doctor must expose the held request in held_for_human_qa")

    # --- Confirmation released the hold: append human_qa: confirmed. The request
    # is no longer held, so if it is STILL parked at REVIEWING (product has not
    # yet moved it to ACCEPTED) the stall fires again -- confirmation, not any
    # mention, ends the exclusion.
    _append_run_log_row(loop_g3, uf_request, "REVIEWING", "REVIEWING", "product", "human_qa: confirmed operator 2026-07-07")
    confirmed = doctor.summarize(loop_g3, stale_heartbeat_mins=doctor.DEFAULT_STALE_HEARTBEAT_MINS)
    if uf_request in confirmed.get("held_for_human_qa", []):
        _fail("a confirmed request must NOT remain in held_for_human_qa")
    conf_stalls = [w for w in confirmed["warnings"] if w["code"] == "stalled_handoff" and uf_request in w["message"]]
    if not conf_stalls:
        _fail("after human_qa: confirmed, a still-parked REVIEWING request must stall again (hold released)")


def _check_g20_doctor(tmp_path: Path) -> None:
    """G20: honest, grace-gated stall detection for REVIEWING.

    The synthetic loop reproduces the run-3 shape: a REVIEWING request OWNED by
    the ``review`` lane whose OWN implementation evidence is gate-green
    (SHIP_CHECK_OK) -- green BEFORE routing to review, exactly the standing
    false-alarm condition. Three acceptance probes, all with a fixed ``now`` so
    heartbeat ages are deterministic:

      (i)   FRESH reviewer heartbeat -> NO stalled_handoff. The grace window: a
            healthy in-progress review must not fire on the green implementation
            evidence (that would be a standing red banner from the moment
            REVIEWING begins).
      (ii)  STALE reviewer heartbeat (and, separately, MISSING) -> a
            stalled_handoff whose record carries
            reason=implementation_evidence_green_no_verdict, and whose message is
            HONEST: it never claims the review "finished" and names the
            implementation evidence + the idle reviewer to push.
      (iii) An archived REVIEW_DONE (review provably finished) with no forward
            transition -> IMMEDIATE stalled_handoff even with a FRESH heartbeat,
            reason=work_done_unreported, citing archived REVIEW_DONE.
    """
    now = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
    fresh_hb = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_hb = (now - timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%SZ")

    loop = tmp_path / "loop_g20"
    _bootstrap(loop, ["--set-thread", "review=codex:20202020-2020-2020-2020-202020202020"])
    requests_path = loop / "requests.md"
    registry = loop / "agent-lanes.md"
    evidence_dir = loop / "evidence"
    rid = "REQ-20260708-120000-review"
    _append_request_row(requests_path, rid, "review", "impl evidence green; awaiting verdict")
    _set_request_status(requests_path, rid, "REVIEWING")
    # The request's OWN implementation evidence is gate-green (the run-3 case:
    # green BEFORE the request is routed to review).
    _write_evidence(evidence_dir, rid, 1, "pytest", 0)

    def _probe():
        return doctor.summarize(
            loop, stale_heartbeat_mins=doctor.DEFAULT_STALE_HEARTBEAT_MINS, now=now
        )

    def _records(result):
        return [s for s in result.get("stalled_handoffs", []) if s["request_id"] == rid]

    def _msgs(result):
        return [
            w for w in result["warnings"]
            if w["code"] == "stalled_handoff" and rid in w["message"]
        ]

    # ---- (i) FRESH reviewer heartbeat -> grace window -> NO stall ------------
    _set_heartbeat(registry, "review", fresh_hb)
    fresh = _probe()
    if _msgs(fresh):
        _fail("G20 (i): a REVIEWING request with green implementation evidence and a FRESH "
              "reviewer heartbeat must NOT stall (grace window); got {0!r}".format(_msgs(fresh)))
    if _records(fresh):
        _fail("G20 (i): the request must be absent from stalled_handoffs while the review is healthy")
    # Sanity: the stall is suppressed by the grace window, not because the request
    # vanished. It must still be a live non-terminal request (probe (ii) then
    # flips ONLY the heartbeat and shows the SAME request stalling -- proving the
    # grace window, not a vacuous pass).
    if rid not in [r["request_id"] for r in (fresh.get("requests") or {}).get("non_terminal", [])]:
        _fail("G20 (i): the probe request must remain a live non-terminal request")

    # ---- (ii) STALE reviewer heartbeat -> honest stall ----------------------
    _set_heartbeat(registry, "review", stale_hb)
    stale = _probe()
    stale_recs = _records(stale)
    if not stale_recs:
        _fail("G20 (ii): a REVIEWING request with green evidence and a STALE reviewer heartbeat must stall")
    if stale_recs[0].get("reason") != "implementation_evidence_green_no_verdict":
        _fail("G20 (ii): the REVIEWING stall reason must be implementation_evidence_green_no_verdict; "
              "got {0!r}".format(stale_recs[0].get("reason")))
    if stale_recs[0].get("evidence") != "SHIP_CHECK_OK":
        _fail("G20 (ii): the REVIEWING stall must cite the SHIP_CHECK_OK implementation evidence")
    stale_msgs = _msgs(stale)
    if not stale_msgs:
        _fail("G20 (ii): expected a stalled_handoff warning naming the request")
    msg = stale_msgs[0]["message"].lower()
    if "finish" in msg:
        _fail("G20 (ii): the REVIEWING stall wording must NOT claim the review 'finished'; got {0!r}".format(
            stale_msgs[0]["message"]))
    for needle in ("implementation evidence", "reviewing", "verdict", "idle"):
        if needle not in msg:
            _fail("G20 (ii): the honest REVIEWING stall wording is missing {0!r}; got {1!r}".format(
                needle, stale_msgs[0]["message"]))

    # ---- (ii-b) MISSING reviewer heartbeat -> same honest stall -------------
    _blank_heartbeat(registry, "review")
    missing = _probe()
    miss_recs = _records(missing)
    if not miss_recs or miss_recs[0].get("reason") != "implementation_evidence_green_no_verdict":
        _fail("G20 (ii-b): a REVIEWING request with green evidence and a MISSING (never-checked-in) "
              "reviewer heartbeat must stall with reason implementation_evidence_green_no_verdict; "
              "got {0!r}".format(miss_recs))

    # ---- (iii) Archived REVIEW_DONE -> immediate stall regardless of HB -----
    # Restore a FRESH heartbeat to prove the grace window does NOT suppress the
    # REVIEW_DONE path (review provably finished).
    _set_heartbeat(registry, "review", fresh_hb)
    rd_dir = loop / "messages" / rid
    rd_dir.mkdir(parents=True, exist_ok=True)
    (rd_dir / "REVIEW_DONE-iter-1.md").write_text("# REVIEW_DONE\n\npass\n", encoding="utf-8")
    reviewed = _probe()
    rd_recs = _records(reviewed)
    if not rd_recs:
        _fail("G20 (iii): an archived REVIEW_DONE with no transition must stall IMMEDIATELY even with a "
              "fresh reviewer heartbeat")
    if rd_recs[0].get("reason") != "work_done_unreported":
        _fail("G20 (iii): a REVIEW_DONE-based stall must carry reason work_done_unreported; got {0!r}".format(
            rd_recs[0].get("reason")))
    rd_msgs = _msgs(reviewed)
    if not rd_msgs or "REVIEW_DONE" not in rd_msgs[0]["message"]:
        _fail("G20 (iii): the REVIEW_DONE-based stall should cite archived REVIEW_DONE as its evidence")


def _check_g22(tmp_path: Path) -> None:
    """G22: disjoint write-scope rules -- doctor checks, bootstrap advisory, SKILL wording.

    Six probes match the spec:
      (i)   src/** vs src/ui/** -> write_scope_overlap fires naming both lanes;
      (ii)  src/core/** vs src/ui/** -> silent (disjoint subtrees);
      (iii) product scope docs/product/** only -> product_scope_gap fires;
      (iv)  product scope docs/loop/**; docs/product/** -> silent;
      (v)   SKILL.md wording (disjoint mandate, product-ledger rule, own-lane-dir
            rule, the worked example);
      (vi)  bootstrap prints an advisory on a colliding --extra-lane (stdout capture).
    Both new codes are WARNING-only and never flip handoff_ready.
    """
    import contextlib
    import io

    # ---- (i) src/** vs src/ui/** -> write_scope_overlap naming both lanes ----
    loop_i = tmp_path / "g22_overlap"
    _bootstrap(loop_i, [
        "--no-default-lanes",
        "--extra-lane", "data-eng|Own core|src/**",
        "--extra-lane", "frontend|Own UI|src/ui/**",
    ])
    res_i = _doctor(loop_i)
    over_i = [w for w in res_i["warnings"] if w["code"] == "write_scope_overlap"]
    if not over_i:
        _fail("G22 (i): src/** vs src/ui/** must warn write_scope_overlap")
    if not any("data-eng" in w["message"] and "frontend" in w["message"] for w in over_i):
        _fail("G22 (i): write_scope_overlap must name BOTH lanes")
    if not any("src/**" in w["message"] and "src/ui/**" in w["message"] for w in over_i):
        _fail("G22 (i): write_scope_overlap must name the offending globs")
    if any(i["code"] == "write_scope_overlap" for i in res_i["issues"]):
        _fail("G22 (i): write_scope_overlap must be a WARNING, never an issue")
    mo = res_i.get("write_scope_overlap") or []
    if not any({o["lane_a"], o["lane_b"]} == {"data-eng", "frontend"} for o in mo):
        _fail("G22 (i): doctor result must expose write_scope_overlap naming both lanes")
    if res_i.get("handoff_ready") is not True:
        _fail("G22 (i): write_scope_overlap must not flip handoff_ready to False")

    # ---- (ii) src/core/** vs src/ui/** -> silent (disjoint) -----------------
    loop_ii = tmp_path / "g22_disjoint"
    _bootstrap(loop_ii, [
        "--no-default-lanes",
        "--extra-lane", "data-eng|Own core|src/core/**",
        "--extra-lane", "frontend|Own UI|src/ui/**",
    ])
    res_ii = _doctor(loop_ii)
    if any(w["code"] == "write_scope_overlap" for w in res_ii["warnings"]):
        _fail("G22 (ii): src/core/** vs src/ui/** are disjoint and must NOT warn overlap")

    # ---- (iii) product scope docs/product/** only -> product_scope_gap ------
    loop_iii = tmp_path / "g22_gap"
    _bootstrap(loop_iii, [
        "--no-default-lanes",
        "--extra-lane", "product|Own product|docs/product/**",
    ])
    res_iii = _doctor(loop_iii)
    gaps = [w for w in res_iii["warnings"] if w["code"] == "product_scope_gap"]
    if not gaps:
        _fail("G22 (iii): a product lane not covering docs/loop/** must warn product_scope_gap")
    if not any("docs/loop" in w["message"] for w in gaps):
        _fail("G22 (iii): product_scope_gap message must name docs/loop/**")
    if any(i["code"] == "product_scope_gap" for i in res_iii["issues"]):
        _fail("G22 (iii): product_scope_gap must be a WARNING, never an issue")
    if not res_iii.get("product_scope_gap"):
        _fail("G22 (iii): doctor result must expose the product_scope_gap finding")
    if res_iii.get("handoff_ready") is not True:
        _fail("G22 (iii): product_scope_gap must not flip handoff_ready to False")

    # ---- (iv) product scope docs/loop/**; docs/product/** -> silent ---------
    loop_iv = tmp_path / "g22_gap_ok"
    _bootstrap(loop_iv, [
        "--no-default-lanes",
        "--extra-lane", "product|Own product|docs/loop/**; docs/product/**",
    ])
    res_iv = _doctor(loop_iv)
    if any(w["code"] == "product_scope_gap" for w in res_iv["warnings"]):
        _fail("G22 (iv): product covering docs/loop/** must NOT warn product_scope_gap")

    # A default-bootstrapped loop (product owns docs/loop/**, disjoint defaults)
    # must be clean on BOTH new codes -- the default team models the rule.
    loop_def = tmp_path / "g22_default"
    _bootstrap(loop_def)
    res_def = _doctor(loop_def)
    if any(w["code"] in ("write_scope_overlap", "product_scope_gap") for w in res_def["warnings"]):
        _fail("G22: a default-bootstrapped loop must be clean on write_scope_overlap/product_scope_gap")

    # ---- (v) SKILL.md wording ------------------------------------------------
    skill_md = _read_doc(_SKILL_MD)
    skill_low = skill_md.lower()
    if "pairwise disjoint" not in skill_low:
        _fail("G22 (v): SKILL.md must mandate pairwise-disjoint write scopes")
    if "docs/loop/**" not in skill_md:
        _fail("G22 (v): SKILL.md must state product's scope MUST include docs/loop/**")
    if ".gitignore" not in skill_md:
        _fail("G22 (v): SKILL.md product-ledger rule must include .gitignore")
    if "docs/loop/lanes/<lane>/**" not in skill_md:
        _fail("G22 (v): SKILL.md must require each lane own its docs/loop/lanes/<lane>/** dir")
    if "src/core/**" not in skill_md or "src/ui/**" not in skill_md:
        _fail("G22 (v): SKILL.md must carry a worked example of a disjoint cut (src/core vs src/ui)")
    if "write_scope_overlap" not in skill_md:
        _fail("G22 (v): SKILL.md must name the write_scope_overlap doctor warning")
    if "product_scope_gap" not in skill_md:
        _fail("G22 (v): SKILL.md must name the product_scope_gap doctor warning")

    # ---- (vi) bootstrap advisory on a colliding --extra-lane (stdout) --------
    loop_vi = tmp_path / "g22_advisory"
    _bootstrap(loop_vi)  # default lanes: implementation owns src/**.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _bootstrap(loop_vi, ["--extra-lane", "collide|Own core|src/**"])
    printed = buf.getvalue()
    printed_low = printed.lower()
    if "advisory" not in printed_low or "overlap" not in printed_low:
        _fail("G22 (vi): a colliding --extra-lane must print an advisory overlap line; got {0!r}".format(printed))
    if "collide" not in printed_low or "implementation" not in printed_low:
        _fail("G22 (vi): the advisory must name both the new lane and the colliding existing lane")
    if "src/**" not in printed:
        _fail("G22 (vi): the advisory must name the offending glob src/**")
    # NEGATIVE: a disjoint --extra-lane prints no overlap advisory (and a
    # pre-existing overlap is not re-announced for a lane that is not new).
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        _bootstrap(loop_vi, ["--extra-lane", "docs-writer|Own docs|docs/writer/**"])
    out2 = buf2.getvalue().lower()
    if "advisory" in out2 and "overlap" in out2:
        _fail("G22 (vi): a disjoint --extra-lane must NOT print an overlap advisory; got {0!r}".format(buf2.getvalue()))


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

        # ---- G16: per-lane advisory model-tier column -----------------------
        # The registry must carry a ``tier`` column whose values are the ABSTRACT
        # tier words (never a model name), assigned by the G16 policy: EVERY lane
        # -- default or custom-named -- defaults to the HIGHEST tier (this
        # supersedes the old F8 coding/non-coding split; downgrading is now a
        # manual human action). And the tier must survive every bootstrap
        # round-trip: the template rerun, a lane registration via --extra-lane,
        # and a --set-thread adoption -- none may clobber a human opt-down.
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

        # (b) Policy assignment for the default lanes: G16 -> every lane highest.
        for lane in ("implementation", "product", "review"):
            if _tier_of(lane) != bootstrap_agent_loop.HIGHEST_TIER:
                _fail("G16: default lane {0!r} should default to the highest tier".format(lane))
        # And the tier words are abstract -- never a concrete model name.
        for word in (bootstrap_agent_loop.HIGHEST_TIER, bootstrap_agent_loop.SECOND_HIGHEST_TIER):
            if "gpt" in word.lower():
                _fail("tier words must be abstract, never a model name; got {0!r}".format(word))

        # (c) recommended_tier_for returns highest for EVERY name -- former coding
        # names and former non-coding names alike (G16 removed the split).
        for lane in ("data-engineering", "backend", "frontend", "data-eng",
                     "implementation", "product", "review", "security-privacy",
                     "research", "docs"):
            if bootstrap_agent_loop.recommended_tier_for(lane) != bootstrap_agent_loop.HIGHEST_TIER:
                _fail("G16: recommended_tier_for({0!r}) should be the highest tier".format(lane))

        # (d) A new custom lane via --extra-lane also registers at the highest
        # tier, whatever its name -- there is no per-lane classification.
        _bootstrap(loop_t, ["--extra-lane", "data-engineering|Own backend|src/core/**"])
        _bootstrap(loop_t, ["--extra-lane", "security-privacy|Own privacy|docs/security/**"])
        if _tier_of("data-engineering") != bootstrap_agent_loop.HIGHEST_TIER:
            _fail("G16: new custom lane 'data-engineering' should register at the highest tier")
        if _tier_of("security-privacy") != bootstrap_agent_loop.HIGHEST_TIER:
            _fail("G16: new custom lane 'security-privacy' should register at the highest tier")

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

        # ---- G5: single-source "close the turn" ritual ----------------------
        # The in-turn report-back ritual must be defined in FULL exactly once
        # (protocol.md), coined with the leading token "close the turn"; SKILL.md
        # and loop-state.md carry the token + a pointer to protocol.md but NOT
        # the full step list. Three drifting copies were the failure this fixes.
        skill_md = _read_doc(_SKILL_MD)
        protocol_md = _read_doc(_PROTOCOL_MD)
        loop_state_md = _read_doc(_LOOP_STATE_MD)

        # The leading token appears in all three files (case-insensitive).
        for name, text in (
            ("SKILL.md", skill_md),
            ("protocol.md", protocol_md),
            ("loop-state.md", loop_state_md),
        ):
            if "close the turn" not in text.lower():
                _fail("{0} must reference the ritual by the token 'close the turn'".format(name))

        # The FULL step list is a numbered "1. ... send the reply" beat. It must
        # appear in exactly one file (protocol.md). Signature: the numbered
        # "1." step naming the reply message, present once.
        def _has_full_step_list(text: str) -> bool:
            lower = text.lower()
            return (
                "1." in text
                and "2." in text
                and "3." in text
                and "4." in text
                and "send the reply message" in lower
                and "append the `loop-run-log.md` row" in lower
                and "refresh your heartbeat" in lower
            )

        full_sources = [
            name
            for name, text in (
                ("SKILL.md", skill_md),
                ("protocol.md", protocol_md),
                ("loop-state.md", loop_state_md),
            )
            if _has_full_step_list(text)
        ]
        if full_sources != ["protocol.md"]:
            _fail(
                "the full close-the-turn step list must live in exactly protocol.md; "
                "found it in {0}".format(full_sources or "(nowhere)")
            )

        # The two pointer files must name protocol.md as the single source.
        if "protocol.md" not in skill_md:
            _fail("SKILL.md must point to references/protocol.md for the ritual")
        if "protocol.md" not in loop_state_md:
            _fail("loop-state.md must point to references/protocol.md for the ritual")

        # ---- G4: commit-as-lane is the 5th close-the-turn step --------------
        # The single source (protocol.md) must carry a 5th mandatory step:
        # commit your slice as your lane, with the WHY (uncommitted slice leaves
        # the scope guard inert + next lane builds on uncommitted state).
        protocol_lower = protocol_md.lower()
        if "5." not in protocol_md or "codex_lane=" not in protocol_lower:
            _fail("protocol.md close-the-turn ritual is missing the 5th commit-as-lane step")
        if "git commit" not in protocol_lower:
            _fail("the commit-as-lane step must name `git commit`")
        if "scope guard" not in protocol_lower or "inert" not in protocol_lower:
            _fail("the commit-as-lane step must state the WHY (uncommitted slice = inert scope guard)")
        # Product's accept/pause path: a paused loop is a fully committed loop.
        if "git status --porcelain" not in protocol_lower:
            _fail("protocol.md must require `git status --porcelain` before pausing")
        if "paused loop is a fully committed loop" not in protocol_lower:
            _fail("protocol.md must state 'a paused loop is a fully committed loop'")
        # UI addendum: restart a serving process and re-smoke the LIVE instance.
        if "re-smoke" not in protocol_lower and "re-run the smoke" not in protocol_lower:
            _fail("protocol.md UI addendum must require re-smoking a restarted serving process")
        if "live instance" not in protocol_lower:
            _fail("protocol.md UI addendum must name the LIVE instance")
        # The full step list (now 5 steps) still lives ONLY in protocol.md: the
        # single-source check above already guards SKILL.md/loop-state.md against
        # restating it.

        # ---- G6: Human Direct-Ask Ritual (hard gate) ------------------------
        # SKILL.md must carry a positively-framed direct-ask ritual: a lane
        # receiving a direct work ask records the preference and ROUTES it
        # (ask product to mint the request, or self-mint cc product); the change
        # ships only through the normal lifecycle.
        skill_lower = skill_md.lower()
        if "human direct-ask ritual" not in skill_lower:
            _fail("SKILL.md must add a 'Human Direct-Ask Ritual (hard gate)' subsection")
        if "record the preference" not in skill_lower and "records the preference" not in skill_lower:
            _fail("the direct-ask ritual must say a lane records the preference")
        if "route" not in skill_lower:
            _fail("the direct-ask ritual must ROUTE the ask into the normal lifecycle")
        if "ask product to create the request" not in skill_lower:
            _fail("the direct-ask ritual must offer the exit 'ask product to create the request'")
        # Paired product rule: product dispatches, does not implement.
        if "product dispatches" not in skill_lower or "does not implement" not in skill_lower:
            _fail("SKILL.md must carry the paired rule 'product dispatches; it does not implement'")
        if "implementation_request" not in skill_lower:
            _fail("the product-dispatches rule must route even a one-line ask into an IMPLEMENTATION_REQUEST")
        # One crisp cardinal statement -- stated EXACTLY once (single-source,
        # same principle as G5): other mentions back-reference it, never restate.
        cardinal_count = skill_lower.count("no code ships without a request_id")
        if cardinal_count != 1:
            _fail(
                "SKILL.md must state the cardinal rule 'no code ships without a "
                "request_id and independent review' exactly once; found {0}".format(cardinal_count)
            )
        if "no code ships without a request_id and independent review" not in skill_lower:
            _fail("the single cardinal statement must carry the full rule text")
        # New Stop Condition: asked to change code with no backing request row.
        if "no backing request row" not in skill_lower:
            _fail("SKILL.md Stop Conditions must include 'asked to change code with no backing request row'")

        # ---- G1: red-capable acceptance criteria ----------------------------
        # protocol.md IMPLEMENTATION_REQUEST template must teach red-capable
        # criteria: each criterion names the exact command that proves it, the
        # command must be able to go RED on that criterion's violation, and the
        # good/bad exemplar pair + the run-2 canonical counterexample are cited.
        protocol_g1 = protocol_lower
        if "red-capable" not in protocol_g1:
            _fail("protocol.md must teach 'red-capable' acceptance criteria")
        if "a criterion with no command that can go red is a vibe" not in protocol_g1:
            _fail("protocol.md must carry the 'a criterion ... is a vibe: sharpen it or drop it' rule")
        # The exemplar pair: bad 'parsing works' vs a good field-level unittest.
        if "parsing works" not in protocol_g1:
            _fail("protocol.md must show the BAD exemplar ('parsing works')")
        if "test_parse_fields" not in protocol_g1:
            _fail("protocol.md must show the GOOD exemplar (a field-level unittest that fails on garbage)")
        if "merchant is non-numeric" not in protocol_g1:
            _fail("the good exemplar must assert merchant is non-numeric text")
        # The canonical run-2 counterexample must be cited with its real defect.
        for needle in ("req-20260707-073729-data-eng", "td-pdf-smoke", 'merchant="9"', "2026-06-00"):
            if needle not in protocol_g1:
                _fail("protocol.md must cite the run-2 counterexample detail: {0}".format(needle))
        # The IMPLEMENTATION_REQUEST template itself carries a per-criterion
        # VERIFY command so an author copies red-capable criteria, not vibes.
        if "verify `python -m unittest" not in protocol_g1:
            _fail("the IMPLEMENTATION_REQUEST template must show a per-criterion VERIFY command")

        # loop-state.md: the product->implementation gate demands a red-capable
        # command per criterion, and the review gate carries the
        # tautological-evidence guard citing the same counterexample.
        loop_state_lower = loop_state_md.lower()
        if "red-capable verify command" not in loop_state_lower:
            _fail("loop-state.md product->implementation gate must require a red-capable verify command per criterion")
        if "tautological-evidence guard" not in loop_state_lower:
            _fail("loop-state.md review gate must carry the tautological-evidence guard")
        if "cannot distinguish" not in loop_state_lower:
            _fail("the tautological-evidence guard must reject evidence that cannot distinguish success from garbage")
        if "req-20260707-073729-data-eng" not in loop_state_lower:
            _fail("loop-state.md tautological-evidence guard must cite the run-2 counterexample")

        # SKILL.md mirrors the red-capable rule in one sentence.
        if "red-capable" not in skill_lower:
            _fail("SKILL.md must mirror the red-capable verify-command rule")

        # ---- G2: real-input correctness + redacted-sample ritual ------------
        # protocol.md must define field-level real-data correctness (row count,
        # valid calendar dates, non-numeric merchant, sign convention) and the
        # redacted-sample ritual (human approves a sanitized excerpt/field-shape
        # spec ONCE at intake; evidence records only counts/booleans).
        if "real-input correctness" not in protocol_g1:
            _fail("protocol.md must define a 'Real-input correctness' verification surface")
        if "field-level correctness" not in protocol_g1:
            _fail("protocol.md real-input section must require field-level correctness")
        if "parses without error" not in protocol_g1:
            _fail("protocol.md must state 'parses without error' alone is never sufficient real-data evidence")
        if "redacted-sample ritual" not in protocol_g1:
            _fail("protocol.md must define the redacted-sample ritual")
        # The four field-level assertions must all be named.
        for needle in ("row count", "valid", "calendar", "non-numeric", "sign"):
            if needle not in protocol_g1:
                _fail("protocol.md real-input section must name the field assertion: {0}".format(needle))
        # Evidence records only counts/booleans, never raw rows.
        if "only counts" not in protocol_g1 and "counts and booleans" not in protocol_g1:
            _fail("the redacted-sample ritual must record only counts/booleans as evidence")
        if "once at intake" not in protocol_g1:
            _fail("the redacted-sample must be approved ONCE at intake")

        # loop-state.md product->implementation gate must require the field-level
        # criterion when the goal names real data.
        if "field-level" not in loop_state_lower:
            _fail("loop-state.md gate must require a field-level criterion for real data")
        if "human-provided real data" not in loop_state_lower:
            _fail("loop-state.md gate must key the field-level rule off human-provided real data")

        # SKILL.md intake must carry the redacted-sample ritual.
        if "redacted-sample ritual" not in skill_lower:
            _fail("SKILL.md intake must define the redacted-sample ritual")
        if "field-shape spec" not in skill_lower:
            _fail("SKILL.md redacted-sample ritual must offer a field-shape spec as the derivative")

        # ---- G3: human-QA gate for user-facing slices (doc wording) ---------
        # protocol.md defines the gate: user_facing marker, hold within existing
        # tokens (REVIEWING + next_action + human_qa_requested run-log row),
        # sign-off via human_qa: confirmed BEFORE ACCEPTED, machine gate first.
        if "human-qa gate for user-facing slices" not in protocol_g1:
            _fail("protocol.md must define the 'Human-QA gate for user-facing slices'")
        if "user_facing: true" not in protocol_md:
            _fail("protocol.md must mark user-facing requests with 'user_facing: true'")
        if "human_qa_requested" not in protocol_g1:
            _fail("protocol.md human-QA gate must record a human_qa_requested run-log row")
        if "human_qa: confirmed" not in protocol_g1:
            _fail("protocol.md human-QA gate must record human_qa: confirmed before ACCEPTED")
        if "machine evidence alone" not in protocol_g1:
            _fail("protocol.md must state a user-facing slice does not reach ACCEPTED on machine evidence alone")
        # It must NOT introduce a new status token; the hold stays at REVIEWING.
        if "do not invent a new status" not in protocol_g1 and "do not\n   invent a new status" not in protocol_g1:
            _fail("protocol.md human-QA gate must keep the hold at REVIEWING (no new status token)")
        # The IMPLEMENTATION_REQUEST envelope carries the user_facing field.
        if "user_facing: false" not in protocol_md:
            _fail("the IMPLEMENTATION_REQUEST template must carry a user_facing envelope field")

        # loop-state.md carries the Human-QA Gate section + the stall exclusion.
        if "human-qa gate" not in loop_state_lower:
            _fail("loop-state.md must carry a 'Human-QA Gate' section")
        if "normal waiting, not a stall" not in loop_state_lower:
            _fail("loop-state.md must state a held request is normal waiting, not a stall")

        # SKILL.md mirrors the human-QA gate.
        if "human-qa sign-off" not in skill_lower:
            _fail("SKILL.md must mirror the human-QA sign-off before ACCEPTED for user-facing slices")

        # ---- G8: review upgrades --------------------------------------------
        # (a) Three named review categories incl. SCOPE CREEP with the
        # changed-files-vs-scope-globs yardstick.
        if "scope creep" not in loop_state_lower:
            _fail("loop-state.md review checklist must name a SCOPE CREEP category")
        if "changed_files" not in loop_state_lower and "changed files" not in loop_state_lower:
            _fail("the scope-creep check must use changed files vs scope globs as the yardstick")
        if "even if it works" not in loop_state_lower:
            _fail("scope creep must be flagged even if it works")
        if "looks-done-but-wrong" not in loop_state_lower:
            _fail("loop-state.md review checklist must name the looks-done-but-wrong category")
        # (b) Empty non_goals gate: not ready to send.
        if "non-empty non-goals" not in loop_state_lower and "non-empty non_goals" not in loop_state_lower:
            _fail("loop-state.md product->implementation gate must require non-empty non_goals")
        if "not ready to send" not in loop_state_lower:
            _fail("an empty non_goals must make the request 'not ready to send'")
        # (c) Standing ease-of-misuse question in the review gate + REVIEW_DONE.
        if "ease-of-misuse" not in loop_state_lower:
            _fail("loop-state.md review gate must carry the standing ease-of-misuse question")
        if "wrong-but-accepted" not in loop_state_lower:
            _fail("the ease-of-misuse question must ask about a wrong-but-accepted outcome the criteria did not forbid")
        if "ease_of_misuse" not in protocol_md:
            _fail("the REVIEW_DONE template must carry an ease_of_misuse field")
        # (d) FIX_REQUEST severity tiers; only blockers force a fix cycle.
        if "severity: blocker" not in protocol_md:
            _fail("the FIX_REQUEST envelope must carry a severity field (blocker|should-fix|nit)")
        for tier in ("blocker", "should-fix", "nit"):
            if tier not in loop_state_lower:
                _fail("loop-state.md must define the severity tier: {0}".format(tier))
        if "only a `blocker` forces a fix cycle" not in loop_state_lower:
            _fail("loop-state.md must state only a blocker forces a fix cycle")
        # iteration increments only for blockers (tolerate line-wrap between the
        # two words by collapsing whitespace before matching).
        loop_state_collapsed = " ".join(loop_state_lower.split())
        if "increments only for blockers" not in loop_state_collapsed:
            _fail("loop-state.md must state iteration increments only for blockers")
        # SKILL.md review-lane wording mirrors the three categories + severity.
        if "scope creep" not in skill_lower:
            _fail("SKILL.md review-lane wording must name scope creep")
        if "ease-of-misuse" not in skill_lower:
            _fail("SKILL.md review-lane wording must carry the ease-of-misuse question")
        if "only a blocker forces a fix cycle" not in skill_lower:
            _fail("SKILL.md must state only a blocker forces a fix cycle and increments iteration")

        # ---- G19: ritual-write carve-out from the scope-creep check ---------
        # Run 3: review blocked data-eng for stamping its own agent-lanes.md
        # heartbeat cell -- a write the close-the-turn ritual REQUIRES, so G8's
        # scope-creep rule as worded condemned every correctly-closed turn. The
        # comparison must exempt a STANDING LIST of protocol-mandated ritual
        # writes in BOTH gate docs (loop-state.md review gate + protocol.md),
        # keep writes to OTHER lanes' rows/dirs as creep, and cite the run-3
        # heartbeat stamp as the canonical non-creep example. SKILL.md's review
        # wording carries the one-sentence mirror.
        for g19_doc_name, g19_doc_lower in (
            ("loop-state.md", loop_state_lower),
            ("protocol.md", protocol_lower),
        ):
            g19_collapsed = " ".join(g19_doc_lower.split())
            if "protocol-mandated ritual writes" not in g19_collapsed:
                _fail("{0} must name the protocol-mandated ritual writes "
                      "exemption".format(g19_doc_name))
            if "exempt" not in g19_collapsed:
                _fail("{0} must say the ritual writes are EXEMPT from the "
                      "scope-creep comparison".format(g19_doc_name))
            # The standing exemption list: all six protocol-mandated writes.
            for g19_needle in (
                "own heartbeat cell in `agent-lanes.md`",
                "request's row in `requests.md`",
                "`loop-run-log.md` rows",
                "lanes/<lane>/**",
                "messages/<request_id>/**",
                "evidence/**",
            ):
                if g19_needle not in g19_collapsed:
                    _fail("{0} ritual-write exemption list is missing: "
                          "{1}".format(g19_doc_name, g19_needle))
            # Writes to OTHER lanes' rows/dirs remain creep.
            if "other lanes'" not in g19_collapsed:
                _fail("{0} must keep writes to OTHER lanes' rows/dirs as "
                      "creep".format(g19_doc_name))
            if "remain creep" not in g19_collapsed:
                _fail("{0} must state other-lane writes REMAIN creep".format(g19_doc_name))
            # The canonical run-3 non-creep example: data-eng's heartbeat stamp.
            if "data-eng" not in g19_collapsed:
                _fail("{0} must cite the run-3 data-eng heartbeat stamp as the "
                      "canonical non-creep example".format(g19_doc_name))
            if "non-creep example" not in g19_collapsed:
                _fail("{0} must label the run-3 heartbeat stamp a non-creep "
                      "example".format(g19_doc_name))
        # SKILL.md: the one-sentence exemption mirror in the review wording.
        skill_collapsed_g19 = " ".join(skill_lower.split())
        if ("ritual writes are exempt from the scope-creep comparison"
                not in skill_collapsed_g19):
            _fail("SKILL.md review wording must carry the one-sentence "
                  "ritual-write exemption")
        if "never creep" not in skill_collapsed_g19:
            _fail("SKILL.md exemption sentence must say the ritual writes are "
                  "never creep")
        if "other lanes'" not in skill_collapsed_g19:
            _fail("SKILL.md exemption sentence must keep OTHER lanes' "
                  "rows/dirs as creep")

        # ---- G9: grilling intake --------------------------------------------
        # SKILL.md Intake becomes an interview: one question at a time with a
        # recommended answer attached, facts looked up not asked, a stop rule,
        # and the MANDATORY operate-it question for user-facing goals. F3/F5
        # semantics (no placeholder BLOCKED, task-size gate, two forks) preserved.
        if "one question at a time" not in skill_lower:
            _fail("SKILL.md intake must ask ONE question at a time")
        if "recommended answer" not in skill_lower:
            _fail("SKILL.md intake must attach a recommended answer to every question")
        if "look up any fact" not in skill_lower and "looked up" not in skill_lower:
            _fail("SKILL.md intake must look up facts the repo/host can answer, never ask them")
        if "stop rule" not in skill_lower:
            _fail("SKILL.md intake must carry a stop rule (checkable objective -> stop asking)")
        if "over-interview" not in skill_lower:
            _fail("SKILL.md intake stop rule must say 'do not over-interview'")
        if "walk me through how you'll actually operate this" not in skill_lower:
            _fail("SKILL.md must carry the MANDATORY operate-it question for user-facing goals")
        if "input method" not in skill_lower or "file selection" not in skill_lower:
            _fail("the operate-it question must ask about input method and file selection")
        # F3/F5 intake semantics preserved in SKILL.md.
        if "absence of a request" not in skill_lower:
            _fail("SKILL.md must preserve the F3 no-placeholder-BLOCKED intake semantic")
        if "which fork" not in skill_lower or "which cut" not in skill_lower:
            _fail("SKILL.md must preserve the two forks (build-vs-operations, discipline-vs-feature)")

        # loop-state.md intake section carries the grilling rules + operate-it Q.
        if "one question at a time" not in loop_state_lower:
            _fail("loop-state.md intake section must ask ONE question at a time")
        if "recommended answer" not in loop_state_lower:
            _fail("loop-state.md intake section must attach a recommended answer")
        if "stop rule" not in loop_state_lower:
            _fail("loop-state.md intake section must carry the stop rule")
        if "walk me through how you'll actually operate this" not in loop_state_lower:
            _fail("loop-state.md intake section must carry the mandatory operate-it question")
        if "placeholder blocked" not in loop_state_lower and "no goal yet" not in loop_state_lower:
            _fail("loop-state.md intake section must preserve the F3 no-placeholder-BLOCKED semantic")

        # ---- G12: handoff redaction wording ---------------------------------
        # SKILL.md and protocol.md must both instruct: reference sensitive
        # material, never quote it into a handoff/auto-chain seed; and name the
        # doctor's handoff_sensitive_content warning.
        if "reference sensitive material" not in skill_lower:
            _fail("SKILL.md must instruct: reference sensitive material, never quote it into a handoff")
        if "handoff_sensitive_content" not in skill_md:
            _fail("SKILL.md must name the handoff_sensitive_content doctor warning")
        if "reference sensitive material" not in protocol_md.lower():
            _fail("protocol.md must instruct: reference sensitive material, never quote it")
        if "handoff_sensitive_content" not in protocol_md:
            _fail("protocol.md must name the handoff_sensitive_content doctor warning")

        # ---- G13: BLOCKED envelopes carry recommended_answer ----------------
        # The BLOCKED template must carry a recommended_answer field, and the
        # prose must generalize F16's install command into "the lane proposes
        # the resolution; the human edits a proposal, not a blank page".
        if "recommended_answer:" not in protocol_md:
            _fail("protocol.md BLOCKED envelope must carry a recommended_answer field")
        if "recommended_answer" not in protocol_md.lower():
            _fail("protocol.md must document the recommended_answer field")
        if "edits a proposal" not in protocol_md.lower():
            _fail("protocol.md must state the human edits a proposal instead of authoring cold")

        # ---- G14 (d/e): tier policy wording in SKILL.md ---------------------
        # (a) SKILL.md instructs recording the OBSERVED model at creation/adoption
        # into current.md's model_observed line (as data, with the abstract tag).
        if "model_observed" not in skill_md:
            _fail("SKILL.md must instruct recording the observed model in current.md model_observed")
        if "observed data" not in skill_lower:
            _fail("SKILL.md must frame model_observed as observed DATA, not policy")
        # (d) SKILL.md resolves the create_thread "no model unless asked" conflict:
        # the recorded tier policy IS the user's explicit request; pass model+thinking.
        if "recorded tier policy is the user's explicit request" not in skill_lower:
            _fail("SKILL.md (G14d) must state the recorded tier policy IS the user's explicit request")
        if "create_thread" not in skill_md:
            _fail("SKILL.md (G14d) must reference create_thread's 'no model unless asked' guidance")
        # (e) G16 policy wording: EVERY lane defaults to the highest tier, and
        # downgrading is a MANUAL human action (any lane DOWN to any lower tier);
        # the skill never silently deviates from the recorded tier.
        if "highest available tier" not in skill_lower:
            _fail("SKILL.md must state every lane defaults to the highest available tier")
        if "manual human action" not in skill_lower:
            _fail("SKILL.md must state downgrading is a manual human action")
        if "any lower tier" not in skill_lower:
            _fail("SKILL.md must state the human may set any lane DOWN to any lower tier")
        if "never silently deviate" not in skill_lower and "never silently deviates" not in skill_lower:
            _fail("SKILL.md must state the skill never silently deviates from the recorded tier")

        # (G14 boundary) No concrete model-name literal may appear anywhere in
        # SKILL.md or protocol.md: policy docs may only speak in the abstract
        # tier words; a concrete model id belongs solely in runtime observed
        # data (e.g. a lane's current.md model_observed line), never in doc
        # prose or examples. Scan both doc files for the two known concrete
        # patterns (a case-insensitive regex, so this is not just a substring
        # check).
        _CONCRETE_MODEL_RE = re.compile(r"gpt-[0-9]|codex-spark", re.IGNORECASE)
        for doc_name, doc_text in (("SKILL.md", skill_md), ("protocol.md", protocol_md)):
            hit = _CONCRETE_MODEL_RE.search(doc_text)
            if hit:
                _fail(
                    "G14 boundary violation: {0} contains a concrete model-name "
                    "literal {1!r}; policy docs may only use abstract tier words "
                    "(a concrete model id is runtime observed data, never doc "
                    "policy text)".format(doc_name, hit.group(0))
                )

        # ---- G15: default design-skill references for UI work ---------------
        # Both skill NAMES + the han-design URL; the style-vs-UX division with the
        # conflict rule; human-override-wins recorded on the request envelope; and
        # absent-degradation (note + proceed, never a blocker). REVISED spec:
        # han-design-skill-v1 = VISUAL STYLE, ui-ux-pro-max = UX MECHANICS.
        if "han-design-skill-v1" not in skill_md:
            _fail("SKILL.md (G15) must name han-design-skill-v1")
        if "ui-ux-pro-max" not in skill_md:
            _fail("SKILL.md (G15) must name ui-ux-pro-max")
        if "github.com/hanco1/han-design-skill-v1" not in skill_md:
            _fail("SKILL.md (G15) must reference the han-design-skill-v1 source URL")
        # The division of labor: han-design = visual style, ui-ux-pro-max = UX.
        if "visual style" not in skill_lower:
            _fail("SKILL.md (G15) must assign the VISUAL STYLE to han-design-skill-v1")
        if "ux mechanics" not in skill_lower:
            _fail("SKILL.md (G15) must assign the UX MECHANICS to ui-ux-pro-max")
        # The conflict rule: visual -> han-design; usability/accessibility -> ui-ux.
        skill_collapsed = " ".join(skill_lower.split())
        if "conflict rule" not in skill_lower:
            _fail("SKILL.md (G15) must state a conflict rule for the two design skills")
        if "accessibility" not in skill_lower:
            _fail("SKILL.md (G15) conflict rule must route accessibility calls to ui-ux-pro-max")
        # Human override wins, recorded on the request envelope design_system line.
        if "explicit choice always overrides" not in skill_collapsed:
            _fail("SKILL.md (G15) must state the human's explicit choice always overrides")
        if "design_system:" not in skill_md:
            _fail("SKILL.md (G15) must record the choice on the design_system envelope line")
        # Absent-degradation: note in worklog, proceed, never a blocker.
        if "never a hard dependency" not in skill_lower and "never a blocker" not in skill_lower:
            _fail("SKILL.md (G15) must state a missing design skill is never a blocker")
        if "by name" not in skill_lower and "by its name" not in skill_lower:
            _fail("SKILL.md (G15) must look skills up BY NAME (not by absolute path)")

        # protocol.md Common Envelope carries the design_system line + the same
        # division/override/lookup semantics.
        if "design_system:" not in protocol_md:
            _fail("protocol.md Common Envelope must carry a design_system line")
        if "han-design-skill-v1" not in protocol_md or "ui-ux-pro-max" not in protocol_md:
            _fail("protocol.md design_system note must name both design skills")

        # NO absolute local path may be introduced by the G15 design wording. The
        # ~/.codex/skills/<name> home-relative convention and the github URL are
        # allowed; an absolute Windows/Unix path is a batch failure. Scan every
        # line that mentions the design skills for absolute-path markers (a drive
        # letter like C:\ or C:/, a /Users/ or /home/<user> prefix, or a UNC \\).
        def _has_absolute_path(text: str) -> bool:
            low = text.lower()
            if "/users/" in low or "/home/" in low or "\\\\" in text:
                return True
            # A Windows drive-letter path: a SINGLE letter, a colon, then \ or /,
            # NOT preceded by an alnum (so a URL scheme like ``https://`` -- many
            # letters before the colon -- is excluded) and NOT a ``://`` scheme.
            for i in range(len(text) - 2):
                if not (text[i].isalpha() and text[i + 1] == ":" and text[i + 2] in "\\/"):
                    continue
                if i > 0 and (text[i - 1].isalnum() or text[i - 1] == "/"):
                    continue  # part of a longer token (e.g. a URL scheme)
                if text[i + 2] == "/" and i + 3 < len(text) and text[i + 3] == "/":
                    continue  # ``x://`` scheme, not a drive path
                return True
            return False

        for doc_name, doc_text in (("SKILL.md", skill_md), ("protocol.md", protocol_md)):
            for ln in doc_text.splitlines():
                low = ln.lower()
                if ("han-design" in low or "ui-ux-pro-max" in low
                        or "design_system" in low or "skills/<name>" in low):
                    if _has_absolute_path(ln):
                        _fail("G15: an absolute local path appears in the design wording of "
                              "{0}: {1!r}".format(doc_name, ln.strip()))

        # ---- G3: doctor stalled_handoff exclusion (synthetic loop) ----------
        _check_g3_doctor(tmp_path)

        # ---- G20: honest, grace-gated stall detection for REVIEWING ---------
        _check_g20_doctor(tmp_path)

        # ---- G7: doctor lineage/hygiene WARNING checks ----------------------
        _check_g7_doctor(tmp_path)

        # ---- G11: no pre-minted message dirs + shuffled-log invariance ------
        _check_g11(tmp_path)

        # ---- G12: handoff/auto-chain seed sensitive-content scan ------------
        _check_g12(tmp_path)

        # ---- G14: tier observability (model_observed + tier_mismatch) -------
        _check_g14(tmp_path)

        # ---- G22: disjoint write-scope rules (doctor + bootstrap + wording) -
        _check_g22(tmp_path)

    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
