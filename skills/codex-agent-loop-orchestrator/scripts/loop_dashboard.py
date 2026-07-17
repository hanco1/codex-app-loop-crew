#!/usr/bin/env python3
"""Read-only local dashboard for a repo-local multi-agent Codex loop.

A tiny stdlib-only web server that renders the loop's durable files as a live
page. It is a VIEW over ``docs/loop`` and nothing more: agents never read it,
deleting it does not affect the loop, and it holds no state of its own.

Endpoints (only five, everything else is 404/405):

- ``GET  /``           serves ``dashboard.html`` (the file next to this script);
- ``GET  /api/state``  returns a JSON snapshot assembled by READING files only:
  the ``agent-lanes.md`` registry (with computed heartbeat freshness), the
  ``requests.md`` queue, each lane's ``current.md`` text + a ``worklog.md`` tail
  + a workspace file listing, the ``loop-run-log.md`` tail, every
  ``evidence/*.json`` record, the doctor result via an IN-PROCESS import of
  ``multi_agent_loop_doctor`` (guarded; degrades gracefully), a decisions
  summary when available, the current ``max_fix_cycles`` policy value, and a
  Codex rate-limit ``usage`` snapshot read from the newest session JSONL. The
  ``usage`` object also carries a scoped ``account`` identity parsed from
  ``auth.json`` (email/name/plan/auth_mode/short-id only -- never tokens). An
  optional ``?refresh=1`` query drops the in-memory usage/account caches and
  rescans before responding; it is still read-only (no writes) and NOT a new
  endpoint -- normal 2s polling omits it and rides the caches;
- ``POST /api/lanes``  a write. Body ``{"lane": ..., "role": ...}``.
  Validates the lane name (lowercase kebab, not already registered, not a
  reserved name), appends a registry row with ``status=needs-thread`` and a
  default write_scope, and creates the lane directory + files by REUSING
  ``bootstrap_agent_loop`` in-process. The registry write is atomic (temp file
  then ``os.replace``). Returns ``{"ok": true, ...}`` or an error object;
- ``POST /api/policy`` a write. Body ``{"max_fix_cycles": <int 1..10>}``.
  Validates the integer, updates the ``max_fix_cycles`` line in
  ``loop-policy.md`` atomically (preserving the rest of the file), and returns
  ``{"ok": true, "max_fix_cycles": ...}`` or a 400 with a reason. This is a
  HUMAN control that bounds fix-cycle token burn; agents read the policy file,
  never this API;
- ``POST /api/project`` a write. Body ``{"name": <str>}``. Validates the name
  (non-empty after trim, no control chars / line breaks, <= 80 chars) and
  writes it atomically into ``docs/loop/project.md`` -- a dedicated display-only
  file that documents its own convention. This is the THIRD AND FINAL write
  endpoint: the human project label shown in the masthead + browser tab title.

The server binds ``127.0.0.1`` only. The write endpoints are EXACTLY three
(``/api/lanes``, ``/api/policy``, ``/api/project``); no other endpoint writes
anything.

Codex host coupling lives in ONE place: the ``usage`` and ``account`` sections
are the only ones that read the Codex host's UNDOCUMENTED data surfaces (session
JSONL rate-limits, ``auth.json`` identity). That entire coupling has been
extracted into the standalone ``codex_host_probe`` module; this dashboard is
host-agnostic apart from a guarded import of it. If the probe module is missing,
``/api/state`` serves usage/account as ``available: false`` (reason
``probe_module_missing``) and never crashes -- the same graceful-degradation
pattern the doctor uses for its imports.

Usage panel privacy: the probe's ``usage`` provider reads the user's Codex
session JSONL files (PRIVATE conversation content) but extracts ONLY the
``rate_limits`` numbers, ``total_token_usage`` numbers, ``plan_type``, and the
event timestamp by walking the exact known JSON paths of a ``token_count``
event -- never a recursive scan -- so no message text, prompt, or conversation
file path can leak through ``/api/state``. Only the newest file's tail
(<= 256 KB) is read, and the parsed result is cached by (path, mtime, size).

Account identity privacy (SECURITY RED LINE): ``auth.json`` holds OAuth tokens
(id_token, access_token, refresh_token). The probe's account provider parses it
server-side and the ONLY fields permitted to leave the parser are ``email``,
``name``, ``plan_type``, ``auth_mode``, a TRUNCATED ``account_id_short`` (first
8 chars), and the auth.json mtime as ISO. No token string, no full JWT, no
id_token, no access/refresh token, and no full account_id may EVER appear in
``/api/state`` or in any log line. The JWT ``id_token`` payload is decoded with
base64url read-only (no signature verification, raw segments never returned) --
see ``codex_host_probe`` for the full implementation and red-line notes. The
parsed result is cached by (auth.json path, mtime, size).

Design constraints honored here:

- stdlib only (``http.server``, ``json``, ``argparse``, ``pathlib``, ``re``,
  ``datetime``, ``os``, ``socketserver``); the Codex host reads (session JSONL,
  auth.json) that once needed ``glob`` + ``threading`` here now live entirely in
  the ``codex_host_probe`` module, imported optionally;
- no subprocess: the doctor and bootstrap are imported in-process, matching the
  rest of this toolkit (this box's sandbox cannot shell out);
- every file read is guarded so a not-yet-bootstrapped loop renders a
  meaningful empty state instead of crashing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socketserver
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs

# Import the loop's own helpers in-process (never a subprocess). They live in
# scripts/ beside this file. Guard every import so a missing/partial toolkit
# degrades the dashboard gracefully instead of failing to start.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _safe_exception(exc: BaseException, limit: int = 300) -> str:
    """Return a one-line, length-bounded exception description for the UI."""
    detail = " ".join(str(exc).split()) or exc.__class__.__name__
    text = "{0}: {1}".format(exc.__class__.__name__, detail)
    return text[:limit]


def _warn_module_unavailable(name: str, reason: str) -> None:
    """Make optional-module startup degradation visible to process operators."""
    print(
        "warning: dashboard module unavailable: {0}: {1}".format(name, reason),
        file=sys.stderr,
    )


DOCTOR_IMPORT_ERROR = ""
try:
    import multi_agent_loop_doctor as doctor  # type: ignore

    DOCTOR_AVAILABLE = True
except Exception as exc:  # pragma: no cover - defensive; doctor import should succeed
    doctor = None  # type: ignore
    DOCTOR_AVAILABLE = False
    DOCTOR_IMPORT_ERROR = _safe_exception(exc)
    _warn_module_unavailable("multi_agent_loop_doctor", DOCTOR_IMPORT_ERROR)

BOOTSTRAP_IMPORT_ERROR = ""
try:
    import bootstrap_agent_loop  # type: ignore

    BOOTSTRAP_AVAILABLE = True
except Exception as exc:  # pragma: no cover - defensive; bootstrap import should succeed
    bootstrap_agent_loop = None  # type: ignore
    BOOTSTRAP_AVAILABLE = False
    BOOTSTRAP_IMPORT_ERROR = _safe_exception(exc)
    _warn_module_unavailable("bootstrap_agent_loop", BOOTSTRAP_IMPORT_ERROR)

LOOP_LOCK_AVAILABLE = False
try:
    from _loop_lock import atomic_replace as _lock_atomic_replace
    from _loop_lock import loop_file_lock

    LOOP_LOCK_AVAILABLE = True
except Exception as exc:  # pragma: no cover - defensive; stdlib-only module
    _lock_atomic_replace = None  # type: ignore
    loop_file_lock = None  # type: ignore
    _warn_module_unavailable("_loop_lock", _safe_exception(exc))

# The Codex host probe (usage/account) is the ONLY module that touches the
# Codex host's undocumented data surfaces (session JSONL rate-limits, auth.json
# identity). It is an OPTIONAL dependency: if it is missing, the dashboard stays
# a pure loop tool and serves usage/account as unavailable rather than crashing.
# Guard the import exactly like the doctor/bootstrap ones above.
PROBE_IMPORT_ERROR = ""
try:
    import codex_host_probe  # type: ignore

    PROBE_AVAILABLE = True
except Exception as exc:
    codex_host_probe = None  # type: ignore
    PROBE_AVAILABLE = False
    PROBE_IMPORT_ERROR = _safe_exception(exc)
    _warn_module_unavailable("codex_host_probe", PROBE_IMPORT_ERROR)


# ``build_usage`` must find the newest session by mtime, but walking every
# session path on every 2s dashboard poll is unnecessary. Keep only the chosen
# path for four seconds; after that short TTL the original full mtime scan is
# mandatory, so a newly-created session can never be pinned indefinitely. A
# manual refresh clears this path cache together with the probe's content cache.
SESSION_PATH_CACHE_TTL_SECONDS = 4.0
_SESSION_PATH_CACHE: dict[str, tuple[float, Optional[Path]]] = {}
_SESSION_PATH_CACHE_LOCK = threading.Lock()
_ORIGINAL_NEWEST_SESSION_FILE = (
    getattr(codex_host_probe, "_newest_session_file", None)
    if PROBE_AVAILABLE and codex_host_probe is not None
    else None
)
_ORIGINAL_PROBE_DROP_CACHES = (
    getattr(codex_host_probe, "drop_caches", None)
    if PROBE_AVAILABLE and codex_host_probe is not None
    else None
)


def _cached_newest_session_file(codex_home: Path) -> Optional[Path]:
    """Reuse newest-session discovery briefly, then revalidate by full mtime scan."""
    if _ORIGINAL_NEWEST_SESSION_FILE is None:
        return None
    key = str(codex_home)
    now = time.monotonic()
    with _SESSION_PATH_CACHE_LOCK:
        cached = _SESSION_PATH_CACHE.get(key)
        if cached is not None:
            checked_at, path = cached
            if now - checked_at < SESSION_PATH_CACHE_TTL_SECONDS:
                return path
        path = _ORIGINAL_NEWEST_SESSION_FILE(codex_home)
        _SESSION_PATH_CACHE[key] = (now, path)
        return path


def _drop_probe_and_session_path_caches() -> None:
    """Clear probe content caches and the G27 short-TTL path cache."""
    with _SESSION_PATH_CACHE_LOCK:
        _SESSION_PATH_CACHE.clear()
    if _ORIGINAL_PROBE_DROP_CACHES is not None:
        _ORIGINAL_PROBE_DROP_CACHES()


if PROBE_AVAILABLE and codex_host_probe is not None:
    if _ORIGINAL_NEWEST_SESSION_FILE is not None:
        codex_host_probe._newest_session_file = _cached_newest_session_file
    if _ORIGINAL_PROBE_DROP_CACHES is not None:
        codex_host_probe.drop_caches = _drop_probe_and_session_path_caches


# A dashboard poll is frequent and the state build can touch many loop files.
# Cache one immutable-by-convention snapshot per loop briefly. The per-key
# Event is the single-flight: waiters share the builder's result instead of
# starting duplicate doctor/evidence/file scans on ThreadingHTTPServer threads.
STATE_SNAPSHOT_CACHE_TTL_SECONDS = 0.75
_STATE_SNAPSHOT_CACHE: dict[
    str, tuple[float, str, tuple[tuple[str, int, int], ...], dict[str, Any]]
] = {}
_STATE_SNAPSHOT_FLIGHTS: dict[str, tuple[threading.Event, bool]] = {}
_STATE_SNAPSHOT_LOCK = threading.Lock()


def _state_snapshot_key(loop_dir: Path) -> str:
    try:
        return str(loop_dir.resolve())
    except OSError:
        return str(loop_dir.absolute())


def _clear_state_snapshot_cache(loop_dir: Optional[Path] = None) -> None:
    """Invalidate one loop snapshot, or all snapshots when no loop is given."""
    with _STATE_SNAPSHOT_LOCK:
        if loop_dir is None:
            _STATE_SNAPSHOT_CACHE.clear()
        else:
            _STATE_SNAPSHOT_CACHE.pop(_state_snapshot_key(loop_dir), None)


def _state_source_stamp(loop_dir: Path) -> tuple[tuple[str, int, int], ...]:
    """Return a cheap invalidation stamp without reading or hashing contents."""
    paths = list(loop_dir.glob("*.md"))
    paths.extend(loop_dir.glob("lanes/*/current.md"))
    paths.extend(loop_dir.glob("lanes/*/worklog.md"))
    paths.extend(loop_dir.glob("lanes/*/workspace"))
    paths.extend((loop_dir / "evidence", loop_dir / "memory" / "decisions.jsonl"))
    stamp: list[tuple[str, int, int]] = []
    for path in sorted(set(paths), key=lambda item: str(item)):
        try:
            stat = path.stat()
        except OSError:
            continue
        stamp.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(stamp)


def _get_state_snapshot(loop_dir: Path, refresh: bool = False) -> dict[str, Any]:
    """Return a short-lived state snapshot with per-loop single-flight builds."""
    key = _state_snapshot_key(loop_dir)
    codex_home = os.environ.get("CODEX_HOME", "")
    force_refresh = refresh
    while True:
        now = time.monotonic()
        source_stamp = _state_source_stamp(loop_dir)
        should_build = False
        with _STATE_SNAPSHOT_LOCK:
            cached = _STATE_SNAPSHOT_CACHE.get(key)
            if not force_refresh and cached is not None:
                built_at, cached_codex_home, cached_stamp, state = cached
                if (cached_codex_home == codex_home
                        and cached_stamp == source_stamp
                        and now - built_at < STATE_SNAPSHOT_CACHE_TTL_SECONDS):
                    return state

            flight = _STATE_SNAPSHOT_FLIGHTS.get(key)
            if flight is None:
                event = threading.Event()
                _STATE_SNAPSHOT_FLIGHTS[key] = (event, force_refresh)
                should_build = True
                flight_was_refresh = force_refresh
            else:
                event, flight_was_refresh = flight

        if not should_build:
            event.wait()
            # A refresh never accepts an ordinary build that happened to be in
            # progress first. It waits, then performs one forced rebuild. Two
            # refresh callers do share the same forced build.
            if force_refresh and not flight_was_refresh:
                continue
            force_refresh = False
            continue

        try:
            state = build_state(loop_dir, refresh=force_refresh)
        except Exception:
            with _STATE_SNAPSHOT_LOCK:
                _STATE_SNAPSHOT_FLIGHTS.pop(key, None)
                event.set()
            raise
        with _STATE_SNAPSHOT_LOCK:
            _STATE_SNAPSHOT_CACHE[key] = (
                time.monotonic(), codex_home, source_stamp, state
            )
            _STATE_SNAPSHOT_FLIGHTS.pop(key, None)
            event.set()
        return state


def _state_etag(state: dict[str, Any]) -> str:
    """Hash semantic state while excluding the rebuild timestamp heartbeat."""
    semantic = dict(state)
    semantic.pop("generated_at", None)
    encoded = json.dumps(
        semantic, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return '"{0}"'.format(hashlib.sha256(encoded).hexdigest())


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Lane names are validated against this exactly (mirrors the bootstrap/doctor
# convention): a lowercase letter, then 1..30 more of lowercase / digit / dash.
LANE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")

# POST body limits: the write endpoints carry tiny JSON payloads, so cap the
# body hard (defends against an accidental or hostile large/slow body tying up
# a request thread) and bound the read.
MAX_REQUEST_BODY_BYTES = 64 * 1024
BODY_READ_TIMEOUT_SECONDS = 10.0

# ``role`` is the only free-text cell a POST writes into the Markdown registry.
# Reject line breaks / control chars / ``|`` so a role can never forge a table
# row, and cap its length.
ROLE_MAX_CHARS = 200
_ROLE_BAD_CHARS_RE = re.compile(r"[\x00-\x1f\x7f|]")

# Names that must never be registered as lanes: they collide with loop control
# files / directories, or are otherwise structural and would confuse recovery
# tooling. Kept lowercase for a case-insensitive check.
RESERVED_LANE_NAMES = frozenset(
    {
        "lanes",
        "messages",
        "evidence",
        "memory",
        "workspace",
        "goal",
        "tracker",
        "constraints",
        "handoff",
        "requests",
        "loop-policy",
        "loop-budget",
        "loop-run-log",
        "leases",
        "agent-lanes",
    }
)

# How many trailing worklog rows and run-log rows to surface. Small on purpose:
# the dashboard is a glance, not an archive.
WORKLOG_TAIL = 15
REQUESTS_PAGE_SIZE = 50
EVIDENCE_PAGE_SIZE = 100
RUNLOG_PAGE_SIZE = 50
CURRENT_MAX_CHARS = 4000

_INACTIVE_REQUEST_STATUSES = frozenset(
    {"ACCEPTED", "CANCELED", "CANCELLED", "CLOSED", "DONE"}
)

# Registry columns, in the order bootstrap writes them. The trailing ``tier`` is
# the F8 advisory model-tier column; parsing here is header-driven so the exact
# order only matters for the shared render_registry writer in bootstrap.
REGISTRY_COLUMNS = ["lane", "thread_id", "role", "write_scope", "worklog", "status", "heartbeat", "tier"]

# A heartbeat older than this many minutes is flagged "stale" in the freshness
# label. This is display-only; the doctor keeps its own orphan-suspect logic.
STALE_HEARTBEAT_MINS = 30

# ---- Tracker progress (F14) ----
# A tracker checkbox line: "- [ ] text" / "- [x] ..." / "- [~] ..." / "- [!] ...".
# Mirrors the doctor's CHECKBOX_RE exactly so the two agree on what a checkpoint
# is. Only the "## Checkpoints" section is treated as the milestone list; the
# "Done When" / "Human QA" checkboxes below it are acceptance criteria, not
# milestones, and are excluded from the progress count.
_CHECKBOX_RE = re.compile(r"^\s*-\s+\[(?P<status>[ xX~!])\]\s+(?P<text>.+?)\s*$")
# Heading that opens the milestone list in tracker.md (case-insensitive; a
# trailing word is tolerated). Any later "## <other>" heading closes it.
_TRACKER_CHECKPOINTS_HEADING = "checkpoints"
# Max characters of a checkpoint's human title surfaced to the page. Titles are
# short by convention; this only guards against a pathological one-line essay.
CHECKPOINT_TITLE_MAX_CHARS = 200

# ---- Project name (F2) ----
# The project name is a HUMAN label shown in the masthead + browser <title>.
# It persists in a single dedicated loop file, ``project.md`` (chosen over a
# key in loop-policy.md so the write never risks disturbing the machine-read
# policy line). The file documents itself; the value is the first non-heading,
# non-blank, non-comment line. Read-only if absent -> the default (loop-dir's
# project-root folder name) is used and no file is written until a rename POST.
PROJECT_FILE_NAME = "project.md"
# Validation for a submitted project name: printable, trimmed, length-bounded,
# and free of control characters / line breaks (it lands in an HTML <title> and
# a one-line file, both of which a newline would corrupt).
PROJECT_NAME_MAX_CHARS = 80
_PROJECT_NAME_BAD_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
# Self-documenting header written above the value so the file explains itself
# (the plan requires the chosen file to document the convention in the file).
_PROJECT_FILE_TEMPLATE = (
    "# Project\n"
    "\n"
    "<!-- The human-facing project name shown in the loop dashboard masthead\n"
    "     and the browser tab title. The dashboard's POST /api/project control\n"
    "     rewrites the single value line below (atomic temp + os.replace). The\n"
    "     value is the first non-heading, non-blank, non-comment line. Agents do\n"
    "     not read this file; it is a display label only. -->\n"
    "\n"
    "{name}\n"
)

_DASHBOARD_HTML = Path(__file__).resolve().parent / "dashboard.html"

# Default max_fix_cycles when loop-policy.md is absent or has no line (mirrors
# the bootstrap template default and protocol.md's documented default).
DEFAULT_MAX_FIX_CYCLES = 3
# Valid inclusive range for the human-set fix-cycle cap.
MAX_FIX_CYCLES_MIN = 1
MAX_FIX_CYCLES_MAX = 10
# Matches the "max_fix_cycles: <int>" line in loop-policy.md. Tolerant of
# surrounding whitespace and an optional leading list marker, case-insensitive
# on the key; captures the integer so it can be read or rewritten in place.
_MAX_FIX_CYCLES_RE = re.compile(
    r"^(?P<prefix>\s*(?:[-*]\s*)?)(?P<key>max_fix_cycles)\s*:\s*(?P<value>\d+)\s*$",
    re.IGNORECASE,
)

# Test seam only: ``main`` stashes the bound server here so an in-process smoke
# can shut down a main() started in a background thread. The app never reads it.
_LAST_SERVER_FOR_TEST: Optional["_ThreadingHTTPServer"] = None


# ---------------------------------------------------------------------------
# File readers (all guarded: missing files -> empty, never an exception)
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _read_text_status(path: Path) -> tuple[str, str, str]:
    """Read UTF-8 text and distinguish missing, empty, and unreadable files."""
    if not path.exists():
        return "", "missing", ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return "", "unreadable", _safe_exception(exc)
    return text, ("empty" if not text.strip() else "ok"), ""


def _split_md_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _parse_md_table_text(
    text: str,
    source: str,
    required_headers: tuple[str, ...] = (),
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Parse a Markdown table while retaining structural parse diagnostics."""
    headers: Optional[list[str]] = None
    header_line = 0
    delimiter_seen = False
    rows: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.lstrip().startswith("|"):
            continue
        cells = _split_md_row(line)
        if not cells:
            continue
        if all(set(cell) <= {"-", ":", " "} for cell in cells):
            if headers is not None:
                delimiter_seen = True
            continue
        if headers is None:
            headers = [cell.strip() for cell in cells]
            header_line = lineno
            continue
        if len(cells) != len(headers):
            errors.append(
                {
                    "source": source,
                    "reason": "line {0}: table row has {1} cells; expected {2}".format(
                        lineno, len(cells), len(headers)
                    ),
                }
            )
        if len(cells) < len(headers):
            cells = cells + [""] * (len(headers) - len(cells))
        rows.append(dict(zip(headers, cells[: len(headers)])))

    if headers is None:
        errors.append({"source": source, "reason": "no Markdown table found"})
        return rows, errors
    if not delimiter_seen:
        errors.append(
            {
                "source": source,
                "reason": "line {0}: table header has no delimiter row".format(header_line),
            }
        )
    missing = [name for name in required_headers if name not in headers]
    if missing:
        errors.append(
            {
                "source": source,
                "reason": "table is missing required column(s): {0}".format(", ".join(missing)),
            }
        )
    return rows, errors


def _parse_md_table(path: Path) -> list[dict[str, str]]:
    """Parse the first Markdown table in ``path`` into a list of row dicts.

    Header cells become keys. A delimiter row (all dashes/colons) is skipped.
    Missing trailing cells are padded with ''. Returns [] if the file is
    absent or has no table.
    """
    text = _read_text(path)
    if not text:
        return []
    rows, _errors = _parse_md_table_text(text, str(path).replace("\\", "/"))
    return rows


def _parse_timestamp(value: str) -> Optional[datetime]:
    """Parse an ISO-8601-ish timestamp into an aware UTC datetime, or None.

    Handles a trailing ``Z`` and a space date/time separator, and assumes UTC
    for naive values. Returns None for blank or unparseable input.
    """
    text = (value or "").strip()
    if not text or text.upper() in {"", "-", "TBD", "NONE", "NULL", "N/A", "NA"}:
        return None
    candidate = text
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
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


def _heartbeat_freshness(raw: str, now: datetime) -> dict[str, Any]:
    """Compute a display-only freshness label for a heartbeat cell."""
    parsed = _parse_timestamp(raw)
    if parsed is None:
        return {"raw": raw.strip(), "age_mins": None, "state": "none"}
    age_mins = (now - parsed).total_seconds() / 60.0
    state = "stale" if age_mins > STALE_HEARTBEAT_MINS else "fresh"
    return {"raw": raw.strip(), "age_mins": round(age_mins, 1), "state": state}


def _tail(text: str, n: int) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-n:] if n > 0 else lines


def _run_log_tail_sorted(text: str, n: int) -> list[str]:
    """Return the run log's non-blank lines with DATA rows timestamp-ordered.

    G11(b): the run log is append-only, so a late-append recovery row can be out
    of chronological order. This keeps every NON-table line (the ``#`` heading,
    the header row, the ``---`` separator, prose) in its original position, and
    STABLY sorts only the pipe-delimited DATA rows by the first column that
    parses as a timestamp -- then returns the last ``n`` lines. Rows with a
    blank/unparseable timestamp keep their original relative order (they sort to
    the epoch). Falls back to the plain tail if no header/timestamp is found, so
    a non-standard log is never mangled.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if n <= 0:
        pass  # keep all, still sort below

    # Identify the table header to find the timestamp column index.
    header_cols: Optional[list[str]] = None
    ts_idx: Optional[int] = None
    data_positions: list[int] = []
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        cells = _split_md_row(line)
        if all(set(c) <= {"-", ":", " "} for c in cells):
            continue  # separator row
        if header_cols is None:
            header_cols = [c.strip().lower() for c in cells]
            for name in ("timestamp", "at", "delivered_at", "time"):
                if name in header_cols:
                    ts_idx = header_cols.index(name)
                    break
            continue
        data_positions.append(i)

    if ts_idx is None or not data_positions:
        # No recognizable timestamp column: preserve the existing behavior.
        return lines[-n:] if n > 0 else lines

    def _row_key(pos: int) -> tuple[Any, int]:
        cells = _split_md_row(lines[pos])
        raw = cells[ts_idx] if ts_idx < len(cells) else ""
        parsed = _parse_timestamp(raw)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return (parsed or epoch, pos)

    sorted_positions = sorted(data_positions, key=_row_key)
    # Rebuild the line list: non-data lines stay put; data slots are refilled in
    # timestamp order (stable). This keeps header/separator/prose intact.
    reordered = list(lines)
    for slot, src in zip(data_positions, sorted_positions):
        reordered[slot] = lines[src]
    return reordered[-n:] if n > 0 else reordered


def _lane_status_label(row: dict[str, str]) -> str:
    """Normalize a registry row's status into one of a few display buckets."""
    thread_id = (row.get("thread_id", "") or "").strip().upper()
    status = (row.get("status", "") or "").strip().lower()
    if status == "stale" or "stale" in status:
        return "stale"
    if thread_id in {"", "UNVERIFIED", "TBD", "NONE", "NULL", "-"} or status in {
        "needs-thread",
        "unverified",
    }:
        return "needs-thread"
    return "registered"


def _lane_workspace_files(lanes_dir: Path, lane: str) -> list[str]:
    """List files under a lane's workspace/, README excluded from the count."""
    workspace = lanes_dir / lane / "workspace"
    if not workspace.is_dir():
        return []
    names: list[str] = []
    try:
        for entry in sorted(workspace.iterdir()):
            if entry.is_file():
                names.append(entry.name)
    except OSError:
        return []
    return names


# current.md key/value header fields, mapped from their file key to the output
# key. These sit above the first "##" heading in the bootstrap CURRENT_TEMPLATE.
_CURRENT_HEADER_KEYS = {
    "current_request_id": "current_request_id",
    "status": "status",
    "iteration": "iteration",
    "last_updated": "last_updated",
    "heartbeat": "heartbeat",
    # G14(a): the lane's OBSERVED model+effort (data, e.g. "gpt-5.5 xhigh
    # (highest)"). Parsed so the lane card can show recommended vs observed.
    "model_observed": "model_observed",
}

# G14(c): pull the abstract tier TAG (highest / second-highest) out of a
# model_observed value like "gpt-5.5 xhigh (highest)" -- the trailing
# parenthetical. Mirrors the doctor's ``observed_tier_tag`` extraction.
_OBSERVED_TIER_RE = re.compile(r"\(([^)]+)\)\s*$")


def _observed_tier_from_value(value: str) -> str:
    """Return the abstract tier tag from a model_observed value, or ''."""
    value = (value or "").strip()
    if not value:
        return ""
    m = _OBSERVED_TIER_RE.search(value)
    tag = (m.group(1) if m else value).strip().lower()
    return tag if tag in ("highest", "second-highest") else ""
# Known "## <heading>" sections of current.md, mapped to the output list key.
# Matching is case-insensitive and tolerant of extra words in the heading.
_CURRENT_SECTION_KEYS = {
    "current checkpoint": "checkpoint_items",
    "next action": "next_action",
    "blockers": "blockers",
}
# A list item that means "nothing here"; filtered so an empty section reads as
# [] rather than ["None."]. Matches "none", "none.", "n/a", "-", "tbd".
_CURRENT_EMPTY_ITEM_RE = re.compile(r"^(none\.?|n/?a|-+|tbd)$", re.IGNORECASE)


def _parse_current_md(text: str) -> dict[str, Any]:
    """Parse a lane's current.md into structured fields (tolerantly).

    Returns a dict with string header fields (``current_request_id``,
    ``status``, ``iteration``, ``last_updated``, ``heartbeat``) and list
    fields (``checkpoint_items``, ``next_action``, ``blockers``). Missing or
    unrecognized input degrades to empty strings / empty lists; this never
    raises. Header key/value lines are read only until the first ``##`` heading,
    so a request-id-looking token buried in prose cannot be mistaken for one.
    """
    header: dict[str, str] = {out: "" for out in _CURRENT_HEADER_KEYS.values()}
    sections: dict[str, list[str]] = {out: [] for out in _CURRENT_SECTION_KEYS.values()}

    current_section: Optional[str] = None
    seen_heading = False
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("#"):
            # Count the heading level. The single-``#`` document title ("# X
            # Current State") must NOT close the header key/value block -- only
            # a ``##``+ section heading does. This lets the key/value lines that
            # sit between the title and the first section be read as header.
            level = len(stripped) - len(stripped.lstrip("#"))
            heading = stripped.lstrip("#").strip().lower()
            current_section = None
            if level >= 2:
                seen_heading = True
                # Normalize "## Current Checkpoint" -> "current checkpoint".
                for known, out_key in _CURRENT_SECTION_KEYS.items():
                    if heading == known or heading.startswith(known):
                        current_section = out_key
                        break
            continue
        if current_section is not None:
            # Collect list items (or any non-blank line) under a known section.
            item = re.sub(r"^\s*(?:[-*]|\d+\.)\s*", "", line).strip()
            if item and not _CURRENT_EMPTY_ITEM_RE.match(item):
                sections[current_section].append(item)
            continue
        if not seen_heading and ":" in stripped:
            key, _, value = stripped.partition(":")
            out_key = _CURRENT_HEADER_KEYS.get(key.strip().lower())
            if out_key is not None and not header[out_key]:
                header[out_key] = value.strip()

    result: dict[str, Any] = {}
    result.update(header)
    result.update(sections)
    return result


def _current_summary(lanes_dir: Path, lane: str) -> dict[str, Any]:
    """Return the lane's parsed current.md summary, raw text, and worklog tail.

    ``current`` keeps the raw (capped) current.md for the "View raw" details
    block; ``summary`` carries the structured fields parsed from it so the card
    can render a clean block without re-parsing on the client.
    """
    lane_dir = lanes_dir / lane
    raw_current = _read_text(lane_dir / "current.md")
    summary = _parse_current_md(raw_current)
    current_text = raw_current
    if len(current_text) > CURRENT_MAX_CHARS:
        current_text = current_text[:CURRENT_MAX_CHARS] + "\n... (truncated)"
    worklog_tail = _tail(_read_text(lane_dir / "worklog.md"), WORKLOG_TAIL)
    workspace_files = _lane_workspace_files(lanes_dir, lane)
    return {
        "current": current_text,
        "summary": summary,
        "worklog_tail": worklog_tail,
        "workspace_files": workspace_files,
        "workspace_count": len(workspace_files),
    }


# ---------------------------------------------------------------------------
# Tracker progress (F14): parse the "## Checkpoints" milestone list
# ---------------------------------------------------------------------------


def _checkpoint_status(raw: str) -> str:
    """Map a checkbox marker char to a display state.

    ``x``/``X`` -> ``done``, ``~`` -> ``current`` (in progress), ``!`` ->
    ``blocked``, anything else (a space) -> ``todo``.
    """
    if raw in ("x", "X"):
        return "done"
    if raw == "~":
        return "current"
    if raw == "!":
        return "blocked"
    return "todo"


def _first_request_id(text: str) -> str:
    """Return the first REQ-... id mentioned in ``text`` (or '')."""
    m = re.search(r"REQ-\d{8}-\d{6}-[a-z0-9-]+", text)
    return m.group(0) if m else ""


def parse_tracker_progress(loop_dir: Path, text: Optional[str] = None) -> dict[str, Any]:
    """Parse ``tracker.md``'s ``## Checkpoints`` section into progress data.

    Returns a structure the dashboard renders as a prominent Progress view:

    - ``available`` (bool): False when tracker.md is missing or has no
      ``## Checkpoints`` section (the page then shows an empty progress state);
    - ``total`` / ``done`` / ``blocked`` (ints): milestone counts;
    - ``current_index`` (int or None): the index of the current milestone --
      the first ``[~]`` in-progress item, else the first ``[ ]`` todo item;
    - ``checkpoints`` (list): each ``{index, status, title, request_id}`` where
      ``status`` is done|current|blocked|todo and ``request_id`` is the first
      REQ id found in that checkpoint's own text (for cross-referencing).

    ONLY the ``## Checkpoints`` section is treated as milestones; the
    ``Done When`` / ``Human QA`` acceptance checkboxes below it are excluded
    (they are criteria, not progress). Never raises; a malformed tracker
    degrades to ``available: False``.
    """
    if text is None:
        text = _read_text(loop_dir / "tracker.md")
    if not text:
        return {"available": False, "total": 0, "done": 0, "blocked": 0,
                "current_index": None, "checkpoints": []}

    in_section = False
    checkpoints: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().lower()
            # Enter on "## Checkpoints"; any OTHER section heading exits.
            if heading == _TRACKER_CHECKPOINTS_HEADING or heading.startswith(
                _TRACKER_CHECKPOINTS_HEADING + " "
            ):
                in_section = True
            else:
                in_section = False
            continue
        if not in_section:
            continue
        m = _CHECKBOX_RE.match(raw_line)
        if not m:
            continue
        title = m.group("text").strip()
        if len(title) > CHECKPOINT_TITLE_MAX_CHARS:
            title = title[:CHECKPOINT_TITLE_MAX_CHARS].rstrip() + "..."
        checkpoints.append(
            {
                "index": len(checkpoints),
                "status": _checkpoint_status(m.group("status")),
                "title": title,
                "request_id": _first_request_id(m.group("text")),
            }
        )

    if not checkpoints:
        return {"available": False, "total": 0, "done": 0, "blocked": 0,
                "current_index": None, "checkpoints": []}

    done = sum(1 for c in checkpoints if c["status"] == "done")
    blocked = sum(1 for c in checkpoints if c["status"] == "blocked")
    # The current milestone: first in-progress ([~]), else first todo ([ ]),
    # else None (all done/blocked). Blocked items are flagged but are not the
    # "current" pointer -- the human acts on them via the your-turn banner.
    current_index: Optional[int] = None
    for c in checkpoints:
        if c["status"] == "current":
            current_index = c["index"]
            break
    if current_index is None:
        for c in checkpoints:
            if c["status"] == "todo":
                current_index = c["index"]
                break
    return {
        "available": True,
        "total": len(checkpoints),
        "done": done,
        "blocked": blocked,
        "current_index": current_index,
        "checkpoints": checkpoints,
    }


# ---------------------------------------------------------------------------
# Project name (F2): read-only reader (the writer lives further down)
# ---------------------------------------------------------------------------


def _default_project_name(loop_dir: Path) -> str:
    """Derive the default project name from the loop-dir's project root.

    The loop dir is conventionally ``<project>/docs/loop``; the project root is
    the folder two levels up. Falls back through the nearest sensible ancestor
    so an unusual layout still yields a non-empty label.
    """
    try:
        resolved = loop_dir.resolve()
    except OSError:
        resolved = loop_dir
    parts = [p for p in resolved.parts]
    # Strip a trailing ``docs/loop`` (case-insensitive) to reach the root.
    lowered = [p.lower() for p in parts]
    if len(lowered) >= 2 and lowered[-1] == "loop" and lowered[-2] == "docs":
        root_parts = parts[:-2]
    else:
        root_parts = parts
    for candidate in reversed(root_parts):
        name = candidate.strip().strip("\\/").strip()
        # Skip a bare drive like "C:" or an empty separator remnant.
        if name and not re.fullmatch(r"[A-Za-z]:", name):
            return name
    return "loop"


def _read_project_name_value(loop_dir: Path) -> Optional[str]:
    """Return the stored project name from ``project.md``, or None if unset.

    The value is the first non-heading, non-blank, non-HTML-comment line. HTML
    comments may span multiple lines; they are skipped wholesale. Returns None
    when the file is absent or carries no value line (caller falls back to the
    default). Never raises.
    """
    text = _read_text(loop_dir / PROJECT_FILE_NAME)
    if not text:
        return None
    in_comment = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if in_comment:
            if "-->" in line:
                in_comment = False
                after = line.split("-->", 1)[1].strip()
                if after and not after.startswith("#"):
                    return after[:PROJECT_NAME_MAX_CHARS]
            continue
        if line.startswith("<!--"):
            if "-->" not in line:
                in_comment = True
                continue
            after = line.split("-->", 1)[1].strip()
            if after and not after.startswith("#"):
                return after[:PROJECT_NAME_MAX_CHARS]
            continue
        if line.startswith("#"):
            continue
        return line[:PROJECT_NAME_MAX_CHARS]
    return None


def read_project(loop_dir: Path) -> dict[str, Any]:
    """Read the project name for /api/state (value + whether it is stored).

    Returns ``{"name": str, "is_default": bool, "source_present": bool}``.
    ``is_default`` is True when no name is stored and the loop-dir-derived
    default is used; ``source_present`` reports whether project.md exists.
    """
    stored = _read_project_name_value(loop_dir)
    if stored:
        return {
            "name": stored,
            "is_default": False,
            "source_present": (loop_dir / PROJECT_FILE_NAME).exists(),
        }
    return {
        "name": _default_project_name(loop_dir),
        "is_default": True,
        "source_present": (loop_dir / PROJECT_FILE_NAME).exists(),
    }


def _load_evidence_records(evidence_dir: Path) -> dict[str, Any]:
    """Parse every ``evidence/*.json`` (non-recursive) into simple records.

    Reuses ``completion_gate.load_evidence`` when the gate is importable so the
    dashboard reads evidence exactly as the gate does; falls back to a direct
    glob otherwise. Malformed files are surfaced as ``load_errors`` rather than
    crashing.
    """
    records: list[dict[str, Any]] = []
    load_errors: list[dict[str, str]] = []
    parser_degraded = False
    parser_reason = ""
    if not evidence_dir.is_dir():
        return {
            "records": records,
            "load_errors": load_errors,
            "parser_degraded": parser_degraded,
            "parser_reason": parser_reason,
        }

    gate = None
    if DOCTOR_AVAILABLE and getattr(doctor, "completion_gate", None) is not None:
        gate = doctor.completion_gate  # reuse the gate the doctor imported
    if gate is not None:
        try:
            gate_records, gate_errors = gate.load_evidence(evidence_dir)
            for rec in gate_records:
                records.append(
                    {
                        "request_id": str(rec.get("request_id", "")).strip(),
                        "checkpoint": str(rec.get("checkpoint", "")).strip(),
                        "command": str(rec.get("command", "")).strip(),
                        "exit_code": rec.get("exit_code"),
                        "ran_at": str(rec.get("ran_at", "")).strip(),
                        "source": str(rec.get("_source", "")).strip(),
                    }
                )
            load_errors = [
                {"source": e.get("source", ""), "reason": e.get("reason", "")}
                for e in gate_errors
            ]
            return {
                "records": records,
                "load_errors": load_errors,
                "parser_degraded": parser_degraded,
                "parser_reason": parser_reason,
            }
        except Exception as exc:
            # Fall through to the direct reader on any gate hiccup.
            records = []
            load_errors = []
            parser_degraded = True
            parser_reason = _safe_exception(exc)
    else:
        parser_degraded = True
        parser_reason = "completion gate evidence loader unavailable"

    for path in sorted(evidence_dir.glob("*.json")):
        source = str(path).replace("\\", "/")
        raw = _read_text(path)
        if not raw:
            load_errors.append({"source": source, "reason": "unreadable or empty"})
            continue
        try:
            data = json.loads(raw)
        except ValueError as exc:
            load_errors.append({"source": source, "reason": "invalid JSON: {0}".format(exc)})
            continue
        if not isinstance(data, dict):
            load_errors.append({"source": source, "reason": "not a JSON object"})
            continue
        required = ("request_id", "checkpoint", "command", "exit_code", "ran_at")
        missing = [key for key in required if key not in data]
        if missing:
            load_errors.append(
                {
                    "source": source,
                    "reason": "missing required field(s): {0}".format(", ".join(missing)),
                }
            )
            continue
        empty = [
            key for key in ("request_id", "checkpoint", "command", "ran_at")
            if not str(data.get(key, "")).strip()
        ]
        if empty:
            load_errors.append(
                {
                    "source": source,
                    "reason": "empty required field(s): {0}".format(", ".join(empty)),
                }
            )
            continue
        exit_code = data.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            load_errors.append({"source": source, "reason": "exit_code must be an integer"})
            continue
        if _parse_timestamp(str(data.get("ran_at", ""))) is None:
            load_errors.append({"source": source, "reason": "ran_at must be a timestamp"})
            continue
        records.append(
            {
                "request_id": str(data.get("request_id", "")).strip(),
                "checkpoint": str(data.get("checkpoint", "")).strip(),
                "command": str(data.get("command", "")).strip(),
                "exit_code": data.get("exit_code"),
                "ran_at": str(data.get("ran_at", "")).strip(),
                "source": source,
            }
        )
    return {
        "records": records,
        "load_errors": load_errors,
        "parser_degraded": parser_degraded,
        "parser_reason": parser_reason,
    }


def _doctor_snapshot(loop_dir: Path) -> dict[str, Any]:
    """Run the in-process doctor and pull out the badge-relevant fields.

    Guarded end to end: if the doctor is not importable, or raises while
    summarizing (for example the loop is not bootstrapped), return a small
    ``available: false`` object so the page can still render.
    """
    if not DOCTOR_AVAILABLE or doctor is None:
        reason = DOCTOR_IMPORT_ERROR or "multi_agent_loop_doctor is not importable"
        return {"available": False, "reason": reason}
    try:
        result = doctor.summarize(
            loop_dir, stale_heartbeat_mins=doctor.DEFAULT_STALE_HEARTBEAT_MINS
        )
    except Exception as exc:  # loop not bootstrapped, unreadable files, etc.
        return {"available": False, "reason": "doctor error: {0}".format(_safe_exception(exc))}

    decisions = result.get("decisions") or {}
    requests = result.get("requests") or {}
    return {
        "available": True,
        "ok": result.get("ok"),
        "handoff_ready": result.get("handoff_ready"),
        "gate_available": result.get("gate_available"),
        "completion_gate_ok": result.get("completion_gate_ok"),
        "evidence_recorded_ok": result.get("evidence_recorded_ok"),
        "gate_failed_requests": result.get("gate_failed_requests", []),
        "orphan_suspects": result.get("orphan_suspects", []),
        "decisions": {
            "total": decisions.get("total", 0),
            "active": decisions.get("active", 0),
            "stale": decisions.get("stale", 0),
            "malformed": decisions.get("malformed", 0),
        },
        # Batch 1 machine-readable fields the dashboard RENDERS (F6/F9/F10/F11/
        # F13/F16). Passed through verbatim; the page decides how to surface
        # each (your-turn banner, needs-you sorting, honest heartbeat labels).
        "heartbeat_gap_owners": result.get("heartbeat_gap_owners", []),
        "stalled_handoffs": result.get("stalled_handoffs", []),
        # G3/G10: request_ids held awaiting a human try-it. The dashboard renders
        # a held slice's lane note as the BLUE "ready to try" confirm tone rather
        # than an alarming red halt.
        "held_for_human_qa": result.get("held_for_human_qa", []),
        # G13: request_id -> the raising lane's recommended_answer for BLOCKED
        # requests; the your-turn halt item renders it inline.
        "recommended_answers": result.get("recommended_answers", {}),
        "workerless_dependencies": result.get("workerless_dependencies", []),
        "missing_dependency_blockers": result.get("missing_dependency_blockers", []),
        "git_present": result.get("git_present"),
        "hook_installed": result.get("hook_installed"),
        # Non-terminal requests carry the human-readable next_action + owner so
        # a lane card can name the goal of its current request (F14) and the
        # your-turn banner can name the advancing thread (F6/F3 discriminator:
        # "any request advancing" => a waiting lane is not a halt).
        "non_terminal_requests": requests.get("non_terminal", []),
        "issues": result.get("issues", []),
        "warnings": result.get("warnings", []),
    }


# ---------------------------------------------------------------------------
# Policy reader: current max_fix_cycles from loop-policy.md (read-only here)
# ---------------------------------------------------------------------------


def read_max_fix_cycles(
    loop_dir: Path,
    text: Optional[str] = None,
    source_present: Optional[bool] = None,
) -> dict[str, Any]:
    """Read the ``max_fix_cycles`` value from ``loop-policy.md``.

    Returns ``{"max_fix_cycles": int, "source_present": bool}``. When the file
    is missing or has no recognizable line, falls back to
    ``DEFAULT_MAX_FIX_CYCLES`` and reports ``source_present`` honestly. Never
    raises; a malformed value degrades to the default.
    """
    policy_path = loop_dir / "loop-policy.md"
    if text is None:
        text = _read_text(policy_path)
    present = bool(text) if source_present is None else source_present
    value = DEFAULT_MAX_FIX_CYCLES
    for line in text.splitlines():
        m = _MAX_FIX_CYCLES_RE.match(line)
        if m:
            try:
                value = int(m.group("value"))
            except ValueError:
                value = DEFAULT_MAX_FIX_CYCLES
            break
    return {"max_fix_cycles": value, "source_present": present}


# ---------------------------------------------------------------------------
# Usage + account providers: delegated to the Codex host probe
# ---------------------------------------------------------------------------
#
# Everything that reads the Codex host's UNDOCUMENTED data surfaces (session
# JSONL rate-limits, auth.json JWT identity) lives in ``codex_host_probe`` -- the
# ONLY module in this skill that touches that data plane. The dashboard imports
# ``build_usage`` / ``build_account`` from it (guarded above) and treats it as an
# OPTIONAL dependency: if the probe is missing, ``build_state`` serves usage and
# account as ``available: false`` with reason ``probe_module_missing`` and never
# crashes. The refresh path calls the probe's ``drop_caches`` (guarded) to force a
# rescan. The privacy/red-line notes for both providers live in that module.

# Legacy token retained for compatibility/documentation; new snapshots use the
# broader module-unavailable code because an import can be absent OR broken.
PROBE_MISSING_REASON = "probe_module_missing"
PROBE_UNAVAILABLE_REASON = "probe_module_unavailable"


def _compute_account_stale(account: dict[str, Any], usage: dict[str, Any]) -> None:
    """Annotate ``account`` in place with whether the quota snapshot is stale.

    The quota snapshot "predates the current login" when either:

    - the JWT-derived ``plan_type`` differs from the usage snapshot's
      ``plan_type`` (the plan changed since the snapshot was written), or
    - auth.json's mtime is NEWER than the usage snapshot's ``as_of`` (the login
      was refreshed after the last rate-limit event landed).

    Sets ``account['snapshot_stale']`` (bool) and ``account['stale_reason']``
    (a stable machine code the UI localizes: ``plan_mismatch`` or
    ``auth_newer_than_snapshot``, else empty). Purely comparative; surfaces no
    new sensitive data. No-op when the account is unavailable.
    """
    account["snapshot_stale"] = False
    account["stale_reason"] = ""
    if not account.get("available"):
        return
    if not isinstance(usage, dict) or usage.get("available") is not True:
        return

    # Plan mismatch: both sides must actually carry a plan to compare.
    acct_plan = account.get("plan_type")
    snap_plan = usage.get("plan_type")
    if isinstance(acct_plan, str) and acct_plan and isinstance(snap_plan, str) and snap_plan:
        if acct_plan != snap_plan:
            account["snapshot_stale"] = True
            account["stale_reason"] = "plan_mismatch"
            return

    # auth.json newer than the snapshot's as_of (login refreshed after the last
    # rate-limit event). Both timestamps are local-time ISO strings written by
    # ``_iso_local``; parse them tolerantly and compare.
    auth_dt = _parse_timestamp(account.get("auth_mtime_iso") or "")
    asof_dt = _parse_timestamp(usage.get("as_of") or "")
    if auth_dt is not None and asof_dt is not None and auth_dt > asof_dt:
        account["snapshot_stale"] = True
        account["stale_reason"] = "auth_newer_than_snapshot"


# Signature fragments of the bootstrap goal.md placeholder (Objective +
# Done-When lines). If goal.md still contains these AND no real request exists,
# the loop has no objective yet -> the friendly "awaiting objective" state (F3),
# never a failure. Kept as substrings so minor template edits still match.
_GOAL_PLACEHOLDER_MARKERS = (
    "State the single durable objective",
    "Define the first concrete, verifiable completion condition",
)


def _is_awaiting_objective(loop_dir: Path, requests: list[dict[str, str]]) -> bool:
    """True when the loop has no real objective yet (F3 intake state).

    Detected when goal.md still carries the placeholder template markers AND
    there is no non-placeholder request in the queue. This is the ABSENCE of a
    goal -- the dashboard shows a friendly "awaiting objective" empty state,
    which must never read as a failure. Never raises.
    """
    goal_text = _read_text(loop_dir / "goal.md")
    if not goal_text:
        # No goal.md at all: only "awaiting" if there is also no real request.
        placeholder_goal = True
    else:
        placeholder_goal = all(m in goal_text for m in _GOAL_PLACEHOLDER_MARKERS)
    if not placeholder_goal:
        return False
    # Any request that is not itself a placeholder means work has begun.
    for req in requests:
        rid = (req.get("request_id", "") or "").strip()
        status = (req.get("status", "") or "").strip().upper()
        if not rid:
            continue
        # A "no goal yet" placeholder request (the old F3 anti-pattern) does not
        # count as real work; a genuine request in any lifecycle state does.
        blob = " ".join(str(v) for v in req.values()).lower()
        if "no goal yet" in blob or "placeholder" in blob:
            continue
        return False
    return True


def build_state(
    loop_dir: Path, now: Optional[datetime] = None, refresh: bool = False
) -> dict[str, Any]:
    """Assemble the full ``/api/state`` snapshot by reading files only.

    Never writes. Every section degrades to an empty/false value when the
    underlying file is missing, so a brand-new (un-bootstrapped) loop renders a
    meaningful empty state.

    When ``refresh`` is True, the in-memory usage + account caches are dropped
    first so the usage/account sections are recomputed from a fresh auth.json
    re-read and a newest-session re-scan. This is still a read-only operation:
    it forces a rescan, it never writes to disk. Normal polling passes
    ``refresh=False`` and rides the caches.
    """
    refresh_degraded = False
    cache_drop_failed = False
    refresh_reason = ""
    if refresh and PROBE_AVAILABLE and codex_host_probe is not None:
        # Drop the probe's usage/account caches so the next read rescans. Guarded
        # so a missing probe (or a probe without the function) never crashes the
        # read; still read-only -- it forces a rescan, it writes nothing.
        try:
            codex_host_probe.drop_caches()
        except Exception as exc:  # keep serving, but admit the response may be cached
            refresh_degraded = True
            cache_drop_failed = True
            refresh_reason = _safe_exception(exc)
    if now is None:
        now = datetime.now(timezone.utc)

    lanes_dir = loop_dir / "lanes"
    registry_path = loop_dir / "agent-lanes.md"
    requests_path = loop_dir / "requests.md"
    run_log_path = loop_dir / "loop-run-log.md"
    policy_path = loop_dir / "loop-policy.md"
    tracker_path = loop_dir / "tracker.md"
    evidence_dir = loop_dir / "evidence"

    bootstrapped = registry_path.exists()

    # Core control-plane reads carry their degradation metadata alongside the
    # existing fallback values. Missing, valid-empty, malformed, and unreadable
    # are distinct so the client never presents a fallback as authoritative.
    read_errors: list[dict[str, str]] = []
    parse_errors: list[dict[str, str]] = []
    file_status: dict[str, str] = {}

    def read_core(path: Path) -> str:
        text, status, reason = _read_text_status(path)
        file_status[path.name] = status
        if status == "unreadable":
            read_errors.append(
                {"source": str(path).replace("\\", "/"), "reason": reason}
            )
        return text

    requests_text = read_core(requests_path)
    registry_text = read_core(registry_path)
    run_log_text = read_core(run_log_path)
    policy_text = read_core(policy_path)
    tracker_text = read_core(tracker_path)

    # Requests queue (parsed first so lane cards can cross-reference the
    # human-readable goal/next_action of the lane's CURRENT request -- F14).
    if file_status[requests_path.name] == "ok":
        requests, request_parse_errors = _parse_md_table_text(
            requests_text,
            str(requests_path).replace("\\", "/"),
            ("request_id", "status"),
        )
        parse_errors.extend(request_parse_errors)
        if request_parse_errors:
            file_status[requests_path.name] = "malformed"
    else:
        requests = []
    requests_by_id: dict[str, dict[str, str]] = {}
    for req in requests:
        rid = (req.get("request_id", "") or "").strip()
        if rid:
            requests_by_id[rid] = req

    # Registry rows + per-lane detail.
    if file_status[registry_path.name] == "ok":
        lane_rows, registry_parse_errors = _parse_md_table_text(
            registry_text,
            str(registry_path).replace("\\", "/"),
            ("lane", "thread_id", "status"),
        )
        parse_errors.extend(registry_parse_errors)
        if registry_parse_errors:
            file_status[registry_path.name] = "malformed"
    else:
        lane_rows = []
    lanes: list[dict[str, Any]] = []
    for row in lane_rows:
        lane = (row.get("lane", "") or "").strip()
        if not lane:
            continue
        detail = _current_summary(lanes_dir, lane)
        # F14: attach the human-readable goal of the lane's current request.
        # The request row's ``next_action`` is the plainest human sentence
        # available; its ``status`` is the machine token. This lets the card
        # lead with WHAT is being worked on and demote the raw REQ id.
        summary = detail["summary"]
        rid = (summary.get("current_request_id", "") or "").strip()
        req_row = requests_by_id.get(rid) if rid else None
        current_request: dict[str, Any] = {}
        if req_row is not None:
            req_owner = (req_row.get("owner_lane", "") or req_row.get("owner", "") or "").strip()
            current_request = {
                "request_id": rid,
                "status": (req_row.get("status", "") or "").strip(),
                "owner_lane": req_owner,
                "goal": (req_row.get("next_action", "") or "").strip(),
                "iteration": (req_row.get("iteration", "") or req_row.get("iter", "") or "").strip(),
                # G17: this lane is the request's OWNER (or the request records no
                # owner_lane at all -> legacy fallback). The your-turn "all
                # running" banner attributes the single "X is working on this
                # request" line to the owner only, so a non-owning lane whose
                # current.md still points at another lane's request (a fresh
                # heartbeat, a tracking reference) is never double-counted with
                # the owner's next_action -- the run-3 data-eng/product duplicate.
                "is_owner": (not req_owner) or (req_owner == lane),
            }
        # G14(a/c): the lane's OBSERVED model+effort and its abstract tier tag,
        # from current.md's model_observed line. observed_model is the DATA value
        # rendered verbatim on the chip; observed_tier is the tag compared to the
        # recommended tier; tier_mismatch is amber when both are present and
        # differ (never a mismatch when observed is not yet stamped).
        observed_model = (summary.get("model_observed", "") or "").strip()
        observed_tier = _observed_tier_from_value(observed_model)
        recommended_tier = (row.get("tier", "") or "").strip().lower()
        tier_mismatch = bool(
            recommended_tier and observed_tier and recommended_tier != observed_tier
        )
        lanes.append(
            {
                "lane": lane,
                "thread_id": (row.get("thread_id", "") or "").strip(),
                "role": (row.get("role", "") or "").strip(),
                "write_scope": (row.get("write_scope", "") or "").strip(),
                # F8 advisory model tier (abstract word from agent-lanes.md).
                "recommended_tier": (row.get("tier", "") or "").strip(),
                # G14: observed model DATA + its abstract tier tag + mismatch.
                "observed_model": observed_model,
                "observed_tier": observed_tier,
                "tier_mismatch": tier_mismatch,
                "status": _lane_status_label(row),
                "status_raw": (row.get("status", "") or "").strip(),
                "heartbeat": _heartbeat_freshness(
                    row.get("heartbeat", "") or row.get("last_heartbeat", ""), now
                ),
                "current": detail["current"],
                "summary": summary,
                "current_request": current_request,
                "worklog_tail": detail["worklog_tail"],
                "workspace_files": detail["workspace_files"],
                "workspace_count": detail["workspace_count"],
            }
        )

    # Evidence records.
    evidence = _load_evidence_records(evidence_dir)

    # Run-log tail. G11(b): the run log is append-only, so a late-append honest
    # recovery row can sit out of chronological order. The displayed tail sorts
    # DATA rows by their timestamp cell (header/separator preserved) so the tail
    # shows the chronologically-latest transitions, matching the timestamp-sorted
    # reconstruction the in-process doctor now uses.
    # Keep the sorted source collection in the cached snapshot. The API layer
    # applies the active+recent bound (or ?full=run_log) without rebuilding.
    run_log_tail = _run_log_tail_sorted(run_log_text, 0)

    # Doctor snapshot (in-process).
    doctor_snapshot = _doctor_snapshot(loop_dir)

    # Current anti-thrash cap (read-only view of loop-policy.md).
    policy = read_max_fix_cycles(
        loop_dir,
        text=policy_text,
        source_present=bool(policy_text),
    )
    if file_status[policy_path.name] == "ok" and not any(
        _MAX_FIX_CYCLES_RE.match(line) for line in policy_text.splitlines()
    ):
        parse_errors.append(
            {
                "source": str(policy_path).replace("\\", "/"),
                "reason": "max_fix_cycles is missing or malformed",
            }
        )
        file_status[policy_path.name] = "malformed"

    # Tracker progress (F14) + human project name (F2).
    tracker_progress = parse_tracker_progress(loop_dir, text=tracker_text)
    if file_status[tracker_path.name] == "ok" and tracker_progress.get("available") is not True:
        parse_errors.append(
            {
                "source": str(tracker_path).replace("\\", "/"),
                "reason": "no valid ## Checkpoints checkbox rows found",
            }
        )
        file_status[tracker_path.name] = "malformed"
    project = read_project(loop_dir)
    # Awaiting-objective state (F3): a fresh loop whose goal.md is still the
    # placeholder template AND that has no real requests yet. This is the
    # ABSENCE of a goal, rendered as a friendly empty state -- never a failure.
    awaiting_objective = _is_awaiting_objective(loop_dir, requests)

    # Codex usage snapshot, delegated to the host probe. Guarded end to end so a
    # missing probe module or a parser hiccup never blocks the rest of the state;
    # degrades to available:false. When the probe module itself is absent, the
    # reason is ``probe_module_missing`` so a consumer can tell that apart from a
    # logged-out / no-data-yet state.
    if PROBE_AVAILABLE and codex_host_probe is not None:
        try:
            usage = codex_host_probe.build_usage()
        except Exception as exc:  # pragma: no cover - defensive belt-and-suspenders
            usage = {
                "available": False,
                "reason_code": "usage_provider_error",
                "reason": _safe_exception(exc),
            }
    else:
        usage = {
            "available": False,
            "reason_code": PROBE_UNAVAILABLE_REASON,
            "reason": PROBE_IMPORT_ERROR or PROBE_UNAVAILABLE_REASON,
        }

    # Scoped account identity from auth.json (email/name/plan/auth_mode/short id
    # only -- never tokens), also from the host probe. Attached UNDER usage as
    # ``usage.account`` so the Usage & Limits panel has both quota and identity in
    # one object. Guarded so a missing probe or parser hiccup degrades to
    # available:false rather than blocking state.
    if PROBE_AVAILABLE and codex_host_probe is not None:
        try:
            account = codex_host_probe.build_account()
        except Exception as exc:  # pragma: no cover - defensive belt-and-suspenders
            account = {"available": False, "detail": _safe_exception(exc)}
    else:
        account = {
            "available": False,
            "reason_code": PROBE_UNAVAILABLE_REASON,
            "detail": PROBE_IMPORT_ERROR or PROBE_UNAVAILABLE_REASON,
        }
    # Flag when the quota snapshot predates the current login (plan changed, or
    # auth.json refreshed after the last rate-limit event). Comparative only.
    _compute_account_stale(account, usage)
    if isinstance(usage, dict):
        usage["account"] = account

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "loop_dir": str(loop_dir).replace("\\", "/"),
        "bootstrapped": bootstrapped,
        "read_errors": read_errors,
        "parse_errors": parse_errors,
        "file_status": file_status,
        "capabilities": {
            "doctor": {
                "available": DOCTOR_AVAILABLE,
                "reason": DOCTOR_IMPORT_ERROR,
            },
            "bootstrap": {
                "available": BOOTSTRAP_AVAILABLE,
                "reason": BOOTSTRAP_IMPORT_ERROR,
            },
            "probe": {
                "available": PROBE_AVAILABLE,
                "reason": PROBE_IMPORT_ERROR,
            },
        },
        "refresh_degraded": refresh_degraded,
        "cache_drop_failed": cache_drop_failed,
        "refresh_reason": refresh_reason,
        "stale_heartbeat_mins": STALE_HEARTBEAT_MINS,
        "reserved_lane_names": sorted(RESERVED_LANE_NAMES),
        "lanes": lanes,
        "requests": requests,
        "evidence": evidence,
        "run_log_tail": run_log_tail,
        "doctor": doctor_snapshot,
        "policy": policy,
        "usage": usage,
        "tracker_progress": tracker_progress,
        "project": project,
        "awaiting_objective": awaiting_objective,
    }


def _request_is_active(request: dict[str, Any]) -> bool:
    status = str(request.get("status", "")).strip().upper()
    return status not in _INACTIVE_REQUEST_STATUSES


def _recent_key(value: str, index: int) -> tuple[datetime, int]:
    return (_parse_timestamp(value) or datetime.fromtimestamp(0, timezone.utc), index)


def _active_plus_recent(
    items: list[Any], active_indexes: set[int], recent_indexes: list[int], limit: int
) -> list[Any]:
    keep = set(active_indexes)
    keep.update(recent_indexes[-limit:] if limit > 0 else recent_indexes)
    return [item for index, item in enumerate(items) if index in keep]


def _bounded_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active = {index for index, row in enumerate(requests) if _request_is_active(row)}
    inactive = [index for index in range(len(requests)) if index not in active]
    inactive.sort(
        key=lambda index: _recent_key(
            str(requests[index].get("updated_at", "")
                or requests[index].get("updated", "")),
            index,
        )
    )
    return _active_plus_recent(requests, active, inactive, REQUESTS_PAGE_SIZE)


def _bounded_evidence(
    records: list[dict[str, Any]], active_request_ids: set[str]
) -> list[dict[str, Any]]:
    active = {
        index for index, row in enumerate(records)
        if str(row.get("request_id", "")).strip() in active_request_ids
    }
    inactive = [index for index in range(len(records)) if index not in active]
    inactive.sort(
        key=lambda index: _recent_key(str(records[index].get("ran_at", "")), index)
    )
    return _active_plus_recent(records, active, inactive, EVIDENCE_PAGE_SIZE)


def _run_log_data(lines: list[str]) -> list[tuple[str, str, str]]:
    """Return (line, timestamp, request_id) for actual table data rows."""
    data: list[tuple[str, str, str]] = []
    for line in lines:
        if not line.lstrip().startswith("|"):
            continue
        cells = _split_md_row(line)
        if not cells or all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        if cells[0].strip().lower() in ("timestamp", "at", "time", "delivered_at"):
            continue
        timestamp = cells[0].strip()
        request_id = cells[1].strip() if len(cells) > 1 else ""
        data.append((line, timestamp, request_id))
    return data


def _bounded_run_log(lines: list[str], active_request_ids: set[str]) -> list[str]:
    data = _run_log_data(lines)
    active = {
        index for index, item in enumerate(data) if item[2] in active_request_ids
    }
    inactive = [index for index in range(len(data)) if index not in active]
    inactive.sort(key=lambda index: _recent_key(data[index][1], index))
    selected = _active_plus_recent(data, active, inactive, RUNLOG_PAGE_SIZE)
    return [item[0] for item in selected]


def _paginate_state(
    state: dict[str, Any], full_collections: set[str]
) -> dict[str, Any]:
    """Project cached full state into an honest active+recent API response."""
    projected = dict(state)
    all_requests = list(state.get("requests") or [])
    active_request_ids = {
        str(row.get("request_id", "")).strip()
        for row in all_requests if _request_is_active(row)
    }
    requests = (
        all_requests if "requests" in full_collections else _bounded_requests(all_requests)
    )
    projected["requests"] = requests

    source_evidence = state.get("evidence") or {}
    evidence = dict(source_evidence)
    all_evidence = list(source_evidence.get("records") or [])
    evidence_records = (
        all_evidence if "evidence" in full_collections
        else _bounded_evidence(all_evidence, active_request_ids)
    )
    evidence["records"] = evidence_records
    projected["evidence"] = evidence

    all_run_log = [item[0] for item in _run_log_data(list(state.get("run_log_tail") or []))]
    run_log = (
        all_run_log if "run_log" in full_collections
        else _bounded_run_log(list(state.get("run_log_tail") or []), active_request_ids)
    )
    projected["run_log_tail"] = run_log

    projected["pagination"] = {
        "requests": {
            "shown": len(requests),
            "total": len(all_requests),
            "truncated": len(requests) < len(all_requests),
        },
        "evidence": {
            "shown": len(evidence_records),
            "total": len(all_evidence),
            "truncated": len(evidence_records) < len(all_evidence),
        },
        "run_log": {
            "shown": len(run_log),
            "total": len(all_run_log),
            "truncated": len(run_log) < len(all_run_log),
        },
    }
    return projected


# ---------------------------------------------------------------------------
# The single write path: add a lane
# ---------------------------------------------------------------------------


def _existing_lane_names(registry_path: Path) -> set[str]:
    names: set[str] = set()
    for row in _parse_md_table(registry_path):
        lane = (row.get("lane", "") or "").strip()
        if lane:
            names.add(lane.lower())
    return names


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (unique temp file + fsync).

    Delegates to the shared ``_loop_lock.atomic_replace`` when available so the
    temp file is unique per write (``tempfile.mkstemp``) -- a fixed ``.tmp-<pid>``
    name collides across this server's request threads, letting one thread's
    ``os.replace`` hit ``FileNotFoundError``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if LOOP_LOCK_AVAILABLE and _lock_atomic_replace is not None:
        _lock_atomic_replace(path, content)
        return
    import tempfile

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def add_lane(loop_dir: Path, lane: str, role: str) -> dict[str, Any]:
    """Validate and append one lane. The ONLY mutation this module performs.

    Steps:
    1. validate the lane name (kebab, length, not reserved);
    2. reject a name already present in the registry;
    3. build the new row (status=needs-thread, default write_scope);
    4. reuse ``bootstrap_agent_loop`` to create the lane directory + files;
    5. rewrite ``agent-lanes.md`` atomically (temp file then os.replace).

    Returns ``{"ok": True, "lane": ...}`` on success, or
    ``{"ok": False, "error": ...}`` on any validation/parse failure. Never
    raises for a bad request; only genuinely unexpected IO errors propagate.
    """
    lane = (lane or "").strip()
    role = (role or "").strip()

    if not lane:
        return {"ok": False, "error": "lane name is required"}
    if not LANE_NAME_RE.match(lane):
        return {
            "ok": False,
            "error": (
                "invalid lane name {0!r}: must match ^[a-z][a-z0-9-]{{1,30}}$ "
                "(lowercase kebab-case)".format(lane)
            ),
        }
    if lane.lower() in RESERVED_LANE_NAMES:
        return {"ok": False, "error": "lane name {0!r} is reserved".format(lane)}

    if role and _ROLE_BAD_CHARS_RE.search(role):
        return {
            "ok": False,
            "error": "role must not contain line breaks, control characters, or '|'",
        }
    if len(role) > ROLE_MAX_CHARS:
        return {
            "ok": False,
            "error": "role must be at most {0} characters".format(ROLE_MAX_CHARS),
        }

    registry_path = loop_dir / "agent-lanes.md"
    if not registry_path.exists():
        return {
            "ok": False,
            "error": "loop is not bootstrapped (no agent-lanes.md); run bootstrap first",
        }

    existing = _existing_lane_names(registry_path)
    if lane.lower() in existing:
        return {"ok": False, "error": "lane {0!r} is already registered".format(lane)}

    if not role:
        role = "Handle scoped {0} work and report evidence.".format(lane)
    write_scope = "docs/loop/lanes/{0}/**".format(lane)

    # Parse the current registry into a lane->row mapping using bootstrap's own
    # reader so column handling matches exactly. Fall back to a local parse if
    # bootstrap is somehow unavailable.
    if BOOTSTRAP_AVAILABLE and bootstrap_agent_loop is not None:
        rows = bootstrap_agent_loop.existing_rows(registry_path)
    else:
        rows = {}
        for row in _parse_md_table(registry_path):
            name = (row.get("lane", "") or "").strip()
            if not name:
                continue
            rows[name] = {
                "thread_id": (row.get("thread_id", "") or "").strip(),
                "role": (row.get("role", "") or "").strip(),
                "write_scope": (row.get("write_scope", "") or "").strip(),
                "worklog": (row.get("worklog", "") or "").strip(),
                "status": (row.get("status", "") or "").strip(),
                "heartbeat": (row.get("heartbeat", "") or "-").strip() or "-",
                # Preserve the F8 advisory tier verbatim (a human opt-down must
                # survive a POST /api/lanes rewrite).
                "tier": (row.get("tier", "") or "").strip(),
            }

    worklog = "{0}/lanes/{1}/worklog.md".format(str(loop_dir).replace("\\", "/"), lane)
    rows[lane] = {
        "thread_id": "UNVERIFIED",
        "role": role,
        "write_scope": write_scope,
        "worklog": worklog,
        "status": "needs-thread",
        "heartbeat": "-",
        # F8 advisory tier: policy default for the new lane (render_registry
        # fills it from recommended_tier_for even if left blank here).
        "tier": "",
    }

    # Create the lane directory + per-lane files by REUSING bootstrap's
    # write-if-missing templates. This mirrors exactly what bootstrap would
    # create for a lane, without touching any other lane's files.
    created_files: list[str] = []
    if BOOTSTRAP_AVAILABLE and bootstrap_agent_loop is not None:
        lane_dir = loop_dir / "lanes" / lane
        lane_dir.mkdir(parents=True, exist_ok=True)
        title = bootstrap_agent_loop.title_for(lane)
        lane_templates = {
            "worklog.md": bootstrap_agent_loop.WORKLOG_TEMPLATE,
            "inbox.md": bootstrap_agent_loop.INBOX_TEMPLATE,
            "outbox.md": bootstrap_agent_loop.OUTBOX_TEMPLATE,
            "current.md": bootstrap_agent_loop.CURRENT_TEMPLATE,
        }
        for filename, template in lane_templates.items():
            path = lane_dir / filename
            if bootstrap_agent_loop.write_if_missing(path, template.format(title=title)):
                created_files.append(str(path).replace("\\", "/"))
        workspace_dir = lane_dir / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        workspace_readme = workspace_dir / "README.md"
        if bootstrap_agent_loop.write_if_missing(
            workspace_readme,
            bootstrap_agent_loop.LANE_WORKSPACE_README_TEMPLATE.format(title=title),
        ):
            created_files.append(str(workspace_readme).replace("\\", "/"))
    else:
        return {
            "ok": False,
            "error": "bootstrap_agent_loop unavailable: {0}; cannot create lane files".format(
                BOOTSTRAP_IMPORT_ERROR or "module is not importable"
            ),
        }

    # Rebuild the registry table from the full row set and write it atomically.
    # Reuse bootstrap's render_registry so the column set (including the F8
    # advisory ``tier`` column) is defined in exactly one place -- a short
    # rebuild here would otherwise silently drop the tier column for every lane.
    _atomic_write(registry_path, bootstrap_agent_loop.render_registry(rows))

    return {
        "ok": True,
        "lane": lane,
        "role": role,
        "write_scope": write_scope,
        "status": "needs-thread",
        # F8 advisory tier the new lane was registered with (policy default).
        "tier": bootstrap_agent_loop.recommended_tier_for(lane),
        "created_files": created_files,
    }


# ---------------------------------------------------------------------------
# The second write path: set max_fix_cycles in loop-policy.md
# ---------------------------------------------------------------------------


def set_max_fix_cycles(loop_dir: Path, value: Any) -> dict[str, Any]:
    """Validate and write ``max_fix_cycles`` into ``loop-policy.md`` atomically.

    Steps:
    1. coerce ``value`` to an int and require ``MAX_FIX_CYCLES_MIN..MAX``;
    2. read the existing ``loop-policy.md`` (must exist -- it is a bootstrap
       output; refusing when absent avoids creating a half-populated policy);
    3. rewrite ONLY the ``max_fix_cycles`` line, preserving every other byte;
       if no such line exists, insert one under the ``## Request Policy``
       heading (or append a minimal section if that heading is absent);
    4. write via temp file + ``os.replace`` (atomic).

    Returns ``{"ok": True, "max_fix_cycles": int}`` or
    ``{"ok": False, "error": ...}``. Never raises for a bad request.
    """
    # (1) validate the integer. Reject bools and non-integers explicitly.
    if isinstance(value, bool):
        return {"ok": False, "error": "max_fix_cycles must be an integer, not a boolean"}
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and value.is_integer():
        n = int(value)
    elif isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        n = int(value.strip())
    else:
        return {
            "ok": False,
            "error": "max_fix_cycles must be an integer in {0}..{1}".format(
                MAX_FIX_CYCLES_MIN, MAX_FIX_CYCLES_MAX
            ),
        }
    if n < MAX_FIX_CYCLES_MIN or n > MAX_FIX_CYCLES_MAX:
        return {
            "ok": False,
            "error": "max_fix_cycles must be in {0}..{1}, got {2}".format(
                MAX_FIX_CYCLES_MIN, MAX_FIX_CYCLES_MAX, n
            ),
        }

    policy_path = loop_dir / "loop-policy.md"
    if not policy_path.exists():
        return {
            "ok": False,
            "error": "loop-policy.md not found; run bootstrap first",
        }

    text = _read_text(policy_path)
    if not text:
        return {"ok": False, "error": "loop-policy.md is empty or unreadable"}

    # (3) rewrite the existing line in place, preserving its prefix marker.
    # ``_read_text`` uses universal-newline reads, so ``lines`` never carries a
    # trailing ``\r``; we re-join with ``\n`` and let the atomic writer apply
    # the platform newline (matching how bootstrap writes this file).
    lines = text.splitlines()
    replaced = False
    for i, line in enumerate(lines):
        m = _MAX_FIX_CYCLES_RE.match(line)
        if m:
            lines[i] = "{0}max_fix_cycles: {1}".format(m.group("prefix"), n)
            replaced = True
            break

    if not replaced:
        # Insert under "## Request Policy" if present, else append a section.
        insert_at = None
        for i, line in enumerate(lines):
            if line.strip().lower() == "## request policy":
                insert_at = i + 1
                break
        policy_line = "max_fix_cycles: {0}".format(n)
        if insert_at is not None:
            # Skip a single blank line after the heading for tidy placement.
            if insert_at < len(lines) and lines[insert_at].strip() == "":
                insert_at += 1
            lines.insert(insert_at, policy_line)
        else:
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.append("## Request Policy")
            lines.append("")
            lines.append(policy_line)

    # Preserve a trailing newline if the original had one.
    content = "\n".join(lines)
    if text.endswith("\n"):
        content += "\n"

    _atomic_write(policy_path, content)
    return {"ok": True, "max_fix_cycles": n}


# ---------------------------------------------------------------------------
# The third (and final) write path: set the project name in project.md
# ---------------------------------------------------------------------------


def set_project_name(loop_dir: Path, value: Any) -> dict[str, Any]:
    """Validate and write the human project name into ``project.md`` atomically.

    Steps:
    1. coerce ``value`` to a string, trim it, and validate: non-empty after
       trim, no control characters / line breaks, at most
       ``PROJECT_NAME_MAX_CHARS`` chars;
    2. render the self-documenting ``project.md`` (a heading + a comment that
       explains the convention + the single value line);
    3. write via temp file + ``os.replace`` (atomic) -- the SAME pattern as the
       lane and policy writers. Creating project.md is fine (unlike the policy
       writer, this file is display-only and has no machine consumer to half-
       populate), but the loop must at least be bootstrapped.

    Returns ``{"ok": True, "name": str}`` or ``{"ok": False, "error": ...}``.
    Never raises for a bad request. This is the THIRD and FINAL write endpoint;
    no other new writes exist.
    """
    if not isinstance(value, str):
        # Accept only a JSON string; numbers/objects are rejected up front.
        if value is None:
            return {"ok": False, "error": "project name is required"}
        return {"ok": False, "error": "project name must be a string"}
    name = value.strip()
    if not name:
        return {"ok": False, "error": "project name is required"}
    if _PROJECT_NAME_BAD_CHARS_RE.search(name):
        return {
            "ok": False,
            "error": "project name must not contain control characters or line breaks",
        }
    if len(name) > PROJECT_NAME_MAX_CHARS:
        return {
            "ok": False,
            "error": "project name must be at most {0} characters".format(
                PROJECT_NAME_MAX_CHARS
            ),
        }

    # The loop must be bootstrapped (a project name for an empty dir is
    # meaningless and would scatter a stray file into an unrelated folder).
    if not (loop_dir / "agent-lanes.md").exists():
        return {
            "ok": False,
            "error": "loop is not bootstrapped (no agent-lanes.md); run bootstrap first",
        }

    project_path = loop_dir / PROJECT_FILE_NAME
    _atomic_write(project_path, _PROJECT_FILE_TEMPLATE.format(name=name))
    return {"ok": True, "name": name}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class DashboardHandler(BaseHTTPRequestHandler):
    """Serve the dashboard page, the state JSON, and the two write endpoints.

    ``loop_dir`` is injected onto the server object by ``make_server`` and read
    here via ``self.server.loop_dir``. The handler itself is stateless. The
    writes are EXACTLY three -- ``POST /api/lanes`` (add one lane),
    ``POST /api/policy`` (set ``max_fix_cycles``), and ``POST /api/project``
    (set the human project name); every other path/verb is refused.
    """

    server_version = "LoopDashboard/1.0"
    # Silence the default per-request stderr logging so the smoke test output
    # stays clean; real operators can watch the process if they want.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    # -- helpers ------------------------------------------------------------

    @property
    def loop_dir(self) -> Path:
        return self.server.loop_dir  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_state_json(self, state: dict[str, Any]) -> None:
        """Send state with a stable validator, or an empty 304 on a match."""
        etag = _state_etag(state)
        candidates = [
            item.strip() for item in self.headers.get("If-None-Match", "").split(",")
        ]
        if "*" in candidates or etag in candidates:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(state, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_plain(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path_only(self) -> str:
        # Strip any query string; we route on the path alone.
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def _query_params(self) -> dict[str, list[str]]:
        parts = self.path.split("?", 1)
        return parse_qs(parts[1], keep_blank_values=True) if len(parts) > 1 else {}

    def _wants_refresh(self) -> bool:
        """True when the request carries a ``refresh=1`` query flag.

        Read-only: the flag only tells ``build_state`` to drop its in-memory
        usage/account caches and rescan; it opens no new endpoint and writes
        nothing. Parsed by hand (no extra import) as a simple ``key=value`` scan.
        """
        return any(
            value in ("1", "true", "yes")
            for value in self._query_params().get("refresh", [])
        )

    def _full_collections(self) -> set[str]:
        allowed = {"requests", "evidence", "run_log"}
        requested: set[str] = set()
        for value in self._query_params().get("full", []):
            requested.update(part.strip() for part in value.split(","))
        return requested & allowed

    # -- verbs --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        route = self._path_only()
        if route == "/":
            html = _read_text(_DASHBOARD_HTML)
            if not html:
                self._send_plain(
                    500,
                    "dashboard.html not found next to loop_dashboard.py",
                )
                return
            self._send_html(200, html)
            return
        if route == "/api/state":
            # ``?refresh=1`` drops the usage/account caches and rescans before
            # responding (read-only; still not a new endpoint, still no writes).
            refresh = self._wants_refresh()
            try:
                state = _get_state_snapshot(self.loop_dir, refresh=refresh)
                state = _paginate_state(state, self._full_collections())
            except Exception as exc:  # never let a read crash the server
                self._send_json(500, {"error": "failed to build state: {0}".format(exc)})
                return
            self._send_state_json(state)
            return
        # Unknown GET path.
        self._send_json(404, {"error": "not found", "path": route})

    def _read_json_body(self) -> tuple[Optional[dict[str, Any]], Optional[str], int]:
        """Read and JSON-parse the request body. Returns (payload, error, status).

        On success ``payload`` is a dict, ``error`` is None, ``status`` is 200.
        On failure ``payload`` is None, ``error`` is a reason, and ``status`` is
        the HTTP status to return (400 bad body, 408 timeout, 413 too large).
        """
        if self.headers.get("Transfer-Encoding"):
            return None, "chunked Transfer-Encoding is not supported", 400
        length = self.headers.get("Content-Length")
        if length is None:
            n = 0
        else:
            try:
                n = int(length)
            except ValueError:
                return None, "invalid Content-Length", 400
            if n < 0:
                return None, "invalid Content-Length", 400
        if n > MAX_REQUEST_BODY_BYTES:
            return (
                None,
                "request body too large (max {0} bytes)".format(MAX_REQUEST_BODY_BYTES),
                413,
            )
        raw = b""
        if n > 0:
            self.connection.settimeout(BODY_READ_TIMEOUT_SECONDS)
            try:
                raw = self.rfile.read(n)
            except (TimeoutError, OSError):
                return None, "timed out reading request body", 408
            finally:
                self.connection.settimeout(None)
            if len(raw) != n:
                return None, "incomplete request body", 400
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return None, "body must be valid JSON", 400
        if not isinstance(payload, dict):
            return None, "body must be a JSON object", 400
        return payload, None, 200

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        route = self._path_only()
        # The write endpoints are EXACTLY three: /api/lanes, /api/policy, and
        # /api/project (F2). Every other POST path is refused. /api/state is
        # GET-only. Do not add a fourth write here without revisiting the hard
        # invariant in the module docstring + smoke.
        if route not in ("/api/lanes", "/api/policy", "/api/project"):
            self._send_json(404, {"error": "not found", "path": route})
            return

        payload, error, status = self._read_json_body()
        if error is not None:
            self._send_json(status, {"ok": False, "error": error})
            return

        if route == "/api/lanes":
            lane = str(payload.get("lane", ""))
            role = str(payload.get("role", ""))
            result = add_lane(self.loop_dir, lane, role)
        elif route == "/api/policy":
            result = set_max_fix_cycles(self.loop_dir, payload.get("max_fix_cycles"))
        else:  # /api/project
            result = set_project_name(self.loop_dir, payload.get("name"))
        if result.get("ok"):
            _clear_state_snapshot_cache(self.loop_dir)
        self._send_json(200 if result.get("ok") else 400, result)

    # Explicitly reject other verbs with 405 (no writes anywhere else).
    def _reject_405(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET, POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_405()

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_405()

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_405()

    def do_HEAD(self) -> None:  # noqa: N802
        self._reject_405()


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """A threading TCP server that binds fast and carries ``loop_dir``.

    ``allow_reuse_address`` avoids TIME_WAIT bind failures on restart;
    ``daemon_threads`` lets the process exit without joining request threads.
    """

    allow_reuse_address = True
    daemon_threads = True
    loop_dir: Path


def make_server(loop_dir: Path, port: int) -> _ThreadingHTTPServer:
    """Create a server bound to 127.0.0.1 on ``port`` (0 = ephemeral).

    Binds the loopback interface ONLY; the dashboard is never exposed off-host.
    Raises ``OSError`` if the port cannot be bound.
    """
    server = _ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    server.loop_dir = loop_dir
    return server


def make_server_with_fallback(
    loop_dir: Path, port: int
) -> tuple[_ThreadingHTTPServer, bool]:
    """Bind ``port`` if free, otherwise fall back to an ephemeral port (0).

    Returns ``(server, fell_back)``. A busy requested port must never crash the
    dashboard: on any bind ``OSError`` (address in use, permission, etc.) with a
    non-zero requested port, retry once on port 0 so the OS assigns a free port.
    A failure to bind port 0 itself is genuinely fatal and propagates.
    """
    try:
        return make_server(loop_dir, port), False
    except OSError:
        if port == 0:
            raise
        return make_server(loop_dir, 0), True


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only local dashboard for a repo-local multi-agent loop."
    )
    parser.add_argument("--loop-dir", default="docs/loop", help="Loop directory to view.")
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port to bind on 127.0.0.1 (default 8765; 0 picks an ephemeral port).",
    )
    args = parser.parse_args(argv)

    loop_dir = Path(args.loop_dir)
    server, fell_back = make_server_with_fallback(loop_dir, args.port)
    # Test seam: expose the bound server so an in-process smoke can shut down a
    # main() started in a background thread. Never read by the running app.
    global _LAST_SERVER_FOR_TEST
    _LAST_SERVER_FOR_TEST = server
    host, port = server.server_address[0], server.server_address[1]
    # One machine-greppable line, printed AFTER binding, so an orchestrating
    # agent can capture the real URL (never a guessed one). If the requested
    # port was busy the OS chose an ephemeral one; this reports the actual bind.
    print("DASHBOARD_URL=http://{0}:{1}/".format(host, port))
    sys.stdout.flush()
    if fell_back:
        print(
            "requested port {0} was busy; bound ephemeral port {1} instead".format(
                args.port, port
            )
        )
    print("Loop dashboard on http://{0}:{1}/  (loop-dir: {2})".format(host, port, loop_dir))
    print(
        "Read-only view. Writes: POST /api/lanes (add lane), "
        "POST /api/policy (max_fix_cycles), POST /api/project (project name). "
        "Ctrl-C to stop."
    )
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
