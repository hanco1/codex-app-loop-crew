#!/usr/bin/env python3
"""Cross-process file lock + atomic replace for loop shared-state writers.

Stdlib-only, cross-platform (POSIX ``fcntl`` / Windows ``msvcrt``). Several small
scripts (bootstrap_agent_loop.py, deliver_message.py, record_decision.py, and the
dashboard) each do read-modify-write on shared loop files -- agent-lanes.md,
decisions.jsonl, a lane inbox. Without a lock two concurrent writers read the
same snapshot and the last writer silently drops the other's edit; a non-atomic
write can also expose a truncated file to a reader. This module provides the one
primitive they should all share.

Note: precommit_scope_guard.py deliberately does NOT import this -- it never
writes the registry, so it stays self-contained/copy-safe.
"""

from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Union

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


DEFAULT_LOCK_TIMEOUT = 30.0
_POLL_SECONDS = 0.05


def _safe_lock_name(name: str) -> str:
    """Reduce an arbitrary resource string to a tame lock filename."""
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)
    return cleaned or "lock"


def _acquire(fd: int, timeout: float) -> None:
    start = time.monotonic()
    if fcntl is not None:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() - start > timeout:
                    raise SystemExit("loop_file_lock: timed out acquiring lock")
                time.sleep(_POLL_SECONDS)
    elif msvcrt is not None:
        # Lock a single fixed byte at offset 0. Ensure the byte exists first so
        # the region is lockable regardless of the lock file being freshly made.
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
        except OSError:
            pass
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() - start > timeout:
                    raise SystemExit("loop_file_lock: timed out acquiring lock")
                time.sleep(_POLL_SECONDS)
    else:  # pragma: no cover - no locking primitive; best-effort no-op
        return


def _release(fd: int) -> None:
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
    elif msvcrt is not None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


@contextmanager
def loop_file_lock(
    loop_dir: Union[str, Path],
    name: str = "registry",
    timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> Iterator[Path]:
    """Hold an exclusive cross-process lock scoped to ``(loop_dir, name)``.

    Lock files live under ``<loop_dir>/.locks/`` and are advisory: every writer
    of the named resource must take the same-named lock for it to serialize.
    """
    locks_dir = Path(loop_dir) / ".locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / (_safe_lock_name(name) + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        _acquire(fd, timeout)
        try:
            yield lock_path
        finally:
            _release(fd)
    finally:
        os.close(fd)


def atomic_replace(path: Union[str, Path], text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + fsync + os.replace)."""
    target = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, str(target))
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def read_merge_write(
    loop_dir: Union[str, Path],
    path: Union[str, Path],
    name: str,
    transform: Callable[[str], str],
    timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> str:
    """Lock ``name``, read ``path`` (``""`` if absent), apply ``transform``,
    then atomically replace ``path``. Returns the new text."""
    target = Path(path)
    with loop_file_lock(loop_dir, name, timeout):
        try:
            current = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = ""
        new_text = transform(current)
        atomic_replace(target, new_text)
        return new_text
