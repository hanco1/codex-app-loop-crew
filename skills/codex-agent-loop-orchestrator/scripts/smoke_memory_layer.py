#!/usr/bin/env python3
"""Smoke test for the memory layer + team-shape flags.

Exercises the reference implementation end to end. Most checks run in-process
as direct module-level imports of ``bootstrap_agent_loop``,
``completion_gate``, ``record_decision``, and ``multi_agent_loop_doctor``, but
some checks DO spawn subprocesses: the pre-commit guard checks build
disposable ``git`` repos and run ``precommit_scope_guard.py`` under
``sys.executable``, so a working ``git`` on PATH is a PREREQUISITE for this
suite. (The G7 uncommitted-work positive case also shells out to git, but
skips itself gracefully when git cannot run.)

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

import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import bootstrap_agent_loop  # noqa: E402
import completion_gate  # noqa: E402
import deliver_message  # noqa: E402
import loop_dashboard  # noqa: E402
import multi_agent_loop_doctor as doctor  # noqa: E402
import precommit_scope_guard  # noqa: E402
import record_decision  # noqa: E402


PASSING_REQUEST = "REQ-20260704-000001-implementation"
FAILING_REQUEST = "REQ-20260704-000002-implementation"

# The skill root (one level above scripts/) holds SKILL.md and references/.
_SKILL_DIR = Path(__file__).resolve().parent.parent
_SKILL_MD = _SKILL_DIR / "SKILL.md"
_PROTOCOL_MD = _SKILL_DIR / "references" / "protocol.md"
_LOOP_STATE_MD = _SKILL_DIR / "references" / "loop-state.md"
_REFERENCE_MD_FILES = tuple(sorted((_SKILL_DIR / "references").glob("*.md")))
_DASHBOARD_HTML = _SKILL_DIR / "scripts" / "dashboard.html"
_LOOP_DASHBOARD_PY = _SKILL_DIR / "scripts" / "loop_dashboard.py"


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
    """Append one non-terminal request row with a filled last_message cell.

    Columns: request_id, status, owner_lane, iteration, source_docs,
    last_message, next_action, updated_at. ``last_message`` is only a message
    pointer; real evidence must come from an ``evidence/*.json`` record.
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


def _git(repo: Path, *args: str) -> None:
    """Run git in a disposable smoke repo and fail with its real stderr."""
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        _fail(
            "git {0} failed in disposable repo: {1}".format(
                " ".join(args), result.stderr.decode("utf-8", "replace").strip()
            )
        )


def _guard_repo(tmp_path: Path, name: str, overlap: bool = True) -> tuple[Path, Path]:
    """Create a committed real git repo, optionally with overlapping scopes."""
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "smoke@example.com")
    _git(repo, "config", "user.name", "smoke")
    loop = repo / "docs" / "loop"
    _bootstrap(
        loop,
        [
            "--no-default-lanes",
            "--extra-lane",
            "implementation|Own source|src/**",
            "--extra-lane",
            (
                "frontend|Own shared source|src/shared/**"
                if overlap
                else "frontend|Own UI|ui/**"
            ),
        ],
    )
    (repo / "src" / "shared").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "own.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "src" / "shared" / "item.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "seed")
    return repo, loop


def _run_guard(repo: Path, loop: Path, lane: str) -> tuple[int, str, str]:
    """Run the actual guard CLI so staged-path discovery uses the real index."""
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            str(Path(precommit_scope_guard.__file__).resolve()),
            "--loop-dir",
            str(loop),
            "--lane",
            lane,
        ],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    return (
        result.returncode,
        result.stdout.decode("utf-8", "replace"),
        result.stderr.decode("utf-8", "replace"),
    )


def _check_g26_c3_leases(tmp_path: Path) -> None:
    """G26 C3-leases: unreadable leases warn and protect other static scopes."""
    repo_ok, loop_ok = _guard_repo(tmp_path, "g26_c3_allow")
    (loop_ok / "leases.md").write_bytes(b"\xff\xfe\x00")
    own_path = repo_ok / "src" / "own.py"
    own_path.write_text("value = 2\n", encoding="utf-8")
    _git(repo_ok, "add", "src/own.py")
    rc_ok, _stdout_ok, stderr_ok = _run_guard(repo_ok, loop_ok, "implementation")
    if rc_ok != 0:
        _fail("G26 C3 negative: unreadable leases must not block an exclusive-scope path")
    if "leases.md" not in stderr_ok or "utf-8" not in stderr_ok.lower():
        _fail("G26 C3 negative: allowed commit must still surface the leases read failure")

    repo_block, loop_block = _guard_repo(tmp_path, "g26_c3_block")
    (loop_block / "leases.md").write_bytes(b"\xff\xfe\x00")
    shared_path = repo_block / "src" / "shared" / "item.py"
    shared_path.write_text("value = 2\n", encoding="utf-8")
    _git(repo_block, "add", "src/shared/item.py")
    rc_block, _stdout_block, stderr_block = _run_guard(
        repo_block, loop_block, "implementation"
    )
    if rc_block == 0:
        _fail("G26 C3 positive: unreadable leases must block a path in another lane's scope")
    if "leases.md" not in stderr_block or "frontend" not in stderr_block:
        _fail("G26 C3 positive: rejection must name the failed file and other static lane")


def _check_g26_c20_blank_active_lease(tmp_path: Path) -> None:
    """G26 C20: an ACTIVE blank lease glob rejects instead of disappearing."""
    repo, loop = _guard_repo(tmp_path, "g26_c20_active")
    request_id = "REQ-20260709-020001-frontend"
    (loop / "leases.md").write_text(
        "# File Leases\n\n"
        "| file_glob | lane | request_id | acquired_at | status |\n"
        "| --- | --- | --- | --- | --- |\n"
        "|  | frontend | {0} | 2026-07-09T00:00:00Z | ACTIVE |\n".format(request_id),
        encoding="utf-8",
    )
    own_path = repo / "src" / "own.py"
    own_path.write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "src/own.py")
    rc, _stdout, stderr = _run_guard(repo, loop, "implementation")
    if rc == 0:
        _fail("G26 C20 positive: another lane's ACTIVE blank lease glob must reject")
    if request_id not in stderr or "frontend" not in stderr:
        _fail("G26 C20 positive: rejection must name the malformed lease request and lane")
    if "file_glob" not in stderr or "blank" not in stderr.lower():
        _fail("G26 C20 positive: rejection must explicitly identify the blank file_glob")

    repo_released, loop_released = _guard_repo(tmp_path, "g26_c20_released")
    (loop_released / "leases.md").write_text(
        "# File Leases\n\n"
        "| file_glob | lane | request_id | acquired_at | status |\n"
        "| --- | --- | --- | --- | --- |\n"
        "|  | frontend | {0} | 2026-07-09T00:00:00Z | RELEASED |\n".format(request_id),
        encoding="utf-8",
    )
    released_path = repo_released / "src" / "own.py"
    released_path.write_text("value = 2\n", encoding="utf-8")
    _git(repo_released, "add", "src/own.py")
    rc_released, _stdout_released, stderr_released = _run_guard(
        repo_released, loop_released, "implementation"
    )
    if rc_released != 0:
        _fail("G26 C20 negative: an inactive blank lease must remain ignored")
    if "blank" in stderr_released.lower():
        _fail("G26 C20 negative: an inactive blank lease must not raise an ACTIVE warning")


def _check_g26_b9_unknown_lease_status(tmp_path: Path) -> None:
    """G26 B9: unknown status uses explicit fall-through, not a dead pass branch."""
    source = Path(precommit_scope_guard.__file__).read_text(encoding="utf-8")
    dead_branch = re.compile(
        r"if status and status not in ACTIVE_LEASE_STATUSES:\s*"
        r"(?:#[^\n]*\n\s*)+pass"
    )
    if dead_branch.search(source):
        _fail("G26 B9: unknown lease status must not rely on a dead pass branch")

    repo, loop = _guard_repo(tmp_path, "g26_b9_unknown", overlap=False)
    (loop / "leases.md").write_text(
        "# File Leases\n\n"
        "| file_glob | lane | request_id | acquired_at | status |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| src/own.py | frontend | REQ-20260709-020002-frontend | "
        "2026-07-09T00:00:00Z | FUTURE_STATUS |\n",
        encoding="utf-8",
    )
    shared_path = repo / "src" / "own.py"
    shared_path.write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "src/own.py")
    rc, _stdout, stderr = _run_guard(repo, loop, "implementation")
    if rc == 0:
        _fail("G26 B9: an unknown non-empty lease status must remain fail-closed")
    if "FUTURE_STATUS" in stderr:
        _fail("G26 B9: behavior should reject by lease coverage, not invent status handling")
    if "active lease held by lane 'frontend'" not in stderr:
        _fail("G26 B9: unknown status must fall through to normal active-lease enforcement")


def _check_g26_b4_guard_overlap(tmp_path: Path) -> None:
    """G26 B4: only paths in a registry's shared region fail closed."""
    repo_shared, loop_shared = _guard_repo(tmp_path, "g26_b4_shared")
    shared_path = repo_shared / "src" / "shared" / "item.py"
    shared_path.write_text("value = 2\n", encoding="utf-8")
    _git(repo_shared, "add", "src/shared/item.py")
    rc_shared, _stdout_shared, stderr_shared = _run_guard(
        repo_shared, loop_shared, "implementation"
    )
    if rc_shared == 0:
        _fail("G26 B4 positive: a staged path in two static scopes must be rejected")
    overlap_words = ("overlap", "implementation", "frontend", "src/shared/item.py")
    if any(word not in stderr_shared for word in overlap_words):
        _fail("G26 B4 positive: rejection must name the overlap, both lanes, and path")
    if "fix" not in stderr_shared.lower() or "agent-lanes.md" not in stderr_shared:
        _fail("G26 B4 positive: rejection must tell the user to fix the registry scopes")

    repo_exclusive, loop_exclusive = _guard_repo(tmp_path, "g26_b4_exclusive")
    exclusive_path = repo_exclusive / "src" / "own.py"
    exclusive_path.write_text("value = 2\n", encoding="utf-8")
    _git(repo_exclusive, "add", "src/own.py")
    rc_exclusive, _stdout_exclusive, stderr_exclusive = _run_guard(
        repo_exclusive, loop_exclusive, "implementation"
    )
    if rc_exclusive != 0:
        _fail("G26 B4 negative: overlap elsewhere must not block an exclusive-scope path")
    if "overlap" in stderr_exclusive.lower():
        _fail("G26 B4 negative: an exclusive path must not emit a scope-overlap warning")

    repo_ritual = tmp_path / "g26_b4_ritual"
    repo_ritual.mkdir(parents=True, exist_ok=True)
    _git(repo_ritual, "init", "--quiet")
    _git(repo_ritual, "config", "user.email", "smoke@example.com")
    _git(repo_ritual, "config", "user.name", "smoke")
    loop_ritual = repo_ritual / "docs" / "loop"
    _bootstrap(loop_ritual)
    _git(repo_ritual, "add", "-A")
    _git(repo_ritual, "commit", "--quiet", "-m", "seed")
    current = loop_ritual / "lanes" / "review" / "current.md"
    current.write_text(current.read_text(encoding="utf-8") + "\nritual: updated\n", encoding="utf-8")
    _git(repo_ritual, "add", "docs/loop/lanes/review/current.md")
    rc_ritual, _stdout_ritual, stderr_ritual = _run_guard(
        repo_ritual, loop_ritual, "review"
    )
    if rc_ritual != 0:
        _fail("G26 B4 carve-out: a lane must still write its own ritual directory")
    if "overlap" in stderr_ritual.lower():
        _fail("G26 B4 carve-out: product ledger nesting must not be reported as ambiguity")


def _check_g26_b5_docs_preset(tmp_path: Path) -> None:
    """G26 B5: the built-in docs preset is disjoint from product."""
    loop = tmp_path / "g26_b5_docs_preset"
    _bootstrap(loop, ["--preset", "docs"])
    rows = bootstrap_agent_loop.existing_rows(loop / "agent-lanes.md")
    docs_scope = rows.get("docs", {}).get("write_scope", "")
    scope_tokens = {token.strip() for token in docs_scope.split(";") if token.strip()}
    if "docs/**" in scope_tokens:
        _fail("G26 B5: the docs preset must not claim the product-owned docs/** tree")
    for expected in ("docs/user/**", "CHANGELOG.md", "docs/loop/lanes/docs/**"):
        if expected not in scope_tokens:
            _fail("G26 B5: docs preset is missing disjoint scope entry {0}".format(expected))
    result = _doctor(loop)
    docs_overlaps = [
        warning
        for warning in result["warnings"]
        if warning["code"] == "write_scope_overlap" and "docs" in warning["message"]
    ]
    if docs_overlaps:
        _fail("G26 B5 positive: built-in product + docs lanes must be disjoint")

    loop_bad = tmp_path / "g26_b5_old_style"
    _bootstrap(
        loop_bad,
        ["--extra-lane", "docs-old|Own every doc|docs/**"],
    )
    bad = _doctor(loop_bad)
    if not any(
        warning["code"] == "write_scope_overlap"
        and "docs-old" in warning["message"]
        and "product" in warning["message"]
        for warning in bad["warnings"]
    ):
        _fail("G26 B5 negative: a custom docs/** lane must still overlap product")


def _check_g26_c10_registry_rows(tmp_path: Path) -> None:
    """G26 C10: malformed registry rows survive every bootstrap rewrite."""
    import contextlib
    import io

    loop = tmp_path / "g26_c10_registry"
    _bootstrap(loop)
    registry = loop / "agent-lanes.md"
    malformed = "| ghost | codex:ghost-thread | Orphaned identity |"
    with registry.open("a", encoding="utf-8") as handle:
        handle.write(malformed + "\n")

    stderr_first = io.StringIO()
    with contextlib.redirect_stderr(stderr_first):
        _bootstrap(
            loop,
            ["--set-thread", "review=codex:20260709-0000-0000-0000-000000000010"],
        )
    first_text = registry.read_text(encoding="utf-8")
    first_warning = stderr_first.getvalue()
    if first_text.count(malformed) != 1:
        _fail("G26 C10: first registry rewrite must preserve the malformed row exactly once")
    if "quarantined malformed registry rows" not in first_text.lower():
        _fail("G26 C10: preserved malformed rows must be visibly quarantined")
    if "agent-lanes.md" not in first_warning or "line" not in first_warning.lower():
        _fail("G26 C10: rewrite must emit a loud warning with file and line")
    if malformed not in first_warning:
        _fail("G26 C10: warning must quote the malformed row that was preserved")
    if "codex:20260709-0000-0000-0000-000000000010" not in first_text:
        _fail("G26 C10: quarantine must not prevent valid --set-thread updates")
    guard_rows = precommit_scope_guard.parse_table(registry)
    if any(row.get("lane") == "ghost" for row in guard_rows):
        _fail("G26 C10: a quarantined row must not become an active guard registry row")

    stderr_second = io.StringIO()
    with contextlib.redirect_stderr(stderr_second):
        _bootstrap(loop)
    second_text = registry.read_text(encoding="utf-8")
    if second_text.count(malformed) != 1:
        _fail("G26 C10: repeated rewrites must neither drop nor duplicate the malformed row")
    if malformed not in stderr_second.getvalue():
        _fail("G26 C10: every rewrite must keep the malformed-row warning visible")

    clean_loop = tmp_path / "g26_c10_clean"
    clean_stderr = io.StringIO()
    with contextlib.redirect_stderr(clean_stderr):
        _bootstrap(clean_loop)
        _bootstrap(clean_loop)
    if clean_stderr.getvalue():
        _fail("G26 C10 negative: a well-formed registry must not emit malformed-row warnings")
    if "quarantined malformed registry rows" in (
        clean_loop / "agent-lanes.md"
    ).read_text(encoding="utf-8").lower():
        _fail("G26 C10 negative: a clean registry must not gain a quarantine section")


def _check_g26_c1_heartbeat_warning(tmp_path: Path) -> None:
    """G26 C1: heartbeat failures warn per file without failing delivery."""
    import contextlib
    import io

    loop = tmp_path / "g26_c1_broken_heartbeat"
    _bootstrap(loop)
    registry = loop / "agent-lanes.md"
    current = loop / "lanes" / "implementation" / "current.md"
    registry.write_bytes(b"\xff\xfe\x00")
    current.write_bytes(b"\xff\xfe\x00")
    message = tmp_path / "g26_c1_message.md"
    message.write_text("# LOOP_STATUS\n\nstill alive\n", encoding="utf-8")

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = deliver_message.main(
                [
                    "--loop-dir",
                    str(loop),
                    "--to-lane",
                    "product",
                    "--from-lane",
                    "implementation",
                    "--request-id",
                    "REQ-20260709-030001-implementation",
                    "--message-type",
                    "LOOP_STATUS",
                    "--iteration",
                    "1",
                    "--message-file",
                    str(message),
                ]
            )
    except (OSError, UnicodeDecodeError) as exc:
        _fail("G26 C1: heartbeat failure escaped and changed delivery behavior: {0}".format(exc))
    if rc != 0:
        _fail("G26 C1: heartbeat failure must not change delivery's zero exit status")
    if "delivered " not in stdout.getvalue() or "indexed " not in stdout.getvalue():
        _fail("G26 C1: heartbeat failure must not hide successful delivery/index output")
    delivered = list((loop / "lanes" / "product" / "inbox" / "new").glob("*.md"))
    if len(delivered) != 1:
        _fail("G26 C1: message must remain delivered despite both heartbeat failures")
    warning_lines = [
        line
        for line in stderr.getvalue().splitlines()
        if "warning:" in line.lower() and "heartbeat" in line.lower()
    ]
    if len(warning_lines) != 2:
        _fail("G26 C1: expected one heartbeat warning per failed file, got {0}".format(warning_lines))
    warning_text = "\n".join(warning_lines)
    for failed_path in (registry, current):
        if deliver_message.posix_path(str(failed_path)) not in warning_text:
            _fail("G26 C1: heartbeat warning must name failed path {0}".format(failed_path))
    if "UnicodeDecodeError" not in warning_text or "utf-8" not in warning_text.lower():
        _fail("G26 C1: heartbeat warning must include the concrete decode exception")

    clean_loop = tmp_path / "g26_c1_clean_heartbeat"
    _bootstrap(clean_loop)
    clean_stdout = io.StringIO()
    clean_stderr = io.StringIO()
    with contextlib.redirect_stdout(clean_stdout), contextlib.redirect_stderr(clean_stderr):
        clean_rc = deliver_message.main(
            [
                "--loop-dir",
                str(clean_loop),
                "--to-lane",
                "product",
                "--from-lane",
                "implementation",
                "--request-id",
                "REQ-20260709-030002-implementation",
                "--message-type",
                "LOOP_STATUS",
                "--iteration",
                "1",
                "--message-file",
                str(message),
            ]
        )
    if clean_rc != 0:
        _fail("G26 C1 negative: clean heartbeat delivery must pass")
    if "heartbeat" in clean_stderr.getvalue().lower():
        _fail("G26 C1 negative: successful heartbeat stamps must not warn")


def _check_g26_c7_malformed_diagnostics(tmp_path: Path) -> None:
    """G26 C7: malformed control-plane inputs warn without changing fallbacks."""
    loop = tmp_path / "g26_c7_malformed_inputs"
    _bootstrap(loop)

    run_request = "REQ-20260709-031000-implementation"
    (loop / "loop-run-log.md").write_text(
        "# Loop Run Log\n\n"
        "| timestamp | request_id | iteration | from_status | to_status | lane | note |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| definitely-not-a-time | {rid} | 1 | IMPLEMENTING | FIX_REQUESTED | review | bad time |\n"
        "| 2026-07-09T03:11:00Z |  | 1 | IMPLEMENTING | FIX_REQUESTED | review | no request |\n"
        "| 2026-07-09T03:12:00Z | {rid} | 2 | FIX_REQUESTED |  | implementation | no status |\n".format(
            rid=run_request
        ),
        encoding="utf-8",
    )

    policy_path = loop / "loop-policy.md"
    policy_text = policy_path.read_text(encoding="utf-8")
    if "max_fix_cycles: 3" not in policy_text:
        _fail("G26 C7 fixture: bootstrap policy is missing max_fix_cycles: 3")
    policy_path.write_text(
        policy_text.replace("max_fix_cycles: 3", "max_fix_cycles: definitely-not-an-int", 1),
        encoding="utf-8",
    )

    iteration_request = "REQ-20260709-031500-implementation"
    with (loop / "requests.md").open("a", encoding="utf-8") as handle:
        handle.write(
            "| {rid} | IMPLEMENTING | implementation | not-an-iteration | goal.md | msg | continue | "
            "2026-07-09T03:15:00Z |\n".format(rid=iteration_request)
        )

    result = _doctor(loop)
    by_code = {}
    for warning in result["warnings"]:
        by_code.setdefault(warning["code"], []).append(warning["message"])

    for code in ("malformed_run_log", "malformed_policy", "malformed_iteration"):
        if code not in by_code:
            _fail("G26 C7: doctor did not emit {0}".format(code))
        if any(issue["code"] == code for issue in result["issues"]):
            _fail("G26 C7: {0} must remain WARNING-only".format(code))

    run_messages = "\n".join(by_code["malformed_run_log"])
    if "loop-run-log.md" not in run_messages or "line" not in run_messages.lower():
        _fail("G26 C7: run-log warnings must name the file and line")
    if run_request not in run_messages:
        _fail("G26 C7: malformed run-log warning must name the request when present")
    if "timestamp" not in run_messages or "request_id" not in run_messages or "to_status" not in run_messages:
        _fail("G26 C7: run-log warnings must distinguish bad timestamp/request/status cells")

    policy_messages = "\n".join(by_code["malformed_policy"])
    if "loop-policy.md" not in policy_messages or "line" not in policy_messages.lower():
        _fail("G26 C7: malformed policy warning must name the file and line")
    if result.get("max_fix_cycles") != doctor.DEFAULT_MAX_FIX_CYCLES:
        _fail("G26 C7: malformed policy must retain the existing default fallback")

    iteration_messages = "\n".join(by_code["malformed_iteration"])
    if iteration_request not in iteration_messages or "requests.md" not in iteration_messages:
        _fail("G26 C7: malformed iteration warning must name requests.md and request_id")
    iteration_summary = next(
        item for item in result["requests"]["non_terminal"]
        if item["request_id"] == iteration_request
    )
    if iteration_summary.get("fix_cycles") != 0:
        _fail("G26 C7: malformed iteration must retain the existing zero fallback")

    clean_loop = tmp_path / "g26_c7_clean_inputs"
    _bootstrap(clean_loop)
    clean = _doctor(clean_loop)
    if any(
        warning["code"] in {"malformed_run_log", "malformed_policy", "malformed_iteration"}
        for warning in clean["warnings"]
    ):
        _fail("G26 C7 negative: clean control-plane files must not emit malformed warnings")


def _check_g26_b2_c4(tmp_path: Path) -> None:
    """G26 B2/C4: zero real evidence never becomes doctor gate-green.

    A pristine loop with no requests is healthy. Once a request reaches
    IMPLEMENTATION_DONE, however, a populated last_message cell is only a
    message pointer: it must not count as evidence or make the completion gate
    pass. Adding one real passing evidence record is the positive control.
    """
    loop = tmp_path / "g26_b2_c4"
    _bootstrap(loop)

    fresh = _doctor(loop)
    if fresh["evidence_recorded_ok"] is not True:
        _fail("G26 B2: a fresh loop with no requests must keep evidence_recorded_ok=True")
    if fresh["completion_gate_ok"] is not True:
        _fail("G26 B2: a fresh loop with no requests must keep completion_gate_ok=True")

    request_id = "REQ-20260709-010001-implementation"
    requests_path = loop / "requests.md"
    _append_request_row(
        requests_path,
        request_id,
        "implementation",
        "messages/REQ-20260709-010001-implementation/IMPLEMENTATION_DONE-iter-1.md",
    )
    _set_request_status(requests_path, request_id, "IMPLEMENTATION_DONE")

    empty = _doctor(loop)
    request = next(item for item in empty["requests"]["non_terminal"] if item["request_id"] == request_id)
    if request["has_evidence"] is not False:
        _fail("G26 C4: last_message must not make a zero-record request report has_evidence=True")
    if empty["evidence_recorded_ok"] is not False:
        _fail("G26 B2: a DONE+ request with zero records must set evidence_recorded_ok=False")
    if empty["completion_gate_ok"] is not False:
        _fail("G26 B2: a DONE+ request with zero records must set completion_gate_ok=False")
    if not any(
        warning["code"] == "missing_evidence" and request_id in warning["message"]
        for warning in empty["warnings"]
    ):
        _fail("G26 B2: the zero-record DONE+ request must emit missing_evidence")

    _write_evidence(loop / "evidence", request_id, 1, "python -m unittest", 0)
    recorded = _doctor(loop)
    request = next(item for item in recorded["requests"]["non_terminal"] if item["request_id"] == request_id)
    if request["has_evidence"] is not True:
        _fail("G26 B2 positive: a real JSON record must make has_evidence=True")
    if recorded["evidence_recorded_ok"] is not True:
        _fail("G26 B2 positive: a real JSON record must make evidence_recorded_ok=True")
    if recorded["completion_gate_ok"] is not True:
        _fail("G26 B2 positive: one valid passing record must make completion_gate_ok=True")


def _check_g26_b1_c15(tmp_path: Path) -> None:
    """G26 B1/C15: manifest coverage and evidence fields fail honestly."""
    import json

    loop = tmp_path / "g26_b1_c15"
    _bootstrap(loop)
    request_id = "REQ-20260709-010002-implementation"
    requests_path = loop / "requests.md"
    _append_request_row(requests_path, request_id, "implementation", "implementation done")
    _set_request_status(requests_path, request_id, "IMPLEMENTATION_DONE")

    message_dir = loop / "messages" / request_id
    message_dir.mkdir(parents=True, exist_ok=True)
    (message_dir / "IMPLEMENTATION_REQUEST-iter-1.md").write_text(
        "# IMPLEMENTATION_REQUEST\n\n"
        "message_type: IMPLEMENTATION_REQUEST\n"
        "request_id: {0}\n"
        "acceptance_criteria:\n"
        "- Core passes. VERIFY `python -m unittest tests.core`\n"
        "- UI passes. VERIFY `python -m unittest tests.ui`\n"
        "- Smoke passes. VERIFY `python scripts/smoke.py --local`\n".format(request_id),
        encoding="utf-8",
    )
    evidence_dir = loop / "evidence"
    _write_evidence(evidence_dir, request_id, 1, "python -m unittest tests.core", 0)
    _write_evidence(evidence_dir, request_id, 1, "python -m unittest tests.ui", 0)

    partial = _doctor(loop)
    gaps = partial.get("evidence_manifest_gaps") or []
    match = [gap for gap in gaps if gap.get("request_id") == request_id]
    if not match:
        _fail("G26 B1: partial VERIFY coverage must expose evidence_manifest_gaps")
    if match[0].get("missing_commands") != ["python scripts/smoke.py --local"]:
        _fail("G26 B1: the manifest gap must name the uncovered VERIFY command")
    if match[0].get("expected_count") != 3 or match[0].get("evidence_count") != 2:
        _fail("G26 B1: manifest gap counts must report expected=3 and evidence=2")
    if not any(
        warning["code"] == "evidence_manifest_gap" and request_id in warning["message"]
        for warning in partial["warnings"]
    ):
        _fail("G26 B1: partial coverage must emit evidence_manifest_gap")

    _write_evidence(evidence_dir, request_id, 1, "python scripts/smoke.py --local", 0)
    covered = _doctor(loop)
    if any(gap.get("request_id") == request_id for gap in covered.get("evidence_manifest_gaps", [])):
        _fail("G26 B1 positive: complete VERIFY coverage must clear evidence_manifest_gap")
    if any(
        warning["code"] == "evidence_manifest_gap" and request_id in warning["message"]
        for warning in covered["warnings"]
    ):
        _fail("G26 B1 positive: complete VERIFY coverage must not warn")

    # C15 negative: required keys with empty string values are malformed, and
    # an invalid ran_at is not a timestamp. Both used to pass key-presence-only
    # loading when exit_code was zero.
    malformed_dir = tmp_path / "g26_c15_evidence"
    malformed_dir.mkdir()
    empty_path = malformed_dir / "empty.json"
    empty_path.write_text(
        json.dumps(
            {
                "request_id": "",
                "checkpoint": "",
                "command": "",
                "exit_code": 0,
                "ran_at": "",
            }
        ),
        encoding="utf-8",
    )
    invalid_time_path = malformed_dir / "invalid-time.json"
    invalid_time_path.write_text(
        json.dumps(
            {
                "request_id": "REQ-20260709-010003-implementation",
                "checkpoint": "smoke",
                "command": "python -m unittest",
                "exit_code": 0,
                "ran_at": "not-a-timestamp",
            }
        ),
        encoding="utf-8",
    )
    malformed_records, malformed_errors = completion_gate.load_evidence(malformed_dir)
    malformed = completion_gate.evaluate(malformed_records, malformed_errors, None)
    if malformed["ok"] is not False:
        _fail("G26 C15: empty string fields and invalid ran_at must fail the unscoped gate")
    reasons = " ".join(error.get("reason", "") for error in malformed_errors)
    if "empty" not in reasons or "ran_at" not in reasons or "timestamp" not in reasons:
        _fail("G26 C15: load errors must clearly name empty fields and invalid ran_at timestamp")

    empty_path.unlink()
    invalid_time_path.unlink()
    valid_id = "REQ-20260709-010004-implementation"
    _write_evidence(malformed_dir, valid_id, 1, "python -m unittest", 0)
    valid_records, valid_errors = completion_gate.load_evidence(malformed_dir)
    valid = completion_gate.evaluate(valid_records, valid_errors, valid_id)
    if valid["ok"] is not True:
        _fail("G26 C15 positive: a complete non-empty record with a valid timestamp must pass")


def _check_g26_b3(tmp_path: Path) -> None:
    """G26 B3: auto-chain requires an available green gate and full manifest."""
    import json

    loop = tmp_path / "g26_b3"
    _bootstrap(loop)
    (loop / "tracker.md").write_text("# Tracker\n\n- [ ] Continue the verified checkpoint.\n", encoding="utf-8")
    with (loop / "handoff.md").open("a", encoding="utf-8") as handle:
        handle.write("\nauto_chain_next_session: true\n")

    request_id = "REQ-20260709-010005-implementation"
    requests_path = loop / "requests.md"
    _append_request_row(requests_path, request_id, "implementation", "implementation done")
    _set_request_status(requests_path, request_id, "IMPLEMENTATION_DONE")
    message_dir = loop / "messages" / request_id
    message_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = message_dir / "IMPLEMENTATION_REQUEST-iter-1.md"
    manifest_path.write_text(
        "# IMPLEMENTATION_REQUEST\n\n"
        "message_type: IMPLEMENTATION_REQUEST\n"
        "request_id: {0}\n"
        "acceptance_criteria:\n"
        "- Tests pass. VERIFY `python -m unittest`\n".format(request_id),
        encoding="utf-8",
    )
    evidence_dir = loop / "evidence"
    _write_evidence(evidence_dir, request_id, 1, "python -m unittest", 0)
    evidence_path = next(evidence_dir.glob("{0}-*.json".format(request_id)))

    ready = _doctor(loop)
    if ready["auto_chain_ready"] is not True:
        _fail("G26 B3 positive: available green gate plus complete manifest must allow auto-chain")

    failing_record = json.loads(evidence_path.read_text(encoding="utf-8"))
    failing_record["exit_code"] = 1
    evidence_path.write_text(json.dumps(failing_record), encoding="utf-8")
    failed = _doctor(loop)
    if failed["completion_gate_ok"] is not False:
        _fail("G26 B3 setup: non-zero evidence must make completion_gate_ok=False")
    if failed["auto_chain_ready"] is not False:
        _fail("G26 B3: auto-chain must stop when completion_gate_ok=False")
    if "completion gate did not pass" not in failed["readiness_reasons"]:
        _fail("G26 B3: readiness reasons must name the failed completion gate")

    failing_record["exit_code"] = 0
    evidence_path.write_text(json.dumps(failing_record), encoding="utf-8")
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        + "- Smoke passes. VERIFY `python scripts/smoke.py`\n",
        encoding="utf-8",
    )
    manifest_gap = _doctor(loop)
    if manifest_gap["completion_gate_ok"] is not True:
        _fail("G26 B3 setup: passing recorded evidence must keep completion_gate_ok=True")
    if not manifest_gap.get("evidence_manifest_gaps"):
        _fail("G26 B3 setup: the uncovered second VERIFY command must create a manifest gap")
    if manifest_gap["auto_chain_ready"] is not False:
        _fail("G26 B3: auto-chain must stop on an evidence-manifest gap")
    if "evidence manifest is incomplete" not in manifest_gap["readiness_reasons"]:
        _fail("G26 B3: readiness reasons must name incomplete evidence manifest coverage")

    _write_evidence(evidence_dir, request_id, 1, "python scripts/smoke.py", 0)
    covered = _doctor(loop)
    if covered["auto_chain_ready"] is not True:
        _fail("G26 B3 positive: filling the manifest gap must restore auto-chain readiness")

    original_gate_available = doctor.GATE_AVAILABLE
    doctor.GATE_AVAILABLE = False
    try:
        unavailable = _doctor(loop)
    finally:
        doctor.GATE_AVAILABLE = original_gate_available
    if unavailable["gate_available"] is not False:
        _fail("G26 B3 setup: the unavailable-gate probe must report gate_available=False")
    if unavailable["auto_chain_ready"] is not False:
        _fail("G26 B3: auto-chain must stop when the completion gate is unavailable")
    if "completion gate is unavailable" not in unavailable["readiness_reasons"]:
        _fail("G26 B3: readiness reasons must name gate unavailability")


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

        # G31: BLOCKED is a human-gate pause. Preserve G7's paused-loop hygiene
        # check even though BLOCKED is no longer a terminal request status.
        _set_request_status(c_requests, "REQ-20260707-092222-data-eng", "BLOCKED")
        blocked_probe = doctor.summarize(
            loop_c, stale_heartbeat_mins=doctor.DEFAULT_STALE_HEARTBEAT_MINS
        )
        if not any(w["code"] == "uncommitted_work" for w in blocked_probe["warnings"]):
            _fail("uncommitted_work should fire while every request is terminal or BLOCKED")

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


def _check_g28(tmp_path: Path) -> None:
    """G28: defect-class closure wording and FIX_REQUEST doctor warning."""
    protocol_md = _read_doc(_PROTOCOL_MD)
    protocol_lower = protocol_md.lower()
    protocol_collapsed = " ".join(protocol_lower.split())

    # The class is a positive invariant over the full domain, never a symptom
    # list. The run-5 Decimal incident is retained only as an explicitly marked
    # example of that project-agnostic rule.
    for needle in (
        "defect_class:",
        "sibling_scan:",
        "if and only if",
        "invariant over inputs/states",
        "list of named failure symptoms",
        "catch every decimal exception",
        "a nonblank raw token is valid if and only if its whole token matches the frozen decimal grammar",
    ):
        if needle not in protocol_collapsed:
            _fail("G28 protocol wording is missing: {0!r}".format(needle))
    if "illustrative run-5 example" not in protocol_collapsed:
        _fail("G28 run-5 Decimal details must be marked as an illustrative example")

    # The sibling enumeration is explicitly three-axis and project-agnostic.
    for needle in (
        "input domain defined positively",
        "every public entry point",
        "writer",
        "alias",
        "boundary and extreme values",
        "simultaneous worst cases",
        "unenumerated write door",
        "unenumerated extreme values",
    ):
        if needle not in protocol_collapsed:
            _fail("G28 three-axis sibling scan wording is missing: {0!r}".format(needle))

    for needle in (
        "defect class closure",
        "implementation-independent",
        "red-capable enumeration",
        "sha-256",
        "one centralized rule",
        "scattered operation-specific patches do not satisfy this request.",
        "second blocker with the same `defect_class`",
        "without waiting for cap exhaustion",
        "10-21h",
        "16-25 min",
    ):
        if needle not in protocol_collapsed:
            _fail("G28 class-closure/anti-thrash wording is missing: {0!r}".format(needle))

    # Doctor positive/negative: archived FIX_REQUEST messages missing the flat
    # defect_class line warn; adding that line clears only this warning.
    loop = tmp_path / "g28_loop"
    _bootstrap(loop)
    request_id = "REQ-20260715-100000-implementation"
    msg_dir = loop / "messages" / request_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    fix_path = msg_dir / "FIX_REQUEST-iter-2.md"
    fix_path.write_text(
        "# FIX_REQUEST\n\n"
        "message_type: FIX_REQUEST\n"
        "request_id: {0}\n"
        "severity: blocker\n"
        "sibling_scan:\n"
        "- all known same-class paths\n".format(request_id),
        encoding="utf-8",
    )
    missing = _doctor(loop)
    missing_warnings = [
        warning for warning in missing["warnings"]
        if warning["code"] == "fix_request_class_missing"
    ]
    if not missing_warnings:
        _fail("G28 doctor must warn fix_request_class_missing when defect_class: is absent")
    if not any("FIX_REQUEST-iter-2.md" in warning["message"] for warning in missing_warnings):
        _fail("G28 fix_request_class_missing must name the message file")
    if any(issue["code"] == "fix_request_class_missing" for issue in missing["issues"]):
        _fail("G28 fix_request_class_missing must be warning-only")

    fix_path.write_text(
        fix_path.read_text(encoding="utf-8")
        + "defect_class: output is valid if and only if it satisfies the declared contract\n",
        encoding="utf-8",
    )
    present = _doctor(loop)
    if any(warning["code"] == "fix_request_class_missing" for warning in present["warnings"]):
        _fail("G28 doctor must stay silent when a defect_class: line exists")


def _check_g31(tmp_path: Path) -> None:
    """G31: BLOCKED pause lifecycle and authorized-exit warning."""
    docs = {
        "SKILL.md": _read_doc(_SKILL_MD),
        "protocol.md": _read_doc(_PROTOCOL_MD),
        "loop-state.md": _read_doc(_LOOP_STATE_MD),
    }
    for name, text in docs.items():
        collapsed = " ".join(text.lower().split())
        for needle in (
            "blocked is a human-gate pause",
            "not a terminal",
            "blocked -> fix_requested",
            "recorded human authorization",
            "human_authorization: approved",
            "accepted is the success terminal",
            "abandoned",
            "blocked -> abandoned",
            "rows keep their evidence",
        ):
            if needle not in collapsed:
                _fail("G31 {0} lifecycle wording is missing: {1!r}".format(name, needle))
        if "exactly one legal" not in collapsed:
            _fail("G31 {0} must state BLOCKED has exactly one legal resume edge".format(name))

    # The generated requests.md template must carry the same lifecycle, not the
    # old contradictory statement that BLOCKED is terminal.
    loop_template = tmp_path / "g31_template"
    _bootstrap(loop_template)
    requests_text = (loop_template / "requests.md").read_text(encoding="utf-8")
    requests_collapsed = " ".join(requests_text.lower().split())
    if "terminal states are accepted and blocked" in requests_collapsed:
        _fail("G31 generated requests.md must not call BLOCKED terminal")
    for needle in (
        "blocked is a human-gate pause",
        "blocked -> fix_requested",
        "human_authorization: approved",
        "accepted is the success terminal",
        "abandoned",
        "rows keep their evidence",
    ):
        if needle not in requests_collapsed:
            _fail("G31 generated requests.md wording is missing: {0!r}".format(needle))

    def _write_log(loop_dir: Path, rows) -> None:
        text = (
            "# Loop Run Log\n\n"
            "| timestamp | request_id | iteration | from_status | to_status | lane | note |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
        )
        text += "".join("| " + " | ".join(row) + " |\n" for row in rows)
        (loop_dir / "loop-run-log.md").write_text(text, encoding="utf-8")

    request_id = "REQ-20260715-110000-implementation"
    unauthorized_loop = tmp_path / "g31_unauthorized"
    _bootstrap(unauthorized_loop)
    _write_log(
        unauthorized_loop,
        [
            ("2026-07-15T11:01:00Z", request_id, "2", "BLOCKED", "FIX_REQUESTED", "product", "retry dispatched"),
        ],
    )
    unauthorized = _doctor(unauthorized_loop)
    unauthorized_warnings = [
        warning for warning in unauthorized["warnings"]
        if warning["code"] == "blocked_exit_unauthorized"
    ]
    if not unauthorized_warnings:
        _fail("G31 doctor must warn on BLOCKED -> FIX_REQUESTED without preceding human authorization")
    if not any(request_id in warning["message"] for warning in unauthorized_warnings):
        _fail("G31 blocked_exit_unauthorized must name the request_id")
    if any(issue["code"] == "blocked_exit_unauthorized" for issue in unauthorized["issues"]):
        _fail("G31 blocked_exit_unauthorized must be warning-only")

    for suffix, note in (
        ("denied", "human authorization denied"),
        ("pending", "human approval needed"),
        ("awaiting", "awaiting human authorization: resume"),
        ("denied_suffix", "human authorization: resume denied"),
        ("question", "human authorized? no"),
        ("false", "human approved: false"),
        ("record_pending", "human authorization recorded: pending"),
    ):
        negative_loop = tmp_path / ("g31_authorization_" + suffix)
        _bootstrap(negative_loop)
        _write_log(
            negative_loop,
            [
                (
                    "2026-07-15T11:01:00Z",
                    request_id,
                    "2",
                    "BLOCKED",
                    "FIX_REQUESTED",
                    "product",
                    note,
                ),
            ],
        )
        negative = _doctor(negative_loop)
        if not any(
            warning["code"] == "blocked_exit_unauthorized"
            for warning in negative["warnings"]
        ):
            _fail(
                "G31 doctor must not treat an ungranted human-gate note as authorization: "
                + note
            )

    authorized_loop = tmp_path / "g31_authorized"
    _bootstrap(authorized_loop)
    _write_log(
        authorized_loop,
        [
            ("2026-07-15T11:00:00Z", request_id, "2", "BLOCKED", "BLOCKED", "product", "human_authorization: approved | resume this request"),
            ("2026-07-15T11:01:00Z", request_id, "2", "BLOCKED", "FIX_REQUESTED", "product", "authorized retry dispatched"),
        ],
    )
    authorized = _doctor(authorized_loop)
    if any(warning["code"] == "blocked_exit_unauthorized" for warning in authorized["warnings"]):
        _fail("G31 doctor must stay silent after a preceding BLOCKED -> BLOCKED authorization row")

    same_row_loop = tmp_path / "g31_same_row_authorized"
    _bootstrap(same_row_loop)
    _write_log(
        same_row_loop,
        [
            ("2026-07-15T11:01:00Z", request_id, "2", "BLOCKED", "FIX_REQUESTED", "product", "human_authorization: approved | resume and dispatch"),
        ],
    )
    same_row = _doctor(same_row_loop)
    if any(warning["code"] == "blocked_exit_unauthorized" for warning in same_row["warnings"]):
        _fail("G31 doctor must allow a BLOCKED exit whose own note records human authorization")


def _check_g28_order_trust_closure(tmp_path: Path) -> None:
    """G28 closure: malformed run-log time never grants or consumes trust."""
    header = (
        "# Loop Run Log\n\n"
        "| timestamp | request_id | iteration | from_status | to_status | lane | note |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
    )

    def _write_log(loop_dir: Path, rows) -> None:
        text = header + "".join("| " + " | ".join(row) + " |\n" for row in rows)
        (loop_dir / "loop-run-log.md").write_text(text, encoding="utf-8")

    request_id = "REQ-20260716-090000-implementation"

    # A blank-timestamp approval look-alike is not a preceding grant. The
    # unauthorized, validly timestamped exit must remain visible, and the
    # malformed-row warning must explain why the apparent grant was skipped.
    phantom_loop = tmp_path / "g28_phantom_authorization"
    _bootstrap(phantom_loop)
    _write_log(
        phantom_loop,
        [
            ("", request_id, "2", "BLOCKED", "BLOCKED", "product", "human_authorization: approved | appended late"),
            ("2026-07-16T09:01:00Z", request_id, "2", "BLOCKED", "FIX_REQUESTED", "product", "retry dispatched"),
        ],
    )
    phantom = _doctor(phantom_loop)
    if not any(w["code"] == "blocked_exit_unauthorized" for w in phantom["warnings"]):
        _fail("G28 blank-timestamp authorization look-alike must not authorize a BLOCKED exit")
    malformed_authorization = [
        w for w in phantom["warnings"]
        if w["code"] == "malformed_run_log" and request_id in w["message"]
    ]
    if not any(
        "authorization" in w["message"].lower() and "skipped" in w["message"].lower()
        for w in malformed_authorization
    ):
        _fail("G28 malformed authorization look-alike warning must say it was skipped")

    # The valid form remains authorizing.
    valid_loop = tmp_path / "g28_valid_authorization"
    _bootstrap(valid_loop)
    _write_log(
        valid_loop,
        [
            ("2026-07-16T09:00:00Z", request_id, "2", "BLOCKED", "BLOCKED", "product", "human_authorization: approved | resume"),
            ("2026-07-16T09:01:00Z", request_id, "2", "BLOCKED", "FIX_REQUESTED", "product", "retry dispatched"),
        ],
    )
    valid = _doctor(valid_loop)
    if any(w["code"] == "blocked_exit_unauthorized" for w in valid["warnings"]):
        _fail("G28 valid timestamped authorization must still authorize the later BLOCKED exit")

    # A malformed exit cannot consume a valid one-shot grant. The later valid
    # exit uses it, while the malformed row is covered only by malformed_run_log.
    nonconsuming_loop = tmp_path / "g28_malformed_exit_nonconsuming"
    _bootstrap(nonconsuming_loop)
    _write_log(
        nonconsuming_loop,
        [
            ("2026-07-16T09:00:00Z", request_id, "2", "BLOCKED", "BLOCKED", "product", "human_authorization: approved | resume"),
            ("", request_id, "2", "BLOCKED", "FIX_REQUESTED", "product", "malformed retry row"),
            ("2026-07-16T09:01:00Z", request_id, "2", "BLOCKED", "FIX_REQUESTED", "product", "valid retry dispatched"),
        ],
    )
    nonconsuming = _doctor(nonconsuming_loop)
    if any(w["code"] == "blocked_exit_unauthorized" for w in nonconsuming["warnings"]):
        _fail("G28 malformed BLOCKED exit must not consume a valid authorization grant")

    print(
        "PROBE_G28_BLOCKED blank_auth=warn+skipped valid_auth=silent "
        "blank_exit=nonconsuming"
    )

    # A malformed hold cannot suppress stalled_handoff. A valid hold still can.
    hold_request = "REQ-20260716-091000-frontend"
    hold_loop = tmp_path / "g28_human_qa_hold"
    _bootstrap(hold_loop)
    _append_request_row(hold_loop / "requests.md", hold_request, "product", "awaiting human QA")
    _set_request_status(hold_loop / "requests.md", hold_request, "REVIEWING")
    review_dir = hold_loop / "messages" / hold_request
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "REVIEW_DONE-iter-1.md").write_text(
        "# REVIEW_DONE\n\npass\n", encoding="utf-8"
    )
    blank_hold = ("", hold_request, "1", "REVIEWING", "REVIEWING", "product", "human_qa_hold: blank timestamp")
    _write_log(hold_loop, [blank_hold])
    malformed_hold = _doctor(hold_loop)
    if hold_request in malformed_hold.get("held_for_human_qa", []):
        _fail("G28 malformed human-QA hold must not enter held_for_human_qa")
    if not any(
        w["code"] == "stalled_handoff" and hold_request in w["message"]
        for w in malformed_hold["warnings"]
    ):
        _fail("G28 malformed human-QA hold must not silence stalled_handoff")

    valid_hold = (
        "2026-07-16T09:10:00Z", hold_request, "1", "REVIEWING", "REVIEWING",
        "product", "human_qa_hold: valid timestamp",
    )
    _write_log(hold_loop, [valid_hold])
    timestamped_hold = _doctor(hold_loop)
    if hold_request not in timestamped_hold.get("held_for_human_qa", []):
        _fail("G28 valid timestamped human-QA hold must remain effective")
    if any(
        w["code"] == "stalled_handoff" and hold_request in w["message"]
        for w in timestamped_hold["warnings"]
    ):
        _fail("G28 valid human-QA hold must still suppress stalled_handoff")

    # A malformed hold must not consume the one durable human message that
    # belongs to a valid hold round. Removing that message must expose the valid
    # round as missing, proving the valid row was not skipped with the malformed.
    message_loop = tmp_path / "g28_human_qa_message_consumption"
    _bootstrap(message_loop)
    message_request = "REQ-20260716-092000-frontend"
    _write_log(
        message_loop,
        [
            ("", message_request, "1", "REVIEWING", "REVIEWING", "product", "human_qa_hold: malformed duplicate"),
            ("2026-07-16T09:20:00Z", message_request, "1", "REVIEWING", "REVIEWING", "product", "human_qa_hold: valid round"),
        ],
    )
    human_dir = message_loop / "messages" / message_request
    human_dir.mkdir(parents=True, exist_ok=True)
    human_message = human_dir / "HUMAN_QA_REQUEST-iter-1.md"
    human_message.write_text(
        "# HUMAN_QA_REQUEST\n\niteration: 1\nto_lane: human\n",
        encoding="utf-8",
    )
    durable = _doctor(message_loop)
    if any(
        w["code"] == "human_qa_message_missing" and message_request in w["message"]
        for w in durable["warnings"]
    ):
        _fail("G28 malformed hold must not consume the valid round's human-QA message")
    human_message.unlink()
    missing = _doctor(message_loop)
    if not any(
        w["code"] == "human_qa_message_missing" and message_request in w["message"]
        for w in missing["warnings"]
    ):
        _fail("G28 valid timestamped hold without a human-QA message must still warn")

    print(
        "PROBE_G28_HUMAN_QA blank_hold=stall valid_hold=held "
        "blank_hold_message=nonconsuming valid_hold_missing=warn"
    )


def _check_g35(tmp_path: Path) -> None:
    """G35: lifecycle markers, authority, holds, and override history."""
    protocol_md = _read_doc(_PROTOCOL_MD)
    loop_state_md = _read_doc(_LOOP_STATE_MD)
    skill_md = _read_doc(_SKILL_MD)
    protocol_collapsed = " ".join(protocol_md.lower().split())
    loop_state_collapsed = " ".join(loop_state_md.lower().split())
    skill_collapsed = " ".join(skill_md.lower().split())

    for needle in (
        "run_complete",
        "| <ts> | - | - | run_complete | run_complete | product |",
        "every request is terminal",
        "done-when holds",
        "two-phase cap raises",
        "blocked -> blocked",
        "separate later checkpoint",
        "cap_authorization:",
        "the lane that performed the transition",
        "acting lane, not the new owner",
        "human-gate transitions are always recorded by product",
        "fix_requested may be owned by review",
        "pre_implementation_test_request",
        "implementation-independent red-capable enumeration",
        "human_qa_hold:",
        "note starts with",
        "human_qa_requested:",
    ):
        if needle not in protocol_collapsed:
            _fail("G35 protocol wording is missing: {0!r}".format(needle))

    authority_sentence = "loop-run-log.md is the authoritative transition history"
    snapshot_sentence = "requests.md is a coarse current-state snapshot at checkpoint granularity"
    for name, collapsed in (
        ("protocol.md", protocol_collapsed),
        ("loop-state.md", loop_state_collapsed),
    ):
        for needle in (
            authority_sentence,
            snapshot_sentence,
            "planned",
            "implementation_done",
            "may never appear",
            "that is legal",
        ):
            if needle not in collapsed:
                _fail("G35 {0} authority wording is missing: {1!r}".format(name, needle))
    if "pre_implementation_test_request" not in skill_collapsed:
        _fail("G35 SKILL.md message vocabulary must include PRE_IMPLEMENTATION_TEST_REQUEST")

    loop = tmp_path / "g35_loop"
    _bootstrap(loop)
    requests_text = (loop / "requests.md").read_text(encoding="utf-8")
    requests_collapsed = " ".join(requests_text.lower().split())
    for needle in (authority_sentence, snapshot_sentence, "may never appear", "that is legal"):
        if needle not in requests_collapsed:
            _fail("G35 generated requests.md authority wording is missing: {0!r}".format(needle))

    policy_text = (loop / "loop-policy.md").read_text(encoding="utf-8")
    policy_collapsed = " ".join(policy_text.lower().split())
    for needle in (
        "## overrides",
        "append-only",
        "active -> superseded/completed",
        "never rewritten in place",
        "override: max_fix_cycles",
        "status: active",
    ):
        if needle not in policy_collapsed:
            _fail("G35 generated loop-policy.md Overrides wording is missing: {0!r}".format(needle))

    run_log_text = (loop / "loop-run-log.md").read_text(encoding="utf-8")
    run_log_collapsed = " ".join(run_log_text.lower().split())
    for needle in (
        "lane that performed the transition",
        "acting lane, not the new owner",
        "human-gate transitions are always recorded by product",
    ):
        if needle not in run_log_collapsed:
            _fail("G35 generated loop-run-log.md wording is missing: {0!r}".format(needle))

    # RUN_COMPLETE is a parseable non-request marker with an exact row shape.
    with (loop / "loop-run-log.md").open("a", encoding="utf-8") as handle:
        handle.write(
            "| 2026-07-15T12:00:00Z | - | - | RUN_COMPLETE | RUN_COMPLETE | product | "
            "every request terminal; goal Done-When holds |\n"
        )
    run_snapshot = doctor.load_run_log_snapshot(loop)
    run_complete_rows = [
        row for row in run_snapshot["rows"]
        if row.get("to_status") == "RUN_COMPLETE"
    ]
    if len(run_complete_rows) != 1:
        _fail("G35 RUN_COMPLETE row must parse exactly once")
    marker = run_complete_rows[0]
    expected_shape = {
        "request_id": "-",
        "iteration": "-",
        "from_status": "RUN_COMPLETE",
        "to_status": "RUN_COMPLETE",
        "lane": "product",
    }
    for key, expected in expected_shape.items():
        if marker.get(key) != expected:
            _fail("G35 RUN_COMPLETE row has wrong {0}: {1!r}".format(key, marker.get(key)))
    if run_snapshot["warnings"]:
        _fail("G35 RUN_COMPLETE row must parse without malformed_run_log warnings: {0!r}".format(
            run_snapshot["warnings"]
        ))

    # The new greppable hold marker suppresses stalls; the legacy prose marker
    # remains covered by _check_g3_doctor and therefore stays additive.
    hold_request = "REQ-20260715-120100-frontend"
    _append_request_row(loop / "requests.md", hold_request, "product", "awaiting human QA")
    _set_request_status(loop / "requests.md", hold_request, "REVIEWING")
    review_dir = loop / "messages" / hold_request
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "REVIEW_DONE-iter-1.md").write_text("# REVIEW_DONE\n\npass\n", encoding="utf-8")
    _append_run_log_row(
        loop, hold_request, "REVIEWING", "REVIEWING", "product",
        "human_qa_hold: operator must judge the interaction",
    )
    held = _doctor(loop)
    if hold_request not in held.get("held_for_human_qa", []):
        _fail("G35 doctor must recognize a note starting human_qa_hold:")
    if any(w["code"] == "stalled_handoff" and hold_request in w["message"] for w in held["warnings"]):
        _fail("G35 human_qa_hold: must suppress stalled_handoff")

    _append_run_log_row(
        loop, hold_request, "REVIEWING", "REVIEWING", "product",
        "human_qa: confirmed by operator",
    )
    released = _doctor(loop)
    if hold_request in released.get("held_for_human_qa", []):
        _fail("G35 human-QA confirmation must release the current hold")
    _append_run_log_row(
        loop, hold_request, "REVIEWING", "REVIEWING", "product",
        "human_qa_hold: a later round needs a new operator judgment",
    )
    held_again = _doctor(loop)
    if hold_request not in held_again.get("held_for_human_qa", []):
        _fail("G35 a later human_qa_hold: must take effect after an earlier confirmation")

    # override_history_gap both ways: non-default cap without an Active record
    # warns; appending the Active override line clears the warning.
    policy_path = loop / "loop-policy.md"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace("max_fix_cycles: 3", "max_fix_cycles: 5"),
        encoding="utf-8",
    )
    gap = _doctor(loop)
    gap_warnings = [w for w in gap["warnings"] if w["code"] == "override_history_gap"]
    if not gap_warnings:
        _fail("G35 doctor must warn override_history_gap for a non-default cap without Active override")
    if any(issue["code"] == "override_history_gap" for issue in gap["issues"]):
        _fail("G35 override_history_gap must be warning-only")
    with policy_path.open("a", encoding="utf-8") as handle:
        handle.write(
            "- override: max_fix_cycles | value: 5 | status: Active | "
            "authorized_at: 2026-07-15T12:05:00Z | decision: human authorization recorded\n"
        )
    active = _doctor(loop)
    if any(w["code"] == "override_history_gap" for w in active["warnings"]):
        _fail("G35 doctor must stay silent when a non-default cap has an Active override line")

    # A resumed-round FIX_REQUEST carries cap_authorization. The doctor reports
    # a missing line and clears once the flat field is added.
    resume_request = "REQ-20260715-120200-implementation"
    _append_run_log_row(
        loop, resume_request, "BLOCKED", "BLOCKED", "product",
        "human_authorization: approved | raise cap for this request",
    )
    _append_run_log_row(
        loop, resume_request, "BLOCKED", "FIX_REQUESTED", "product",
        "authorized resumed round",
    )
    fix_dir = loop / "messages" / resume_request
    fix_dir.mkdir(parents=True, exist_ok=True)
    resumed_fix = fix_dir / "FIX_REQUEST-iter-1.md"
    resumed_fix.write_text(
        "# FIX_REQUEST\n\n"
        "request_id: {0}\n"
        "iteration: 1\n"
        "defect_class: an outcome is valid if and only if the contract holds\n"
        "sibling_scan:\n- complete domain\n".format(resume_request),
        encoding="utf-8",
    )
    cap_gap = _doctor(loop)
    if not any(w["code"] == "fix_request_cap_authorization_missing" for w in cap_gap["warnings"]):
        _fail("G35 doctor must warn when a resumed-round FIX_REQUEST lacks cap_authorization:")
    resumed_fix.write_text(
        resumed_fix.read_text(encoding="utf-8")
        + "cap_authorization: loop-run-log.md 2026-07-15T10:00:00Z\n",
        encoding="utf-8",
    )
    cap_present = _doctor(loop)
    if any(w["code"] == "fix_request_cap_authorization_missing" for w in cap_present["warnings"]):
        _fail("G35 doctor must stay silent when resumed FIX_REQUEST has cap_authorization:")


def _check_g32(tmp_path: Path) -> None:
    """G32: evidence mirrors, scoped gate records, freeze hashes, schema widening."""
    protocol_md = _read_doc(_PROTOCOL_MD)
    loop_state_md = _read_doc(_LOOP_STATE_MD)
    gate_source = Path(completion_gate.__file__).read_text(encoding="utf-8")
    protocol_collapsed = " ".join(protocol_md.lower().split())
    loop_state_collapsed = " ".join(loop_state_md.lower().split())
    gate_collapsed = " ".join(gate_source.lower().split())

    for name, collapsed in (
        ("protocol.md", protocol_collapsed),
        ("loop-state.md", loop_state_collapsed),
    ):
        for needle in (
            "docs/loop/lanes/<lane>/evidence/",
            "docs/loop/evidence/",
            "byte-for-byte",
            "from request 1",
        ):
            if needle not in collapsed:
                _fail("G32 {0} dual-write wording is missing: {1!r}".format(name, needle))

    pinned_gate = (
        "python <skill_dir>/scripts/completion_gate.py --loop-dir docs/loop "
        "--request-id <request_id>"
    )
    if pinned_gate not in protocol_collapsed:
        _fail("G32 protocol.md must pin the request-scoped completion-gate invocation")
    for needle in (
        "frozen-artifact ritual",
        "both the review document",
        "review-lane evidence json",
        "at freeze time",
        "re-hashes",
        "before pass",
    ):
        if needle not in protocol_collapsed:
            _fail("G32 frozen-artifact wording is missing: {0!r}".format(needle))
    for needle in ("started_at", "finished_at", "result", "optional"):
        if needle not in protocol_collapsed or needle not in gate_collapsed:
            _fail("G32 additive evidence schema wording is missing: {0!r}".format(needle))
    if "all five fields are required" not in protocol_collapsed:
        _fail("G32 must keep all five existing evidence fields required")
    if "records that omit" not in protocol_collapsed or "remain valid" not in protocol_collapsed:
        _fail("G32 must state old records without optional fields remain valid")

    # Old five-field records and widened records must have identical passing
    # verdicts. Optional fields are additive metadata, never new requirements.
    schema_dir = tmp_path / "g32_schema"
    schema_dir.mkdir()
    old_id = "REQ-20260715-130000-implementation"
    new_id = "REQ-20260715-130001-implementation"
    old_record = {
        "request_id": old_id,
        "checkpoint": "schema-old",
        "command": "python -m unittest",
        "exit_code": 0,
        "ran_at": "2026-07-15T13:00:00Z",
    }
    new_record = dict(old_record)
    new_record.update(
        {
            "request_id": new_id,
            "checkpoint": "schema-new",
            "started_at": "2026-07-15T13:00:00Z",
            "finished_at": "2026-07-15T13:00:02Z",
            "result": "PASS",
        }
    )
    (schema_dir / "old.json").write_text(json.dumps(old_record), encoding="utf-8")
    (schema_dir / "new.json").write_text(json.dumps(new_record), encoding="utf-8")
    schema_records, schema_errors = completion_gate.load_evidence(schema_dir)
    for request_id in (old_id, new_id):
        verdict = completion_gate.evaluate(schema_records, schema_errors, request_id)
        if not verdict["ok"]:
            _fail("G32 additive schema changed a passing fixture verdict: {0}".format(request_id))

    loop = tmp_path / "g32_loop"
    _bootstrap(loop)
    request_id = "REQ-20260715-131000-implementation"
    requests_path = loop / "requests.md"
    _append_request_row(requests_path, request_id, "product", "accepted evidence")
    _set_request_status(requests_path, request_id, "ACCEPTED")
    msg_dir = loop / "messages" / request_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    (msg_dir / "IMPLEMENTATION_DONE-iter-1.md").write_text(
        "# IMPLEMENTATION_DONE\n\n"
        "message_type: IMPLEMENTATION_DONE\n"
        "request_id: {0}\n"
        "iteration: 1\n"
        "from_lane: implementation\n"
        "to_lane: product\n".format(request_id),
        encoding="utf-8",
    )

    evidence_dir = loop / "evidence"
    _write_evidence(evidence_dir, request_id, 1, "python -m unittest", 0)
    missing = _doctor(loop)
    mirror_warnings = [w for w in missing["warnings"] if w["code"] == "evidence_mirror_gap"]
    if not mirror_warnings or not any(request_id in w["message"] for w in mirror_warnings):
        _fail("G32 doctor must warn evidence_mirror_gap when the implementing lane has no twin")
    if any(issue["code"] == "evidence_mirror_gap" for issue in missing["issues"]):
        _fail("G32 evidence_mirror_gap must be warning-only")

    lane_evidence = loop / "lanes" / "implementation" / "evidence"
    lane_evidence.mkdir()
    root_test = next(evidence_dir.glob("{0}-iter-1-python-m-unittest.json".format(request_id)))
    # A byte-identical twin with a DIFFERENT name proves matching is by content,
    # not by filename.
    lane_twin = lane_evidence / "content-addressed-copy.json"
    lane_twin.write_bytes(root_test.read_bytes())
    paired = _doctor(loop)
    if any(w["code"] == "evidence_mirror_gap" for w in paired["warnings"]):
        _fail("G32 doctor must stay silent for a byte-identical lane/root evidence pair")

    lane_twin.write_bytes(root_test.read_bytes() + b"\n")
    differing = _doctor(loop)
    if not any(w["code"] == "evidence_mirror_gap" for w in differing["warnings"]):
        _fail("G32 doctor must warn when the only lane twin differs by one byte")
    lane_twin.write_bytes(root_test.read_bytes())

    no_gate = _doctor(loop)
    if not any(w["code"] == "gate_evidence_missing" for w in no_gate["warnings"]):
        _fail("G32 doctor must warn gate_evidence_missing when ACCEPTED has no gate record")
    if any(issue["code"] == "gate_evidence_missing" for issue in no_gate["issues"]):
        _fail("G32 gate_evidence_missing must be warning-only")

    def _probe_gate_command(label: str, command: str, expect_missing: bool) -> None:
        _write_evidence(evidence_dir, request_id, 1, command, 0)
        root = next(
            path
            for path in evidence_dir.glob("*.json")
            if json.loads(path.read_text(encoding="utf-8")).get("command") == command
        )
        twin = lane_evidence / (label + "-gate-copy.json")
        twin.write_bytes(root.read_bytes())
        result = _doctor(loop)
        missing_warning = any(
            w["code"] == "gate_evidence_missing" for w in result["warnings"]
        )
        if missing_warning != expect_missing:
            _fail(
                "G32 gate command probe {0} expected gate_evidence_missing={1}, got {2}".format(
                    label, expect_missing, missing_warning
                )
            )
        if any(w["code"] == "evidence_mirror_gap" for w in result["warnings"]):
            _fail("G32 gate command probe must keep every root record mirrored: " + label)
        root.unlink()
        twin.unlink()

    unscoped = "python <skill_dir>/scripts/completion_gate.py --loop-dir docs/loop"
    wrong = (
        "python <skill_dir>/scripts/completion_gate.py --loop-dir docs/loop "
        "--request-id={0}-wrong".format(request_id)
    )
    equals_scoped = (
        "python <skill_dir>/scripts/completion_gate.py --loop-dir docs/loop "
        "--request-id={0}".format(request_id)
    )
    whitespace_scoped = (
        "python <skill_dir>/scripts/completion_gate.py --loop-dir docs/loop "
        "--request-id {0}".format(request_id)
    )
    _probe_gate_command("unscoped", unscoped, True)
    _probe_gate_command("wrong-prefix", wrong, True)
    _probe_gate_command("equals", equals_scoped, False)
    _probe_gate_command("whitespace", whitespace_scoped, False)
    print(
        "PROBE_G28_CLI unscoped=warn wrong_prefix=warn "
        "equals=silent whitespace=silent"
    )


def _check_g33(tmp_path: Path) -> None:
    """G33: durable, self-probed, human-focused QA requests."""
    protocol_md = _read_doc(_PROTOCOL_MD)
    collapsed = " ".join(protocol_md.lower().split())

    # The invariant precedes artifact-specific examples, and the fixed message
    # template carries all five required elements.
    for needle in (
        "# human_qa_request",
        "message_type: human_qa_request",
        "to_lane: human",
        "self-probe",
        "never advertise an entry point to the human that you have not just proven live yourself",
        "exact artifact or entry point being advertised",
        "first steps the human is being asked to take",
        "real evidence",
        "what only you can judge",
        "charts",
        "layout",
        "focus behavior",
        "never spend the human on machine-verifiable facts",
        "reply pass, or the first concrete problem.",
        "cache-control: no-store",
        "ctrl+f5",
        "needed_from_human:",
        "recommended_answer:",
    ):
        if needle not in collapsed:
            _fail("G33 HUMAN_QA_REQUEST wording is missing: {0!r}".format(needle))

    for needle in (
        "served-app example",
        "http status",
        "page title",
        "app-identifying endpoint",
        "listener pid",
        "second bind attempt fails",
        "cli-deliverable example",
        "run the advertised command verbatim",
        "exit code",
        "first output lines",
        "file/library-deliverable example",
        "open, validate, or import the exact advertised path",
    ):
        if needle not in collapsed:
            _fail("G33 artifact example wording is missing: {0!r}".format(needle))
    if "illustrative run-5 example" not in collapsed or "~10h" not in collapsed:
        _fail("G33 run-5 rationale must be clearly marked illustrative and retain the measured delay")
    for needle in (
        "every human-addressed message type",
        "cap raise",
        "qa request",
        "intake question",
        "one decision",
    ):
        if needle not in collapsed:
            _fail("G33 all-human-message mandate is missing: {0!r}".format(needle))

    # Doctor positive/negative for both the structured G35 marker and legacy
    # prose. A file addressed to another lane does not satisfy the durable human
    # message contract; changing that exact file to to_lane: human clears it.
    for suffix, note in (
        ("structured", "human_qa_hold: judge the interaction"),
        ("legacy", "human_qa_requested: judge the interaction"),
    ):
        loop = tmp_path / ("g33_" + suffix)
        _bootstrap(loop)
        request_id = "REQ-20260715-14{0}-frontend".format(
            "0000" if suffix == "structured" else "0100"
        )
        _append_run_log_row(
            loop, request_id, "REVIEWING", "REVIEWING", "product", note
        )
        missing = _doctor(loop)
        warnings = [
            warning for warning in missing["warnings"]
            if warning["code"] == "human_qa_message_missing"
        ]
        if not warnings or not any(request_id in warning["message"] for warning in warnings):
            _fail("G33 doctor must warn for {0} hold without a human message".format(suffix))
        if any(issue["code"] == "human_qa_message_missing" for issue in missing["issues"]):
            _fail("G33 human_qa_message_missing must be warning-only")

        msg_dir = loop / "messages" / request_id
        msg_dir.mkdir(parents=True, exist_ok=True)
        qa_path = msg_dir / "HUMAN_QA_REQUEST-iter-1.md"
        qa_path.write_text(
            "# HUMAN_QA_REQUEST\n\n"
            "message_type: HUMAN_QA_REQUEST\n"
            "request_id: {0}\n"
            "iteration: 1\n"
            "from_lane: product\n"
            "to_lane: review\n".format(request_id),
            encoding="utf-8",
        )
        wrong_lane = _doctor(loop)
        if not any(
            warning["code"] == "human_qa_message_missing"
            for warning in wrong_lane["warnings"]
        ):
            _fail("G33 a durable message not addressed to human must not satisfy the hold")

        qa_path.write_text(
            qa_path.read_text(encoding="utf-8").replace(
                "to_lane: review", "to_lane: human"
            ),
            encoding="utf-8",
        )
        present = _doctor(loop)
        if any(
            warning["code"] == "human_qa_message_missing"
            for warning in present["warnings"]
        ):
            _fail("G33 doctor must stay silent for {0} hold with a durable human message".format(suffix))


def _check_g34(tmp_path: Path) -> None:
    """G34: reserved infrastructure ports and exclusive lane-server binds."""
    protocol_md = _read_doc(_PROTOCOL_MD)
    collapsed = " ".join(protocol_md.lower().split())
    for needle in (
        "shared runtime resource",
        "reserved infrastructure identifier",
        "lane-launched server",
        "must not default to a reserved port",
        "bind exclusively",
        "fail loudly at startup when the port is occupied",
        "so_reuseaddr-style co-binds are forbidden",
    ):
        if needle not in collapsed:
            _fail("G34 exclusive-bind wording is missing: {0!r}".format(needle))
    if "illustrative run-5 example" not in collapsed:
        _fail("G34 port-collision rationale must be marked as an illustrative example")
    for needle in ("8765", "silently co-bound", "served the wrong application"):
        if needle not in collapsed:
            _fail("G34 illustrative run-5 rationale is missing: {0!r}".format(needle))

    project = tmp_path / "g34_project"
    loop = project / "docs" / "loop"
    _bootstrap(loop)
    constraints_path = loop / "constraints.md"
    constraints = constraints_path.read_text(encoding="utf-8")
    if "Reserved loop infrastructure ports: 8765" not in constraints:
        _fail("G34 bootstrap must write the dashboard default reserved-ports line")
    if "append" not in constraints.lower() or "manual dashboard port" not in constraints.lower():
        _fail("G34 reserved-ports line must allow later manual dashboard ports to be appended")

    # Reserved URL in goal warns; an ordinary unreserved product URL does not.
    (loop / "goal.md").write_text(
        "# Goal\n\nUse http://127.0.0.1:8765/app for human QA.\n",
        encoding="utf-8",
    )
    product_dir = project / "docs" / "product"
    product_dir.mkdir(parents=True)
    product_doc = product_dir / "spec.md"
    product_doc.write_text(
        "# Product\n\nUse http://127.0.0.1:8123/app.\n",
        encoding="utf-8",
    )
    goal_reserved = _doctor(loop)
    warnings = [
        warning for warning in goal_reserved["warnings"]
        if warning["code"] == "reserved_port_advertised"
    ]
    if not warnings or not any("goal.md" in w["message"] and "8765" in w["message"] for w in warnings):
        _fail("G34 doctor must warn when goal.md advertises a reserved port")
    if any(issue["code"] == "reserved_port_advertised" for issue in goal_reserved["issues"]):
        _fail("G34 reserved_port_advertised must be warning-only")

    # A manually selected dashboard port appended to the same line is also
    # reserved, and product docs are part of the scan surface.
    constraints_path.write_text(
        constraints.replace(
            "Reserved loop infrastructure ports: 8765",
            "Reserved loop infrastructure ports: 8765, 9001",
        ),
        encoding="utf-8",
    )
    (loop / "goal.md").write_text(
        "# Goal\n\nUse http://127.0.0.1:8123/app.\n",
        encoding="utf-8",
    )
    product_doc.write_text(
        "# Product\n\nUse http://localhost:9001/app.\n",
        encoding="utf-8",
    )
    product_reserved = _doctor(loop)
    product_warnings = [
        warning for warning in product_reserved["warnings"]
        if warning["code"] == "reserved_port_advertised"
    ]
    if not product_warnings or not any("product/spec.md" in w["message"] and "9001" in w["message"] for w in product_warnings):
        _fail("G34 doctor must scan docs/product/*.md for appended reserved ports")

    product_doc.write_text(
        "# Product\n\nUse http://localhost:8123/app.\n",
        encoding="utf-8",
    )
    clean = _doctor(loop)
    if any(w["code"] == "reserved_port_advertised" for w in clean["warnings"]):
        _fail("G34 doctor must stay silent when advertised URLs avoid reserved ports")

    # Old loops have no parseable reserved-ports line. They skip cleanly even if
    # a legacy goal happens to use today's dashboard default.
    old_constraints = "\n".join(
        line
        for line in constraints_path.read_text(encoding="utf-8").splitlines()
        if "Reserved loop infrastructure ports:" not in line
    ) + "\n"
    constraints_path.write_text(old_constraints, encoding="utf-8")
    (loop / "goal.md").write_text(
        "# Goal\n\nLegacy URL http://127.0.0.1:8765/app.\n",
        encoding="utf-8",
    )
    old_loop = _doctor(loop)
    if old_loop.get("reserved_port_advertised"):
        _fail("G34 old loops without a reserved-ports line must return an empty finding list")
    if any(w["code"] == "reserved_port_advertised" for w in old_loop["warnings"]):
        _fail("G34 old loops without a reserved-ports line must skip without a warning")


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


def _check_g27_gate_manifest_performance(tmp_path: Path) -> None:
    """Bound the doctor's 800-request gate + manifest pass against O(N^2)."""
    request_count = 800
    limit_seconds = request_count * 0.00125
    loop = tmp_path / "g27_perf_800"
    evidence_dir = loop / "evidence"
    messages_dir = loop / "messages"
    lane_dir = loop / "lanes" / "implementation"
    evidence_dir.mkdir(parents=True)
    messages_dir.mkdir()
    lane_dir.mkdir(parents=True)

    files = {
        "goal.md": "# Goal\n## Invariants\n- none\n",
        "tracker.md": "# Tracker\n## Checkpoints\n- [x] perf\n",
        "constraints.md": "# Constraints\n",
        "handoff.md": "# Handoff\n",
        "loop-policy.md": "max_fix_cycles: 3\nauto_chain_next_session: false\n",
        "loop-budget.md": "budget_exhausted: false\n",
        "loop-run-log.md": (
            "| timestamp | request_id | iteration | from_status | to_status | lane | note |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
        ),
        "agent-lanes.md": (
            "| lane | thread_id | role | write_scope | worklog | status | heartbeat | tier |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| implementation | t | impl | src/** | lanes/implementation/worklog.md | "
            "active | 2026-07-10T00:00:00Z | highest |\n"
        ),
    }
    for name, text in files.items():
        (loop / name).write_text(text, encoding="utf-8")
    for name in ("inbox.md", "outbox.md", "current.md", "worklog.md"):
        (lane_dir / name).write_text("# x\n", encoding="utf-8")

    request_lines = [
        "| request_id | status | owner_lane | iteration | source_docs | last_message | next_action | updated_at |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index in range(request_count):
        request_id = "REQ-20260710-{0:06d}-implementation".format(index)
        command = "python -m test_{0}".format(index)
        status = "ACCEPTED" if index == 0 else "PLANNED"
        request_lines.append(
            "| {0} | {1} | implementation | 1 | goal.md | done | none | "
            "2026-07-10T00:00:00Z |".format(request_id, status)
        )
        evidence = {
            "request_id": request_id,
            "checkpoint": "perf",
            "command": command,
            "exit_code": 0,
            "ran_at": "2026-07-10T00:00:00Z",
        }
        (evidence_dir / (request_id + "-iter-1.json")).write_text(
            json.dumps(evidence), encoding="utf-8"
        )
        if index == 0:
            request_messages = messages_dir / request_id
            request_messages.mkdir()
            (request_messages / "IMPLEMENTATION_REQUEST-iter-1.md").write_text(
                "# IMPLEMENTATION_REQUEST\nVERIFY `{0}`\n".format(command),
                encoding="utf-8",
            )
    (loop / "requests.md").write_text(
        "\n".join(request_lines) + "\n", encoding="utf-8"
    )

    parsed_records, parsed_errors = completion_gate.load_evidence(evidence_dir)

    class ScanBoundRecords(list):
        """Charge a fixed tax each time code rescans the full evidence list."""

        def __init__(self, records):
            super().__init__(records)
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            time.sleep(0.002)
            return super().__iter__()

    bounded_records = ScanBoundRecords(parsed_records)
    original_loader = doctor.completion_gate.load_evidence

    def load_preparsed(_evidence_dir: Path):
        return bounded_records, parsed_errors

    doctor.completion_gate.load_evidence = load_preparsed
    try:
        started = time.perf_counter()
        result = _doctor(loop)
        elapsed = time.perf_counter() - started
    finally:
        doctor.completion_gate.load_evidence = original_loader
    if not result["completion_gate_ok"]:
        _fail("G27 perf fixture must remain gate-green")
    if result["evidence_manifest_gaps"]:
        _fail("G27 perf fixture must have complete evidence manifests")
    print(
        "G27_PERF_800 seconds={0:.6f} limit={1:.3f}".format(
            elapsed, limit_seconds
        )
    )
    if elapsed >= limit_seconds:
        _fail(
            "G27 doctor gate+manifest pass regressed toward O(N^2): "
            "{0:.3f}s >= {1:.3f}s for {2} requests".format(
                elapsed, limit_seconds, request_count
            )
        )
    if bounded_records.iterations > 2:
        _fail(
            "G27 doctor rescanned the full evidence list {0} times".format(
                bounded_records.iterations
            )
        )


def _check_g27_single_parse_and_decision_cache(tmp_path: Path) -> None:
    """Pin one run-log read and one hash for repeated source signatures."""
    loop = tmp_path / "g27_single_parse"
    _bootstrap(loop)
    run_log = loop / "loop-run-log.md"
    run_log.write_text(
        "| timestamp | request_id | iteration | from_status | to_status | lane | note |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| 2026-07-10T00:00:02Z | REQ-G27 | 1 | FIX_REQUESTED | "
        "IMPLEMENTING | implementation | retry |\n"
        "| 2026-07-10T00:00:01Z | REQ-G27 | 1 | REVIEWING | "
        "FIX_REQUESTED | review | fix |\n",
        encoding="utf-8",
    )
    source = loop / "source.md"
    source.write_text("shared decision source\n", encoding="utf-8")
    content_hash = record_decision.normalize_then_hash([source])
    decisions = []
    for index in range(12):
        decisions.append(
            json.dumps(
                {
                    "decision_id": "REQ-G27-d{0}".format(index),
                    "request_id": "REQ-G27",
                    "source_docs": ["source.md"],
                    "content_hash": content_hash,
                }
            )
        )
    (loop / "memory" / "decisions.jsonl").write_text(
        "\n".join(decisions) + "\n", encoding="utf-8"
    )

    original_read_text = doctor.read_text
    original_hash = doctor.normalize_then_hash
    calls = {"run_log_reads": 0, "hashes": 0}

    def counted_read_text(path: Path) -> str:
        if Path(path) == run_log:
            calls["run_log_reads"] += 1
        return original_read_text(path)

    def counted_hash(paths) -> str:
        calls["hashes"] += 1
        return original_hash(paths)

    doctor.read_text = counted_read_text
    doctor.normalize_then_hash = counted_hash
    try:
        result = _doctor(loop)
    finally:
        doctor.read_text = original_read_text
        doctor.normalize_then_hash = original_hash

    if result["decisions"]["stale"] != 0:
        _fail("G27 decision cache fixture must remain non-stale")
    if calls != {"run_log_reads": 1, "hashes": 1}:
        _fail(
            "G27 per-run sharing expected one run-log read and one repeated-source hash; "
            "got run_log_reads={0}, hashes={1}".format(
                calls["run_log_reads"], calls["hashes"]
            )
        )


def _usage_event(used_percent: float) -> str:
    return json.dumps(
        {
            "timestamp": "2026-07-10T00:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "primary": {
                        "used_percent": used_percent,
                        "window_minutes": 300,
                        "resets_at": 1,
                    },
                    "plan_type": "pro",
                },
            },
        }
    )


def _check_g27_session_path_and_git_cadence(tmp_path: Path) -> None:
    """Pin short-TTL session discovery and terminal-loop git cadence."""
    errors = []
    probe = loop_dashboard.codex_host_probe
    home = tmp_path / "g27_codex_home"
    sessions = home / "sessions" / "2026" / "07" / "10"
    sessions.mkdir(parents=True)
    first = sessions / "first.jsonl"
    first.write_text(_usage_event(10.0) + "\n", encoding="utf-8")
    os.utime(first, (1000.0, 1000.0))

    original_iglob = probe.glob.iglob
    original_ttl = getattr(loop_dashboard, "SESSION_PATH_CACHE_TTL_SECONDS", None)
    scans = {"count": 0}

    def counted_iglob(*args, **kwargs):
        scans["count"] += 1
        return original_iglob(*args, **kwargs)

    probe.drop_caches()
    probe.glob.iglob = counted_iglob
    try:
        usage_first = probe.build_usage(home)
        usage_cached = probe.build_usage(home)
        second = sessions / "second.jsonl"
        second.write_text(_usage_event(20.0) + "\n", encoding="utf-8")
        os.utime(second, (2000.0, 2000.0))
        loop_dashboard.SESSION_PATH_CACHE_TTL_SECONDS = 0.0
        usage_newer = probe.build_usage(home)
        if scans["count"] != 2:
            errors.append(
                "G27 newest-session path cache expected two scans across TTL expiry; got {0}".format(
                    scans["count"]
                )
            )
        if usage_first != usage_cached:
            errors.append("G27 session path cache changed an unchanged usage snapshot")
        newer_primary = (usage_newer.get("primary") or {}).get("used_percent")
        if newer_primary != 20.0:
            errors.append("G27 TTL expiry must select the newer session by mtime")
        probe.drop_caches()
        probe.build_usage(home)
        if scans["count"] != 3:
            errors.append("G27 manual refresh must clear the newest-session path cache")
    finally:
        probe.glob.iglob = original_iglob
        if original_ttl is None:
            try:
                del loop_dashboard.SESSION_PATH_CACHE_TTL_SECONDS
            except AttributeError:
                pass
        else:
            loop_dashboard.SESSION_PATH_CACHE_TTL_SECONDS = original_ttl
        probe.drop_caches()

    repo = tmp_path / "g27_git_cadence"
    repo.mkdir()
    _git(repo, "init")
    loop = repo / "docs" / "loop"
    loop.mkdir(parents=True)
    src = repo / "src"
    src.mkdir()
    (src / "work.py").write_text("print('dirty')\n", encoding="utf-8")
    lanes = [{"lane": "implementation", "write_scope": "src/**"}]
    original_status = doctor._git_status_porcelain
    status_calls = {"count": 0}

    def counted_status(git_root: Path):
        status_calls["count"] += 1
        return original_status(git_root)

    doctor._git_status_porcelain = counted_status
    try:
        first_findings = doctor.check_uncommitted_work(loop, lanes, True, True)
        second_findings = doctor.check_uncommitted_work(loop, lanes, True, True)
    finally:
        doctor._git_status_porcelain = original_status
    cadence = getattr(doctor, "GIT_STATUS_CADENCE_SECONDS", 0)
    if not 15 <= cadence <= 30:
        errors.append("G27 git status cadence must be within 15..30 seconds")
    if status_calls["count"] != 1:
        errors.append(
            "G27 repeated terminal-loop checks must reuse git status; got {0} calls".format(
                status_calls["count"]
            )
        )
    if not first_findings or first_findings != second_findings:
        errors.append("G27 git cadence must preserve uncommitted_work warning semantics")
    if errors:
        _fail("; ".join(errors))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # ---- G26 C3-leases: unreadable lease file is visible/fail-closed ---
        _check_g26_c3_leases(tmp_path)

        # ---- G26 C20: ACTIVE blank lease globs cannot disappear ------------
        _check_g26_c20_blank_active_lease(tmp_path)

        # ---- G26 B9: unknown lease statuses use one explicit fall-through --
        _check_g26_b9_unknown_lease_status(tmp_path)

        # ---- G26 B4: scope overlap blocks only the shared region -----------
        _check_g26_b4_guard_overlap(tmp_path)

        # ---- G26 B5: docs preset is disjoint from product ------------------
        _check_g26_b5_docs_preset(tmp_path)

        # ---- G26 C10: malformed registry rows survive rewrites ------------
        _check_g26_c10_registry_rows(tmp_path)

        # ---- G26 C1: heartbeat write failures warn, delivery still passes --
        _check_g26_c1_heartbeat_warning(tmp_path)

        # ---- G26 C7: malformed control-plane inputs warn, fallbacks stay ----
        _check_g26_c7_malformed_diagnostics(tmp_path)

        # ---- G26 B2/C4: evidence truth (real records, never messages) -------
        _check_g26_b2_c4(tmp_path)

        # ---- G26 B1/C15: manifest coverage + strict evidence fields --------
        _check_g26_b1_c15(tmp_path)

        # ---- G26 B3: auto-chain depends on gate + manifest truth -----------
        _check_g26_b3(tmp_path)

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
        protocol_docs = [("SKILL.md", skill_md)]
        protocol_docs.extend(
            ("references/{0}".format(path.name), _read_doc(path))
            for path in _REFERENCE_MD_FILES
        )

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

        # ---- G26 chunk 4: protocol-doc synchronization ---------------------
        skill_lower = skill_md.lower()
        protocol_lower = protocol_md.lower()
        loop_state_lower = loop_state_md.lower()

        # B7: REVIEW_DONE is a review verdict, never the ACCEPTED transition.
        # The example stays non-terminal at REVIEWING; product alone performs
        # acceptance, after human QA when the request is user-facing.
        review_done_match = re.search(
            r"(?ms)^## REVIEW_DONE\s*$.*?(?=^## FIX_REQUEST\s*$)",
            protocol_md,
        )
        if not review_done_match:
            _fail("protocol.md must contain a REVIEW_DONE section before FIX_REQUEST")
        review_done_section = review_done_match.group(0)
        review_done_lower = review_done_section.lower()
        if "status: accepted" in review_done_lower:
            _fail("B7: the REVIEW_DONE example must never write status: ACCEPTED")
        for name, text in protocol_docs:
            for code_fence in re.findall(r"(?ms)^```[^\n]*\n(.*?)^```\s*$", text):
                if (
                    "review_done" in code_fence.lower()
                    and re.search(r"(?im)^status:\s*accepted\s*$", code_fence)
                ):
                    _fail(
                        "B7: REVIEW_DONE example in {0} must never write "
                        "status: ACCEPTED".format(name)
                    )
        if "status: reviewing" not in review_done_lower:
            _fail("B7: the REVIEW_DONE example must remain at status: REVIEWING")
        if "verdict: pass" not in review_done_lower:
            _fail("B7: the REVIEW_DONE example must carry a pass/fail VERDICT")
        if "product" not in review_done_lower or "accepted transition" not in review_done_lower:
            _fail("B7: REVIEW_DONE wording must assign the ACCEPTED transition to product")
        if "pass/fail `verdict`" not in skill_lower:
            _fail("B7: SKILL.md must describe REVIEW_DONE as a pass/fail VERDICT")
        if "product alone performs the `accepted` transition" not in skill_lower:
            _fail("B7: SKILL.md must reserve the ACCEPTED transition for product")
        if "unless the user assigns that authority elsewhere" in skill_lower:
            _fail("B7: SKILL.md must not delegate the product-only ACCEPTED transition")
        if "product alone performs the `accepted` transition" not in loop_state_lower:
            _fail("B7: loop-state.md must reserve the ACCEPTED transition for product")
        if "pass/fail `verdict`" not in loop_state_lower or "at `reviewing`" not in loop_state_lower:
            _fail("B7: loop-state.md must keep REVIEW_DONE as a verdict at REVIEWING")
        if "product or review marks a request `accepted`" in protocol_lower:
            _fail("B7: protocol.md must not authorize review to mark a request ACCEPTED")

        # OPEN-THE-TURN: the full rule lives exactly once, immediately beside
        # the close-the-turn ritual in protocol.md. The two other docs carry
        # only the leading token + pointer, never another full rule.
        for name, text in (
            ("SKILL.md", skill_md),
            ("protocol.md", protocol_md),
            ("loop-state.md", loop_state_md),
        ):
            if "open the turn" not in text.lower():
                _fail("{0} must reference the ritual by the token 'open the turn'".format(name))

        open_full_signature = "a lane's turn starts by re-reading"
        open_full_sources = [
            name
            for name, text in protocol_docs
            if open_full_signature in " ".join(text.lower().split())
        ]
        if open_full_sources != ["references/protocol.md"]:
            _fail(
                "the full open-the-turn rule must live in exactly protocol.md; "
                "found it in {0}".format(open_full_sources or "(nowhere)")
            )
        open_rule_needles = (
            "agent-lanes.md",
            "requests.md",
            "goal.md",
            "## invariants",
            "constraints.md",
            "loop-policy.md",
            "anything added since your last turn is binding",
        )
        for needle in open_rule_needles:
            if needle not in protocol_lower:
                _fail("protocol.md open-the-turn rule is missing: {0}".format(needle))
        open_heading_at = protocol_lower.find("open the turn")
        close_heading_at = protocol_lower.find("close the turn", open_heading_at + 1)
        if open_heading_at < 0 or close_heading_at < 0 or close_heading_at - open_heading_at > 1200:
            _fail("protocol.md must place open the turn immediately beside close the turn")
        for name, text in (("SKILL.md", skill_md), ("loop-state.md", loop_state_md)):
            lower = text.lower()
            if "open the turn" not in lower or "protocol.md" not in lower:
                _fail("{0} must carry the open-the-turn token + protocol.md pointer".format(name))
            pointer_lines = [line for line in text.splitlines() if "open the turn" in line.lower()]
            if len(pointer_lines) != 1 or "protocol.md" not in pointer_lines[0]:
                _fail(
                    "{0} must carry exactly one one-line open-the-turn pointer + token".format(
                        name
                    )
                )

        # Recovery Gate gains the pointer as an additional numbered item; all
        # pre-existing recovery reads remain present.
        recovery_match = re.search(
            r"(?ms)^## Recovery Gate\s*$.*?(?=^## Stop Conditions\s*$)",
            loop_state_md,
        )
        if not recovery_match:
            _fail("loop-state.md must retain Recovery Gate before Stop Conditions")
        recovery_lower = recovery_match.group(0).lower()
        if "1. **open the turn**" not in recovery_lower or "protocol.md" not in recovery_lower:
            _fail("OPEN-THE-TURN must be added as an item in loop-state.md's Recovery Gate")
        for needle in (
            "`goal.md`",
            "`tracker.md`",
            "`constraints.md`",
            "`handoff.md`",
            "`agent-lanes.md`",
            "`requests.md`",
            # E5: inbox/new/ is the canonical pending-work surface; the flat
            # inbox.md is only the Python-unavailable degrade path.
            "this lane's `current.md` and its `inbox/new/`",
        ):
            if needle not in recovery_lower:
                _fail("OPEN-THE-TURN must not replace Recovery Gate item: {0}".format(needle))

        # A6: loop-state.md is the one complete Stop Conditions source;
        # SKILL.md keeps only a hard gate and an exact pointer, not a second list.
        skill_stop_match = re.search(r"(?ms)^## Stop Conditions\s*$.*?(?=^---\s*$)", skill_md)
        state_stop_match = re.search(r"(?ms)^## Stop Conditions\s*$.*\Z", loop_state_md)
        if not skill_stop_match or not state_stop_match:
            _fail("A6: both docs must retain a Stop Conditions heading")
        skill_stop = skill_stop_match.group(0)
        state_stop_lower = state_stop_match.group(0).lower()
        if "references/loop-state.md" not in skill_stop or "- " in skill_stop:
            _fail("A6: SKILL.md Stop Conditions must be pointer-only, with no duplicate bullet list")
        for needle in (
            "`done when` is satisfied",
            "budget_exhausted: true",
            "no backing request row",
            "violate `constraints.md`",
            "unbounded or duplicate continuation",
        ):
            if needle not in state_stop_lower:
                _fail("A6: canonical loop-state.md Stop Conditions is missing: {0}".format(needle))

        # D2/D3: capability discovery and the real-input cross-file pointer.
        if "`create_thread`, `list_threads`, `read_thread`" not in skill_md:
            _fail("D2: SKILL.md discovery list must include list_threads")
        if "see g2 real-input correctness below" in loop_state_lower:
            _fail("D3: loop-state.md must not point to a nonexistent section below")
        if "references/protocol.md" not in loop_state_md or '"Real-input correctness"' not in loop_state_md:
            _fail("D3: loop-state.md must point to protocol.md's Real-input correctness section")

        # B6: compare public wording with the server's canonical POST route
        # tuple, then ensure the HTML footer names all three human controls.
        dashboard_html = _read_doc(_DASHBOARD_HTML)
        loop_dashboard_py = _read_doc(_LOOP_DASHBOARD_PY)
        route_tuple = re.search(
            r'if route not in \(([^\n]+)\):',
            loop_dashboard_py,
        )
        if not route_tuple:
            _fail("B6: could not find loop_dashboard.py's canonical write-route tuple")
        actual_write_endpoints = set(re.findall(r'"(/api/[^"?]+)"', route_tuple.group(1)))
        expected_write_endpoints = {"/api/lanes", "/api/policy", "/api/project"}
        if actual_write_endpoints != expected_write_endpoints:
            _fail(
                "B6: server write endpoints changed; expected {0}, got {1}".format(
                    sorted(expected_write_endpoints), sorted(actual_write_endpoints)
                )
            )
        if "three human-only write endpoints" not in skill_lower:
            _fail("B6: SKILL.md must state there are three human-only write endpoints")
        dashboard_lower = dashboard_html.lower()
        dashboard_collapsed = " ".join(dashboard_lower.split())
        if "three human-only write endpoints" not in dashboard_collapsed:
            _fail("B6: dashboard.html header comment must state there are three write endpoints")
        for endpoint in sorted(expected_write_endpoints):
            public_token = "post {0}".format(endpoint)
            if public_token not in skill_lower:
                _fail("B6: SKILL.md is missing public endpoint wording: {0}".format(public_token))
            if public_token not in dashboard_collapsed:
                _fail("B6: dashboard.html is missing public endpoint wording: {0}".format(public_token))
        footer_match = re.search(
            r'"key":"footer_reassurance","en":"([^"]+)","zh":"([^"]+)"',
            dashboard_html,
        )
        if not footer_match:
            _fail("B6: dashboard footer_reassurance bilingual entry is missing")
        footer_en = footer_match.group(1).lower()
        footer_zh = footer_match.group(2)
        if "three" not in footer_en or "\u4e09" not in footer_zh:
            _fail("B6: dashboard footer must state the endpoint count in EN and ZH")
        for needle in ("adding a lane", "fix-retry limit", "project name"):
            if needle not in footer_en:
                _fail("B6: dashboard footer must name all three writes; missing {0!r}".format(needle))
        for needle in ("\u65b0\u589e\u901a\u9053", "\u4fee\u590d\u91cd\u8bd5\u4e0a\u9650", "\u9879\u76ee\u540d\u79f0"):
            if needle not in footer_zh:
                _fail("B6: dashboard ZH footer must name all three writes; missing {0!r}".format(needle))
        if "the only thing it can change" in dashboard_collapsed:
            _fail("B6: dashboard footer must not claim adding a lane is its only write")

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
        # A6 single-sources the complete list in loop-state.md; SKILL.md points
        # there instead of maintaining a second copy.
        if "no backing request row" not in loop_state_lower:
            _fail("loop-state.md Stop Conditions must include 'asked to change code with no backing request row'")

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

        # ---- G23: hybrid intake (serial cap + on-demand batch) --------------
        # SKILL.md Intake + loop-state.md intake section add the hybrid rules ON
        # TOP of G9's one-at-a-time grilling: one-at-a-time stays the default
        # only for FORK-SHAPED decisions; after at most three serial questions
        # the coordinator MUST offer questionnaire mode; the human may say "list
        # them all" at any time and it switches immediately; only INDEPENDENT
        # items go in the batch (a decision that would change another is held
        # back and asked serially). G9's own rules are unchanged (asserted below).
        for g23_name, g23_lower in (("SKILL.md", skill_lower), ("loop-state.md", loop_state_lower)):
            if "fork-shaped" not in g23_lower:
                _fail("{0} must keep one-at-a-time only for FORK-SHAPED decisions (G23)".format(g23_name))
            if "at most three" not in g23_lower:
                _fail("{0} must cap serial intake at 'at most three' questions (G23)".format(g23_name))
            if "questionnaire mode" not in g23_lower:
                _fail("{0} must OFFER questionnaire mode after the cap (G23)".format(g23_name))
            if "list them all" not in g23_lower:
                _fail("{0} must let the human switch with 'list them all' at any time (G23)".format(g23_name))
            if "held back" not in g23_lower:
                _fail("{0} must hold a dependent decision BACK from the batch (G23)".format(g23_name))
            if "independent" not in g23_lower:
                _fail("{0} must batch only INDEPENDENT decisions (G23)".format(g23_name))

        # NEGATIVE: the G23 hybrid rules were ADDED, not swapped in -- every G9
        # grilling-intake anchor must still be present UNCHANGED in both docs.
        for g9_anchor in ("one question at a time", "recommended answer", "stop rule", "over-interview"):
            if g9_anchor not in skill_lower:
                _fail("G23 regressed a G9 SKILL.md intake anchor: {0!r}".format(g9_anchor))
            if g9_anchor not in loop_state_lower:
                _fail("G23 regressed a G9 loop-state.md intake anchor: {0!r}".format(g9_anchor))
        if "walk me through how you'll actually operate this" not in skill_lower:
            _fail("G23 regressed the G9 mandatory operate-it question in SKILL.md")
        if "which fork" not in skill_lower or "which cut" not in skill_lower:
            _fail("G23 regressed the G9 two-forks intake wording in SKILL.md")

        # ---- G24: invariants-first intake -----------------------------------
        # (a) SKILL.md Intake + loop-state.md: for a data / multi-step goal, right
        # after the objective is confirmed, intake asks for domain INVARIANTS and
        # DRAFTS a recommended set; the example class list is present; invariants
        # live in goal.md under a CANONICAL "## Invariants" section; the run-4
        # expense app is the worked example.
        if "invariants-first" not in skill_lower:
            _fail("SKILL.md must add an invariants-first intake step (G24)")
        for g24_name, g24_md, g24_lower in (
            ("SKILL.md", skill_md, skill_lower),
            ("loop-state.md", loop_state_md, loop_state_lower),
        ):
            if "## Invariants" not in g24_md:
                _fail("{0} must name goal.md's canonical '## Invariants' section (G24)".format(g24_name))
            if "canonical" not in g24_lower:
                _fail("{0} must state '## Invariants' is the canonical location (G24)".format(g24_name))
            # The example class list (substance; adapted wording tolerated).
            for g24_needle in ("no silent drops", "outrank", "add-only", "run_id"):
                if g24_needle not in g24_lower:
                    _fail("{0} invariant example-class list is missing: {1!r} (G24)".format(g24_name, g24_needle))
            if "displayed number" not in g24_lower:
                _fail("{0} must include the 'every displayed number traces to its source' invariant (G24)".format(g24_name))
            # The run-4 expense-app worked example.
            if "expense app" not in g24_lower:
                _fail("{0} must cite the run-4 expense app as the worked invariants example (G24)".format(g24_name))

        # (b) protocol.md IMPLEMENTATION_REQUEST template: every request lists
        # WHICH invariants apply (or none apply and why), and the G1 red-capable
        # rule extends so each APPLICABLE invariant names a command that can FAIL.
        if "invariants:" not in protocol_md:
            _fail("protocol.md IMPLEMENTATION_REQUEST template must carry an invariants: line (G24)")
        if "applicable invariant" not in protocol_lower:
            _fail("protocol.md must extend the red-capable rule to each APPLICABLE invariant (G24)")
        if "none apply" not in protocol_lower:
            _fail("protocol.md must allow a request to state 'none apply and why' (G24)")
        if "red-capable" not in protocol_lower:
            _fail("protocol.md must tie applicable invariants to the red-capable rule (G24)")

        # (c) loop-state.md review gate: reviewers check changed behavior against
        # goal.md's ## Invariants section, and an invariant violation is ALWAYS a
        # blocker-severity finding (feeds G8's severity tiers).
        loop_state_collapsed_g24 = " ".join(loop_state_lower.split())
        if "invariant check" not in loop_state_lower:
            _fail("loop-state.md review gate must carry an 'Invariant check' subsection (G24)")
        if "invariant violation is always a blocker" not in loop_state_collapsed_g24:
            _fail("loop-state.md must state an invariant violation is ALWAYS a blocker (G24)")
        if "## invariants" not in loop_state_lower:
            _fail("loop-state.md review gate must check behavior against goal.md's ## Invariants section (G24)")

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
        # The field name is a literal protocol token: every mention must use
        # the exact lowercase spelling, or agents copy a drifted casing into
        # real envelopes that tooling then fails to parse.
        for cased in re.finditer(r"(?i)recommended_answer", protocol_md):
            if cased.group(0) != "recommended_answer":
                _fail(
                    "protocol.md spells the recommended_answer field with wrong casing: {0!r}".format(
                        cased.group(0)
                    )
                )
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

        # ---- G25: tier-resolution hardening (frontier label, xhigh ceiling,
        # explicit-id gap window) --------------------------------------------
        # (a) FRONTIER LABEL primary; list POSITION is only the fallback. The
        # resolution must key on the model's frontier/most-capable description in
        # the create_thread list, not on top-of-list position, so a host
        # reordering the list cannot silently change what "highest" means.
        if "frontier label" not in skill_lower:
            _fail("SKILL.md (G25a) must resolve 'highest available' by the FRONTIER LABEL")
        if "frontier or most-capable" not in skill_lower:
            _fail("SKILL.md (G25a) must identify the frontier/most-capable model in the create_thread list")
        if "position is only the fallback" not in skill_lower:
            _fail("SKILL.md (G25a) must keep list POSITION only as the fallback when no frontier label exists")
        # (b) EFFORT CEILING: default thinking is xhigh (or the highest standard
        # effort the model supports, never above xhigh); ultra/max are NEVER
        # auto-selected -- reserved for long-horizon tasks, human-explicit only,
        # and recorded like any human tier choice (G14e mechanics).
        if "xhigh" not in skill_lower:
            _fail("SKILL.md (G25b) must set the default thinking effort to xhigh")
        if "never above `xhigh`" not in skill_lower:
            _fail("SKILL.md (G25b) must cap the effort ceiling at xhigh (never above)")
        if "never auto-selected" not in skill_lower:
            _fail("SKILL.md (G25b) must state ultra/max are NEVER auto-selected")
        if "`ultra` and `max`" not in skill_md:
            _fail("SKILL.md (G25b) must name `ultra` and `max` as the reserved (never auto-selected) efforts")
        if "long-horizon" not in skill_lower:
            _fail("SKILL.md (G25b) must reserve ultra/max for long-horizon tasks")
        if "explicitly requests" not in skill_lower:
            _fail("SKILL.md (G25b) must require ultra/max only on an explicit human request")
        if "record that request" not in skill_lower:
            _fail("SKILL.md (G25b) must record the explicit ultra/max request like any human tier choice")
        if "g14e" not in skill_lower:
            _fail("SKILL.md (G25b) must tie the recorded-request rule to the G14e human-choice mechanics")
        # (c) GAP WINDOW: when the host's interactive picker offers a newer model
        # than the create_thread list names, pass the explicit model id; the host
        # validates at call time; on rejection fall back per the degradation rule
        # (never fail the dispatch) and tell the human.
        if "gap window" not in skill_lower:
            _fail("SKILL.md (G25c) must define the gap-window case (host offers a newer model than the list names)")
        if "interactive picker" not in skill_lower:
            _fail("SKILL.md (G25c) must anchor the gap window on the host's interactive picker")
        if "explicit model id" not in skill_lower:
            _fail("SKILL.md (G25c) must instruct passing the explicit model id in the gap window")
        if "validates the id at call time" not in skill_lower:
            _fail("SKILL.md (G25c) must state the host validates the id at call time")
        if "never fail the dispatch" not in skill_lower:
            _fail("SKILL.md (G25c) must fall back per the degradation rule and never fail the dispatch")
        if "tell the human which tier the lane ran on" not in skill_lower:
            _fail("SKILL.md (G25c) must, on rejection, tell the human which tier the lane ran on")

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

        # ---- G28: defect-class closure + FIX_REQUEST schema warning ---------
        _check_g28(tmp_path)

        # ---- G31: BLOCKED pause lifecycle + authorized-exit warning ---------
        _check_g31(tmp_path)

        # ---- G28 review closure: timestamp trust + CLI syntax parity --------
        _check_g28_order_trust_closure(tmp_path)

        # ---- G32: evidence mirrors + scoped gate evidence + schema ----------
        _check_g32(tmp_path)

        # ---- G33: durable self-probed human-QA requests ---------------------
        _check_g33(tmp_path)

        # ---- G34: reserved infrastructure ports + exclusive bind -----------
        _check_g34(tmp_path)

        # ---- G35: lifecycle bookkeeping + override history -----------------
        _check_g35(tmp_path)

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

        # ---- G27: gate + manifest pass remains linear at 800 requests -------
        _check_g27_gate_manifest_performance(tmp_path)

        # ---- G27: run log + decision sources are shared within one run -------
        _check_g27_single_parse_and_decision_cache(tmp_path)

        # ---- G27: newest-session and git checks use bounded cadences ---------
        _check_g27_session_path_and_git_cadence(tmp_path)

    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
