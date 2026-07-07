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


# Marker the pre-commit hook installer writes so we can recognize a hook this
# skill owns. Kept in sync with install_precommit.py's HOOK_MARKER by value; a
# drift here only weakens the advisory hook_installed field, never a gate.
HOOK_MARKER = "# codex-agent-loop-orchestrator:lease-precommit"


def find_git_dir(start: Path) -> Optional[Path]:
    """Walk upward from ``start`` looking for a git dir, stdlib-only.

    Returns the resolved ``.git`` directory (or the dir a ``.git`` file points
    at, for worktrees/submodules) when found, else None. Never shells out to
    git: this box's sandbox cannot reliably run subprocesses, and the doctor is
    read-only.
    """
    try:
        current = start.resolve()
    except OSError:
        current = start
    for candidate in [current, *current.parents]:
        dotgit = candidate / ".git"
        if dotgit.is_dir():
            return dotgit
        if dotgit.is_file():
            # A ``.git`` FILE (worktree/submodule) points at the real gitdir.
            try:
                text = dotgit.read_text(encoding="utf-8")
            except OSError:
                return dotgit
            for line in text.splitlines():
                line = line.strip()
                if line.lower().startswith("gitdir:"):
                    pointer = line.split(":", 1)[1].strip()
                    resolved = Path(pointer)
                    if not resolved.is_absolute():
                        resolved = (candidate / pointer).resolve()
                    return resolved
            return dotgit
    return None


def detect_git_health(loop_dir: Path) -> dict[str, Any]:
    """Report whether the loop is under git and whether the scope guard is armed.

    ``git_present``: a git dir was found at or above ``loop_dir``.
    ``hook_installed``: a ``hooks/pre-commit`` exists and carries this skill's
    ``HOOK_MARKER`` (so an unrelated hand-written pre-commit hook does not read
    as armed). Both are advisory health fields; neither is a gate.
    """
    git_dir = find_git_dir(loop_dir)
    git_present = git_dir is not None
    hook_installed = False
    if git_dir is not None:
        hook_path = git_dir / "hooks" / "pre-commit"
        if hook_path.is_file():
            try:
                hook_installed = HOOK_MARKER in hook_path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                hook_installed = False
    return {"git_present": git_present, "hook_installed": hook_installed}


def _pending_inbox_count(loop_dir: Path, lane: str) -> int:
    """Count undelivered messages in ``lane``'s Maildir inbox (inbox/new/*.md).

    A file in ``new`` is a message the receiving lane has not yet processed. If
    the lane has no worker (no verified thread), those messages are stuck. A
    missing inbox tree returns 0.
    """
    new_dir = loop_dir / "lanes" / lane / "inbox" / "new"
    if not new_dir.is_dir():
        return 0
    return sum(1 for path in new_dir.glob("*.md") if path.is_file())


def classify_missing_dependency_blocker(loop_dir: Path, request_id: str) -> Optional[dict[str, Any]]:
    """Parse a missing-dependency marker out of a request's BLOCKED message.

    A missing-dependency blocker is written into the durable BLOCKED message
    under ``messages/<request_id>/`` with a greppable, flat marker (documented in
    references/protocol.md "Missing-Dependency Blocker"):

        blocker: missing_dependency
        dependency: pip | pytesseract | pip install pytesseract
        dependency: system | tesseract | choco install tesseract

    Each ``dependency:`` line is ``kind | name | install-command`` (``|``
    separated); ``kind`` is ``pip`` (pip-installable package) or ``system`` (a
    system binary needing an installer/choco). This function returns a dict with
    the parsed dependencies, or None when no missing-dependency marker is found.
    Only the newest BLOCKED-*.md is read (highest iteration wins by name sort).
    """
    request_id = (request_id or "").strip()
    if not request_id:
        return None
    msg_dir = loop_dir / "messages" / request_id
    if not msg_dir.is_dir():
        return None
    blocked_files = sorted(
        (p for p in msg_dir.glob("BLOCKED*") if p.is_file()),
        key=lambda p: p.name,
    )
    if not blocked_files:
        return None
    text = ""
    for path in reversed(blocked_files):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "blocker: missing_dependency" in text:
            break
    else:
        return None
    if "blocker: missing_dependency" not in text:
        return None

    deps: list[dict[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("dependency:"):
            continue
        payload = stripped.split(":", 1)[1].strip()
        parts = [p.strip() for p in payload.split("|")]
        kind = parts[0].lower() if parts else ""
        name = parts[1] if len(parts) > 1 else ""
        install = parts[2] if len(parts) > 2 else ""
        if kind not in ("pip", "system"):
            kind = "system"  # fail safe: unknown kind is treated as a system binary
        deps.append({"kind": kind, "name": name, "install": install})
    return {
        "request_id": request_id,
        "dependencies": deps,
        "has_pip": any(d["kind"] == "pip" for d in deps),
        "has_system": any(d["kind"] == "system" for d in deps),
    }


def _has_archived_review_done(loop_dir: Path, request_id: str) -> bool:
    """True if a REVIEW_DONE message is archived for ``request_id``.

    The durable message store lives at ``messages/<request_id>/``; a
    ``REVIEW_DONE-*.md`` there means review passed the request even if the
    request row never advanced. Used only for the F10 stall heuristic; a missing
    directory just returns False.
    """
    request_id = (request_id or "").strip()
    if not request_id:
        return False
    msg_dir = loop_dir / "messages" / request_id
    if not msg_dir.is_dir():
        return False
    for path in msg_dir.glob("REVIEW_DONE*"):
        if path.is_file():
            return True
    return False


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

    # git health (F4): is the loop under version control, and is the write-scope
    # pre-commit guard armed? Both are advisory health fields, never gates. A
    # missing repo means write_scope/leases degrade to the honor system, so it
    # is a WARNING (invariant 1 wants version control) but never blocks handoff.
    git_health = detect_git_health(loop_dir)
    git_present = git_health["git_present"]
    hook_installed = git_health["hook_installed"]

    # F7 mandatory heartbeats: a lane that OWNS an active (non-terminal) request
    # must report a heartbeat. This is distinct from orphan_suspect (which fires
    # for any registered lane whose heartbeat is stale regardless of whether it
    # owns work) and distinct from the dashboard's display-only staleness. Here
    # the trigger is narrow and protocol-level: an active non-terminal request
    # whose owner lane has a MISSING heartbeat, or one older than the stale
    # window. Build a lane -> heartbeat-state lookup, then scan the owners.
    lane_by_name = {ls["lane"]: ls for ls in lane_summaries}
    heartbeat_gap_owners: list[dict[str, str]] = []
    seen_owner_lanes: set[str] = set()
    for request in non_terminal_requests:
        owner = (request.get("owner_lane") or "").strip()
        if not owner or owner in seen_owner_lanes:
            continue
        lane_info = lane_by_name.get(owner)
        if lane_info is None:
            # An unregistered owner is already an owner_issue error; skip here.
            continue
        raw_hb = (lane_info.get("heartbeat") or "").strip()
        age = lane_info.get("heartbeat_age_mins")
        missing = not raw_hb or raw_hb.upper() in EMPTY_CELL_VALUES or age is None
        stale = age is not None and age > stale_heartbeat_mins
        if missing or stale:
            seen_owner_lanes.add(owner)
            heartbeat_gap_owners.append(
                {
                    "lane": owner,
                    "request_id": request["request_id"],
                    "reason": "missing" if missing else "stale",
                    "age_mins": age,
                }
            )

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
    gate_passing_requests: set[str] = set()
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
            else:
                # This request's own evidence all exits 0: it is SHIP_CHECK_OK.
                gate_passing_requests.add(request_id)
    completion_gate_ok = (
        gate_available and not gate_failed_requests and not gate_load_errors
    )

    # F10 stalled_handoff: a request still parked in a pre-acceptance,
    # non-terminal state (REQUESTED / IMPLEMENTING / REVIEWING) whose WORK is
    # demonstrably done -- either its own evidence already reports SHIP_CHECK_OK,
    # or an archived REVIEW_DONE message exists for it -- but no forward
    # transition happened. This is the systemic cross-thread stall: a lane
    # finished (even got the gate green) then its turn ended without sending the
    # reply, updating requests.md, or appending the run-log row, so the requester
    # waits forever. It is a WARNING that names the lane + request as a genuine
    # your-turn nudge; it never blocks handoff.
    stalled_handoff_requests: list[dict[str, str]] = []
    pre_acceptance_states = {"REQUESTED", "IMPLEMENTING", "REVIEWING"}
    for request in non_terminal_requests:
        status = request["status"]
        if status not in pre_acceptance_states:
            continue
        request_id = request["request_id"]
        done_by_gate = request_id in gate_passing_requests
        done_by_review = _has_archived_review_done(loop_dir, request_id)
        if done_by_gate or done_by_review:
            stalled_handoff_requests.append(
                {
                    "request_id": request_id,
                    "lane": (request.get("owner_lane") or "").strip() or "(unassigned)",
                    "status": status,
                    "evidence": "SHIP_CHECK_OK" if done_by_gate else "archived REVIEW_DONE",
                }
            )

    # F11 workerless_lane_dependency: a lane with NO verified thread (status
    # needs-thread / unverified) that nonetheless has work waiting on it -- it
    # owns a non-terminal request, or messages are stuck in its inbox/new. With
    # create_thread absent (a real mid-run host regression), such a lane has no
    # worker to process the dispatched request, so the requester waits forever.
    # This is a WARNING, and ESCALATES to an ERROR when another lane's active
    # request depends on the loop advancing (i.e. some OTHER lane is actively
    # working a non-terminal request): then the deadlock stalls live work, not
    # just an idle branch.
    workerless_lanes = {
        ls["lane"] for ls in lane_summaries if ls["status"] == "needs-thread"
    }
    # Does any OTHER (verified) lane own an active non-terminal request?
    verified_active_owners = {
        (req.get("owner_lane") or "").strip()
        for req in non_terminal_requests
        if (req.get("owner_lane") or "").strip()
        and (req.get("owner_lane") or "").strip() not in workerless_lanes
    }
    workerless_dependencies: list[dict[str, Any]] = []
    for lane in sorted(workerless_lanes):
        owns_nonterminal = [
            req["request_id"]
            for req in non_terminal_requests
            if (req.get("owner_lane") or "").strip() == lane
        ]
        pending_inbox = _pending_inbox_count(loop_dir, lane)
        if not owns_nonterminal and pending_inbox == 0:
            # No thread but also no waiting work: that is just unverified_lane_thread.
            continue
        escalate = bool(verified_active_owners)
        workerless_dependencies.append(
            {
                "lane": lane,
                "requests": owns_nonterminal,
                "pending_inbox": pending_inbox,
                "severity": "error" if escalate else "warning",
            }
        )

    # F16 missing-dependency blocker: a BLOCKED request whose durable BLOCKED
    # message carries the greppable ``blocker: missing_dependency`` marker is a
    # distinct blocker type with a built-in exit ramp (record what is missing +
    # exact install commands, ask the human for one-line approval, install,
    # re-run the failed verify, unblock the SAME request_id). The doctor
    # classifies it and surfaces the install commands so the dashboard/human can
    # act with zero hops, instead of rendering a generic red dead-end.
    missing_dependency_blockers: list[dict[str, Any]] = []
    for request in request_summaries:
        if request["status"] != "BLOCKED":
            continue
        classified = classify_missing_dependency_blocker(loop_dir, request["request_id"])
        if classified is not None:
            missing_dependency_blockers.append(classified)

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
    if not git_present:
        warnings.append(
            {
                "severity": "warning",
                "code": "git_absent",
                "message": "loop dir is not under git; write_scope/leases degrade to "
                "the honor system (run git init, then install_precommit.py)",
            }
        )
    elif not hook_installed:
        warnings.append(
            {
                "severity": "warning",
                "code": "hook_absent",
                "message": "git repo present but the write_scope pre-commit guard is not "
                "installed (run install_precommit.py)",
            }
        )
    for owner in heartbeat_gap_owners:
        if owner["reason"] == "missing":
            detail = "has no heartbeat"
        else:
            detail = "heartbeat is {0} min old (> {1})".format(owner["age_mins"], stale_heartbeat_mins)
        warnings.append(
            {
                "severity": "warning",
                "code": "stale_heartbeat_active_owner",
                "message": "lane {lane} owns active request {req} but {detail}; "
                "refresh the heartbeat in the in-turn ritual".format(
                    lane=owner["lane"], req=owner["request_id"], detail=detail
                ),
            }
        )
    for stalled in stalled_handoff_requests:
        warnings.append(
            {
                "severity": "warning",
                "code": "stalled_handoff",
                "message": "request {req} is still {status} but its work is done "
                "({evidence}); nudge lane {lane} to send the reply, advance "
                "requests.md, and append the run-log row".format(
                    req=stalled["request_id"],
                    status=stalled["status"],
                    evidence=stalled["evidence"],
                    lane=stalled["lane"],
                ),
            }
        )
    for dep in workerless_dependencies:
        waiting = []
        if dep["requests"]:
            waiting.append("owns " + ", ".join(dep["requests"]))
        if dep["pending_inbox"]:
            waiting.append("{0} message(s) stuck in inbox/new".format(dep["pending_inbox"]))
        detail = "; ".join(waiting) if waiting else "work waiting"
        message = (
            "lane {lane} has a pending dispatched request but no verified thread "
            "({detail}); open a Codex thread for it and adopt it "
            "(bootstrap_agent_loop.py --set-thread {lane}=<thread_id>)".format(
                lane=dep["lane"], detail=detail
            )
        )
        entry = {"severity": dep["severity"], "code": "workerless_lane_dependency", "message": message}
        if dep["severity"] == "error":
            issues.append(entry)
        else:
            warnings.append(entry)
    for blocker in missing_dependency_blockers:
        cmds = "; ".join(
            "{0} [{1}]".format(dep["install"], dep["kind"]) for dep in blocker["dependencies"] if dep["install"]
        )
        warnings.append(
            {
                "severity": "warning",
                "code": "missing_dependency",
                "message": "request {req} is BLOCKED on a missing dependency (has an "
                "install-and-retry exit ramp): {cmds}".format(
                    req=blocker["request_id"], cmds=cmds or "(no install command recorded)"
                ),
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
        "heartbeat_gap_owners": heartbeat_gap_owners,
        "stalled_handoffs": stalled_handoff_requests,
        "workerless_dependencies": workerless_dependencies,
        "missing_dependency_blockers": missing_dependency_blockers,
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
        "git_present": git_present,
        "hook_installed": hook_installed,
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
    print(f"Git present: {result.get('git_present', False)}")
    print(f"Scope-guard hook installed: {result.get('hook_installed', False)}")
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
