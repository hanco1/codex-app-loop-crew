#!/usr/bin/env python3
"""Deterministic completion gate for repo-local multi-agent Codex loops.

Scan ``docs/loop/evidence/*.json`` verification records and emit the
deterministic token ``SHIP_CHECK_OK <request_id>`` only when every record for a
request reports ``exit_code == 0``. Otherwise print ``SHIP_CHECK_FAIL`` with the
failing records and exit non-zero.

This helper is read-only and stdlib-only. It exists so that a request may move
to ACCEPTED (and auto-chain may proceed) only on machine-checked evidence, not
on a self-report that verification "passed". Completion cannot be hallucinated.

Each evidence file is a JSON object:

    {
      "request_id": "REQ-20260623-101500-implementation",
      "checkpoint": "mvp-color-match",
      "command": "npm test",
      "exit_code": 0,
      "ran_at": "2026-06-23T11:00:00Z",
      "started_at": "2026-06-23T10:59:57Z",
      "finished_at": "2026-06-23T11:00:00Z",
      "result": "PASS"
    }

The existing five fields remain required. The four required string fields must
be non-empty, and ``ran_at`` must be a valid timezone-aware ISO-8601 timestamp.
``started_at`` and ``finished_at`` are OPTIONAL per-command execution-window
timestamps, and ``result`` is OPTIONAL result metadata. Records that omit these
optional fields remain valid. They are additive metadata: this gate already
tolerates extra fields and does not add a new failure mode for an older record.

Exit codes:
    0  SHIP_CHECK_OK   -- gate passed for the scoped request(s)
    1  SHIP_CHECK_FAIL -- a record failed, is malformed, or required evidence is missing
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REQUIRED_FIELDS = ("request_id", "checkpoint", "command", "exit_code", "ran_at")
REQUIRED_STRING_FIELDS = ("request_id", "checkpoint", "command", "ran_at")

# Keep these in sync with multi_agent_loop_doctor.py EVIDENCE_REQID_RE /
# EVIDENCE_ITERATION_RE. Evidence files are named
# ``REQ-YYYYMMDD-HHMMSS-<lane>-iter-<n>-<slug>.json``; the request_id is stable
# across fix cycles while the iteration increments, so both must be read from
# the filename to scope the gate to the request's CURRENT iteration.
EVIDENCE_REQID_RE = re.compile(
    r"^(?P<request_id>REQ-\d{8}-\d{6}-[A-Za-z0-9][A-Za-z0-9-]*?)-iter-\d+-.+$"
)
EVIDENCE_ITERATION_RE = re.compile(r"(?i)-iter-(?P<iteration>\d+)-")


def _name_attribution(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return (request_id, iteration) parsed from an evidence filename."""
    name = path.name
    match = EVIDENCE_REQID_RE.match(name)
    name_req = match.group("request_id") if match else None
    iter_match = EVIDENCE_ITERATION_RE.search(name)
    name_iter = iter_match.group("iteration") if iter_match else None
    return name_req, name_iter


def _norm_iter(value: Any) -> Optional[str]:
    """Normalize an iteration value so '01' and '1' compare equal."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return str(int(text)) if text.isdigit() else text


def _iter_num(value: Any) -> Optional[int]:
    text = _norm_iter(value)
    if text is not None and text.isdigit():
        return int(text)
    return None


def _split_row(line: str) -> List[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def read_current_iteration(loop_dir: Path, request_id: str) -> Optional[str]:
    """Return the normalized ``iteration`` for ``request_id`` from requests.md.

    Anchors on the first markdown table whose header carries both
    ``request_id`` and ``iteration`` columns (so an explanatory preface table
    cannot be mistaken for the request ledger). Returns None when requests.md is
    missing/unreadable or the request row is absent.
    """
    requests_path = Path(loop_dir) / "requests.md"
    try:
        text = requests_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    headers: Optional[List[str]] = None
    req_idx: Optional[int] = None
    it_idx: Optional[int] = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = _split_row(stripped)
        if all(set(cell) <= set("-: ") for cell in cells if cell):
            continue  # delimiter row
        if headers is None:
            if "request_id" in cells and "iteration" in cells:
                headers = cells
                req_idx = cells.index("request_id")
                it_idx = cells.index("iteration")
            continue
        if req_idx is None or it_idx is None or len(cells) <= max(req_idx, it_idx):
            continue
        if cells[req_idx] == request_id:
            return _norm_iter(cells[it_idx])
    return None


def posix_path(path: Path) -> str:
    return str(path).replace("\\", "/")


def coerce_exit_code(value: Any) -> Optional[int]:
    """Return the exit code as an int, or None when it cannot be trusted.

    Accept ints and clean integer strings only. Bools, floats, and anything
    else are treated as untrustworthy so a malformed record never passes the
    gate.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            return None
    return None


def valid_timestamp(value: Any) -> bool:
    """Return True for a non-empty ISO-8601 timestamp with a timezone."""
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def load_evidence(evidence_dir: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Load every evidence file under ``evidence_dir``.

    Returns a tuple of (records, load_errors). Each record carries the parsed
    JSON plus an injected ``_source`` field with the posix path. ``load_errors``
    holds files that could not be parsed or were missing required fields; these
    are always treated as failures.
    """
    records: List[Dict[str, Any]] = []
    load_errors: List[Dict[str, str]] = []

    if not evidence_dir.exists():
        return records, load_errors

    for path in sorted(evidence_dir.glob("*.json")):
        source = posix_path(path)
        name_req, name_iter = _name_attribution(path)

        def err(reason: str) -> Dict[str, Any]:
            return {
                "source": source,
                "reason": reason,
                "request_id": name_req,
                "iteration": name_iter,
            }

        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            load_errors.append(err("unreadable: {0}".format(exc)))
            continue
        try:
            data = json.loads(raw)
        except ValueError as exc:
            load_errors.append(err("invalid JSON: {0}".format(exc)))
            continue
        if not isinstance(data, dict):
            load_errors.append(err("not a JSON object"))
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in data]
        if missing:
            load_errors.append(err("missing fields: {0}".format(", ".join(missing))))
            continue
        empty_or_non_string = [
            field
            for field in REQUIRED_STRING_FIELDS
            if not isinstance(data.get(field), str) or not data.get(field, "").strip()
        ]
        if empty_or_non_string:
            load_errors.append(
                err("empty or non-string fields: {0}".format(", ".join(empty_or_non_string)))
            )
            continue
        if not valid_timestamp(data.get("ran_at")):
            load_errors.append(err("ran_at is not a valid timezone-aware ISO timestamp"))
            continue
        if (
            "iteration" in data
            and name_iter is not None
            and _norm_iter(data.get("iteration")) != name_iter
        ):
            load_errors.append(err("iteration field disagrees with filename"))
            continue
        record = dict(data)
        record["_source"] = source
        record["_iteration"] = name_iter
        record["_name_request_id"] = name_req
        records.append(record)

    return records, load_errors


def index_records(
    records: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Index pre-parsed evidence by stripped request_id, preserving order."""
    records_by_request: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        request_id = str(record.get("request_id", "")).strip()
        if request_id:
            records_by_request.setdefault(request_id, []).append(record)
    return records_by_request


def evaluate(
    records: List[Dict[str, Any]],
    load_errors: List[Dict[str, Any]],
    request_id: Optional[str],
    *,
    current_iteration: Optional[str] = None,
    records_by_request: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Evaluate the gate for a single request or all requests.

    A record fails when its exit_code is not an int equal to 0, or when it
    cannot be coerced to a trustworthy int. When ``request_id`` is given, only
    records for that request's CURRENT iteration are considered: stale
    prior-iteration evidence is ignored, and missing evidence for the current
    iteration is a failure. Malformed evidence files (in ``load_errors``) are
    failures, scoped to the affected request+iteration when the filename
    attributes them and otherwise treated as globally in-scope (fail closed).
    In all-request mode each request is judged on its newest iteration only.
    """
    norm_current = _norm_iter(current_iteration)

    # Newest iteration seen per request (used by all-request mode).
    max_iter: Dict[str, Optional[int]] = {}
    for rec in records:
        rid = str(rec.get("request_id", "")).strip()
        if not rid:
            continue
        num = _iter_num(rec.get("_iteration"))
        prev = max_iter.get(rid)
        if num is not None and (prev is None or num > prev):
            max_iter[rid] = num

    if request_id is None:
        scoped_records = []
        for rec in records:
            rid = str(rec.get("request_id", "")).strip()
            if not rid:
                continue
            top = max_iter.get(rid)
            num = _iter_num(rec.get("_iteration"))
            if top is None or num is None or num == top:
                scoped_records.append(rec)
    elif norm_current is None:
        return {
            "ok": False,
            "request_id": request_id,
            "request_ids": [],
            "passing": [],
            "failing": [],
            "load_errors": [],
            "reasons": [
                "cannot determine current iteration for request {0} "
                "(requests.md row/iteration missing) -- pass --iteration to "
                "override".format(request_id)
            ],
            "total_records": 0,
        }
    else:
        group = (
            list(records_by_request.get(request_id, []))
            if records_by_request is not None
            else [r for r in records if str(r.get("request_id", "")).strip() == request_id]
        )
        scoped_records = [r for r in group if _norm_iter(r.get("_iteration")) == norm_current]

    # Scope malformed-file errors by the request_id/iteration parsed from the
    # filename. An error the gate cannot attribute (no REQ- prefix) stays
    # globally in-scope so it fails closed.
    scoped_errors: List[Dict[str, Any]] = []
    for error in load_errors:
        e_req = error.get("request_id")
        e_iter = _norm_iter(error.get("iteration"))
        if request_id is None:
            if e_req is None or e_iter is None:
                scoped_errors.append(error)
            else:
                top = max_iter.get(e_req)
                e_num = _iter_num(e_iter)
                if top is None or e_num is None or e_num >= top:
                    scoped_errors.append(error)
        else:
            if e_req is None:
                scoped_errors.append(error)
            elif e_req == request_id and (e_iter is None or e_iter == norm_current):
                scoped_errors.append(error)

    failing: List[Dict[str, Any]] = []
    passing: List[Dict[str, Any]] = []
    for rec in scoped_records:
        code = coerce_exit_code(rec.get("exit_code"))
        entry = {
            "request_id": str(rec.get("request_id", "")).strip(),
            "checkpoint": str(rec.get("checkpoint", "")).strip(),
            "command": str(rec.get("command", "")).strip(),
            "exit_code": rec.get("exit_code"),
            "ran_at": str(rec.get("ran_at", "")).strip(),
            "source": rec.get("_source", ""),
        }
        if code == 0:
            passing.append(entry)
        else:
            entry["reason"] = (
                "exit_code not 0"
                if code is not None
                else "exit_code is not a valid integer"
            )
            failing.append(entry)

    request_ids = sorted({entry["request_id"] for entry in (passing + failing) if entry["request_id"]})

    # Determine pass/fail.
    reasons: List[str] = []
    if scoped_errors:
        reasons.append("malformed or missing-field evidence files present")
    if failing:
        reasons.append("one or more evidence records did not exit 0")
    if request_id is not None and not scoped_records and not scoped_errors:
        reasons.append(
            "no evidence records found for request {0} at iteration {1}".format(
                request_id, norm_current
            )
        )
    if request_id is None and not scoped_records and not scoped_errors:
        reasons.append("no evidence records found")

    ok = not reasons

    return {
        "ok": ok,
        "request_id": request_id,
        "request_ids": request_ids,
        "passing": passing,
        "failing": failing,
        "load_errors": scoped_errors,
        "reasons": reasons,
        "total_records": len(scoped_records),
    }


def print_text(result: Dict[str, Any]) -> None:
    if result["ok"]:
        scope = result["request_id"]
        if scope is not None:
            print("SHIP_CHECK_OK {0}".format(scope))
        else:
            ids = result["request_ids"]
            if ids:
                for request_id in ids:
                    print("SHIP_CHECK_OK {0}".format(request_id))
            else:
                # ok with no records cannot happen (guarded in evaluate), but be safe.
                print("SHIP_CHECK_OK")
        return

    print("SHIP_CHECK_FAIL")
    for reason in result["reasons"]:
        print("- reason: {0}".format(reason))
    if result["failing"]:
        print("- failing records:")
        for entry in result["failing"]:
            print(
                "  - {request_id} | checkpoint={checkpoint} | command={command} | "
                "exit_code={exit_code} | {reason} | {source}".format(
                    request_id=entry["request_id"] or "(none)",
                    checkpoint=entry["checkpoint"] or "(none)",
                    command=entry["command"] or "(none)",
                    exit_code=entry["exit_code"],
                    reason=entry.get("reason", ""),
                    source=entry["source"],
                )
            )
    if result["load_errors"]:
        print("- malformed evidence files:")
        for error in result["load_errors"]:
            print("  - {source}: {reason}".format(source=error["source"], reason=error["reason"]))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print SHIP_CHECK_OK only when every evidence record for a request exited 0."
    )
    parser.add_argument("--loop-dir", default="docs/loop", help="Loop directory to inspect.")
    parser.add_argument(
        "--request-id",
        default=None,
        help="Scope the gate to one request_id. Omit to evaluate every request found.",
    )
    parser.add_argument(
        "--iteration",
        default=None,
        help="Override the current iteration for --request-id (else read from requests.md).",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of the text token.")
    args = parser.parse_args()

    evidence_dir = Path(args.loop_dir) / "evidence"
    records, load_errors = load_evidence(evidence_dir)

    if args.request_id is not None:
        request_id = args.request_id.strip()
        if not request_id:
            parser.error(
                "--request-id was provided but is empty; pass a real request id "
                "or omit the flag to evaluate every request"
            )
    else:
        request_id = None

    current_iteration: Optional[str] = None
    if request_id is not None:
        if args.iteration is not None and args.iteration.strip():
            current_iteration = args.iteration.strip()
        else:
            current_iteration = read_current_iteration(Path(args.loop_dir), request_id)

    result = evaluate(
        records,
        load_errors,
        request_id,
        current_iteration=current_iteration,
    )
    result["evidence_dir"] = posix_path(evidence_dir)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_text(result)

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
