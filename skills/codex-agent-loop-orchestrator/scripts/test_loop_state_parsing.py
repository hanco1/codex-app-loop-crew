#!/usr/bin/env python3
"""Contract test: the doctor path and the dashboard path parse identically.

``loop_state_parsing`` is the ONE canonical no-I/O body for the loop's state
grammar. This test drives every fixture through THREE entry points -- the
shared module directly, the doctor's file-reading ``parse_table`` wrapper, and
the dashboard's ``_parse_md_table_text`` wrapper -- and asserts byte-identical
rows plus matching error firings, so the historical doctor/dashboard drift
(max_fix_cycles regex, preface-table shadowing) can never reopen silently.

Plain asserts, stdlib only, no pytest dependency. Run directly:

    python test_loop_state_parsing.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import loop_dashboard  # noqa: E402
import loop_state_parsing as lsp  # noqa: E402
import multi_agent_loop_doctor as doctor  # noqa: E402

REQUIRED = ("request_id", "status", "iteration")


def run_both(
    tmp: Path, text: str, required: tuple[str, ...] = REQUIRED
) -> tuple[list[dict[str, str]], list[dict], list[dict[str, str]], list[dict[str, str]]]:
    """Run one fixture through the shared, doctor, and dashboard entry points.

    Asserts the three row lists are byte-identical (JSON) and that all three
    report the same NUMBER of structural errors, then returns
    ``(rows, shared_errors, doctor_errors, dashboard_errors)`` so callers can
    assert per-shape details.
    """
    fixture = tmp / "requests.md"
    fixture.write_text(text, encoding="utf-8")

    shared_rows, shared_errors = lsp.parse_md_table(text, "requests.md", required)
    doctor_rows, doctor_errors = doctor.parse_table(fixture, required_headers=required)
    dash_rows, dash_errors = loop_dashboard._parse_md_table_text(
        text, "requests.md", required
    )

    blobs = [json.dumps(rows, sort_keys=True) for rows in (shared_rows, doctor_rows, dash_rows)]
    assert blobs[0] == blobs[1] == blobs[2], (
        "rows must be byte-identical across shared/doctor/dashboard paths:\n"
        "shared:    {0}\ndoctor:    {1}\ndashboard: {2}".format(*blobs)
    )
    assert len(shared_errors) == len(doctor_errors) == len(dash_errors), (
        "error counts must match across paths: shared={0} doctor={1} dashboard={2}".format(
            shared_errors, doctor_errors, dash_errors
        )
    )
    # Shape contract: each consumer keeps its historical error envelope.
    for entry in doctor_errors:
        assert entry["severity"] == "error" and entry["code"] == "malformed_table", entry
        assert entry["message"].startswith("requests.md"), entry
    for entry in dash_errors:
        assert set(entry) == {"source", "reason"} and entry["source"] == "requests.md", entry
    return shared_rows, shared_errors, doctor_errors, dash_errors


def check_well_formed(tmp: Path) -> None:
    text = (
        "# Requests\n\n"
        "| request_id | status | iteration |\n"
        "| --- | --- | --- |\n"
        "| REQ-1 | REQUESTED | 1 |\n"
    )
    rows, errors, _, _ = run_both(tmp, text)
    assert errors == [], errors
    assert rows == [{"request_id": "REQ-1", "status": "REQUESTED", "iteration": "1"}], rows


def check_literal_pipe_in_cell(tmp: Path) -> None:
    # A literal (escaped) pipe splits the cell in BOTH programs today; the
    # contract locks that shared behavior: extra cells -> a cell-count error,
    # the row truncated to the header width, identical on every path.
    text = (
        "| request_id | status | iteration |\n"
        "| --- | --- | --- |\n"
        "| REQ-A | REQUESTED | note \\| detail |\n"
    )
    rows, errors, doctor_errors, dash_errors = run_both(tmp, text)
    assert rows == [{"request_id": "REQ-A", "status": "REQUESTED", "iteration": "note \\"}], rows
    assert [e["code"] for e in errors] == ["row_cell_count"], errors
    assert "row has 4 cells; expected 3" in doctor_errors[0]["message"], doctor_errors
    assert dash_errors[0]["reason"] == "line 3: table row has 4 cells; expected 3", dash_errors


def check_preface_table_skipped(tmp: Path) -> None:
    # A legitimate explanatory table BEFORE the control table must be skipped
    # by the required-header anchoring -- the real rows are returned, they do
    # not vanish, and no error fires.
    text = (
        "# Requests\n\n"
        "| label | meaning |\n"
        "| --- | --- |\n"
        "| REQ-doc | how ids are minted |\n\n"
        "| request_id | status | iteration |\n"
        "| --- | --- | --- |\n"
        "| REQ-1 | IMPLEMENTING | 2 |\n"
        "| REQ-2 | BLOCKED | 1 |\n"
    )
    rows, errors, _, _ = run_both(tmp, text)
    assert errors == [], errors
    assert [row["request_id"] for row in rows] == ["REQ-1", "REQ-2"], rows
    assert rows[0]["status"] == "IMPLEMENTING" and rows[1]["status"] == "BLOCKED", rows


def check_missing_required_column(tmp: Path) -> None:
    # A table missing a required column is never accepted as the control
    # table: a structural error is returned, not silently mis-keyed rows.
    text = (
        "| request_id | owner |\n"
        "| --- | --- |\n"
        "| REQ-1 | product |\n"
    )
    rows, errors, doctor_errors, dash_errors = run_both(tmp, text)
    assert rows == [], rows
    assert [e["code"] for e in errors] == ["no_header"], errors
    expected_cols = "iteration, request_id, status"
    assert "no table header row containing {0} found".format(expected_cols) in doctor_errors[0]["message"], doctor_errors
    assert expected_cols in dash_errors[0]["reason"], dash_errors


def check_header_without_delimiter(tmp: Path) -> None:
    # Half-written file: the header is the last table row (no delimiter, no
    # data). Must error, never read as an empty-but-healthy queue.
    text = "| request_id | status | iteration |\n"
    rows, errors, doctor_errors, _ = run_both(tmp, text)
    assert rows == [], rows
    assert [e["code"] for e in errors] == ["no_delimiter"], errors
    assert "table header has no delimiter row" in doctor_errors[0]["message"], doctor_errors


def check_row_with_wrong_cell_count(tmp: Path) -> None:
    # Half-written data row: fewer cells than headers -> an error AND the
    # padded row stays visible (partial data is shown while readiness blocks).
    text = (
        "| request_id | status | iteration |\n"
        "| --- | --- | --- |\n"
        "| REQ-1 | REQUESTED |\n"
    )
    rows, errors, _, dash_errors = run_both(tmp, text)
    assert rows == [{"request_id": "REQ-1", "status": "REQUESTED", "iteration": ""}], rows
    assert [e["code"] for e in errors] == ["row_cell_count"], errors
    assert dash_errors[0]["reason"] == "line 3: table row has 2 cells; expected 3", dash_errors


def check_delimiter_borrowed_from_later_table(tmp: Path) -> None:
    # The delimiter must IMMEDIATELY follow the header. A later table's
    # delimiter must not retroactively "complete" a torn control table -- this
    # is the case the dashboard's old parser silently accepted.
    text = (
        "| request_id | status | iteration |\n"
        "| REQ-1 | REQUESTED | 1 |\n"
        "| --- | --- | --- |\n"
        "| REQ-2 | REQUESTED | 1 |\n"
    )
    rows, errors, doctor_errors, dash_errors = run_both(tmp, text)
    assert [row["request_id"] for row in rows] == ["REQ-1", "REQ-2"], rows
    assert [e["code"] for e in errors] == ["data_before_delimiter"], errors
    assert "is not followed by a delimiter row" in doctor_errors[0]["message"], doctor_errors
    assert "is not followed by a delimiter row" in dash_errors[0]["reason"], dash_errors


def check_dashboard_legacy_no_table_reason() -> None:
    # Regression guard for the dashboard's historical required-less reason.
    rows, errors = loop_dashboard._parse_md_table_text("just prose\n", "notes.md")
    assert rows == [] and len(errors) == 1, (rows, errors)
    assert errors[0] == {"source": "notes.md", "reason": "no Markdown table found"}, errors


def check_max_fix_cycles(tmp: Path) -> None:
    # '- max_fix_cycles: 1' (list-marker form) and 'max_fix_cycles: 1' must
    # both read 1 from every entry point -- the exact drift that once made the
    # doctor silently fall back to 3 while the dashboard read 1.
    for text in ("- max_fix_cycles: 1\n", "max_fix_cycles: 1\n"):
        assert lsp.read_max_fix_cycles(text) == 1, text
        assert doctor.read_max_fix_cycles(text) == 1, text
        dash = loop_dashboard.read_max_fix_cycles(tmp, text=text, source_present=True)
        assert dash["max_fix_cycles"] == 1 and dash["source_present"] is True, dash
    # Absent line -> the shared default, everywhere.
    assert (
        lsp.read_max_fix_cycles("")
        == doctor.read_max_fix_cycles("")
        == loop_dashboard.read_max_fix_cycles(tmp, text="", source_present=False)["max_fix_cycles"]
        == lsp.DEFAULT_MAX_FIX_CYCLES
    )
    # The strict diagnostics stay list-marker tolerant too: a well-formed
    # marker line is NOT malformed_policy.
    assert lsp.diagnose_policy("- max_fix_cycles: 1\n", True) == []
    assert doctor.diagnose_policy("- max_fix_cycles: 1\n", True) == []


def check_timestamps() -> None:
    expected = datetime(2026, 7, 18, 9, 30, 0, tzinfo=timezone.utc)
    forms = (
        "2026-07-18T09:30:00Z",  # trailing Z
        "2026-07-18 09:30:00",  # space separator
        "2026-07-18T09:30:00",  # naive -> assumed UTC
        "2026-07-18T11:30:00+02:00",  # offset -> normalized to UTC
    )
    for value in forms:
        shared = lsp.parse_timestamp(value)
        via_doctor = doctor.parse_timestamp(value)
        via_dashboard = loop_dashboard._parse_timestamp(value)
        assert shared == via_doctor == via_dashboard == expected, (
            value,
            shared,
            via_doctor,
            via_dashboard,
        )
        assert shared.tzinfo is not None and shared.utcoffset().total_seconds() == 0
    for blank in ("", "-", "TBD", "definitely-not-a-time"):
        assert lsp.parse_timestamp(blank) is None, blank
        assert doctor.parse_timestamp(blank) is None, blank
        assert loop_dashboard._parse_timestamp(blank) is None, blank


def check_status_vocabulary() -> None:
    assert "ABANDONED" in lsp.TERMINAL_REQUEST_STATUSES
    assert "ACCEPTED" in lsp.TERMINAL_REQUEST_STATUSES
    assert "BLOCKED" in lsp.PAUSED_REQUEST_STATUSES
    # Both consumers carry the SAME vocabulary objects/values.
    assert doctor.TERMINAL_REQUEST_STATUSES == lsp.TERMINAL_REQUEST_STATUSES
    assert doctor.PAUSED_REQUEST_STATUSES == lsp.PAUSED_REQUEST_STATUSES
    assert lsp.TERMINAL_REQUEST_STATUSES <= loop_dashboard._INACTIVE_REQUEST_STATUSES
    # BLOCKED is a pause, not an end: it must stay ACTIVE for display.
    assert "BLOCKED" not in loop_dashboard._INACTIVE_REQUEST_STATUSES
    assert loop_dashboard._request_is_active({"status": "ABANDONED"}) is False
    assert loop_dashboard._request_is_active({"status": "BLOCKED"}) is True


def main() -> int:
    checks_with_tmp = (
        check_well_formed,
        check_literal_pipe_in_cell,
        check_preface_table_skipped,
        check_missing_required_column,
        check_header_without_delimiter,
        check_row_with_wrong_cell_count,
        check_delimiter_borrowed_from_later_table,
        check_max_fix_cycles,
    )
    checks_plain = (
        check_dashboard_legacy_no_table_reason,
        check_timestamps,
        check_status_vocabulary,
    )
    ran = 0
    with tempfile.TemporaryDirectory(prefix="loop-state-parsing-") as tmp_name:
        tmp = Path(tmp_name)
        for check in checks_with_tmp:
            check(tmp)
            ran += 1
            print("ok - {0}".format(check.__name__))
    for check in checks_plain:
        check()
        ran += 1
        print("ok - {0}".format(check.__name__))
    print("PASS test_loop_state_parsing: {0} checks".format(ran))
    return 0


if __name__ == "__main__":
    sys.exit(main())
