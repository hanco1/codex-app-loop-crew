#!/usr/bin/env python3
"""Install a git pre-commit hook that enforces lane write scope and leases.

The installed hook is a tiny portable shell wrapper that invokes
``precommit_scope_guard.py`` with the repo's Python. The guard reads
``docs/loop/agent-lanes.md`` write scopes and ``docs/loop/leases.md`` active
leases and rejects commits whose staged files escape the committing lane's
scope (lane is passed via the ``CODEX_LANE`` environment variable).

This installer is stdlib-only (Python 3.8+), deterministic, and idempotent:
re-running it rewrites only the hook it manages and never clobbers an
unrelated, hand-written pre-commit hook unless ``--force`` is given.
"""

from __future__ import annotations

import argparse
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional


# Marker line so the installer can recognize a hook it owns and safely
# overwrite it without clobbering an unrelated user hook.
HOOK_MARKER = "# codex-agent-loop-orchestrator:lease-precommit"


HOOK_TEMPLATE = """#!/bin/sh
{marker}
# Enforces docs/loop/agent-lanes.md write_scope and docs/loop/leases.md leases.
# Regenerate with scripts/install_precommit.py. Do not edit by hand.
# Pass the active lane via CODEX_LANE; the guard fails closed without it.

GUARD={guard}
LOOP_DIR={loop_dir}
DEFAULT_PY={default_python}

if [ -n \"$CODEX_PRECOMMIT_SKIP\" ]; then
    echo \"precommit_scope_guard: skipped via CODEX_PRECOMMIT_SKIP.\" 1>&2
    exit 0
fi

# Interpreter resolution order: $CODEX_PYTHON override -> the interpreter that
# installed this hook (baked at install time) -> python3 -> python. Bare
# python/python3 are unreliable inside git's hook environment on Windows
# (they may resolve to the Microsoft Store stub), so the baked path wins.
PY=\"$CODEX_PYTHON\"
if [ -z \"$PY\" ] && [ -n \"$DEFAULT_PY\" ] && [ -x \"$DEFAULT_PY\" ]; then
    PY=\"$DEFAULT_PY\"
fi
if [ -z \"$PY\" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PY=python3
    else
        PY=python
    fi
fi

exec \"$PY\" \"$GUARD\" --loop-dir \"$LOOP_DIR\"
"""


def posix_path(value: str) -> str:
    return str(value).replace("\\", "/")


def find_git_dir(repo: Path) -> Path:
    """Resolve the repo's git dir, honoring worktrees and ``git`` config.

    Falls back to ``<repo>/.git`` when git is unavailable so the installer
    still works in a plain checkout without git on PATH.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        out = None

    if out is not None and out.returncode == 0:
        git_dir = out.stdout.decode("utf-8", "replace").strip()
        if git_dir:
            candidate = Path(git_dir)
            if not candidate.is_absolute():
                candidate = (repo / candidate).resolve()
            return candidate

    fallback = repo / ".git"
    if fallback.is_dir():
        return fallback
    raise SystemExit(
        "install_precommit: {0} is not a git repo (no .git dir found). "
        "Run inside the target repository or pass --repo.".format(posix_path(str(repo)))
    )


def hooks_dir_for(repo: Path, git_dir: Path) -> Path:
    """Return the hooks directory, honoring ``core.hooksPath`` when set."""
    try:
        out = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        out = None
    if out is not None and out.returncode == 0:
        configured = out.stdout.decode("utf-8", "replace").strip()
        if configured:
            hooks = Path(configured)
            if not hooks.is_absolute():
                hooks = (repo / hooks).resolve()
            return hooks
    return git_dir / "hooks"


def existing_is_managed(hook_path: Path) -> bool:
    if not hook_path.exists():
        return True
    try:
        text = hook_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return HOOK_MARKER in text


def make_executable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        # Filesystems without POSIX exec bits (e.g. Windows) ignore this; the
        # hook still runs because git invokes it through the shell.
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install a git pre-commit hook enforcing lane write_scope and leases."
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Target repository root (default: current directory).",
    )
    parser.add_argument(
        "--loop-dir",
        default="docs/loop",
        help="Loop directory the hook reads (default docs/loop).",
    )
    parser.add_argument(
        "--guard",
        default=None,
        help="Path to precommit_scope_guard.py. Defaults to the copy next to this installer.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing non-managed pre-commit hook.",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print the hook body and target path without writing.",
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not repo.exists():
        raise SystemExit("install_precommit: repo path does not exist: {0}".format(posix_path(str(repo))))

    guard_path = (
        Path(args.guard).resolve()
        if args.guard
        else (Path(__file__).resolve().parent / "precommit_scope_guard.py")
    )
    if not args.print_only and not guard_path.exists():
        raise SystemExit(
            "install_precommit: guard script not found: {0}".format(posix_path(str(guard_path)))
        )

    git_dir = find_git_dir(repo)
    hooks_dir = hooks_dir_for(repo, git_dir)
    hook_path = hooks_dir / "pre-commit"

    # shlex.quote supplies its own single-quoting, so the template assigns the
    # values bare; a path containing $, backticks, or spaces stays a literal.
    body = HOOK_TEMPLATE.format(
        marker=HOOK_MARKER,
        guard=shlex.quote(posix_path(str(guard_path))),
        loop_dir=shlex.quote(posix_path(args.loop_dir)),
        default_python=shlex.quote(posix_path(sys.executable or "")),
    )

    if args.print_only:
        print("# target: {0}".format(posix_path(str(hook_path))))
        print(body, end="")
        return 0

    if not existing_is_managed(hook_path) and not args.force:
        raise SystemExit(
            "install_precommit: refusing to overwrite existing pre-commit hook at\n"
            "  {0}\n"
            "It was not created by this installer. Re-run with --force to replace it.".format(
                posix_path(str(hook_path))
            )
        )

    hooks_dir.mkdir(parents=True, exist_ok=True)
    # Atomic install: write to a temp file in the hooks dir, set the exec bit
    # on the temp file, then os.replace onto the hook path so a concurrent
    # ``git commit`` never reads a truncated/half-written hook.
    fd, tmp_name = tempfile.mkstemp(prefix=".pre-commit.", dir=str(hooks_dir))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
        make_executable(tmp_path)
        os.replace(str(tmp_path), str(hook_path))
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise

    print("install_precommit: wrote {0}".format(posix_path(str(hook_path))))
    print("install_precommit: guard {0}".format(posix_path(str(guard_path))))
    print("install_precommit: loop-dir {0}".format(posix_path(args.loop_dir)))
    print(
        "Set CODEX_LANE before committing, e.g.:\n"
        "  CODEX_LANE=implementation git commit -m \"...\""
    )
    if os.name == "nt":
        print(
            "Note: on Windows, git runs hooks via its bundled sh; ensure the\n"
            "      committing shell exports CODEX_LANE (env var, not PowerShell $env only\n"
            "      unless committing from that same PowerShell session)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
