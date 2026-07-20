#!/usr/bin/env python3
"""Atomically deliver a cross-agent message into a target lane inbox.

This helper writes a message file using a Maildir-style two-step contract so a
concurrent reader never observes a torn or partial message:

    1. Write the full body to docs/loop/lanes/<lane>/inbox/tmp/<id>.md.
    2. fsync the file, then os.replace() it to inbox/new/<id>.md.

os.replace() is atomic on the same filesystem on both POSIX and Windows, so the
target name appears only once the whole message is on disk. The reader scans
inbox/new, processes each message, then moves it to inbox/cur.

The helper is read-only with respect to project code. It only touches the
target lane inbox tree plus an append-only index row. It is deterministic and
idempotent: a given request_id + message_type + iteration always maps to the
same message id, and re-delivery is a no-op once the message exists in
inbox/new or inbox/cur (unless --force is given). Metadata comes from the CLI
flags or, when a flag is omitted, from the body's leading envelope lines
(message_type:/request_id:/iteration:/from_lane:); a flag that disagrees with
the envelope is an error, and message_type + iteration must be supplied by one
of the two sources.

It is stdlib-only and compatible with the existing flat inbox.md fallback; see
references/protocol.md "Atomic Message Delivery" for the migration story.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loop_lock import loop_file_lock


KNOWN_MESSAGE_TYPES = [
    "IMPLEMENTATION_REQUEST",
    "IMPLEMENTATION_DONE",
    "PRE_IMPLEMENTATION_TEST_REQUEST",
    "REVIEW_REQUEST",
    "REVIEW_DONE",
    "FIX_REQUEST",
    "BLOCKED",
    "HUMAN_QA_REQUEST",
    "LOOP_STATUS",
]

INDEX_HEADER = (
    "# {title} Inbox Index\n"
    "\n"
    "Append-only delivery log for inbox/new. One row per atomically delivered\n"
    "message. Readers process inbox/new, then move each file to inbox/cur.\n"
    "\n"
    "| delivered_at | message_id | request_id | iteration | from_lane | message_type | state |\n"
    "| --- | --- | --- | --- | --- | --- | --- |\n"
)

SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")

# D5 path-traversal guard: a value that becomes a single path segment under
# docs/loop must be a plain name. The leading character class rejects empty
# values and dot-leading segments ('.', '..', hidden files); the body class
# rejects '/', '\\', ':', NUL, and every other separator or metacharacter.
# fullmatch (not match) so a trailing newline cannot ride along; trailing
# dots/spaces and DOS device names are rejected separately because Windows
# aliases them onto other paths ('lane.' resolves to 'lane'; NUL/COM1 are
# devices regardless of extension).
SAFE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

_DOS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {"com{0}".format(i) for i in range(1, 10)}
    | {"lpt{0}".format(i) for i in range(1, 10)}
)


def is_safe_name(value: str) -> bool:
    """True when ``value`` is a safe single path segment on POSIX and Windows."""

    if not SAFE_NAME_RE.fullmatch(value):
        return False
    if value.endswith(".") or value.endswith(" "):
        return False
    if value.split(".", 1)[0].lower() in _DOS_RESERVED_NAMES:
        return False
    return True


def posix_path(value: str) -> str:
    return value.replace("\\", "/")


def safe_name(value: str, flag: str) -> str:
    """Validate ``value`` as a safe single path segment; exit loudly otherwise."""

    if not is_safe_name(value):
        raise SystemExit(
            "{0} value {1!r} is not a safe name: use only letters, digits, "
            "'.', '_' and '-', starting with a letter or digit (no path "
            "separators, no leading/trailing dot, no DOS device names).".format(
                flag, value
            )
        )
    return value


def assert_within(base: Path, child: Path) -> None:
    """Belt-and-suspenders containment check: ``child`` must resolve under ``base``."""

    base_resolved = str(base.resolve())
    child_resolved = str(child.resolve())
    try:
        contained = os.path.commonpath([base_resolved, child_resolved]) == base_resolved
    except ValueError:
        # Different drives / mixed absolute-relative on Windows: not contained.
        contained = False
    if not contained:
        raise SystemExit(
            "refusing to write outside {0}: {1}".format(
                posix_path(base_resolved), posix_path(child_resolved)
            )
        )


def title_for(lane: str) -> str:
    return lane.replace("-", " ").replace("_", " ").title()


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def slugify(value: str, default: str) -> str:
    cleaned = SLUG_RE.sub("-", value.strip()).strip("-._")
    return cleaned or default


def build_message_id(
    request_id: str,
    message_type: str,
    iteration: str,
    explicit_id: Optional[str],
) -> str:
    """Derive a deterministic, filesystem-safe message id.

    Determinism makes re-delivery idempotent: the same logical message always
    resolves to the same target filename, so a duplicate run is a no-op rather
    than a second copy.
    """

    if explicit_id:
        return slugify(explicit_id, default="message")

    req = slugify(request_id, default="REQ-unknown") if request_id else "REQ-unknown"
    mtype = slugify(message_type, default="MESSAGE") if message_type else "MESSAGE"
    itr = slugify(iteration, default="1") if iteration else "1"
    return "{req}--{mtype}--iter-{itr}".format(req=req, mtype=mtype, itr=itr)


def read_body(message_file: Optional[str]) -> str:
    if message_file and message_file != "-":
        path = Path(message_file)
        if not path.exists():
            raise SystemExit("--message-file not found: {0}".format(message_file))
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise SystemExit(
                "--message-file is not valid UTF-8: {0}; re-save it as "
                "UTF-8".format(posix_path(str(path)))
            )
    try:
        data = sys.stdin.read()
    except UnicodeDecodeError:
        raise SystemExit(
            "stdin message body is not valid UTF-8; re-encode it as UTF-8 "
            "before piping."
        )
    # A strict-UTF-8 stdin raises above; a surrogateescape stdin (Windows)
    # reads garbage bytes "successfully" as lone surrogates that would only
    # blow up later when the body is written back out. Reject them here with
    # the same actionable message.
    try:
        data.encode("utf-8")
    except UnicodeEncodeError:
        raise SystemExit(
            "stdin message body is not valid UTF-8; re-encode it as UTF-8 "
            "before piping."
        )
    if not data.strip():
        raise SystemExit(
            "No message body provided. Pass --message-file PATH or pipe the body on stdin."
        )
    return data


# Envelope fields deliver_message can fall back to when the matching CLI flag
# is omitted (references/protocol.md "Message Envelope").
ENVELOPE_FIELDS = ("message_type", "request_id", "iteration", "from_lane")

ENVELOPE_LINE_RE = re.compile(r"^([a-z][a-z0-9_]*):\s*(.*?)\s*$")


def parse_envelope(body: str) -> dict:
    """Extract the leading protocol-envelope fields from a message body.

    The protocol envelope opens the body with a ``# <MESSAGE_TYPE>`` heading
    followed IMMEDIATELY by contiguous ``key: value`` lines (``message_type:``,
    ``request_id:``, ``iteration:``, ``from_lane:``, ...) plus ``- item`` list
    values. Two hard boundaries keep prose from being misread as metadata:
    (1) a body whose first non-empty line is NOT a heading has no envelope at
    all — a stray ``label: text`` opening a plain-prose body is never treated
    as metadata; (2) the first blank line after the heading ends the envelope
    block, so a quoted/stray ``request_id:``-style line further down the body
    is never captured. Only the first occurrence of each field wins. Returns a
    dict of the ENVELOPE_FIELDS present with non-empty values; a body with no
    envelope yields an empty dict (malformed envelopes degrade, not crash).
    """

    fields: dict = {}
    seen_heading = False
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            if seen_heading:
                break  # a blank line ends the envelope block
            continue  # leading blank lines before the heading
        if line.startswith("#"):
            if seen_heading:
                break  # a second heading means the envelope block is over
            seen_heading = True
            continue
        if not seen_heading:
            break  # body does not open with a protocol heading: no envelope
        if line.startswith("- "):
            continue  # list values (source_docs, delivery, ...) inside the envelope
        match = ENVELOPE_LINE_RE.match(line)
        if not match:
            break
        key, value = match.group(1), match.group(2)
        if key in ENVELOPE_FIELDS and key not in fields and value:
            fields[key] = value
    return fields


def resolve_field(name: str, flag_value: str, envelope: dict) -> str:
    """Resolve one metadata field: explicit CLI flag wins, else envelope value.

    If both are present and disagree, exit non-zero naming the field so a
    mismatched flag can never silently misfile a message.
    """

    flag = (flag_value or "").strip()
    env = (envelope.get(name) or "").strip()
    if flag and env and flag != env:
        raise SystemExit(
            "{0} mismatch: --{1} {2!r} disagrees with the message envelope's "
            "'{0}: {3}'. Make them agree or drop one source.".format(
                name, name.replace("_", "-"), flag, env
            )
        )
    return flag or env


def ensure_inbox_tree(inbox_dir: Path) -> None:
    for sub in ("tmp", "new", "cur"):
        (inbox_dir / sub).mkdir(parents=True, exist_ok=True)


def existing_delivery(inbox_dir: Path, message_id: str) -> Optional[str]:
    """Return the subdir name ('new' or 'cur') if this message already exists."""

    for sub in ("new", "cur"):
        if (inbox_dir / sub / (message_id + ".md")).exists():
            return sub
    return None


def fsync_dir(directory: Path) -> None:
    """Best-effort directory fsync so a rename is durable. No-op on Windows."""

    if os.name != "posix":
        return
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(target: Path, body: str) -> None:
    """Write body to a sibling tmp file, fsync, then os.replace into target.

    os.replace is atomic on the same filesystem on POSIX and Windows, so a
    concurrent reader scanning the parent directory never sees a partial file
    under the final name.
    """

    tmp_dir = target.parent.parent / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=target.stem + ".",
        suffix=".tmp",
        dir=str(tmp_dir),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp_path), str(target))
    except BaseException:
        # On any failure, leave nothing half-delivered under the final name.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise
    fsync_dir(target.parent)


def archive_message(loop_dir: Path, request_id: str, message_type: str,
                    iteration: str, body: str) -> Optional[str]:
    """Archive a message into the durable ``messages/<request_id>/`` store.

    G11(a): the request-scoped message directory is created ONLY at the moment a
    real message file is written into it -- never pre-created before the
    ``request_id`` is final. Run 2 left two empty ``messages/<request_id>/``
    stray dirs behind because a request was re-keyed after its directory had
    already been minted; routing every archive through this helper (which
    ``mkdir``s the dir in the same call that writes the file) makes an empty
    stray dir impossible to produce through the tooling.

    Returns the posix path of the archived file, or ``None`` when there is no
    ``request_id`` to key it under (no request-scoped store applies then, so no
    directory is created). Never raises for a missing dir; it creates what it
    needs, but only alongside a real write.
    """
    request_id = (request_id or "").strip()
    if not request_id:
        # No final id -> no request-scoped store, and crucially no empty dir.
        return None
    mtype = slugify(message_type, default="MESSAGE") if message_type else "MESSAGE"
    itr = slugify(iteration, default="1") if iteration else "1"
    msg_dir = loop_dir / "messages" / request_id
    # D5: the request id was validated by safe_name in main; keep a
    # belt-and-suspenders containment check here so no importer can archive
    # outside the messages store either.
    assert_within(loop_dir / "messages", msg_dir)
    # mkdir + write happen together: the directory never exists without a file.
    msg_dir.mkdir(parents=True, exist_ok=True)
    target = msg_dir / "{mtype}-iter-{itr}.md".format(mtype=mtype, itr=itr)
    _rewrite_atomic(target, body)
    return posix_path(str(target))


def lane_registered(loop_dir: Path, lane: str) -> Optional[bool]:
    """Return whether ``lane`` has a row in agent-lanes.md.

    True/False when the registry could be read; None when it is missing or
    unreadable (unknown -- callers should not punish the sender for a registry
    they cannot see).
    """

    registry = loop_dir / "agent-lanes.md"
    try:
        text = registry.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    header_cols: Optional[list] = None
    lane_index = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if cells and all(set(c) <= {"-", ":", " "} for c in cells):
            continue
        if header_cols is None:
            header_cols = [c.lower() for c in cells]
            if "lane" in header_cols:
                lane_index = header_cols.index("lane")
            continue
        if lane_index < len(cells) and cells[lane_index] == lane:
            return True
    return False


def stamp_lane_heartbeat(loop_dir: Path, lane: str, when: str) -> list[str]:
    """Refresh ``lane``'s heartbeat when it sends a message (F7).

    Updates two mirrors of the same value, both write-if-present (never creates
    a file, never adds a lane):

    - the ``heartbeat`` column of ``lane``'s row in ``agent-lanes.md``;
    - the ``heartbeat:`` and ``last_updated:`` lines in
      ``lanes/<lane>/current.md`` if that file exists.

    A sender that delivers a message is demonstrably alive, so delivery is a
    natural heartbeat. Returns the list of files it rewrote (for reporting).
    Best-effort and defensive: any per-file failure emits a stderr warning but
    never blocks the actual delivery.
    """
    if not lane:
        return []
    touched: list[str] = []

    registry = loop_dir / "agent-lanes.md"
    if _update_registry_heartbeat(registry, lane, when):
        touched.append(posix_path(str(registry)))

    # D5 belt-and-suspenders: callers validate the lane name, but this is a
    # module-level helper -- never write a current.md outside the lanes tree.
    # Soft check (skip, not exit): the heartbeat is best-effort and runs after
    # a successful delivery.
    lanes_root = loop_dir / "lanes"
    current = lanes_root / lane / "current.md"
    try:
        contained = (
            os.path.commonpath([str(lanes_root.resolve()), str(current.resolve())])
            == str(lanes_root.resolve())
        )
    except (OSError, ValueError):
        contained = False
    if contained and _update_current_heartbeat(current, when):
        touched.append(posix_path(str(current)))

    return touched


def _warn_heartbeat_failure(path: Path, exc: BaseException) -> None:
    """Make a best-effort heartbeat degradation visible without changing delivery."""
    sys.stderr.write(
        "warning: heartbeat stamp failed for {0}: {1}: {2}\n".format(
            posix_path(str(path)), type(exc).__name__, exc
        )
    )


def _rewrite_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a tmp file + os.replace (atomic swap)."""
    fd, tmp_name = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp_path), str(path))
    except BaseException:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _update_registry_heartbeat(registry: Path, lane: str, when: str) -> bool:
    """Set the ``heartbeat`` cell of ``lane``'s row in agent-lanes.md.

    Locates the header row to find the heartbeat column index, then rewrites
    only the matching lane's data row. Returns True if a row was updated.
    """
    if not registry.exists():
        return False
    # Serialize under the shared ``registry`` lock and re-read inside it so a
    # concurrent bootstrap (which rewrites the whole registry) or another
    # heartbeat cannot lose this update to a stale-snapshot overwrite.
    with loop_file_lock(registry.parent, "registry"):
        try:
            original = registry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _warn_heartbeat_failure(registry, exc)
            return False

        lines = original.splitlines(keepends=True)
        header_cols: Optional[list[str]] = None
        hb_index: Optional[int] = None
        lane_index = 0
        changed = False
        out: list[str] = []

        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("|"):
                out.append(line)
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            # Separator row (all dashes/colons): pass through untouched.
            if cells and all(set(c) <= {"-", ":", " "} for c in cells):
                out.append(line)
                continue
            if header_cols is None:
                header_cols = [c.lower() for c in cells]
                if "heartbeat" in header_cols:
                    hb_index = header_cols.index("heartbeat")
                if "lane" in header_cols:
                    lane_index = header_cols.index("lane")
                out.append(line)
                continue
            # Data row.
            if hb_index is None or lane_index >= len(cells):
                out.append(line)
                continue
            if cells[lane_index] == lane:
                while len(cells) <= hb_index:
                    cells.append("-")
                if cells[hb_index] != when:
                    cells[hb_index] = when
                    changed = True
                newline_suffix = "\n" if line.endswith("\n") else ""
                out.append("| " + " | ".join(cells) + " |" + newline_suffix)
            else:
                out.append(line)

        if not changed:
            return False
        try:
            _rewrite_atomic(registry, "".join(out))
        except OSError as exc:
            _warn_heartbeat_failure(registry, exc)
            return False
        return True


def _update_current_heartbeat(current: Path, when: str) -> bool:
    """Set ``heartbeat:`` and ``last_updated:`` lines in a lane current.md.

    Only rewrites lines that already exist; never adds fields. Returns True if
    anything changed.
    """
    if not current.exists():
        return False
    try:
        original = current.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _warn_heartbeat_failure(current, exc)
        return False

    changed = False
    out_lines: list[str] = []
    for line in original.splitlines(keepends=True):
        newline_suffix = "\n" if line.endswith("\n") else ""
        body = line[: -len(newline_suffix)] if newline_suffix else line
        low = body.strip().lower()
        if low.startswith("heartbeat:"):
            replacement = "heartbeat: " + when
            if body != replacement:
                changed = True
            out_lines.append(replacement + newline_suffix)
        elif low.startswith("last_updated:"):
            replacement = "last_updated: " + when
            if body != replacement:
                changed = True
            out_lines.append(replacement + newline_suffix)
        else:
            out_lines.append(line)

    if not changed:
        return False
    try:
        _rewrite_atomic(current, "".join(out_lines))
    except OSError as exc:
        _warn_heartbeat_failure(current, exc)
        return False
    return True


def append_index_row(
    inbox_dir: Path,
    title: str,
    delivered_at: str,
    message_id: str,
    request_id: str,
    iteration: str,
    from_lane: str,
    message_type: str,
) -> None:
    index_path = inbox_dir / "index.md"
    if not index_path.exists():
        index_path.write_text(INDEX_HEADER.format(title=title), encoding="utf-8")

    row = "| {at} | {mid} | {req} | {itr} | {frm} | {mtype} | new |\n".format(
        at=delivered_at,
        mid=message_id,
        req=request_id or "-",
        itr=iteration or "-",
        frm=from_lane or "-",
        mtype=message_type or "-",
    )
    with index_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(row)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Atomically deliver a message into a lane inbox (Maildir tmp->new)."
    )
    parser.add_argument("--loop-dir", default="docs/loop")
    parser.add_argument("--to-lane", required=True, help="Target lane name, e.g. implementation.")
    parser.add_argument(
        "--message-file",
        default=None,
        help="Path to the message body. Use '-' or omit to read the body from stdin.",
    )
    parser.add_argument(
        "--request-id",
        default="",
        help="Request id this message belongs to (falls back to the body's "
        "'request_id:' envelope line).",
    )
    parser.add_argument(
        "--message-type",
        default="",
        help="Message type (falls back to the body's 'message_type:' envelope "
        "line; one of the two must supply it). Known: "
        + ", ".join(KNOWN_MESSAGE_TYPES),
    )
    parser.add_argument(
        "--from-lane",
        default="",
        help="Sender lane name (falls back to the body's 'from_lane:' envelope "
        "line; unknown sender skips the heartbeat stamp).",
    )
    parser.add_argument(
        "--iteration",
        default="",
        help="Iteration counter for this request (falls back to the body's "
        "'iteration:' envelope line; one of the two must supply it).",
    )
    parser.add_argument(
        "--message-id",
        default=None,
        help="Override the derived message id (will be slugified).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-deliver even if a message with the same id already exists in new/cur.",
    )
    parser.add_argument(
        "--no-heartbeat",
        action="store_true",
        help="Do not stamp the --from-lane heartbeat on delivery (default: stamp it).",
    )
    parser.add_argument(
        "--also-archive",
        action="store_true",
        help=(
            "Also archive a copy into the durable messages/<request_id>/ store "
            "(the dir is created only when the message is written; requires "
            "--request-id). G11: never pre-creates an empty request dir."
        ),
    )
    args = parser.parse_args(argv)

    lane = args.to_lane.strip()
    if not lane:
        raise SystemExit("--to-lane must not be empty.")
    # D5: reject traversal before the lane becomes loop_dir/lanes/<lane>.
    safe_name(lane, "--to-lane")

    body = read_body(args.message_file)

    # Resolve metadata: explicit flag wins, else the body's envelope lines; a
    # flag/envelope disagreement is a hard error (see resolve_field).
    envelope = parse_envelope(body)
    message_type = resolve_field("message_type", args.message_type, envelope)
    request_id = resolve_field("request_id", args.request_id, envelope)
    iteration = resolve_field("iteration", args.iteration, envelope)
    from_lane = resolve_field("from_lane", args.from_lane, envelope)

    # message_type and iteration must be known: defaulting them (the old
    # behavior) collapsed every message for a request onto one message id, so
    # the 2nd+ delivery became a silent idempotent no-op.
    missing = [name for name, value in
               (("message_type", message_type), ("iteration", iteration)) if not value]
    if missing:
        raise SystemExit(
            "{0} unknown; pass --message-type/--iteration or include "
            "'message_type:'/'iteration:' lines in the message envelope. "
            "Without them every message for a request maps to the same id and "
            "later deliveries silently no-op.".format(" and ".join(missing))
        )
    # D9: --also-archive without a request_id used to skip the durable archive
    # SILENTLY (archive_message returns None on a blank id). The check sits
    # AFTER envelope resolution on purpose: an envelope-supplied request_id
    # satisfies it. D5: the id then becomes loop_dir/messages/<request_id>, so
    # it must also be a safe path segment.
    if args.also_archive:
        if not request_id:
            parser.error(
                "--also-archive requires a request_id: pass --request-id or "
                "include a 'request_id:' envelope line in the message body."
            )
        safe_name(request_id, "--request-id")
    if not from_lane:
        sys.stderr.write(
            "warning: from_lane unknown (no --from-lane flag and no 'from_lane:' "
            "envelope line); delivering anyway, but the sender heartbeat will "
            "NOT be stamped.\n"
        )

    if message_type not in KNOWN_MESSAGE_TYPES:
        sys.stderr.write(
            "warning: message_type {0!r} is not a known type ({1}).\n".format(
                message_type, ", ".join(KNOWN_MESSAGE_TYPES)
            )
        )

    loop_dir = Path(args.loop_dir)

    # A misspelled or unregistered sender must not lose its heartbeat SILENTLY:
    # the message still delivers (the index records the claimed sender), but say
    # plainly why no heartbeat will be stamped so a typo'd lane name is caught
    # instead of quietly producing stale/orphan judgments later.
    heartbeat_lane = from_lane
    # D5: from_lane becomes a path segment in stamp_lane_heartbeat
    # (lanes/<from_lane>/current.md), and it can arrive via the message body's
    # envelope -- a traversal-shaped sender must never reach that write.
    if from_lane and not is_safe_name(from_lane):
        sys.stderr.write(
            "warning: from_lane {0!r} is not a safe lane name; delivering "
            "anyway, but the sender heartbeat will NOT be stamped.\n".format(
                from_lane
            )
        )
        heartbeat_lane = ""
    elif from_lane and lane_registered(loop_dir, from_lane) is False:
        sys.stderr.write(
            "warning: from_lane {0!r} is not registered in agent-lanes.md; "
            "delivering anyway, but the sender heartbeat will NOT be "
            "stamped.\n".format(from_lane)
        )
        heartbeat_lane = ""

    inbox_dir = loop_dir / "lanes" / lane / "inbox"
    assert_within(loop_dir / "lanes", inbox_dir)
    ensure_inbox_tree(inbox_dir)

    message_id = build_message_id(
        request_id=request_id,
        message_type=message_type,
        iteration=iteration,
        explicit_id=args.message_id,
    )

    already = existing_delivery(inbox_dir, message_id)
    if already and not args.force:
        print(
            "already delivered: {0} (in inbox/{1}); use --force to re-deliver".format(
                posix_path(str(inbox_dir / already / (message_id + ".md"))), already
            )
        )
        # A re-run still proves the sender is alive; refresh its heartbeat (F7)
        # unless opted out.
        if heartbeat_lane and not args.no_heartbeat:
            for path in stamp_lane_heartbeat(loop_dir, heartbeat_lane, utc_now()):
                print("heartbeat {0}".format(path))
        return 0

    target = inbox_dir / "new" / (message_id + ".md")
    delivered_at = utc_now()
    atomic_write(target, body)
    append_index_row(
        inbox_dir=inbox_dir,
        title=title_for(lane),
        delivered_at=delivered_at,
        message_id=message_id,
        request_id=request_id,
        iteration=iteration,
        from_lane=from_lane,
        message_type=message_type,
    )

    print("delivered {0}".format(posix_path(str(target))))
    print("indexed {0}".format(posix_path(str(inbox_dir / "index.md"))))
    print("reader should process inbox/new then move to inbox/cur")

    # G11(a): optionally archive a durable copy into messages/<request_id>/.
    # The dir is minted only here, alongside the write, so a re-keyed request
    # can never leave an empty stray dir behind.
    if args.also_archive:
        archived = archive_message(
            loop_dir, request_id, message_type, iteration, body
        )
        if archived:
            print("archived {0}".format(archived))

    # F7: delivering a message is proof the sender lane is alive, so stamp its
    # heartbeat (agent-lanes.md heartbeat column + lanes/<lane>/current.md
    # last_updated/heartbeat if present). Best-effort; never blocks delivery.
    if heartbeat_lane and not args.no_heartbeat:
        for path in stamp_lane_heartbeat(loop_dir, heartbeat_lane, delivered_at):
            print("heartbeat {0}".format(path))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
