#!/usr/bin/env python3
"""The ONE canonical, no-I/O parser for the loop's durable state grammar.

``multi_agent_loop_doctor`` (the read-only gate) and ``loop_dashboard`` (the
read-only view) used to carry private copies of the same parsing primitives,
and the copies drifted: the doctor's ``max_fix_cycles`` regex once rejected the
list-marker form the dashboard accepted, and the dashboard's table parser
accepted a preface table that the doctor's header-anchored parser skips. Every
function in this module is PURE TEXT -> value (no filesystem access, no clock,
no subprocess), so both programs -- and the contract test
``test_loop_state_parsing.py`` -- exercise the exact same body and cannot drift
again.

Behavior contract (preserved from the doctor's hardened implementations):

- ``parse_md_table`` anchors the control-table header on ``required_headers``
  (a preface/explanatory table can never shadow the real control table),
  requires the delimiter row to IMMEDIATELY follow the header (a later table's
  delimiter never retroactively "completes" a torn table), reports per-row
  cell-count mismatches, and returns ``(rows, errors)`` where each error is a
  NEUTRAL structured dict (``{source, code, line, ...}``). Callers render
  those errors into their own shapes via ``doctor_table_error`` /
  ``dashboard_table_error`` so each program's existing output contract is
  unchanged while the parsing semantics stay single-bodied.
- ``parse_timestamp`` handles a trailing ``Z``, a space date/time separator,
  strptime fallbacks, and assumes UTC for naive values.
- ``read_max_fix_cycles`` accepts the ``- max_fix_cycles: N`` list-marker form
  and the bare form alike (the exact doctor/dashboard drift this module exists
  to kill); ``diagnose_policy`` keeps the strict-line WARNING diagnostics.
- ``TERMINAL_REQUEST_STATUSES`` / ``PAUSED_REQUEST_STATUSES`` are the single
  protocol vocabulary consumed by both programs' gating and display
  predicates.

Stdlib only, Python 3.9+, importable with zero side effects.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Protocol vocabulary (single source for doctor gating + dashboard display)
# ---------------------------------------------------------------------------

# ACCEPTED and ABANDONED end a request's lifecycle; BLOCKED is a human-gate
# pause (normal waiting, not terminal). references/protocol.md G31.
TERMINAL_REQUEST_STATUSES = {"ACCEPTED", "ABANDONED"}
PAUSED_REQUEST_STATUSES = {"BLOCKED"}

# Cell values that mean "nothing here" in a control-table or timestamp cell.
EMPTY_CELL_VALUES = {"", "-", "TBD", "NONE", "NULL", "N/A", "NA"}

# Default anti-thrash cap when loop-policy.md is absent or carries no line
# (mirrors the bootstrap template default and protocol.md's documented default).
DEFAULT_MAX_FIX_CYCLES = 3

# The tolerant READER regex: first match anywhere in the policy text, accepting
# an optional list marker (``- max_fix_cycles: 1``). The ``\b`` tail keeps the
# doctor's historical tolerance of trailing prose after the integer; the strict
# line diagnostics below flag such lines without changing the read value.
MAX_FIX_CYCLES_RE = re.compile(r"(?im)^\s*(?:[-*]\s*)?max_fix_cycles\s*:\s*(\d+)\b")

# Strict LINE regexes used by policy diagnostics and by the dashboard's
# in-place policy rewrite. POLICY_KEY_RE spots a line that is TRYING to be the
# setting; POLICY_LINE_RE accepts only an exactly-well-formed line. The named
# groups let the dashboard rewrite the value in place while preserving the
# list marker prefix.
POLICY_KEY_RE = re.compile(r"^\s*(?:[-*]\s*)?max_fix_cycles\s*:", re.IGNORECASE)
POLICY_LINE_RE = re.compile(
    r"^(?P<prefix>\s*(?:[-*]\s*)?)(?P<key>max_fix_cycles)\s*:\s*(?P<value>\d+)\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Row / cell primitives
# ---------------------------------------------------------------------------


def split_md_row(line: str) -> list[str]:
    """Split one ``| a | b |`` Markdown table line into stripped cells."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def status_key(value: str) -> str:
    """Normalize a status cell into the canonical UPPER_SNAKE token."""
    return value.strip().upper().replace(" ", "_").replace("-", "_")


# ---------------------------------------------------------------------------
# Control-table parsing (the shared grammar)
# ---------------------------------------------------------------------------


def parse_md_table(
    text: str,
    source_name: str,
    required_headers: tuple[str, ...] = (),
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Parse the control table in ``text``, anchored on ``required_headers``.

    A candidate header row is accepted only when its cell set contains every
    required header, so a preface/explanatory table earlier in the file can
    never shadow the real control table (whose rows would then be coerced into
    the wrong columns and silently dropped). A delimiter row legitimizes the
    table only as the row IMMEDIATELY after its header; a later table's
    delimiter must not retroactively "complete" a torn control table.

    Returns ``(rows, errors)``. Rows keep partial data visible (short rows are
    padded, long rows truncated to the header width) while every structural
    violation is reported as a NEUTRAL error dict::

        {"source": source_name, "code": <code>, "line": <int>, ...}

    with codes ``data_before_delimiter`` (data row directly after the header),
    ``row_cell_count`` (+ ``found``/``expected``), ``no_header``
    (+ ``required``), and ``no_delimiter`` (header was the last table row).
    Render per-consumer shapes with ``doctor_table_error`` /
    ``dashboard_table_error``.
    """
    required = set(required_headers)
    headers: Optional[list[str]] = None
    header_line = 0
    awaiting_delimiter = False
    rows: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.lstrip().startswith("|"):
            continue
        cells = split_md_row(line)
        if not cells:
            continue
        if all(set(cell) <= {"-", ":", " "} for cell in cells):
            # A delimiter legitimizes the control table only as the row
            # IMMEDIATELY after its header.
            if awaiting_delimiter:
                awaiting_delimiter = False
            continue
        if headers is None:
            candidate = [cell.strip() for cell in cells]
            if required and not required <= set(candidate):
                continue  # a preface table, not the control table: keep looking
            headers = candidate
            header_line = lineno
            awaiting_delimiter = True
            continue
        if awaiting_delimiter:
            # The row right after the header is data, not a delimiter: the
            # control table is torn/half-written. Report once; keep collecting
            # rows so partial data stays visible while the error blocks
            # readiness.
            errors.append(
                {
                    "source": source_name,
                    "code": "data_before_delimiter",
                    "line": header_line,
                }
            )
            awaiting_delimiter = False
        if len(cells) != len(headers):
            errors.append(
                {
                    "source": source_name,
                    "code": "row_cell_count",
                    "line": lineno,
                    "found": len(cells),
                    "expected": len(headers),
                }
            )
        if len(cells) < len(headers):
            cells = cells + [""] * (len(headers) - len(cells))
        rows.append(dict(zip(headers, cells[: len(headers)])))
    if headers is None:
        errors.append(
            {
                "source": source_name,
                "code": "no_header",
                "line": 0,
                "required": sorted(required),
            }
        )
    elif awaiting_delimiter:
        # The header was the last table row in the file: no delimiter, no data.
        errors.append(
            {
                "source": source_name,
                "code": "no_delimiter",
                "line": header_line,
            }
        )
    return rows, errors


def doctor_table_error(error: dict[str, Any]) -> dict[str, str]:
    """Render a neutral parse error into the doctor's ``issues`` entry shape.

    Byte-preserves the message strings the doctor emitted before this module
    existed, so golden fixtures and human readers see no change.
    """
    code = error.get("code", "")
    source = str(error.get("source", ""))
    line = error.get("line", 0)
    if code == "data_before_delimiter":
        message = (
            "{0} line {1}: table header is not followed by a "
            "delimiter row (torn or half-written table)".format(source, line)
        )
    elif code == "row_cell_count":
        message = "{0} line {1}: row has {2} cells; expected {3}".format(
            source, line, error.get("found"), error.get("expected")
        )
    elif code == "no_header":
        required = [str(name) for name in (error.get("required") or [])]
        message = (
            "{0}: no table header row containing {1} found; the file "
            "is empty, torn, or its control table is shadowed -- rows cannot "
            "be read".format(source, ", ".join(required) or "the expected columns")
        )
    elif code == "no_delimiter":
        message = (
            "{0} line {1}: table header has no delimiter row (torn or "
            "half-written table)".format(source, line)
        )
    else:  # pragma: no cover - future-proofing; no other codes exist today
        message = "{0}: malformed table".format(source)
    return {"severity": "error", "code": "malformed_table", "message": message}


def dashboard_table_error(error: dict[str, Any]) -> dict[str, str]:
    """Render a neutral parse error into the dashboard's ``{source, reason}``.

    Keeps the dashboard's historical reason strings where the same condition
    existed before ("table row has N cells", "table header has no delimiter
    row", "no Markdown table found"); the conditions the dashboard gained from
    the doctor's stricter grammar (torn table, anchored header) reuse the same
    plain phrasing without the doctor's file-name prefix.
    """
    code = error.get("code", "")
    line = error.get("line", 0)
    if code == "row_cell_count":
        reason = "line {0}: table row has {1} cells; expected {2}".format(
            line, error.get("found"), error.get("expected")
        )
    elif code == "no_delimiter":
        reason = "line {0}: table header has no delimiter row".format(line)
    elif code == "data_before_delimiter":
        reason = "line {0}: table header is not followed by a delimiter row".format(
            line
        )
    elif code == "no_header":
        required = [str(name) for name in (error.get("required") or [])]
        if required:
            reason = (
                "no table header row containing {0} found (the table is "
                "missing, torn, or shadowed by another table)".format(
                    ", ".join(required)
                )
            )
        else:
            reason = "no Markdown table found"
    else:  # pragma: no cover - future-proofing; no other codes exist today
        reason = "malformed table"
    return {"source": str(error.get("source", "")), "reason": reason}


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def parse_timestamp(value: str) -> Optional[datetime]:
    """Parse an ISO-8601-ish timestamp into an aware UTC datetime.

    Handles a trailing ``Z`` (which ``datetime.fromisoformat`` rejects on
    Python 3.8-3.10), a space separator between date and time, and naive
    timestamps (assumed UTC). Returns ``None`` for blank or unparseable
    values.
    """
    text = (value or "").strip()
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


# ---------------------------------------------------------------------------
# loop-policy.md: the max_fix_cycles anti-thrash cap
# ---------------------------------------------------------------------------


def read_max_fix_cycles(policy_text: str) -> int:
    """Read the cap with the tolerant reader; fall back to the default."""
    match = MAX_FIX_CYCLES_RE.search(policy_text)
    if not match:
        return DEFAULT_MAX_FIX_CYCLES
    try:
        return int(match.group(1))
    except ValueError:
        return DEFAULT_MAX_FIX_CYCLES


def diagnose_policy(policy_text: str, source_present: bool) -> list[dict[str, str]]:
    """Warn on malformed max_fix_cycles while preserving the tolerant reader."""
    if not source_present:
        return []
    for lineno, line in enumerate(policy_text.splitlines(), start=1):
        if not POLICY_KEY_RE.match(line):
            continue
        match = POLICY_LINE_RE.match(line)
        if match is None:
            return [
                {
                    "severity": "warning",
                    "code": "malformed_policy",
                    "message": "loop-policy.md line {0}: max_fix_cycles is not an integer; "
                    "using the existing fallback {1}".format(lineno, DEFAULT_MAX_FIX_CYCLES),
                }
            ]
        value = int(match.group("value"))
        if value < 1 or value > 10:
            return [
                {
                    "severity": "warning",
                    "code": "malformed_policy",
                    "message": "loop-policy.md line {0}: max_fix_cycles {1} is outside 1..10; "
                    "reader behavior is unchanged".format(lineno, value),
                }
            ]
        return []
    return [
        {
            "severity": "warning",
            "code": "malformed_policy",
            "message": "loop-policy.md line 1: max_fix_cycles is missing; using the existing "
            "fallback {0}".format(DEFAULT_MAX_FIX_CYCLES),
        }
    ]
