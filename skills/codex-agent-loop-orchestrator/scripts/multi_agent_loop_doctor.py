#!/usr/bin/env python3
"""Inspect repo-local multi-agent Codex loop state.

This helper is read-only. It summarizes whether the project has enough durable
state to hand off work, continue a request, or auto-chain a next session.

It also enforces the hardened loop engineering invariants:

- goal/tracker/constraints/handoff must already exist (bootstrap creates them);
- per-lane ``heartbeat`` is parsed and stale heartbeats are flagged as
  ``orphan-suspect`` against ``--stale-heartbeat-mins`` (default 30);
- ``loop-budget.md`` is read and a ``budget_exhausted: true`` flag warns;
- FIX_REQUESTED<->IMPLEMENTING iterations per request are counted and warned
  when they exceed ``max_fix_cycles`` from ``loop-policy.md`` (anti-thrash);
- ``loop-run-log.md`` and ``evidence/`` presence is checked;
- ``evidence_recorded_ok`` is true only when every non-terminal request has a
  non-empty evidence cell in ``requests.md`` (it proves a cell was filled in,
  not that any command passed);
- ``completion_gate_ok`` runs the real deterministic gate: it imports
  ``completion_gate`` in-process (never a subprocess), loads ``evidence/*.json``
  once, and evaluates every distinct ``request_id`` that appears in the RECORDS
  themselves -- NOT only the non-terminal rows of ``requests.md``. Any record
  whose ``exit_code`` is not a clean 0 flips the flag and names that
  ``request_id`` in ``gate_failed_requests``, whether or not the request is
  registered in ``requests.md`` and whether or not it is terminal: a terminal
  ACCEPTED request with failing evidence is exactly the lie this gate exists to
  catch. Registered requests with zero records are left to ``missing_evidence``
  only (never ``gate_failed``). It is true only when the gate is importable, no
  recorded request failed, and no evidence file was malformed. If
  ``completion_gate`` cannot be imported the doctor still runs, sets
  ``gate_available`` false, and emits a ``gate_unavailable`` warning;
- decision-memory drift is checked against ``memory/decisions.jsonl`` using the
  single canonical ``normalize_then_hash`` imported from ``record_decision``.
  Every finding (``stale_decision``, ``missing_source_doc``,
  ``malformed_decision``) is a WARNING only and NEVER affects ``handoff_ready``
  or ``auto_chain_ready``: the memory cache is fail-open, the completion gate is
  fail-closed. An absent ``decisions.jsonl`` degrades gracefully to zero
  warnings; ``decisions`` reports ``{total, active, stale, malformed}``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Import the deterministic completion gate in-process (never a subprocess: this
# box's sandbox cannot shell out). The gate lives beside this file in scripts/.
# Guard the import so the read-only doctor never crashes if it is missing.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
try:
    import completion_gate  # type: ignore

    GATE_AVAILABLE = True
except ImportError:
    completion_gate = None  # type: ignore
    GATE_AVAILABLE = False

# Import THE single canonical memory hash from record_decision (in-process, no
# subprocess). The drift check reuses this exact function so the writer and the
# doctor agree byte-for-byte; a second hash implementation would make Windows
# CRLF files look permanently stale. Guard the import so the read-only doctor
# never crashes when record_decision is absent -- the drift check is simply
# skipped in that case.
try:
    from record_decision import normalize_then_hash  # type: ignore

    DECISIONS_HELPER_AVAILABLE = True
except ImportError:
    normalize_then_hash = None  # type: ignore
    DECISIONS_HELPER_AVAILABLE = False


REQUIRED_FILES = [
    "goal.md",
    "tracker.md",
    "constraints.md",
    "handoff.md",
    "agent-lanes.md",
    "requests.md",
    "loop-policy.md",
]
LANE_FILES = ["inbox.md", "outbox.md", "current.md", "worklog.md"]
TERMINAL_REQUEST_STATUSES = {"ACCEPTED", "BLOCKED"}
UNVERIFIED_THREAD_VALUES = {"", "UNVERIFIED", "TBD", "NONE", "NULL", "-"}
EMPTY_CELL_VALUES = {"", "-", "TBD", "NONE", "NULL", "N/A", "NA"}
# Statuses involved in the FIX_REQUESTED<->IMPLEMENTING thrash cycle.
THRASH_STATUSES = {"FIX_REQUESTED", "IMPLEMENTING"}
DEFAULT_MAX_FIX_CYCLES = 3
DEFAULT_STALE_HEARTBEAT_MINS = 30

CHECKBOX_RE = re.compile(r"^\s*-\s+\[(?P<status>[ xX~!])\]\s+(?P<text>.+?)\s*$")
THREAD_ID_RE = re.compile(r"\b(?:codex:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
AUTO_CHAIN_RE = re.compile(r"(?i)\bauto_chain_next_session\s*:\s*true\b")
STALE_RE = re.compile(r"(?i)\b(stale|pending re-creation|unreadable|unopenable|not visible|not found)\b")
MAX_FIX_CYCLES_RE = re.compile(r"(?im)^\s*max_fix_cycles\s*:\s*(\d+)\b")
BUDGET_EXHAUSTED_RE = re.compile(r"(?im)^\s*budget_exhausted\s*:\s*true\b")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def split_md_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_table(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []

    headers: Optional[list[str]] = None
    rows: list[dict[str, str]] = []
    for line in read_text(path).splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = split_md_row(line)
        if not cells:
            continue
        if all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        if headers is None:
            headers = [cell.strip() for cell in cells]
            continue
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        rows.append(dict(zip(headers, cells[: len(headers)])))
    return rows


def find_checkboxes(text: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for line in text.splitlines():
        match = CHECKBOX_RE.match(line)
        if match:
            items.append({"status": match.group("status"), "text": match.group("text")})
    return items


def status_key(value: str) -> str:
    return value.strip().upper().replace(" ", "_").replace("-", "_")


def is_empty_cell(value: str) -> bool:
    return value.strip().upper() in EMPTY_CELL_VALUES or value.strip() == ""


def parse_timestamp(value: str) -> Optional[datetime]:
    """Parse an ISO-8601-ish timestamp into an aware UTC datetime.

    Handles a trailing ``Z`` (which ``datetime.fromisoformat`` rejects on
    Python 3.8-3.10) and naive timestamps (assumed UTC). Returns ``None`` for
    blank or unparseable values.
    """
    text = value.strip()
    if not text or text.upper() in EMPTY_CELL_VALUES:
        return None

    candidate = text
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    # Normalize a space separator between date and time to 'T'.
    if " " in candidate and "T" not in candidate:
        candidate = candidate.replace(" ", "T", 1)

    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(candidate, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def lane_status(row: dict[str, str]) -> str:
    thread_id = row.get("thread_id", "").strip()
    status = row.get("status", "").strip().lower()
    if status == "stale":
        return "stale"
    if thread_id.upper() in UNVERIFIED_THREAD_VALUES or status in {"needs-thread", "unverified"}:
        return "needs-thread"
    if "stale" in status:
        return "stale"
    return "registered"


def lane_heartbeat_age_mins(row: dict[str, str], now: datetime) -> Optional[float]:
    """Return heartbeat age in minutes, or ``None`` if absent/unparseable."""
    raw = row.get("heartbeat", "") or row.get("last_heartbeat", "")
    parsed = parse_timestamp(raw)
    if parsed is None:
        return None
    delta = now - parsed
    return delta.total_seconds() / 60.0


def read_max_fix_cycles(policy_text: str) -> int:
    match = MAX_FIX_CYCLES_RE.search(policy_text)
    if not match:
        return DEFAULT_MAX_FIX_CYCLES
    try:
        return int(match.group(1))
    except ValueError:
        return DEFAULT_MAX_FIX_CYCLES


def count_fix_cycles_from_log(loop_dir: Path) -> dict[str, int]:
    """Count FIX_REQUESTED<->IMPLEMENTING transitions per request_id.

    The append-only ``loop-run-log.md`` is the durable transition log. We count
    each entry into a thrash status (FIX_REQUESTED or IMPLEMENTING). A full
    round trip (FIX_REQUESTED then IMPLEMENTING, or vice versa) is one cycle, so
    the iteration count is ``transitions_into_thrash_status // 2`` rounded up to
    the nearest entry pair. We report the raw transition count and let callers
    compare against ``max_fix_cycles``.
    """
    counts: dict[str, int] = {}
    rows = parse_table(loop_dir / "loop-run-log.md")
    for row in rows:
        request_id = (
            row.get("request_id", "")
            or row.get("request", "")
            or row.get("req", "")
        ).strip()
        if not request_id:
            continue
        to_status = status_key(
            row.get("to_status", "")
            or row.get("status", "")
            or row.get("new_status", "")
        )
        if to_status in THRASH_STATUSES:
            counts[request_id] = counts.get(request_id, 0) + 1
    return counts


def check_decision_drift(loop_dir: Path) -> dict[str, Any]:
    """Detect drift in the append-only decision memory cache (invariant 5).

    Reads ``memory/decisions.jsonl`` line by line and, for every decision that
    has NOT been superseded by any later line, recomputes ``content_hash`` with
    the shared canonical ``normalize_then_hash`` and compares it to the stored
    value. Returns a dict with ``warnings`` and ``decisions`` counts::

        {"warnings": [...], "decisions": {total, active, stale, malformed}}

    Rules (memory is fail-open, verification is fail-closed):

    - blank or unparseable line -> ``malformed_decision`` warning;
    - a source doc that no longer exists -> ``missing_source_doc`` warning;
    - stored hash != live hash for a non-superseded decision ->
      ``stale_decision`` warning naming the decision_id and its source docs;
    - ``decisions.jsonl`` absent -> zero warnings, all counts 0 (graceful
      degrade, never an error);
    - the shared hash helper missing -> skip drift silently (counts still
      report total/malformed so the log's shape is visible, but nothing is
      flagged stale because it cannot be checked).

    Drift is ALWAYS a warning. It never contributes to ``handoff_ready`` or
    ``auto_chain_ready``.
    """
    warnings: list[dict[str, str]] = []
    decisions_path = loop_dir / "memory" / "decisions.jsonl"

    counts = {"total": 0, "active": 0, "stale": 0, "malformed": 0}

    if not decisions_path.exists():
        # Graceful degrade: no memory cache yet. Never a warning, never an error.
        return {"warnings": warnings, "decisions": counts}

    try:
        raw_text = decisions_path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append(
            {
                "severity": "warning",
                "code": "malformed_decision",
                "message": "decisions.jsonl unreadable: {0}".format(exc),
            }
        )
        return {"warnings": warnings, "decisions": counts}

    parsed: list[dict[str, Any]] = []
    for lineno, line in enumerate(raw_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            counts["malformed"] += 1
            warnings.append(
                {
                    "severity": "warning",
                    "code": "malformed_decision",
                    "message": "decisions.jsonl line {0} is blank".format(lineno),
                }
            )
            continue
        try:
            obj = json.loads(stripped)
        except ValueError as exc:
            counts["malformed"] += 1
            warnings.append(
                {
                    "severity": "warning",
                    "code": "malformed_decision",
                    "message": "decisions.jsonl line {0} is not valid JSON: {1}".format(lineno, exc),
                }
            )
            continue
        if not isinstance(obj, dict) or not str(obj.get("decision_id", "")).strip():
            counts["malformed"] += 1
            warnings.append(
                {
                    "severity": "warning",
                    "code": "malformed_decision",
                    "message": "decisions.jsonl line {0} lacks a decision_id".format(lineno),
                }
            )
            continue
        parsed.append(obj)

    counts["total"] = len(parsed)

    # Any decision_id named by any later (or earlier) supersedes is inactive.
    superseded_ids = {
        str(obj.get("supersedes", "")).strip()
        for obj in parsed
        if str(obj.get("supersedes", "")).strip()
    }

    active = [obj for obj in parsed if str(obj.get("decision_id", "")).strip() not in superseded_ids]
    counts["active"] = len(active)

    if not DECISIONS_HELPER_AVAILABLE or normalize_then_hash is None:
        # Cannot recompute without the canonical helper; report shape only and
        # do not flag anything stale (fail open, never a false stale).
        return {"warnings": warnings, "decisions": counts}

    for obj in active:
        decision_id = str(obj.get("decision_id", "")).strip()
        source_docs = obj.get("source_docs", []) or []
        if not isinstance(source_docs, list):
            source_docs = []
        source_docs = [str(doc) for doc in source_docs]

        missing_docs = [doc for doc in source_docs if not Path(loop_dir_join(loop_dir, doc)).exists()]
        for doc in missing_docs:
            warnings.append(
                {
                    "severity": "warning",
                    "code": "missing_source_doc",
                    "message": "{0}: source doc not found: {1}".format(decision_id, doc),
                }
            )

        stored_hash = str(obj.get("content_hash", "")).strip()
        live_hash = normalize_then_hash([loop_dir_join(loop_dir, doc) for doc in source_docs])
        if stored_hash and stored_hash != live_hash:
            counts["stale"] += 1
            changed = ", ".join(source_docs) if source_docs else "(no source docs listed)"
            warnings.append(
                {
                    "severity": "warning",
                    "code": "stale_decision",
                    "message": "{0} is stale: source docs changed since it was recorded [{1}]".format(
                        decision_id, changed
                    ),
                }
            )

    return {"warnings": warnings, "decisions": counts}


def loop_dir_join(loop_dir: Path, doc: str) -> Path:
    """Resolve a recorded source_doc path.

    Absolute paths are used as-is. Relative paths are interpreted first as
    given (relative to the process CWD, matching how record_decision hashed
    them when invoked from the repo root) and, if that does not exist, relative
    to ``loop_dir`` so records written with loop-relative paths still resolve.
    """
    candidate = Path(doc)
    if candidate.is_absolute():
        return candidate
    if candidate.exists():
        return candidate
    loop_relative = loop_dir / doc
    if loop_relative.exists():
        return loop_relative
    # Neither exists: return the as-given path so the missing-doc check reports
    # the value the operator actually recorded.
    return candidate


def summarize(loop_dir: Path, stale_heartbeat_mins: int, now: Optional[datetime] = None) -> dict[str, Any]:
    if now is None:
        now = datetime.now(timezone.utc)

    paths = {name: loop_dir / name for name in REQUIRED_FILES}
    missing_files = [str(path) for path in paths.values() if not path.exists()]

    tracker_text = read_text(loop_dir / "tracker.md")
    handoff_text = read_text(loop_dir / "handoff.md")
    policy_text = read_text(loop_dir / "loop-policy.md")
    budget_text = read_text(loop_dir / "loop-budget.md")
    all_loop_text = "\n".join(read_text(path) for path in paths.values())
    stale_scan_text = "\n".join(
        [
            handoff_text,
            read_text(loop_dir / "agent-lanes.md"),
            read_text(loop_dir / "requests.md"),
        ]
    )

    checkboxes = find_checkboxes(tracker_text)
    unchecked = [item for item in checkboxes if item["status"] == " "]
    blocked = [item for item in checkboxes if item["status"] == "!"]

    max_fix_cycles = read_max_fix_cycles(policy_text)

    lanes = parse_table(loop_dir / "agent-lanes.md")
    lane_names = [row.get("lane", "") for row in lanes if row.get("lane")]
    lane_file_missing: dict[str, list[str]] = {}
    lane_summaries: list[dict[str, Any]] = []
    orphan_suspects: list[str] = []
    for row in lanes:
        lane = row.get("lane", "").strip()
        if not lane:
            continue
        missing = [
            str(loop_dir / "lanes" / lane / filename)
            for filename in LANE_FILES
            if not (loop_dir / "lanes" / lane / filename).exists()
        ]
        if missing:
            lane_file_missing[lane] = missing

        age_mins = lane_heartbeat_age_mins(row, now)
        heartbeat_raw = (row.get("heartbeat", "") or row.get("last_heartbeat", "")).strip()
        registered = lane_status(row) == "registered"
        is_orphan = bool(
            registered
            and age_mins is not None
            and age_mins > stale_heartbeat_mins
        )
        if is_orphan:
            orphan_suspects.append(lane)
        lane_summaries.append(
            {
                "lane": lane,
                "thread_id": row.get("thread_id", ""),
                "status": lane_status(row),
                "write_scope": row.get("write_scope", ""),
                "heartbeat": heartbeat_raw,
                "heartbeat_age_mins": round(age_mins, 2) if age_mins is not None else None,
                "orphan_suspect": is_orphan,
            }
        )

    log_fix_counts = count_fix_cycles_from_log(loop_dir)

    requests = parse_table(loop_dir / "requests.md")
    request_summaries: list[dict[str, Any]] = []
    owner_issues: list[str] = []
    non_terminal_requests: list[dict[str, Any]] = []
    thrash_requests: list[dict[str, Any]] = []
    missing_evidence_requests: list[str] = []
    for row in requests:
        request_id = row.get("request_id", "").strip()
        if not request_id:
            continue
        status = status_key(row.get("status", ""))
        owner = row.get("owner_lane", "").strip()
        iteration_raw = row.get("iteration", "").strip()
        try:
            iteration_num = int(iteration_raw)
        except ValueError:
            iteration_num = 0
        # Prefer the durable transition log; fall back to the iteration column.
        fix_cycles = log_fix_counts.get(request_id, iteration_num)
        evidence_cell = (
            row.get("evidence", "")
            or row.get("evidence_path", "")
            or row.get("last_message", "")
        )
        has_evidence = not is_empty_cell(evidence_cell)
        summary = {
            "request_id": request_id,
            "status": status,
            "owner_lane": owner,
            "iteration": iteration_raw,
            "next_action": row.get("next_action", ""),
            "fix_cycles": fix_cycles,
            "has_evidence": has_evidence,
        }
        request_summaries.append(summary)
        if status not in TERMINAL_REQUEST_STATUSES:
            non_terminal_requests.append(summary)
            if not has_evidence:
                missing_evidence_requests.append(request_id)
        if owner and owner not in lane_names:
            owner_issues.append(f"{request_id} owner_lane {owner!r} is not registered")
        if fix_cycles > max_fix_cycles:
            thrash_requests.append(
                {
                    "request_id": request_id,
                    "fix_cycles": fix_cycles,
                    "max_fix_cycles": max_fix_cycles,
                }
            )

    stale_markers = [
        line.strip()
        for line in stale_scan_text.splitlines()
        if STALE_RE.search(line)
    ]
    auto_chain_enabled = bool(AUTO_CHAIN_RE.search(handoff_text) or AUTO_CHAIN_RE.search(policy_text))

    budget_present = (loop_dir / "loop-budget.md").exists()
    budget_exhausted = bool(BUDGET_EXHAUSTED_RE.search(budget_text))

    run_log_present = (loop_dir / "loop-run-log.md").exists()
    evidence_dir = loop_dir / "evidence"
    evidence_dir_present = evidence_dir.exists() and evidence_dir.is_dir()

    # evidence_recorded_ok: every non-terminal request has a non-empty evidence
    # cell in requests.md. This only proves the cell was filled in; it does NOT
    # prove any verification command exited 0. Vacuously true with no
    # non-terminal requests.
    evidence_recorded_ok = not missing_evidence_requests

    # completion_gate_ok: run the real deterministic gate in-process. Load the
    # recorded evidence/*.json records once, then evaluate every distinct
    # request_id that actually appears in the RECORDS -- NOT only the
    # non-terminal rows of requests.md. This makes the doctor agree with the
    # gate on failing evidence regardless of registration: a failing record for
    # a request that is unregistered, or already terminal (ACCEPTED/BLOCKED),
    # still flips completion_gate_ok. A terminal ACCEPTED request with failing
    # evidence is exactly the lie this gate exists to catch. Requests with zero
    # records produce no record here, so registered-but-empty requests are left
    # to the missing_evidence warning above (no gate_failed, no double-report).
    # The gate is unavailable -> completion_gate_ok is false and a
    # gate_unavailable warning is emitted, but the doctor never crashes.
    gate_available = GATE_AVAILABLE
    gate_failed_requests: list[str] = []
    gate_load_errors: list[dict[str, str]] = []
    if gate_available:
        gate_records, gate_load_errors = completion_gate.load_evidence(evidence_dir)
        recorded_ids = sorted(
            {
                str(rec.get("request_id", "")).strip()
                for rec in gate_records
                if str(rec.get("request_id", "")).strip()
            }
        )
        for request_id in recorded_ids:
            # Evaluate against the records only (pass no load_errors here): a
            # request lands in gate_failed_requests when its OWN records did not
            # all exit 0. Malformed files are fail-closed on their own via the
            # `not gate_load_errors` term below and the gate_malformed_evidence
            # issues, so they must not smear a failure across every request_id.
            gate_result = completion_gate.evaluate(gate_records, [], request_id)
            if not gate_result["ok"]:
                gate_failed_requests.append(request_id)
    completion_gate_ok = (
        gate_available and not gate_failed_requests and not gate_load_errors
    )

    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    for path in missing_files:
        issues.append({"severity": "error", "code": "missing_file", "message": path})
    for lane, missing in lane_file_missing.items():
        for path in missing:
            issues.append({"severity": "error", "code": "missing_lane_file", "message": f"{lane}: {path}"})
    for lane in lane_summaries:
        if lane["status"] == "stale":
            issues.append({"severity": "error", "code": "stale_lane_thread", "message": lane["lane"]})
        elif lane["status"] == "needs-thread":
            warnings.append({"severity": "warning", "code": "unverified_lane_thread", "message": lane["lane"]})
        if lane["orphan_suspect"]:
            warnings.append(
                {
                    "severity": "warning",
                    "code": "orphan_suspect",
                    "message": f"{lane['lane']} heartbeat is {lane['heartbeat_age_mins']} min old "
                    f"(> {stale_heartbeat_mins})",
                }
            )
    for message in owner_issues:
        issues.append({"severity": "error", "code": "unknown_request_owner", "message": message})
    for marker in stale_markers:
        warnings.append({"severity": "warning", "code": "stale_marker", "message": marker})
    if blocked:
        warnings.append({"severity": "warning", "code": "blocked_tracker_items", "message": str(len(blocked))})
    for thrash in thrash_requests:
        warnings.append(
            {
                "severity": "warning",
                "code": "fix_cycle_thrash",
                "message": f"{thrash['request_id']} reached {thrash['fix_cycles']} fix cycles "
                f"(max {thrash['max_fix_cycles']})",
            }
        )
    if budget_exhausted:
        warnings.append(
            {
                "severity": "warning",
                "code": "budget_exhausted",
                "message": "loop-budget.md has budget_exhausted: true",
            }
        )
    for request_id in missing_evidence_requests:
        warnings.append(
            {
                "severity": "warning",
                "code": "missing_evidence",
                "message": f"{request_id} has no recorded evidence",
            }
        )
    if not gate_available:
        warnings.append(
            {
                "severity": "warning",
                "code": "gate_unavailable",
                "message": "completion_gate could not be imported; completion_gate_ok is false",
            }
        )
    for request_id in gate_failed_requests:
        issues.append(
            {
                "severity": "error",
                "code": "gate_failed",
                "message": f"{request_id} failed completion_gate (a recorded command did not exit 0)",
            }
        )
    for error in gate_load_errors:
        issues.append(
            {
                "severity": "error",
                "code": "gate_malformed_evidence",
                "message": f"{error.get('source', '(unknown)')}: {error.get('reason', 'malformed evidence')}",
            }
        )

    # Memory-cache drift (invariant 5). This is fail-open: every finding is a
    # WARNING and it deliberately does NOT feed handoff_ready, auto_chain_ready,
    # or readiness_reasons below. A stale/malformed decision never blocks a
    # handoff; the operator re-reads the live sources instead. An absent
    # decisions.jsonl yields zero warnings and never an error.
    drift = check_decision_drift(loop_dir)
    warnings.extend(drift["warnings"])
    decisions_summary = drift["decisions"]

    handoff_ready = not missing_files and not lane_file_missing and not owner_issues
    auto_chain_ready = (
        handoff_ready
        and auto_chain_enabled
        and bool(unchecked)
        and not blocked
        and not stale_markers
        and not budget_exhausted
        and not thrash_requests
    )

    readiness_reasons: list[str] = []
    if missing_files:
        readiness_reasons.append("loop files missing")
    if lane_file_missing:
        readiness_reasons.append("lane files missing")
    if owner_issues:
        readiness_reasons.append("request owner is not registered")
    if not unchecked:
        readiness_reasons.append("no unchecked tracker item")
    if blocked:
        readiness_reasons.append("tracker has blocked items")
    if stale_markers:
        readiness_reasons.append("stale thread markers present")
    if not auto_chain_enabled:
        readiness_reasons.append("auto_chain_next_session is not true")
    if budget_exhausted:
        readiness_reasons.append("budget is exhausted")
    if thrash_requests:
        readiness_reasons.append("a request exceeded max_fix_cycles")

    return {
        "loop_dir": str(loop_dir),
        "ok": not issues,
        "missing_files": missing_files,
        "tracker": {
            "total_checkboxes": len(checkboxes),
            "unchecked": len(unchecked),
            "blocked": len(blocked),
            "next_unchecked": unchecked[0]["text"] if unchecked else None,
        },
        "lanes": lane_summaries,
        "lane_file_missing": lane_file_missing,
        "orphan_suspects": orphan_suspects,
        "stale_heartbeat_mins": stale_heartbeat_mins,
        "requests": {
            "total": len(request_summaries),
            "non_terminal": non_terminal_requests,
            "owner_issues": owner_issues,
            "thrash": thrash_requests,
            "missing_evidence": missing_evidence_requests,
        },
        "max_fix_cycles": max_fix_cycles,
        "budget": {
            "present": budget_present,
            "exhausted": budget_exhausted,
        },
        "run_log_present": run_log_present,
        "evidence_dir_present": evidence_dir_present,
        "evidence_recorded_ok": evidence_recorded_ok,
        "gate_available": gate_available,
        "completion_gate_ok": completion_gate_ok,
        "gate_failed_requests": gate_failed_requests,
        "decisions": decisions_summary,
        "decisions_helper_available": DECISIONS_HELPER_AVAILABLE,
        "auto_chain_enabled": auto_chain_enabled,
        "thread_ids": sorted(set(THREAD_ID_RE.findall(all_loop_text))),
        "stale_markers": stale_markers,
        "handoff_ready": handoff_ready,
        "auto_chain_ready": auto_chain_ready,
        "readiness_reasons": readiness_reasons,
        "issues": issues,
        "warnings": warnings,
    }


def print_text(result: dict[str, Any]) -> None:
    print(f"Loop dir: {result['loop_dir']}")
    print(f"OK: {result['ok']}")
    print(f"Handoff ready: {result['handoff_ready']}")
    print(f"Auto-chain enabled: {result['auto_chain_enabled']}")
    print(f"Auto-chain ready: {result['auto_chain_ready']}")
    print(f"Evidence recorded OK: {result['evidence_recorded_ok']}")
    gate_available = result.get("gate_available", False)
    if gate_available:
        print(f"Completion gate OK: {result['completion_gate_ok']}")
    else:
        print(f"Completion gate OK: {result['completion_gate_ok']} (gate unavailable)")
    gate_failed = result.get("gate_failed_requests", [])
    if gate_failed:
        print(f"Gate failed requests: {', '.join(gate_failed)}")
    decisions = result.get("decisions") or {}
    if decisions.get("total"):
        print(
            "Decisions: total={total} active={active} stale={stale} malformed={malformed}".format(
                total=decisions.get("total", 0),
                active=decisions.get("active", 0),
                stale=decisions.get("stale", 0),
                malformed=decisions.get("malformed", 0),
            )
            + (" (advisory only; never blocks handoff)" if decisions.get("stale") else "")
        )
    print(f"Next unchecked: {result['tracker']['next_unchecked'] or '(none)'}")

    budget = result["budget"]
    budget_state = "exhausted" if budget["exhausted"] else ("ok" if budget["present"] else "absent")
    print(f"Budget: {budget_state}")
    print(f"Run log present: {result['run_log_present']}")
    print(f"Evidence dir present: {result['evidence_dir_present']}")
    print(f"Max fix cycles: {result['max_fix_cycles']}")

    print("\nLanes:")
    if result["lanes"]:
        for lane in result["lanes"]:
            flags = []
            if lane["orphan_suspect"]:
                flags.append(f"orphan-suspect {lane['heartbeat_age_mins']}min")
            suffix = f" [{', '.join(flags)}]" if flags else ""
            print(f"- {lane['lane']}: {lane['status']} ({lane['thread_id'] or 'no thread'}){suffix}")
    else:
        print("- (none)")

    if result["orphan_suspects"]:
        print(
            f"\nOrphan-suspect lanes (heartbeat > {result['stale_heartbeat_mins']} min): "
            + ", ".join(result["orphan_suspects"])
        )

    non_terminal = result["requests"]["non_terminal"]
    print("\nOpen requests:")
    if non_terminal:
        for request in non_terminal:
            evidence_flag = "" if request["has_evidence"] else " NO-EVIDENCE"
            print(
                f"- {request['request_id']}: {request['status']} "
                f"owner={request['owner_lane'] or '(none)'} "
                f"fix_cycles={request['fix_cycles']} "
                f"next={request['next_action'] or '(none)'}{evidence_flag}"
            )
    else:
        print("- (none)")

    if result["requests"]["thrash"]:
        print("\nFix-cycle thrash:")
        for thrash in result["requests"]["thrash"]:
            print(
                f"- {thrash['request_id']}: {thrash['fix_cycles']} cycles "
                f"(max {thrash['max_fix_cycles']})"
            )

    if result["issues"]:
        print("\nIssues:")
        for issue in result["issues"]:
            print(f"- [{issue['code']}] {issue['message']}")
    if result["warnings"]:
        print("\nWarnings:")
        for warning in result["warnings"]:
            print(f"- [{warning['code']}] {warning['message']}")
    if result["readiness_reasons"]:
        print("\nReadiness blockers:")
        for reason in result["readiness_reasons"]:
            print(f"- {reason}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop-dir", default="docs/loop", help="Loop directory to inspect.")
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    parser.add_argument(
        "--stale-heartbeat-mins",
        type=int,
        default=DEFAULT_STALE_HEARTBEAT_MINS,
        help="Flag lanes whose heartbeat is older than this many minutes as orphan-suspect.",
    )
    args = parser.parse_args()

    result = summarize(Path(args.loop_dir), stale_heartbeat_mins=args.stale_heartbeat_mins)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_text(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
