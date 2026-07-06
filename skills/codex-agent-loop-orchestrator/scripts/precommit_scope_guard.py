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


def other_lane_leases(leases: List[Dict[str, str]], lane: str) -> List[Dict[str, str]]:
    """Return active lease rows held by lanes other than ``lane``."""
    active: List[Dict[str, str]] = []
    for row in leases:
        status = row.get("status", "").strip().upper()
        if status in INACTIVE_LEASE_STATUSES:
            continue
        if status and status not in ACTIVE_LEASE_STATUSES:
            # Unknown status: treat as active (fail closed) but only if it
            # names a real glob and another lane.
            pass
        holder = row.get("lane", "").strip()
        if not holder or holder == lane:
            continue
        glob = posix_path(row.get("file_glob", ""))
        if not glob:
            continue
        active.append(row)
    return active


def evaluate(
    loop_dir: Path,
    lane: str,
    paths: List[str],
    allow_unscoped: bool,
) -> Dict[str, Any]:
    registry = loop_dir / "agent-lanes.md"
    leases_file = loop_dir / "leases.md"

    lanes = parse_table(registry)
    leases = parse_table(leases_file)

    violations: List[str] = []
    notes: List[str] = []

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

    held = other_lane_leases(leases, lane)

    for path in paths:
        if not path_matches_any(path, globs):
            violations.append(
                "{path} is outside write_scope for lane {lane!r} ({scope})".format(
                    path=path, lane=lane, scope="; ".join(globs)
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
