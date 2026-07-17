#!/usr/bin/env python3
"""Append one decision to the repo-local, append-only decision log.

This is the memory layer's only writer (reference implementation of
methodology invariant 5: derived memory is an auditable cache, never truth).
It appends exactly one JSON line to ``docs/loop/memory/decisions.jsonl`` in
append mode. It NEVER reads and rewrites prior lines: a decision is superseded
by appending a new line whose ``supersedes`` names the old ``decision_id``.

The ``content_hash`` is computed by ``normalize_then_hash`` over the current
bytes of the listed ``source_docs``. That function is THE single canonical hash
implementation for the whole memory layer; the doctor imports it from here so
both sides agree byte-for-byte. Two hash implementations would make Windows
CRLF files look permanently stale, so there is exactly one definition and the
doctor self-checks that it imported this one.

This helper is stdlib-only and deterministic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loop_lock import loop_file_lock


# Separator concatenated between per-document normalized text before hashing.
# A NUL byte cannot appear in decoded UTF-8 text content in practice and keeps
# distinct source docs from bleeding into one another (so [\"ab\", \"c\"] and
# [\"a\", \"bc\"] hash differently).
_HASH_SEPARATOR = "\x00"


def normalize_then_hash(paths: Iterable) -> str:
    """Return the canonical sha256 hex digest of the given source documents.

    THE single canonical hash for the memory layer. Contract (see
    references/memory.md): for each source doc, read its bytes, decode UTF-8,
    replace every CRLF with LF, strip trailing newlines, then concatenate all
    per-doc normalized texts with a fixed separator and sha256 the UTF-8 bytes
    of the result.

    This function is total: a missing or unreadable file contributes an empty
    string for its slot rather than raising, so both the writer and the doctor
    can always compute a comparable digest. Existence of source docs is checked
    separately by callers that need to warn about it (the doctor emits
    ``missing_source_doc``); this keeps the hash deterministic regardless.
    """
    normalized_parts: List[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        try:
            data = path.read_bytes()
        except OSError:
            normalized_parts.append("")
            continue
        text = data.decode("utf-8", errors="replace")
        # CRLF -> LF first, then a lone CR -> LF, so line endings never affect
        # the digest across platforms.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # Strip trailing newlines only (do not touch interior or leading ones).
        text = text.rstrip("\n")
        normalized_parts.append(text)
    joined = _HASH_SEPARATOR.join(normalized_parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    """ISO-8601 UTC timestamp with a trailing Z, second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _count_existing_for_request(decisions_path: Path, request_id: str) -> int:
    """Count existing decision lines already recorded for ``request_id``.

    Read-only: this reads the log to derive a stable per-request sequence
    number for the next ``decision_id``. It never rewrites any line. A missing
    or unreadable log counts as zero.
    """
    if not decisions_path.exists():
        return 0
    count = 0
    try:
        text = decisions_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    except UnicodeDecodeError:
        raise SystemExit(
            "decisions.jsonl is not valid UTF-8: {0}; re-save it as UTF-8".format(
                str(decisions_path)
            )
        )
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            # A malformed prior line is the doctor's concern, not ours; skip it
            # for counting so we still produce a unique-enough id.
            continue
        if isinstance(obj, dict) and str(obj.get("request_id", "")).strip() == request_id:
            count += 1
    return count


def _decision_id_exists(decisions_path: Path, decision_id: str) -> bool:
    """Return True if ``decision_id`` is already recorded in the log."""
    if not decisions_path.exists():
        return False
    try:
        text = decisions_path.read_text(encoding="utf-8")
    except OSError:
        return False
    except UnicodeDecodeError:
        raise SystemExit(
            "decisions.jsonl is not valid UTF-8: {0}; re-save it as UTF-8".format(
                str(decisions_path)
            )
        )
    target = decision_id.strip()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and str(obj.get("decision_id", "")).strip() == target:
            return True
    return False


def _derive_decision_id(request_id: str, decisions_path: Path, explicit: str) -> str:
    """Return a stable decision_id.

    Priority: an explicit ``--decision-id`` wins (rejected if it already exists);
    otherwise derive from the request_id plus the next per-request sequence
    number. Callers must hold the ``decisions`` lock so the count/derivation and
    the append are one critical section.
    """
    explicit = (explicit or "").strip()
    if explicit:
        if _decision_id_exists(decisions_path, explicit):
            raise SystemExit("decision_id already exists: {0}".format(explicit))
        return explicit
    seq = _count_existing_for_request(decisions_path, request_id) + 1
    safe_request = request_id.strip() or "REQ-UNKNOWN"
    return "{0}-d{1}".format(safe_request, seq)


def _normalize_gate_status(value: str) -> str:
    """Record the completion-gate token verbatim, restricted to known values.

    Accepts SHIP_CHECK_OK / SHIP_CHECK_FAIL / none exactly. Anything else (or
    blank) is normalized to ``none`` so a decision made without a fresh gate is
    visibly tentative rather than silently trusted.
    """
    token = (value or "").strip()
    if token in {"SHIP_CHECK_OK", "SHIP_CHECK_FAIL", "none"}:
        return token
    return "none"


def build_record(args: argparse.Namespace, loop_dir: Path, decisions_path: Path) -> dict:
    source_docs = [str(doc) for doc in (args.source_doc or [])]
    # Hash over the source docs as they exist right now (write-time snapshot).
    content_hash = normalize_then_hash(source_docs)
    decision_id = _derive_decision_id(args.request_id, decisions_path, args.decision_id)
    created_at = (args.created_at or "").strip() or _now_iso()
    return {
        "decision_id": decision_id,
        "request_id": args.request_id.strip(),
        "lane": (args.lane or "").strip(),
        "decision": (args.decision or "").strip(),
        "rationale": (args.rationale or "").strip(),
        "alternatives_rejected": (args.alternatives or "").strip(),
        "supersedes": (args.supersedes or "").strip(),
        "source_docs": source_docs,
        "content_hash": content_hash,
        "gate_status": _normalize_gate_status(args.gate_status),
        "created_at": created_at,
    }


def append_decision(decisions_path: Path, record: dict) -> None:
    """Append exactly one JSON line. Never reads or rewrites prior lines."""
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with decisions_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Append one decision to docs/loop/memory/decisions.jsonl "
            "(append-only; supersede by appending, never edit prior lines)."
        )
    )
    parser.add_argument("--loop-dir", default="docs/loop", help="Loop directory root.")
    parser.add_argument("--request-id", required=True, help="Request this decision serves.")
    parser.add_argument("--lane", default="", help="Lane that made the decision.")
    parser.add_argument("--decision", default="", help="What was decided (one line).")
    parser.add_argument("--rationale", default="", help="Why this decision was made.")
    parser.add_argument(
        "--alternatives",
        default="",
        help="Alternatives considered and rejected.",
    )
    parser.add_argument(
        "--supersedes",
        default="",
        help="decision_id this record supersedes, or blank.",
    )
    parser.add_argument(
        "--source-doc",
        action="append",
        default=[],
        help="A source file this decision derives from/depends on (repeatable).",
    )
    parser.add_argument(
        "--gate-status",
        default="none",
        help="Completion-gate token at write time: SHIP_CHECK_OK, SHIP_CHECK_FAIL, or none.",
    )
    parser.add_argument(
        "--decision-id",
        default="",
        help="Override the derived decision_id (otherwise request_id + sequence).",
    )
    parser.add_argument(
        "--created-at",
        default="",
        help="Override the created_at timestamp (otherwise now, ISO-8601 UTC).",
    )
    args = parser.parse_args(argv)

    if not args.request_id.strip():
        raise SystemExit("--request-id must not be blank")

    loop_dir = Path(args.loop_dir)
    decisions_path = loop_dir / "memory" / "decisions.jsonl"

    # A missing source doc at write time is a user error worth surfacing, even
    # though normalize_then_hash tolerates it. Fail closed so the recorded hash
    # is never quietly taken over a file that does not exist.
    missing = [doc for doc in (args.source_doc or []) if not Path(doc).exists()]
    if missing:
        raise SystemExit(
            "source doc(s) not found: {0}".format(", ".join(missing))
        )

    # Count-and-append under one lock so two concurrent writers for the same
    # request cannot both derive the same "-dN" id, and an explicit
    # --decision-id collision is detected against the log at write time.
    with loop_file_lock(loop_dir, "decisions"):
        record = build_record(args, loop_dir, decisions_path)
        append_decision(decisions_path, record)
    print("recorded {0} -> {1}".format(record["decision_id"], decisions_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
