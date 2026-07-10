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
      "ran_at": "2026-06-23T11:00:00Z"
    }

The four string fields must be non-empty. ``ran_at`` must be a valid
timezone-aware ISO-8601 timestamp.

Exit codes:
    0  SHIP_CHECK_OK   -- gate passed for the scoped request(s)
    1  SHIP_CHECK_FAIL -- a record failed, is malformed, or required evidence is missing
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REQUIRED_FIELDS = ("request_id", "checkpoint", "command", "exit_code", "ran_at")
REQUIRED_STRING_FIELDS = ("request_id", "checkpoint", "command", "ran_at")


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
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            load_errors.append({"source": source, "reason": "unreadable: {0}".format(exc)})
            continue
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            load_errors.append({"source": source, "reason": "invalid JSON: {0}".format(exc)})
            continue
        if not isinstance(data, dict):
            load_errors.append({"source": source, "reason": "not a JSON object"})
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in data]
        if missing:
            load_errors.append(
                {"source": source, "reason": "missing fields: {0}".format(", ".join(missing))}
            )
            continue
        empty_or_non_string = [
            field
            for field in REQUIRED_STRING_FIELDS
            if not isinstance(data.get(field), str) or not data.get(field, "").strip()
        ]
        if empty_or_non_string:
            load_errors.append(
                {
                    "source": source,
                    "reason": "empty or non-string fields: {0}".format(
                        ", ".join(empty_or_non_string)
                    ),
                }
            )
            continue
        if not valid_timestamp(data.get("ran_at")):
            load_errors.append(
                {"source": source, "reason": "ran_at is not a valid timezone-aware ISO timestamp"}
            )
            continue
        record = dict(data)
        record["_source"] = source
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
    load_errors: List[Dict[str, str]],
    request_id: Optional[str],
    records_by_request: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Evaluate the gate for a single request or all requests.

    A record fails when its exit_code is not an int equal to 0, or when it
    cannot be coerced to a trustworthy int. Malformed evidence files (in
    ``load_errors``) are always failures. When ``request_id`` is given, only
    records and errors for that request are scoped; missing evidence for a
    requested id is itself a failure.
    """
    # A malformed file cannot be parsed, so its request_id field is unreadable.
    # Fail closed: every unparseable evidence file is treated as in-scope for any
    # request, because the gate cannot prove the file is not part of this request.
    scoped_errors: List[Dict[str, str]] = list(load_errors)

    if request_id is None:
        scoped_records = list(records)
    elif records_by_request is not None:
        scoped_records = list(records_by_request.get(request_id, []))
    else:
        scoped_records = [rec for rec in records if str(rec.get("request_id", "")).strip() == request_id]

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
        reasons.append("no evidence records found for request {0}".format(request_id))
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
    parser.add_argument("--json", action="store_true", help="Print JSON instead of the text token.")
    args = parser.parse_args()

    evidence_dir = Path(args.loop_dir) / "evidence"
    records, load_errors = load_evidence(evidence_dir)
    request_id = args.request_id.strip() if args.request_id else None
    result = evaluate(records, load_errors, request_id or None)
    result["evidence_dir"] = posix_path(evidence_dir)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_text(result)

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
