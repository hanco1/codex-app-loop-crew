#!/usr/bin/env python3
"""Reject a commit whose staged files escape the committing lane's write scope.

This script is the body that the installed git ``pre-commit`` hook invokes. It
is intentionally self-contained (stdlib only, Python 3.8+) so it can be copied
or symlinked into ``.git/hooks`` and run with whatever ``python`` the repo uses.

Enforcement model (advisory leases + lane write scope):

1. Read the active lane from ``CODEX_LANE`` (the committing agent must export
   it). Without a lane the guard fails closed unless ``--allow-unscoped`` is
   set, so an unconfigured commit cannot silently bypass the gate.
2. Read ``docs/loop/agent-lanes.md`` and collect the committing lane's
   ``write_scope`` globs (``;`` separated). Every staged path must match at
   least one of the lane's globs.
3. Read ``docs/loop/leases.md`` and collect active leases held by *other*
   lanes. A staged path that matches another lane's active lease glob is
   rejected, even if it is inside the committing lane's write scope.

The guard only inspects staged paths (``git diff --cached``). It never edits
files and exits non-zero with a clear, actionable message on violation.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


# Lease rows with one of these statuses are treated as held / enforced.
ACTIVE_LEASE_STATUSES = {"ACTIVE", "HELD", "ACQUIRED", "LOCKED"}
# Lease rows with one of these statuses are ignored (no longer held).
INACTIVE_LEASE_STATUSES = {"RELEASED", "EXPIRED", "DONE", "REVOKED", "STALE", ""}


def posix_path(value: str) -> str:
    """Normalize a path to forward slashes for stable glob matching."""
    return value.replace("\\", "/").strip()


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def split_md_row(line: str) -> List[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator_row(cells: List[str]) -> bool:
    return bool(cells) and all(set(cell) <= {"-", ":", " "} for cell in cells)


def parse_table(path: Path) -> List[Dict[str, str]]:
    """Parse the first markdown table in ``path`` into header-keyed rows.

    Mirrors the doctor's parser so the guard sees the registry the same way.
    """
    headers: Optional[List[str]] = None
    rows: List[Dict[str, str]] = []
    for line in read_text(path).splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = split_md_row(line)
        if not cells:
            continue
        if is_separator_row(cells):
            continue
        if headers is None:
            headers = [cell.strip().lower() for cell in cells]
            continue
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        rows.append(dict(zip(headers, cells[: len(headers)])))
    return rows


def parse_table_with_error(path: Path) -> tuple[List[Dict[str, str]], Optional[str]]:
    """Parse a table while preserving a leases-file read failure for the guard."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], "cannot read {0}: {1}: {2}".format(
            posix_path(str(path)), type(exc).__name__, exc
        )

    headers: Optional[List[str]] = None
    rows: List[Dict[str, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.lstrip().startswith("|"):
            continue
        cells = split_md_row(line)
        if not cells:
            continue
        if is_separator_row(cells):
            continue
        if headers is None:
            headers = [cell.strip().lower() for cell in cells]
            continue
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        row = dict(zip(headers, cells[: len(headers)]))
        row["__line__"] = str(line_number)
        rows.append(row)
    return rows, None


def split_scope_globs(write_scope: str) -> List[str]:
    """Split a ``write_scope`` cell into individual glob patterns.

    ``write_scope`` is ``;`` separated and may contain free-text notes such as
    ``implementation notes named by request``. Free-text tokens (no path-like
    or glob characters) are dropped so they neither widen nor block scope.
    """
    globs: List[str] = []
    for raw in write_scope.split(";"):
        token = posix_path(raw)
        if not token:
            continue
        if looks_like_glob(token):
            globs.append(token)
    return globs


def looks_like_glob(token: str) -> bool:
    """Heuristic: keep path/glob tokens, drop English prose tokens."""
    if any(ch in token for ch in "*?[]"):
        return True
    if "/" in token:
        return True
    # A bare filename like ``tracker.md`` is a valid literal path token.
    if "." in token and " " not in token:
        return True
    return False


def path_matches_any(path: str, globs: List[str]) -> bool:
    candidate = posix_path(path)
    for pattern in globs:
        if glob_matches(candidate, pattern):
            return True
    return False


def glob_matches(path: str, pattern: str) -> bool:
    """Match ``path`` against ``pattern`` with ``**`` recursive support.

    fnmatch treats ``*`` as crossing ``/``; that is acceptable here because the
    write-scope globs in this skill use ``**`` for recursion and ``*`` segments
    are rare. We special-case a trailing ``/**`` to also match the directory
    prefix itself (``docs/loop/lanes/review/**`` should match
    ``docs/loop/lanes/review/current.md``).
    """
    pattern = posix_path(pattern)
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.endswith("/**"):
        prefix = pattern[: -len("/**")]
        if path == prefix or path.startswith(prefix + "/"):
            return True
    # Allow a directory-style pattern (``src/`` or ``src``) to match contents.
    if pattern.endswith("/"):
        base = pattern[:-1]
        if path == base or path.startswith(pattern):
            return True
    return False


def staged_paths() -> List[str]:
    """Return staged (cached) paths via git, normalized to posix slashes."""
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise SystemExit(f"precommit_scope_guard: cannot run git: {exc}")
    if out.returncode != 0:
        err = out.stderr.decode("utf-8", "replace").strip()
        raise SystemExit(f"precommit_scope_guard: git diff failed: {err}")
    raw = out.stdout.decode("utf-8", "replace")
    return [posix_path(part) for part in raw.split("\0") if part.strip()]


def lane_write_globs(lanes: List[Dict[str, str]], lane: str) -> Optional[List[str]]:
    """Return the write-scope globs for ``lane``, or None if lane is unknown."""
    for row in lanes:
        if row.get("lane", "").strip() == lane:
            return split_scope_globs(row.get("write_scope", ""))
    return None


def other_lane_scope_matches(
    lanes: List[Dict[str, str]], lane: str, path: str
) -> List[tuple[str, str]]:
    """Return other static lane scopes that cover ``path``."""
    matches: List[tuple[str, str]] = []
    for row in lanes:
        other_lane = row.get("lane", "").strip()
        if not other_lane or other_lane == lane:
            continue
        for glob in split_scope_globs(row.get("write_scope", "")):
            if glob_matches(path, glob):
                if expected_ledger_lane_overlap(lane, other_lane, path):
                    continue
                matches.append((other_lane, glob))
    return matches


def expected_ledger_lane_overlap(lane: str, other_lane: str, path: str) -> bool:
    """Keep the one intentional product-ledger/lane-directory nesting."""
    candidate = posix_path(path)
    if lane == "product":
        return candidate.startswith("docs/loop/lanes/{0}/".format(other_lane))
    if other_lane == "product":
        return candidate.startswith("docs/loop/lanes/{0}/".format(lane))
    return False


def other_lane_leases(leases: List[Dict[str, str]], lane: str) -> List[Dict[str, str]]:
    """Return active lease rows held by lanes other than ``lane``."""
    active: List[Dict[str, str]] = []
    for row in leases:
        status = row.get("status", "").strip().upper()
        if status in INACTIVE_LEASE_STATUSES:
            continue
        # Every remaining status is either known-active or an unknown non-blank
        # value. Both use the same fail-closed enforcement below.
        holder = row.get("lane", "").strip()
        if not holder or holder == lane:
            continue
        glob = posix_path(row.get("file_glob", ""))
        if not glob:
            continue
        active.append(row)
    return active


def malformed_active_lease_findings(
    leases: List[Dict[str, str]], lane: str, leases_file: Path
) -> tuple[List[str], List[str]]:
    """Report ACTIVE-like lease rows whose blank glob would protect no path."""
    violations: List[str] = []
    notes: List[str] = []
    for row in leases:
        status = row.get("status", "").strip().upper()
        if status in INACTIVE_LEASE_STATUSES:
            continue
        if posix_path(row.get("file_glob", "")):
            continue
        holder = row.get("lane", "").strip()
        request_id = row.get("request_id", "").strip() or "?"
        message = (
            "{path} line {line}: {status} lease for lane {holder!r}, request {request}, "
            "has a blank file_glob"
        ).format(
            path=posix_path(str(leases_file)),
            line=row.get("__line__", "?") or "?",
            status=status or "ACTIVE-like",
            holder=holder or "?",
            request=request_id,
        )
        if holder and holder != lane:
            violations.append(message + "; commit rejected until the lease row is fixed")
        else:
            notes.append(message)
    return violations, notes


def evaluate(
    loop_dir: Path,
    lane: str,
    paths: List[str],
    allow_unscoped: bool,
) -> Dict[str, Any]:
    registry = loop_dir / "agent-lanes.md"
    leases_file = loop_dir / "leases.md"

    lanes = parse_table(registry)
    leases, leases_read_error = parse_table_with_error(leases_file)

    violations: List[str] = []
    notes: List[str] = []
    if leases_read_error:
        notes.append(leases_read_error)

    known_lanes = {row.get("lane", "").strip() for row in lanes if row.get("lane", "").strip()}
    globs = lane_write_globs(lanes, lane)

    if globs is None:
        if known_lanes:
            violations.append(
                "lane {lane!r} is not registered in {reg} (known lanes: {known})".format(
                    lane=lane, reg=posix_path(str(registry)), known=", ".join(sorted(known_lanes))
                )
            )
        else:
            violations.append(
                "no lanes registered in {reg}; run bootstrap_agent_loop.py first".format(
                    reg=posix_path(str(registry))
                )
            )
        return {"ok": False, "violations": violations, "notes": notes}

    if not globs:
        violations.append(
            "lane {lane!r} has an empty write_scope in {reg}".format(
                lane=lane, reg=posix_path(str(registry))
            )
        )
        return {"ok": False, "violations": violations, "notes": notes}

    malformed_violations, malformed_notes = malformed_active_lease_findings(
        leases, lane, leases_file
    )
    violations.extend(malformed_violations)
    notes.extend(malformed_notes)
    held = other_lane_leases(leases, lane)

    for path in paths:
        if not path_matches_any(path, globs):
            violations.append(
                "{path} is outside write_scope for lane {lane!r} ({scope})".format(
                    path=path, lane=lane, scope="; ".join(globs)
                )
            )
            continue
        if leases_read_error:
            static_matches = other_lane_scope_matches(lanes, lane, path)
            if static_matches:
                violations.append(
                    "{path} cannot be safely checked against dynamic leases because {error}; "
                    "it is also inside other lane static scope(s): {scopes}".format(
                        path=path,
                        error=leases_read_error,
                        scopes=", ".join(
                            "{0!r} ({1})".format(other_lane, glob)
                            for other_lane, glob in static_matches
                        ),
                    )
                )
            continue
        static_matches = other_lane_scope_matches(lanes, lane, path)
        if static_matches:
            own_matches = [glob for glob in globs if glob_matches(path, glob)]
            violations.append(
                "{path} is inside overlapping write_scope entries for lane {lane!r} "
                "({own}) and {others}. Fix the scopes in {registry} before committing "
                "this shared region.".format(
                    path=path,
                    lane=lane,
                    own="; ".join(own_matches),
                    others=", ".join(
                        "lane {0!r} ({1})".format(other_lane, glob)
                        for other_lane, glob in static_matches
                    ),
                    registry=posix_path(str(registry)),
                )
            )
            continue
        for lease in held:
            glob = posix_path(lease.get("file_glob", ""))
            if glob_matches(path, glob):
                violations.append(
                    "{path} is covered by an active lease held by lane {holder!r} "
                    "(file_glob {glob}, request {req})".format(
                        path=path,
                        holder=lease.get("lane", "").strip(),
                        glob=glob,
                        req=lease.get("request_id", "").strip() or "?",
                    )
                )

    return {"ok": not violations, "violations": violations, "notes": notes}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reject staged files outside the committing lane's write scope or "
        "covered by another lane's active lease."
    )
    parser.add_argument("--loop-dir", default="docs/loop", help="Loop directory (default docs/loop).")
    parser.add_argument(
        "--lane",
        default=os.environ.get("CODEX_LANE", "").strip(),
        help="Committing lane. Defaults to env CODEX_LANE.",
    )
    parser.add_argument(
        "--allow-unscoped",
        action="store_true",
        help="Allow the commit when no lane is set (default fails closed).",
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        default=None,
        help="Override staged paths (for testing). Defaults to git diff --cached.",
    )
    args = parser.parse_args(argv)

    lane = args.lane.strip()
    if not lane:
        if args.allow_unscoped:
            print("precommit_scope_guard: no CODEX_LANE set; skipping (--allow-unscoped).")
            return 0
        sys.stderr.write(
            "precommit_scope_guard: blocked.\n"
            "  No committing lane set. Export CODEX_LANE=<lane> before committing,\n"
            "  e.g. CODEX_LANE=implementation git commit -m ...\n"
            "  (or pass --allow-unscoped to bypass deliberately).\n"
        )
        return 1

    loop_dir = Path(args.loop_dir)
    if args.paths is not None:
        paths = [posix_path(p) for p in args.paths if p.strip()]
    else:
        paths = staged_paths()

    if not paths:
        print("precommit_scope_guard: no staged files; nothing to check.")
        return 0

    result = evaluate(loop_dir, lane, paths, args.allow_unscoped)
    for note in result["notes"]:
        sys.stderr.write("precommit_scope_guard: warning: {0}\n".format(note))
    if result["ok"]:
        print(
            "precommit_scope_guard: OK ({n} staged file(s) within lane {lane!r} scope).".format(
                n=len(paths), lane=lane
            )
        )
        return 0

    sys.stderr.write("precommit_scope_guard: commit REJECTED for lane {lane!r}.\n".format(lane=lane))
    for violation in result["violations"]:
        sys.stderr.write("  - {0}\n".format(violation))
    sys.stderr.write(
        "\nFix options:\n"
        "  - Restage only files inside your lane's write_scope.\n"
        "  - Coordinate via requests.md and hand the file to its owning lane.\n"
        "  - Release the blocking lease in {leases} if it is stale.\n".format(
            leases=posix_path(str(loop_dir / "leases.md"))
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
