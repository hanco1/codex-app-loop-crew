#!/usr/bin/env python3
"""Read-only probe for the Codex host's UNDOCUMENTED local data surfaces.

This is the ONLY file in the skill that touches the Codex host's undocumented
data plane: the rate-limit snapshots buried in ``~/.codex/sessions/**/*.jsonl``
and the OAuth identity in ``~/.codex/auth.json``. Every other module in the
toolkit is host-agnostic -- it reads only the loop's own durable files under
``docs/loop``. Isolating the host coupling here keeps the dashboard (and anyone
else) a pure loop tool with an OPTIONAL probe: if this module is deleted or
fails to import, the caller degrades gracefully and the loop is unaffected.

Two public providers, both READ-ONLY and both degrade gracefully -- they NEVER
raise, NEVER block the caller, and NEVER write to disk:

- ``build_usage(codex_home=None)`` -> the ``usage`` snapshot assembled from the
  tail of the newest ``sessions/**/*.jsonl`` file (selected by mtime). Returns a
  dict; on any missing-data condition returns ``{"available": False,
  "reason": ...}`` with a stable reason code (``codex_not_logged_in`` when no
  ``auth.json`` exists, ``no_session_data_yet`` when auth exists but no usable
  rate-limit event has landed).
- ``build_account(codex_home=None)`` -> the SCOPED ``account`` identity parsed
  from ``auth.json``. Returns a dict; on any missing/degraded condition returns
  ``{"available": False, ...}``.

``drop_caches()`` clears the in-memory ``(path, mtime_ns, size)`` caches so the
next read rescans (used by the dashboard's ``?refresh=1`` path). It is read-only.

PRIVACY (SECURITY RED LINE): both providers use field-scoped extraction so no
token material or conversation content can ever leave this module.

- The usage provider walks ONLY the exact known JSON paths of a ``token_count``
  event (``rate_limits`` numbers, ``total_token_usage`` numbers, ``plan_type``,
  and the event timestamp) -- never a recursive/whole-object scan -- so no
  message text, prompt, or conversation file path can leak. Only the newest
  file's tail (<= 256 KB) is read; the parsed result is cached by
  ``(path, mtime, size)``.
- ``auth.json`` holds OAuth tokens (id_token, access_token, refresh_token). The
  account provider parses it server-side and the ONLY fields permitted to leave
  the parser are ``email``, ``name``, ``plan_type``, ``auth_mode``, a TRUNCATED
  ``account_id_short`` (first 8 chars), and the auth.json mtime as ISO. No token
  string, no full JWT, no id_token, no access/refresh token, and no full
  account_id may EVER appear in the returned dict. The JWT ``id_token`` payload
  is decoded read-only with base64url (split on '.', pad '=', ``json.loads`` of
  the payload segment ONLY); the signature is never verified and raw segments
  are never returned. The parsed result is cached by ``(path, mtime, size)``.

Running ``python codex_host_probe.py`` prints the probe result (usage + account)
as JSON -- a useful standalone diagnostic. By construction that output contains
no token material.

stdlib only (``json``, ``os``, ``glob``, ``threading``, ``base64``,
``datetime``, ``pathlib``); pure ASCII; no third-party dependencies.
"""

from __future__ import annotations

import base64
import glob
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Only the tail of the newest session file is ever read; these files can be
# hundreds of MB. 256 KB comfortably holds many trailing token_count events.
USAGE_TAIL_BYTES = 256 * 1024

# Reason codes for an unavailable usage panel, so the UI can localize a helpful
# hint instead of surfacing a raw internal string. ``codex_not_logged_in`` means
# no ``auth.json`` exists under the Codex home (the user must run ``codex
# login``); ``no_session_data_yet`` means auth exists but no session/rate-limit
# data has landed yet (usually clears after one agent run).
USAGE_REASON_NOT_LOGGED_IN = "codex_not_logged_in"
USAGE_REASON_NO_SESSION_DATA = "no_session_data_yet"
# Human-readable hints paired with the reason codes above. The UI localizes its
# own copy, but these travel in the payload so a non-UI consumer (or a probe)
# gets a meaningful message. The login command is kept verbatim.
_USAGE_HINT_NOT_LOGGED_IN = "Codex is not logged in on this machine. Run: codex login"
_USAGE_HINT_NO_SESSION_DATA = (
    "No Codex session data found yet. This usually clears up once an agent has "
    "run at least once."
)

# Namespaced claim under which Codex stashes the ChatGPT plan/account fields in
# the id_token payload (verified live on this machine's auth.json).
_JWT_AUTH_CLAIM = "https://api.openai.com/auth"
# How many leading characters of the opaque account_id are safe to show. The id
# is never surfaced in full; only this prefix travels for a disambiguating hint.
_ACCOUNT_ID_SHORT_LEN = 8

# Module-level cache for the parsed usage snapshot, keyed by (path, mtime_ns,
# size). The 2s poll only re-reads a session file when it actually changed.
_USAGE_CACHE_KEY: Optional[tuple[str, int, int]] = None
_USAGE_CACHE_VALUE: Optional[dict[str, Any]] = None
_USAGE_CACHE_LOCK = threading.Lock()

# Module-level cache for the parsed account identity, keyed by
# (auth.json path, mtime_ns, size). Like the usage cache, the 2s poll only
# re-reads (and re-decodes the JWT) when auth.json actually changed on disk.
_ACCOUNT_CACHE_KEY: Optional[tuple[str, int, int]] = None
_ACCOUNT_CACHE_VALUE: Optional[dict[str, Any]] = None
_ACCOUNT_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Shared read helpers (self-contained: this module has no import-time coupling
# to the dashboard, so a missing dashboard cannot break the probe)
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    """Read a file as UTF-8, or '' on any IO/decode error. Never raises."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _iso_local(epoch_seconds: float) -> str:
    """Render a unix mtime as a local-time ISO-8601 string (no microseconds)."""
    try:
        return datetime.fromtimestamp(epoch_seconds).replace(microsecond=0).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _codex_home() -> Path:
    """Locate the Codex home dir: ``$CODEX_HOME`` if set, else ``~/.codex``."""
    env = os.environ.get("CODEX_HOME")
    if env and env.strip():
        return Path(env).expanduser()
    return Path.home() / ".codex"


# ---------------------------------------------------------------------------
# Usage provider: Codex rate-limit snapshot from the newest session JSONL
# ---------------------------------------------------------------------------


def _usage_unavailable(home: Path, detail: str) -> dict[str, Any]:
    """Build an ``available: false`` usage dict with a differentiated reason.

    The reason is decided by whether ``<codex_home>/auth.json`` exists: absent
    means the user has never logged in (``codex_not_logged_in``); present means
    auth is set up but no usable session/rate-limit data has appeared yet
    (``no_session_data_yet``). ``reason`` carries the stable machine code (also
    mirrored as ``reason_code``); ``hint`` is the human-readable message the UI
    localizes against; ``detail`` keeps the internal diagnostic for operators.
    """
    try:
        logged_in = (home / "auth.json").exists()
    except OSError:
        logged_in = False
    if logged_in:
        reason_code = USAGE_REASON_NO_SESSION_DATA
        hint = _USAGE_HINT_NO_SESSION_DATA
    else:
        reason_code = USAGE_REASON_NOT_LOGGED_IN
        hint = _USAGE_HINT_NOT_LOGGED_IN
    return {
        "available": False,
        "reason": reason_code,
        "reason_code": reason_code,
        "hint": hint,
        "detail": detail,
        "source": "codex-session-jsonl",
    }


def _newest_session_file(codex_home: Path) -> Optional[Path]:
    """Return the most-recently-MODIFIED ``sessions/**/*.jsonl`` file, or None.

    Selection is by mtime, never by filename ordering. Missing directory or an
    IO error yields None (the panel then reports available:false).
    """
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.is_dir():
        return None
    pattern = str(sessions_dir / "**" / "*.jsonl")
    newest: Optional[Path] = None
    newest_mtime = -1.0
    try:
        for name in glob.iglob(pattern, recursive=True):
            path = Path(name)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest_mtime = mtime
                newest = path
    except OSError:
        return None
    return newest


def _read_tail_bytes(path: Path, max_bytes: int) -> str:
    """Read at most the last ``max_bytes`` of ``path`` as UTF-8 (lossy).

    Never reads the whole file: seeks to end - max_bytes. A leading partial
    line (from the seek cut) is naturally skipped by the caller, which only
    trusts fully-formed JSON lines. Returns '' on any IO error.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            start = max(0, end - max_bytes)
            handle.seek(start)
            chunk = handle.read()
    except OSError:
        return ""
    return chunk.decode("utf-8", errors="replace")


def _num(value: Any) -> Optional[float]:
    """Coerce a value to a plain number, or None. Never leaks strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _extract_window(raw: Any) -> Optional[dict[str, Any]]:
    """Extract ONLY the numeric fields of a rate-limit window object.

    Reads ``used_percent``, ``window_minutes``, ``resets_at`` and nothing else,
    and computes ``remaining_percent`` = 100 - used_percent (clamped 0..100).
    Returns None when the window is absent or carries no usable number. This is
    deliberately field-scoped so no unexpected/private key can pass through.
    """
    if not isinstance(raw, dict):
        return None
    used = _num(raw.get("used_percent"))
    if used is None:
        return None
    remaining = 100.0 - used
    if remaining < 0.0:
        remaining = 0.0
    if remaining > 100.0:
        remaining = 100.0
    return {
        "used_percent": round(float(used), 1),
        "remaining_percent": round(remaining, 1),
        "window_minutes": _num(raw.get("window_minutes")),
        "resets_at": _num(raw.get("resets_at")),
    }


def _extract_token_usage(raw: Any) -> Optional[dict[str, Any]]:
    """Extract ONLY the five known numeric token fields, if present."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        val = _num(raw.get(key))
        if val is not None:
            out[key] = val
    return out or None


def _usage_from_line(line: str) -> Optional[dict[str, Any]]:
    """Parse ONE JSONL line into a privacy-scoped usage dict, or None.

    PRIVACY-CRITICAL: this walks ONLY the exact known paths of a Codex
    ``token_count`` event (verified live on codex-cli 0.142.2):

        {"timestamp": ..., "type": "event_msg",
         "payload": {"type": "token_count",
                     "rate_limits": {"primary": {...}, "secondary": {...},
                                     "plan_type": ...},
                     "info": {"total_token_usage": {...}}}}

    It never does a recursive/whole-object scan, so message text, prompts, and
    conversation file paths in other event types can never be surfaced. Only
    numbers and ``plan_type`` are copied out. Malformed input -> None.
    """
    line = line.strip()
    if not line or '"rate_limits"' not in line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None

    primary = _extract_window(rate_limits.get("primary"))
    secondary = _extract_window(rate_limits.get("secondary"))
    if primary is None and secondary is None:
        return None

    plan_type_raw = rate_limits.get("plan_type")
    plan_type = plan_type_raw if isinstance(plan_type_raw, str) else None

    info = payload.get("info")
    token_usage = None
    if isinstance(info, dict):
        token_usage = _extract_token_usage(info.get("total_token_usage"))

    event_ts_raw = obj.get("timestamp")
    event_ts = event_ts_raw if isinstance(event_ts_raw, str) else None

    return {
        "plan_type": plan_type,
        "primary": primary,
        "secondary": secondary,
        "total_token_usage": token_usage,
        "event_timestamp": event_ts,
    }


def _last_usage_in_text(text: str) -> Optional[dict[str, Any]]:
    """Scan lines backwards for the LAST parseable rate_limits event."""
    lines = text.splitlines()
    for line in reversed(lines):
        parsed = _usage_from_line(line)
        if parsed is not None:
            return parsed
    return None


def build_usage(codex_home: Optional[Path] = None) -> dict[str, Any]:
    """Assemble the ``usage`` snapshot from the newest session JSONL tail.

    Cached by (path, mtime_ns, size) so the 2s poll only re-reads when the file
    changed. Always returns a dict; on any missing-data condition returns
    ``{"available": False, "reason": ...}``. Never raises, never blocks the
    caller, and never exposes session conversation content.
    """
    global _USAGE_CACHE_KEY, _USAGE_CACHE_VALUE

    home = codex_home if codex_home is not None else _codex_home()
    if not (home / "sessions").is_dir():
        return _usage_unavailable(home, "no codex sessions dir at {0}".format(
            str(home / "sessions").replace("\\", "/")
        ))

    newest = _newest_session_file(home)
    if newest is None:
        return _usage_unavailable(home, "no session .jsonl files found")

    try:
        st = newest.stat()
    except OSError:
        return _usage_unavailable(home, "could not stat newest session file")

    key = (str(newest), st.st_mtime_ns, st.st_size)
    with _USAGE_CACHE_LOCK:
        if key == _USAGE_CACHE_KEY and _USAGE_CACHE_VALUE is not None:
            return _USAGE_CACHE_VALUE

    text = _read_tail_bytes(newest, USAGE_TAIL_BYTES)
    parsed = _last_usage_in_text(text) if text else None
    if parsed is None:
        result = _usage_unavailable(home, "no rate_limits event in newest session tail")
    else:
        result = {
            "available": True,
            "source": "codex-session-jsonl",
            "plan_type": parsed["plan_type"],
            "primary": parsed["primary"],
            "secondary": parsed["secondary"],
            "total_token_usage": parsed["total_token_usage"],
            "event_timestamp": parsed["event_timestamp"],
            "as_of": _iso_local(st.st_mtime),
        }

    with _USAGE_CACHE_LOCK:
        _USAGE_CACHE_KEY = key
        _USAGE_CACHE_VALUE = result
    return result


# ---------------------------------------------------------------------------
# Account identity: parsed from auth.json's JWT id_token (read-only, scoped)
# ---------------------------------------------------------------------------
#
# SECURITY RED LINE: ``auth.json`` holds OAuth tokens (id_token, access_token,
# refresh_token) which must NEVER leave this process. The ONLY fields this
# parser is permitted to surface are: ``email``, ``name``, ``plan_type``,
# ``auth_mode``, a TRUNCATED ``account_id_short`` (first 8 chars, for display),
# and the auth.json mtime as ISO. No token string, no full JWT, no id_token,
# no access/refresh token, and no full account_id may ever appear in the output
# dict or in any log line. The JWT is decoded read-only with base64url (split on
# '.', pad '=', json.loads of the payload segment ONLY); the signature is never
# verified and the raw segments are never returned.


def _account_unavailable(detail: str, auth_mode: Optional[str] = None) -> dict[str, Any]:
    """Build an ``available: false`` account dict.

    Carries a short machine ``detail`` for operators and, when known, the
    ``auth_mode`` (e.g. ``apikey`` logins have no JWT to derive an identity
    from, but the mode itself is safe and useful to show). NEVER carries token
    material.
    """
    out: dict[str, Any] = {"available": False, "detail": detail}
    if auth_mode:
        out["auth_mode"] = auth_mode
    return out


def _decode_jwt_claims(id_token: Any) -> Optional[dict[str, Any]]:
    """Decode ONLY the payload claims of a JWT with base64url (no verify).

    Splits on '.', takes the middle (payload) segment, pads it to a multiple of
    four with '=', base64url-decodes, and ``json.loads`` the result. Returns the
    claims dict, or None on any malformation. The signature is never checked and
    neither the header nor signature segments are ever read or returned. This is
    a read-only decode purely to extract display identity fields.
    """
    if not isinstance(id_token, str):
        return None
    parts = id_token.split(".")
    if len(parts) != 3:
        return None
    payload_seg = parts[1]
    if not payload_seg:
        return None
    padded = payload_seg + "=" * (-len(payload_seg) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, TypeError):
        return None
    try:
        claims = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    return claims


def _account_from_auth(auth_path: Path) -> dict[str, Any]:
    """Parse a scoped account identity from ``auth.json`` (never tokens).

    Reads auth.json, pulls ``auth_mode``, decodes the ``tokens.id_token`` JWT
    payload for ``email`` / ``name`` and the plan type under the OpenAI auth
    claim, truncates ``tokens.account_id`` to a short prefix, and records the
    file's mtime as ISO. Handles every degraded case without raising:

    - auth.json missing/unreadable/invalid -> ``available: false``;
    - ``auth_mode`` apikey with no usable JWT -> ``available: false`` but with
      ``auth_mode`` surfaced (account details genuinely unavailable);
    - malformed/absent JWT -> ``available: false`` with ``auth_mode`` if known.

    The returned dict is field-scoped by construction: it can only ever contain
    ``available``, ``email``, ``name``, ``plan_type``, ``auth_mode``,
    ``account_id_short``, ``auth_mtime_iso``, and (on failure) ``detail``. No
    token material is copied into it under any code path.
    """
    raw_text = _read_text(auth_path)
    if not raw_text:
        return _account_unavailable("no auth.json")
    try:
        data = json.loads(raw_text)
    except ValueError:
        return _account_unavailable("auth.json is not valid JSON")
    if not isinstance(data, dict):
        return _account_unavailable("auth.json is not a JSON object")

    auth_mode_raw = data.get("auth_mode")
    auth_mode = auth_mode_raw if isinstance(auth_mode_raw, str) and auth_mode_raw else None

    try:
        st = auth_path.stat()
        auth_mtime_iso = _iso_local(st.st_mtime)
    except OSError:
        auth_mtime_iso = ""

    tokens = data.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}

    # Truncated account id (opaque, first 8 chars). Never the full value.
    account_id_raw = tokens.get("account_id")
    account_id_short = ""
    if isinstance(account_id_raw, str) and account_id_raw:
        account_id_short = account_id_raw[:_ACCOUNT_ID_SHORT_LEN]

    claims = _decode_jwt_claims(tokens.get("id_token"))
    if claims is None:
        # No usable JWT (apikey login, malformed token, or none). The account
        # identity is genuinely unavailable, but auth_mode / mtime are safe.
        out = _account_unavailable("no usable id_token JWT", auth_mode=auth_mode)
        if account_id_short:
            out["account_id_short"] = account_id_short
        if auth_mtime_iso:
            out["auth_mtime_iso"] = auth_mtime_iso
        return out

    email_raw = claims.get("email")
    email = email_raw if isinstance(email_raw, str) and email_raw else None
    name_raw = claims.get("name")
    name = name_raw if isinstance(name_raw, str) and name_raw else None

    plan_type = None
    auth_claim = claims.get(_JWT_AUTH_CLAIM)
    if isinstance(auth_claim, dict):
        plan_raw = auth_claim.get("chatgpt_plan_type")
        if isinstance(plan_raw, str) and plan_raw:
            plan_type = plan_raw

    return {
        "available": True,
        "email": email,
        "name": name,
        "plan_type": plan_type,
        "auth_mode": auth_mode,
        "account_id_short": account_id_short or None,
        "auth_mtime_iso": auth_mtime_iso or None,
    }


def build_account(codex_home: Optional[Path] = None) -> dict[str, Any]:
    """Assemble the scoped ``account`` identity from ``<codex_home>/auth.json``.

    Cached by (path, mtime_ns, size) so the 2s poll only re-decodes the JWT when
    auth.json actually changed. Always returns a dict; on any missing/degraded
    condition returns ``{"available": False, ...}``. Never raises, never blocks
    the caller, and -- by the field-scoping in ``_account_from_auth`` -- never
    exposes token material.
    """
    global _ACCOUNT_CACHE_KEY, _ACCOUNT_CACHE_VALUE

    home = codex_home if codex_home is not None else _codex_home()
    auth_path = home / "auth.json"
    try:
        st = auth_path.stat()
    except OSError:
        # No auth.json (or unreadable): not logged in / apikey-with-no-file.
        return _account_unavailable("no auth.json")

    key = (str(auth_path), st.st_mtime_ns, st.st_size)
    with _ACCOUNT_CACHE_LOCK:
        if key == _ACCOUNT_CACHE_KEY and _ACCOUNT_CACHE_VALUE is not None:
            return _ACCOUNT_CACHE_VALUE

    result = _account_from_auth(auth_path)

    with _ACCOUNT_CACHE_LOCK:
        _ACCOUNT_CACHE_KEY = key
        _ACCOUNT_CACHE_VALUE = result
    return result


def drop_caches() -> None:
    """Clear the in-memory usage + account caches so the next read rescans.

    Used ONLY by a caller's ``?refresh=1`` path (e.g. the dashboard). This is a
    read-only operation: it forces a fresh auth.json re-read and newest-session
    re-scan on the next ``build_usage`` / ``build_account`` call; it never writes
    to disk.
    """
    global _USAGE_CACHE_KEY, _USAGE_CACHE_VALUE, _ACCOUNT_CACHE_KEY, _ACCOUNT_CACHE_VALUE
    with _USAGE_CACHE_LOCK:
        _USAGE_CACHE_KEY = None
        _USAGE_CACHE_VALUE = None
    with _ACCOUNT_CACHE_LOCK:
        _ACCOUNT_CACHE_KEY = None
        _ACCOUNT_CACHE_VALUE = None


def probe(codex_home: Optional[Path] = None) -> dict[str, Any]:
    """Return both providers as one dict: ``{"usage": ..., "account": ...}``.

    A convenience for standalone diagnostics. By construction (the field-scoping
    in the two providers) the result contains no token material or conversation
    content. Never raises.
    """
    return {"usage": build_usage(codex_home), "account": build_account(codex_home)}


if __name__ == "__main__":
    # Standalone diagnostic: print the probe result (usage + account) as JSON.
    # Contains no token material by construction, so it is safe to eyeball.
    print(json.dumps(probe(), indent=2, ensure_ascii=False))
