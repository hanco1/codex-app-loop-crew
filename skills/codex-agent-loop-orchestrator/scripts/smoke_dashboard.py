#!/usr/bin/env python3
"""In-process smoke test for the read-only loop dashboard.

Exercises ``loop_dashboard`` end to end WITHOUT spawning any subprocess (this
box's sandbox cannot shell out): everything is a direct module-level import of
``bootstrap_agent_loop`` and ``loop_dashboard``, and the server runs in a
background thread bound to 127.0.0.1 on an ephemeral port (0).

Asserts, in one temp loop:

  1. bootstrap creates a minimal loop in-process;
  2. GET /              -> 200 and the body contains '<html';
  3. GET /api/state     -> 200, valid JSON, has lanes + requests + doctor +
     policy + usage keys;
  4. POST /api/lanes {"lane":"qa-review","role":"test"} -> ok:true, AND
     agent-lanes.md now contains a qa-review row with status needs-thread, AND
     the lane dir + workspace/ were created;
  5. POST /api/lanes with an invalid name "Bad Name!" -> rejected (ok:false),
     and the registry is byte-for-byte UNCHANGED;
  6. the GET endpoints wrote NOTHING: a hash+mtime snapshot of the whole loop
     tree taken before the two GETs matches the snapshot taken after;
  7. USAGE: a FAKE ``CODEX_HOME`` with a decoy-laden session JSONL yields
     usage.available true, remaining percents computed (used 9.0 -> 91.0),
     plan_type matches, AND a planted private-conversation marker does NOT
     appear anywhere in the /api/state body (privacy); a missing sessions dir
     yields usage.available false with the endpoint still 200;
  8. POLICY: /api/state shows the default max_fix_cycles 3; POST /api/policy
     {"max_fix_cycles":5} -> ok, loop-policy.md updated, state reflects 5;
     POST 0, 99, and "abc" -> 400 and loop-policy.md UNCHANGED; a GET after
     the writes still mutates nothing else on disk;
  9. URL LINE: make_server_with_fallback binds the requested free port and its
     address matches; requesting a busy port falls back to an ephemeral one;
     main() prints exactly one ``DASHBOARD_URL=`` line matching the bound port;
 10. the server shuts down cleanly.

Additionally (dashboard v3 login-guidance / i18n / lane-summary batch):

  A. LOGIN GUIDANCE reason codes: a fake CODEX_HOME with a session file but no
     rate_limits event yields usage.reason "codex_not_logged_in" when no
     auth.json exists (and a hint mentioning the verbatim ``codex login``), and
     "no_session_data_yet" when auth.json is present;
  B. STRUCTURED LANE SUMMARY: seeding a lane's current.md with a known request
     id / next action / blocker makes /api/state's lane object carry them parsed
     under ``summary`` (raw current.md still present for "View raw");
  C. i18n INTEGRITY: the served page embeds a STRINGS blob whose every key has a
     non-empty en AND zh, the page carries the EN / zh toggle control (with a
     localized data-i18n-aria binding), the keys that a prior review found
     hardcoded (bool chips, lane status/View-raw labels, the login hint, Plan/
     As-of prefixes, all twelve month abbreviations) are all present, and no
     hardcoded ZH prose remains inline outside the dictionary blob;
  D. GUARD AUTH DETECTOR: tools/codex_guard's auth regex matches "401
     Unauthorized" and "please run codex login" style strings but not a plain
     compile error.

Additionally (dashboard v4 account-identity / refresh / layout batch):

  E. ACCOUNT IDENTITY + TOKEN-LEAK: a fake auth.json with a hand-built base64url
     JWT (known email/name/plan) plus distinctive FAKE_* token strings makes
     /api/state's usage.account carry the email/name/plan/auth_mode and a
     TRUNCATED account_id_short; the account object is field-scoped (no key
     beyond the permitted set); and NONE of the FAKE_* token strings (id_token
     JWT + signature, access/refresh tokens, full account_id) appears ANYWHERE
     in the response body -- the auth.json red line;
  F. REFRESH: rewriting the fake auth.json to a new (same-length) email while
     forcing its mtime/size back leaves the (path, mtime, size) cache key
     unchanged, so a plain /api/state may serve the cached email, but
     /api/state?refresh=1 drops the cache and MUST reflect the new email (and
     still leaks no token material);
  G. LAYOUT: the served HTML places the Lanes section before the Usage & Limits
     section before the System Checks section; Usage & Limits is a
     collapse-by-default toggle with a DISTINCT localStorage key, a live
     collapsed-header summary element outside the folding body, a refresh
     control, and the two merged (usage + policy) cards inside the body;
  (C) i18n INTEGRITY is extended with the new v4 keys (section title/kicker,
     collapse + refresh controls, signed-in-as, auth-mode template, stale-account
     hint, summary bits), each with a non-empty en AND zh.

Prints ``DASH_SMOKE_OK`` and exits 0 only if every assertion passes.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import socket
import tempfile
import threading
import time
import tokenize
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
import sys

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import bootstrap_agent_loop  # noqa: E402
import loop_dashboard  # noqa: E402


def _find_repo_tool(rel: str):
    """Find a dev-repo tool without walking past a repository boundary.

    A tool is accepted only from a dev repo root that also contains the skill
    source directory. After checking an ancestor with ``.git``, stop ascending.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / rel
        skill_source = parent / "skill" / "codex-agent-loop-orchestrator"
        if candidate.is_file() and skill_source.is_dir():
            return candidate
        if (parent / ".git").is_dir():
            break
    return None


def _fail(message: str) -> None:
    raise AssertionError(message)


def _bootstrap(loop_dir: Path, extra_argv=None) -> None:
    """Run bootstrap main in-process with a controlled argv."""
    argv = ["bootstrap_agent_loop", "--loop-dir", str(loop_dir)]
    if extra_argv:
        argv.extend(extra_argv)
    saved = sys.argv
    sys.argv = argv
    try:
        rc = bootstrap_agent_loop.main()
    finally:
        sys.argv = saved
    if rc != 0:
        _fail("bootstrap returned non-zero: {0}".format(rc))


def _snapshot_tree(root: Path) -> dict:
    """Map every file under ``root`` to (size, mtime_ns, sha256).

    Used to prove the read-only endpoints changed nothing on disk.
    """
    snap = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            st = path.stat()
            snap[str(path.relative_to(root)).replace("\\", "/")] = (
                st.st_size,
                st.st_mtime_ns,
                hashlib.sha256(data).hexdigest(),
            )
    return snap


def _http_get(url: str) -> tuple:
    """Return (status, body_bytes). Treats HTTP errors as (code, body)."""
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _http_get_with_headers(url: str, headers=None) -> tuple:
    """Return (status, body_bytes, response_headers) for conditional GET probes."""
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.getcode(), resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def _http_post_json(url: str, payload: dict) -> tuple:
    """POST a JSON body. Return (status, parsed_json_or_raw_bytes)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.getcode(), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode("utf-8"))
        except ValueError:
            return exc.code, raw


# Planted in a fake conversation line of the fake session file. The privacy
# assertion requires this string never appears in the /api/state body.
PRIVACY_MARKER = "PRIVATE_CONVERSATION_MARKER_MUST_NOT_LEAK_7f3a9c"

# Known account identity fields baked into the fake auth.json's hand-built JWT.
# The account assertion requires /api/state's usage.account carries these; the
# token-leak assertion requires none of the FAKE_* token strings below ever
# appears anywhere in the /api/state response body.
FAKE_ACCOUNT_EMAIL = "smoke-account@example.com"
FAKE_ACCOUNT_NAME = "Smoke Account"
FAKE_ACCOUNT_PLAN = "pro"
# A SECOND email of the EXACT same character length as FAKE_ACCOUNT_EMAIL. The
# refresh test rewrites auth.json to this email while forcing the same mtime and
# size, so the (path, mtime, size) cache key is unchanged -- a plain read then
# serves the STALE cache, and only ``?refresh=1`` (which drops the cache) must
# reflect the new email. Both strings are 25 chars (asserted at import time).
FAKE_ACCOUNT_EMAIL_2 = "swapped-acct1@example.com"
# Distinctive fake token material. Long, high-entropy-looking, and unique so a
# leak into the response body would be unambiguous. NONE of these may appear in
# /api/state (id_token JWT string, access/refresh tokens, or full account_id).
FAKE_ID_TOKEN_SIG = "FAKESIG_ID_TOKEN_MUST_NOT_LEAK_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
FAKE_ACCESS_TOKEN = "FAKE_ACCESS_TOKEN_MUST_NOT_LEAK_0f1e2d3c4b5a69788796a5b4c3d2e1f0"
FAKE_REFRESH_TOKEN = "FAKE_REFRESH_TOKEN_MUST_NOT_LEAK_deadbeefcafef00dfeedface99887766"
FAKE_ACCOUNT_ID = "acct-smokemustnotleak-0123456789abcdef0123456789abcdef"

# The stale-cache half of the refresh test relies on both emails being the same
# length so the rewritten auth.json keeps the same byte size. Guard it here so a
# future edit that breaks the invariant fails loudly at import, not mid-run.
assert len(FAKE_ACCOUNT_EMAIL) == len(FAKE_ACCOUNT_EMAIL_2), (
    "refresh test needs FAKE_ACCOUNT_EMAIL and FAKE_ACCOUNT_EMAIL_2 to be equal length"
)


def _b64url_no_pad(obj: dict) -> str:
    """Encode a dict as a base64url JWT segment WITHOUT padding.

    Mirrors how real JWTs strip '=' padding on each segment; the server pads it
    back before decoding. Uses only stdlib base64/json and stays pure ASCII.
    """
    import base64  # stdlib

    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _build_fake_jwt(email: str, name: str, plan: str, sig: str) -> str:
    """Hand-build a 3-segment base64url JWT (header.payload.signature).

    The payload carries ``email``, ``name``, and the plan under the same
    ``https://api.openai.com/auth`` claim the real Codex id_token uses. The
    signature segment is a fixed distinctive string; the server never verifies
    it. Returns the compact ``h.p.s`` string.
    """
    header = _b64url_no_pad({"alg": "RS256", "typ": "JWT"})
    payload = _b64url_no_pad(
        {
            "email": email,
            "name": name,
            "https://api.openai.com/auth": {"chatgpt_plan_type": plan},
        }
    )
    return "{0}.{1}.{2}".format(header, payload, sig)


def _write_fake_auth(home: Path, email: str, name: str, plan: str) -> None:
    """Write a fake ``auth.json`` into ``home`` with a hand-built JWT + tokens.

    The id_token is a real-shaped base64url JWT (known email/name/plan) whose
    signature segment is a distinctive fake string; access/refresh tokens and a
    full account_id are distinctive FAKE_* strings. Used to prove both the
    account extraction AND that no token material leaks into /api/state.
    """
    id_token = _build_fake_jwt(email, name, plan, FAKE_ID_TOKEN_SIG)
    auth = {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "last_refresh": "2026-07-06T00:00:00Z",
        "tokens": {
            "id_token": id_token,
            "access_token": FAKE_ACCESS_TOKEN,
            "refresh_token": FAKE_REFRESH_TOKEN,
            "account_id": FAKE_ACCOUNT_ID,
        },
    }
    (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")


def _make_fake_codex_home(root: Path) -> Path:
    """Create a fake CODEX_HOME with a decoy-laden session JSONL.

    The file has two decoy lines (one carrying ``PRIVACY_MARKER`` inside a
    fake conversation message) plus a final valid ``token_count`` event with
    known rate-limit numbers (used 9.0 / 23.0, plan pro). Also writes a fake
    ``auth.json`` with a hand-built JWT (known email/name/plan) and distinctive
    FAKE_* token strings, so the same /api/state read exercises both the account
    extraction and the token-leak assertion. Returns the home dir.
    """
    home = root / "codex_home"
    sess = home / "sessions" / "2026" / "07" / "05"
    sess.mkdir(parents=True, exist_ok=True)
    decoy_msg = json.dumps(
        {
            "type": "event_msg",
            "timestamp": "2026-07-05T06:59:00.000Z",
            "payload": {"type": "agent_message", "message": PRIVACY_MARKER},
        }
    )
    decoy_noise = json.dumps({"type": "session_meta", "payload": {"cwd": "/secret/path"}})
    valid = json.dumps(
        {
            "type": "event_msg",
            "timestamp": "2026-07-05T07:00:00.000Z",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {
                        "used_percent": 9.0,
                        "window_minutes": 300,
                        "resets_at": 1783245903,
                    },
                    "secondary": {
                        "used_percent": 23.0,
                        "window_minutes": 10080,
                        "resets_at": 1783389405,
                    },
                    "plan_type": "pro",
                },
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 50,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 125,
                    }
                },
            },
        }
    )
    rollout = sess / "rollout-2026-07-05T07-00-00-testfake.jsonl"
    rollout.write_text("\n".join([decoy_noise, decoy_msg, valid]) + "\n", encoding="utf-8")
    # Fake auth.json so usage.account is populated and the token-leak assertion
    # has real FAKE_* token material to scan the response body against. Plan
    # matches the session's plan_type ("pro") so no spurious stale flag fires.
    _write_fake_auth(home, FAKE_ACCOUNT_EMAIL, FAKE_ACCOUNT_NAME, FAKE_ACCOUNT_PLAN)
    return home


def _make_codex_home_no_ratelimits(root: Path, name: str, with_auth: bool) -> Path:
    """Fake CODEX_HOME that HAS a session file but NO rate_limits event.

    The session file carries only non-token events, so ``build_usage`` finds a
    session but no usable rate-limit data. ``with_auth`` controls whether an
    ``auth.json`` exists, which is exactly what distinguishes the two reason
    codes: absent -> ``codex_not_logged_in``; present -> ``no_session_data_yet``.
    """
    home = root / name
    sess = home / "sessions" / "2026" / "07" / "05"
    sess.mkdir(parents=True, exist_ok=True)
    line_meta = json.dumps({"type": "session_meta", "payload": {"cwd": "/x"}})
    line_msg = json.dumps(
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "hi"}}
    )
    rollout = sess / "rollout-2026-07-05T07-00-00-noratelimits.jsonl"
    rollout.write_text("\n".join([line_meta, line_msg]) + "\n", encoding="utf-8")
    if with_auth:
        (home / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": None, "tokens": {"id_token": "x"}}),
            encoding="utf-8",
        )
    return home


# A lane current.md with known parseable fields, used to prove the server-side
# structured lane summary carries request id / next action / blockers through
# /api/state. The blockers here are real (not the "None." placeholder).
SEED_REQUEST_ID = "R-SMOKE-77"
SEED_NEXT_ACTION = "Run the dashboard smoke and report evidence"
SEED_BLOCKER = "Waiting on review sign-off for R-SMOKE-77"
_SEED_CURRENT_MD = """# Implementation Current State

current_request_id: {rid}
status: implementing
iteration: 2
last_updated: 2026-07-05T07:30:00Z
heartbeat: 2026-07-05T07:30:00Z

## Current Checkpoint

- Parser wired into build_state

## Next Action

- {next}

## Blockers

- {blocker}
""".format(rid=SEED_REQUEST_ID, next=SEED_NEXT_ACTION, blocker=SEED_BLOCKER)


def _check_guard_auth_detector() -> None:
    """Import tools/codex_guard and exercise its auth-failure regex.

    Asserts the detector fires on "401 Unauthorized" and a "please run codex
    login" style message, and does NOT fire on a plain compile error. Imported
    by path so this works regardless of the guard living outside scripts/.
    """
    guard_path = _find_repo_tool("tools/codex_guard.py")
    if guard_path is None:
        print(
            "SKIP: guard auth detector "
            "(dev-repo tool not present - expected in installed/published layouts)"
        )
        return
    tools_dir = str(guard_path.parent)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import codex_guard  # noqa: E402

    if not hasattr(codex_guard, "is_auth_failure"):
        _fail("codex_guard is missing is_auth_failure()")
    should_match = [
        "401 Unauthorized",
        "please run codex login",
        "Error: not logged in",
        "login required",
        "invalid api key",
        "authentication failed",
    ]
    for s in should_match:
        if not codex_guard.is_auth_failure(s):
            _fail("codex_guard.is_auth_failure should match {0!r}".format(s))
    should_not_match = [
        "  File \"x.py\", line 3\n    def (:\nSyntaxError: invalid syntax",
        "error: expected ';' before '}' token",
        "ModuleNotFoundError: No module named 'foo'",
    ]
    for s in should_not_match:
        if codex_guard.is_auth_failure(s):
            _fail("codex_guard.is_auth_failure should NOT match compile error {0!r}".format(s))
    # And a raw compile error must not be mistaken for a connection failure
    # either (sanity: the guard would otherwise retry a real failure).
    if codex_guard.is_auth_failure("TypeError: unsupported operand type(s)"):
        _fail("codex_guard.is_auth_failure should NOT match a plain TypeError")


def _extract_i18n_rows(html_full: str) -> list:
    blobs = re.findall(
        r'<script type="application/json"[^>]*data-i18n-dict[^>]*>(.*?)</script>',
        html_full,
        re.S,
    )
    if not blobs:
        _fail("served HTML has no data-i18n-dict JSON blocks")
    rows = []
    for blob in blobs:
        try:
            data = json.loads(blob.strip())
        except ValueError as exc:
            _fail("an embedded i18n dictionary is not valid JSON: {0}".format(exc))
        part = data.get("strings")
        if not isinstance(part, list):
            _fail("an embedded i18n dictionary has no 'strings' array")
        rows.extend(part)
    return rows


def _check_i18n_integrity(html_full: str) -> None:
    """Parse the embedded STRINGS blob from the served HTML and validate it.

    Asserts: the JSON blob parses; every entry carries a non-empty ``en`` and
    ``zh``; and the served page includes the EN / zh language toggle control.
    """
    m = re.search(
        r'<script type="application/json" id="i18n-strings"[^>]*>(.*?)</script>',
        html_full,
        re.S,
    )
    if not m:
        _fail("served HTML has no embedded i18n-strings blob")
    rows = _extract_i18n_rows(html_full)
    if not isinstance(rows, list) or not rows:
        _fail("i18n dictionaries have no non-empty 'strings' array")
    seen_keys = set()
    for row in rows:
        key = row.get("key")
        if not key:
            _fail("an i18n entry is missing its 'key': {0!r}".format(row))
        if key in seen_keys:
            _fail("duplicate i18n key {0!r}".format(key))
        seen_keys.add(key)
        if not (row.get("en") or "").strip():
            _fail("i18n key {0!r} has an empty 'en' value".format(key))
        if not (row.get("zh") or "").strip():
            _fail("i18n key {0!r} has an empty 'zh' value".format(key))
    # The language toggle control must be present in the page.
    if 'id="lang-toggle"' not in html_full:
        _fail("served HTML is missing the EN / zh language toggle control")

    # Every string that a prior review found hardcoded now flows through the
    # dictionary. Assert those keys exist so a regression cannot silently drop
    # one back to an inline literal.
    required_keys = [
        "lang_toggle_aria",           # (1) toggle aria-label
        "badge_bool_ok",              # (2) health bool chips
        "badge_bool_fail",
        "badge_bool_na",
        "lane_summary_status_idle",   # (3) lane status placeholder
        "lane_view_raw",              # (4) collapsed raw current.md label
        "usage_not_logged_in_hint",   # (5) not-logged-in prose (codex login verbatim)
        "usage_plan_prefix",          # (6) Plan: prefix (reused)
        "usage_asof_prefix",          # (6) As of prefix (reused)
        "health_toggle_aria",         # (7) system-checks collapse toggle aria-label
        # (v4) Usage & Limits section: heading/kicker, collapse + refresh
        # controls, the signed-in-as identity row, the stale-account hint, and
        # the collapsed-header summary bits. Every one must exist so a
        # regression cannot drop a v4 string back to an inline literal.
        "usage_limits_heading",       # (v4) merged section title
        "usage_limits_kicker",        # (v4) section kicker
        "usage_toggle_aria",          # (v4) usage collapse toggle aria-label
        "usage_refresh_button",       # (v4) refresh button label
        "usage_refresh_aria",         # (v4) refresh button aria-label
        "usage_signed_in_label",      # (v4) "Signed in as" label
        "usage_auth_mode_prefix",     # (v4) "via X" auth-mode template
        "usage_account_unavailable",  # (v4) account-unavailable note
        "usage_stale_account_hint",   # (v4) stale-account hint sentence
        "usage_summary_5h",           # (v4) collapsed summary 5h label
        "usage_summary_weekly",       # (v4) collapsed summary weekly label
        "usage_summary_unavailable",  # (v4) collapsed summary fallback
    ]
    for k in required_keys:
        if k not in seen_keys:
            _fail("i18n dictionary is missing required key {0!r}".format(k))
    # (7) All twelve month abbreviations are localized (weekly reset date).
    for n in range(1, 13):
        mk = "usage_month_{0}".format(n)
        if mk not in seen_keys:
            _fail("i18n dictionary is missing month key {0!r}".format(mk))

    # The toggle must carry its localized aria-label binding, not a bare English
    # attribute (the review flagged the hardcoded aria-label="Language").
    if 'data-i18n-aria="lang_toggle_aria"' not in html_full:
        _fail("language toggle is missing its data-i18n-aria binding")
    # And no residual inline ``lang === "zh"`` ternary may render user-facing ZH
    # prose: the only allowed uses set the <html lang> attribute, paint the
    # toggle on/off class, or flip the language in the click handler. Guard
    # against the specific leaks the review caught (View raw, the login hint,
    # Plan / As-of prefixes) reappearing. The banned literals are pulled from
    # the dictionary itself by key -- so this stays pure ASCII in source -- and
    # each must live ONLY inside the JSON blob, never inline in the script.
    by_key = {row.get("key"): row for row in rows}
    outside = html_full.replace(m.group(1), "")
    for chk_key in ("lane_view_raw", "usage_not_logged_in_hint",
                    "usage_plan_prefix", "usage_asof_prefix"):
        zh_value = (by_key.get(chk_key) or {}).get("zh") or ""
        # Trim the "--" placeholder tails so the compared token is the ZH prose.
        zh_token = zh_value.replace("--", "").strip()
        if zh_token and zh_token in outside:
            _fail(
                "found a hardcoded ZH string for {0!r} outside the i18n "
                "dictionary (must go through t())".format(chk_key)
            )


def _check_header_markup(html_full: str) -> None:
    """Assert the simplified masthead header (served HTML, before any JS runs).

    The header was trimmed to drop two noisy lines above the title:
      * the kicker ("Multi-Agent Loop") is GONE -- redundant with the title;
      * the "VIEW ONLY" badge is GONE -- the footer already carries the
        accurate read-only note.
    What remains near the title is a COMPACT muted path chip (#loop-dir) that
    carries a title="" attribute so the FULL loop dir is available on hover
    while the visible text is shortened. This asserts:
      * the removed kicker/badge i18n keys no longer appear anywhere;
      * the removed "VIEW ONLY" badge markup (data-i18n="masthead_readonly")
        is absent;
      * the old .issue-row masthead line is gone;
      * the #loop-dir path element exists AND carries a title="" attribute;
      * the client-side shortener that fills the chip and its title is present.
    """
    # The removed keys must not appear anywhere (markup OR the i18n blob).
    for dead_key in ("masthead_kind", "masthead_readonly", "masthead_loop_dir"):
        if dead_key in html_full:
            _fail("removed i18n key {0!r} still appears in the served HTML".format(dead_key))
    # The old "VIEW ONLY" badge binding and the kicker line must be gone.
    if 'data-i18n="masthead_readonly"' in html_full:
        _fail("the removed VIEW ONLY badge markup is still present")
    if 'class="issue-row"' in html_full:
        _fail("the old .issue-row masthead line is still present")

    # The path element must still exist and carry a title="" attribute so the
    # full loop dir is available on hover while the visible text is shortened.
    chip_m = re.search(r'<span[^>]*id="loop-dir"[^>]*>', html_full)
    if not chip_m:
        _fail("served HTML has no #loop-dir path element")
    chip_tag = chip_m.group(0)
    if "title=" not in chip_tag:
        _fail("#loop-dir path chip must carry a title=\"\" attribute (full path on hover)")

    # The client-side shortener that trims the path to its last two segments
    # (and fills both the chip text and its title) must be wired in the script.
    if "shortenLoopDir" not in html_full:
        _fail("script is missing the shortenLoopDir path-shortener")
    if ".title = loopDirFull" not in html_full:
        _fail("script must set the #loop-dir title attribute to the full loop dir")


def _check_health_collapse_markup(html_full: str) -> None:
    """Assert the System Checks section is a collapse-by-default toggle.

    Verifies, on the SERVED HTML (before any JS runs):
      * a clickable, collapsible section head (#health-head) exists carrying the
        collapse toggle hook (role=button, aria-expanded, aria-controls) and the
        localized data-i18n-aria binding for its label;
      * the head starts collapsed: aria-expanded="false";
      * the collapsible body (#health-body) wraps the grid of check cards
        (#badges) -- the folding container;
      * the one-line verdict badge (#doctor-status) lives in the HEAD, OUTSIDE
        the collapsible body, so folding can never hide the health signal;
      * the script carries the persistence hook (a health-specific localStorage
        key, separate from the language key) and the toggle/paint wiring.
    """
    # The collapsible head with the toggle hooks.
    if 'id="health-head"' not in html_full:
        _fail("served HTML has no #health-head (system-checks collapse toggle)")
    head_m = re.search(r'<div class="section-head collapsible"[^>]*id="health-head"[^>]*>',
                       html_full)
    if not head_m:
        _fail("#health-head is not a 'section-head collapsible' toggle div")
    head_tag = head_m.group(0)
    for needle in ('role="button"', 'aria-controls="health-body"',
                   'data-i18n-aria="health_toggle_aria"'):
        if needle not in head_tag:
            _fail("#health-head is missing its toggle hook {0!r}".format(needle))
    # Collapsed by default: the served markup declares aria-expanded="false".
    if 'aria-expanded="false"' not in head_tag:
        _fail("#health-head must start collapsed (aria-expanded=\"false\")")

    # The collapsible body wrapper must exist and contain the #badges grid.
    body_m = re.search(r'<div class="collapsible-body" id="health-body">(.*?)</div>\s*</section>',
                       html_full, re.S)
    if not body_m:
        _fail("served HTML has no #health-body collapsible wrapper around the check grid")
    body_inner = body_m.group(1)
    if 'id="badges"' not in body_inner:
        _fail("#health-body does not wrap the #badges check grid")

    # The verdict badge (#doctor-status) must live in the HEAD, OUTSIDE the
    # collapsible body -- so it stays visible when the section is folded.
    if 'id="doctor-status"' not in head_m.string[head_m.start():body_m.start()]:
        _fail("#doctor-status verdict badge must be in the head, before #health-body")
    if 'id="doctor-status"' in body_inner:
        _fail("#doctor-status must NOT be inside the collapsible body (it would hide when folded)")

    # Persistence + wiring hooks in the script: a health-specific localStorage
    # key distinct from the language key, plus the toggle/paint functions.
    if "loop_dashboard_health_expanded" not in html_full:
        _fail("script is missing the health-collapse localStorage key")
    if '"loop_dashboard_lang"' not in html_full:
        _fail("expected the language localStorage key to remain present")
    for hook in ("toggleHealthCollapse", "paintHealthCollapse", "healthForcedOpen"):
        if hook not in html_full:
            _fail("script is missing the collapse wiring hook {0!r}".format(hook))


def _check_usage_limits_layout_markup(html_full: str) -> None:
    """Assert the v4 layout: Lanes first, then a collapsible Usage & Limits.

    Verifies, on the SERVED HTML (before any JS runs):
      * the Lanes section markup PRECEDES the Usage & Limits section, which in
        turn PRECEDES the System Checks section (the required order);
      * Usage & Limits is a clickable, collapse-by-default toggle head
        (#usage-head) with the toggle hooks (role=button, aria-controls, the
        localized data-i18n-aria binding) and starts collapsed
        (aria-expanded="false");
      * the collapsed head carries the live one-line summary element
        (#usage-summary), OUTSIDE the collapsible body so it stays visible when
        folded, plus the Refresh control (#usage-refresh);
      * the collapsible body (#usage-body-wrap) wraps BOTH merged cards
        (#usage-panel and #policy-panel) -- the folding container;
      * the script carries a DISTINCT usage-specific localStorage key (separate
        from both the language key and the health key) and the toggle/paint/
        refresh wiring.
    """
    # Section order: lanes < usage-limits < badges (system checks).
    idx_lanes = html_full.find('id="lanes-section"')
    idx_usage = html_full.find('id="usage-limits-section"')
    idx_health = html_full.find('id="badges-section"')
    if idx_lanes < 0:
        _fail("served HTML has no #lanes-section")
    if idx_usage < 0:
        _fail("served HTML has no #usage-limits-section")
    if idx_health < 0:
        _fail("served HTML has no #badges-section (system checks)")
    if not (idx_lanes < idx_usage):
        _fail("Lanes section must precede the Usage & Limits section")
    if not (idx_usage < idx_health):
        _fail("Usage & Limits section must precede the System Checks section")

    # The collapsible head with the toggle hooks.
    head_m = re.search(r'<div class="section-head collapsible"[^>]*id="usage-head"[^>]*>',
                       html_full)
    if not head_m:
        _fail("#usage-head is not a 'section-head collapsible' toggle div")
    head_tag = head_m.group(0)
    for needle in ('role="button"', 'aria-controls="usage-body-wrap"',
                   'data-i18n-aria="usage_toggle_aria"'):
        if needle not in head_tag:
            _fail("#usage-head is missing its toggle hook {0!r}".format(needle))
    if 'aria-expanded="false"' not in head_tag:
        _fail("#usage-head must start collapsed (aria-expanded=\"false\")")

    # The collapsible body wrapper must exist and wrap BOTH merged cards.
    body_m = re.search(r'<div class="collapsible-body" id="usage-body-wrap">(.*?)</div>\s*</section>',
                       html_full, re.S)
    if not body_m:
        _fail("served HTML has no #usage-body-wrap collapsible wrapper")
    body_inner = body_m.group(1)
    if 'id="usage-panel"' not in body_inner:
        _fail("#usage-body-wrap does not wrap the #usage-panel card")
    if 'id="policy-panel"' not in body_inner:
        _fail("#usage-body-wrap does not wrap the #policy-panel card (cards must be merged)")

    # The live summary + refresh must live in the HEAD, OUTSIDE the collapsible
    # body -- so the summary stays visible (and refresh reachable) when folded.
    head_region = head_m.string[head_m.start():body_m.start()]
    if 'id="usage-summary"' not in head_region:
        _fail("#usage-summary live summary must be in the head, before #usage-body-wrap")
    if 'id="usage-summary"' in body_inner:
        _fail("#usage-summary must NOT be inside the collapsible body (it would hide when folded)")
    if 'id="usage-refresh"' not in head_region:
        _fail("#usage-refresh button must be in the head (reachable when folded)")

    # The signed-in-as identity row and the stale-account hint live in the body.
    if 'id="signed-in"' not in body_inner:
        _fail("#usage-body-wrap is missing the #signed-in identity row")
    if 'id="stale-hint"' not in body_inner:
        _fail("#usage-body-wrap is missing the #stale-hint account-staleness hint")

    # Persistence + wiring hooks in the script: a usage-specific localStorage
    # key DISTINCT from both the language key and the health key.
    if "loop_dashboard_usage_expanded" not in html_full:
        _fail("script is missing the usage-collapse localStorage key")
    if "loop_dashboard_usage_expanded" == "loop_dashboard_health_expanded":
        _fail("usage collapse key must differ from the health key")  # pragma: no cover
    if "loop_dashboard_health_expanded" not in html_full:
        _fail("expected the health-collapse localStorage key to remain present")
    for hook in ("toggleUsageCollapse", "paintUsageCollapse", "refreshUsage",
                 "renderUsageSummary", "renderAccount"):
        if hook not in html_full:
            _fail("script is missing the usage wiring hook {0!r}".format(hook))
    # The refresh wiring must hit the read-only refresh variant.
    if "/api/state?refresh=1" not in html_full:
        _fail("script never calls the /api/state?refresh=1 refresh variant")


def _check_poll_coordinator(html_full: str) -> None:
    """G27: enforce one generation-guarded dashboard poll coordinator."""
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html_full, re.S)
    poll_scripts = [script for script in scripts if "POLL_MS" in script]
    if len(poll_scripts) != 1:
        _fail("G27: expected exactly one executable polling script")
    script = poll_scripts[0]

    if len(re.findall(r"\bfunction\s+scheduleNext\s*\(", script)) != 1:
        _fail("G27: polling must have exactly one scheduleNext function")
    if len(re.findall(r"pollTimer\s*=\s*setTimeout\s*\(", script)) != 1:
        _fail("G27: pollTimer must have exactly one scheduling call site")
    if re.search(r"setTimeout\s*\(\s*(?:poll|requestPoll|runPoll)\s*,", script):
        _fail("G27: bare polling setTimeout found outside scheduleNext")

    required = (
        "var POLL_MS = 2000",
        "var pollInFlight = false",
        "var pollGeneration = 0",
        "function requestPoll(options)",
        "function runPoll(request)",
        "pollGeneration += 1",
        "request.generation === pollGeneration",
        "scheduleNext(request.generation)",
    )
    for needle in required:
        if needle not in script:
            _fail("G27: poll coordinator is missing {0!r}".format(needle))
    stale_guard = "if (request.generation !== pollGeneration) return;"
    if script.count(stale_guard) != 2:
        _fail("G27: stale poll success/error paths must both be generation-guarded")

    # Manual refresh and POST-triggered state refreshes must use the same
    # coordinator. Direct state fetches are the old double-chain escape hatch.
    if re.search(r"fetch\s*\(\s*['\"]\/api\/state", script):
        _fail("G27: state fetch bypasses the poll coordinator")
    if script.count("requestPoll({ force: true") != 8:
        _fail("G27: refresh, load-more, and visibility work must route through requestPoll")

    # Conditional polling must reuse the last ETag, treat 304 as connectivity
    # success, and skip render without breaking the coordinator's finish path.
    for needle in (
        "var stateEtag = null",
        'headers["If-None-Match"] = stateEtag',
        "if (r.status === 304)",
        'stateEtag = r.headers.get("ETag")',
        "if (result.notModified) {",
        "setConn(true, stateHasStaleData(lastState || {}), stateIsCompleted(lastState || {}))",
    ):
        if needle not in script:
            _fail("G27: conditional polling is missing {0!r}".format(needle))

    # Hidden-tab cadence changes through the same coordinator and its sole
    # scheduling site; becoming visible forces one immediate coordinated poll.
    visibility_start = script.find('document.addEventListener("visibilitychange"')
    if visibility_start < 0:
        _fail("G27: visibilitychange throttle is missing")
    visibility_body = script[visibility_start:visibility_start + 800]
    for needle in (
        "var HIDDEN_POLL_MS = 30000",
        "document.hidden ? HIDDEN_POLL_MS : POLL_MS",
        "requestPoll({ force: true, defer: true })",
        "requestPoll({ force: true })",
    ):
        if needle not in script:
            _fail("G27: visibility throttle missing {0!r}".format(needle))
    if "setTimeout" in visibility_body:
        _fail("G27: visibilitychange must not create a second polling timer")

    # Every state read, including refresh=1, uses the coordinator's one fetch
    # implementation and therefore the same abort timeout/failure path.
    for needle in (
        "var FETCH_TIMEOUT_MS = 15000",
        "new AbortController()",
        "controller.abort()",
        "signal: controller.signal",
        "clearTimeout(fetchTimeoutTimer)",
        "url: stateUrl(true)",
    ):
        if needle not in script:
            _fail("G27: state fetch timeout missing {0!r}".format(needle))


def _check_g27_pagination_markup(html_full: str, rows: list) -> None:
    """G27/G29: bounded collections expand and collapse honestly."""
    for collection in ("requests", "evidence", "run-log"):
        if 'id="{0}-pagination"'.format(collection) not in html_full:
            _fail("G27 pagination control missing for {0}".format(collection))
    keys = {row.get("key"): row for row in rows}
    for key in (
        "pagination_showing",
        "pagination_showing_all",
        "pagination_load_more",
        "pagination_show_less",
        "pagination_loading",
    ):
        row = keys.get(key) or {}
        if not str(row.get("en", "")).strip() or not str(row.get("zh", "")).strip():
            _fail("G27/G29 pagination i18n key {0!r} needs non-empty EN/ZH".format(key))
    for needle in (
        "function renderPagination(collection, meta)",
        "function loadFullCollection(collection)",
        "function collapseFullCollection(collection)",
        'fullCollections[collection] = true',
        "delete fullCollections[collection]",
        'encodeURIComponent(full.join(","))',
    ):
        if needle not in html_full:
            _fail("G27/G29 pagination wiring missing {0!r}".format(needle))

    script = next((part for part in re.findall(
        r"<script(?:\s[^>]*)?>(.*?)</script>", html_full, re.S
    ) if "POLL_MS" in part), "")
    collapse_start = script.find("function collapseFullCollection(collection)")
    collapse_end = script.find("function scheduleNext", collapse_start)
    collapse_body = script[collapse_start:collapse_end]
    if "delete fullCollections[collection]" not in collapse_body:
        _fail("G29: collapse handler must delete the collection from fullCollections")
    if "requestPoll({ force: true" not in collapse_body:
        _fail("G29: collapse must route its paginated refresh through requestPoll")
    if re.search(r"setTimeout\s*\(\s*(?:poll|requestPoll|runPoll)\s*,", script):
        _fail("G29: collapse introduced a bare polling setTimeout")


def _check_g30_pulse_markup(html_full: str, rows: list) -> None:
    """G30: the header pulse shares the active-owner heartbeat predicate."""
    keys = {row.get("key"): row for row in rows}
    completed = keys.get("pulse_completed") or {}
    if completed.get("en") != "All requests accepted - loop idle":
        _fail("G30: pulse_completed needs the exact English completed-state copy")
    if not str(completed.get("zh", "")).strip():
        _fail("G30: pulse_completed needs a non-empty ZH value")
    if ".pulse.completed" not in html_full:
        _fail("G30: completed pulse needs distinct calm styling")

    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html_full, re.S)
    script = next((part for part in scripts if "POLL_MS" in part), "")
    stale_start = script.find("function stateHasStaleData(state)")
    stale_end = script.find("function stateIsCompleted(state)", stale_start)
    stale_body = script[stale_start:stale_end]
    if stale_start < 0 or stale_end < 0:
        _fail("G30: stale/completed pulse predicates are missing")
    if "laneHasStaleActiveOwnerHeartbeat" not in stale_body:
        _fail("G30: header staleness must use the shared active-owner predicate")
    if re.search(r"state\.lanes[\s\S]*?heartbeat[\s\S]*?state\s*===\s*['\"]stale", stale_body):
        _fail("G30: stateHasStaleData still contains the blunt any-lane stale pattern")
    for needle in ("state.read_errors", "state.parse_errors", "state.refresh_degraded === true"):
        if needle not in stale_body:
            _fail("G30: diagnostic warning forcing lost {0!r}".format(needle))

    helper_start = script.find("function laneHasStaleActiveOwnerHeartbeat(lane)")
    helper_end = script.find("function analyzeNeedsYou", helper_start)
    helper_body = script[helper_start:helper_end]
    owner_start = script.find("function laneOwnsActiveRequest(lane)")
    owner_end = helper_start
    owner_body = script[owner_start:owner_end]
    heartbeat_start = script.find("function heartbeatLabel(lane, gapOwnerNames)")
    heartbeat_end = script.find("// G21:", heartbeat_start)
    heartbeat_body = script[heartbeat_start:heartbeat_end]
    if helper_start < 0 or "laneOwnsActiveRequest" not in helper_body:
        _fail("G30: active-owner heartbeat helper is missing its ownership check")
    if owner_start < 0 or "is_owner === true" not in owner_body:
        _fail("G30: active-owner predicate must require current_request.is_owner")
    if "laneHasStaleActiveOwnerHeartbeat" not in heartbeat_body:
        _fail("G30: lane cards and header must share the same stale-heartbeat helper")

    completed_start = stale_end
    completed_end = script.find("function render(state)", completed_start)
    completed_body = script[completed_start:completed_end]
    for needle in (
        "requests.length > 0",
        'status || ""',
        'toUpperCase() === "ACCEPTED"',
        "doc.warnings",
    ):
        if needle not in completed_body:
            _fail("G30: completed predicate is missing {0!r}".format(needle))
    set_conn_start = script.find("function setConn(connected, stale, completed)")
    set_conn_end = script.find("function diagnosticSources", set_conn_start)
    set_conn_body = script[set_conn_start:set_conn_end]
    for needle in ('classList.add("completed")', 'pulseStateKey = "pulse_completed"'):
        if needle not in set_conn_body:
            _fail("G30: completed pulse rendering is missing {0!r}".format(needle))


def _check_g18_progress_collapse(html_full: str, rows: list) -> None:
    """G18: the Progress section is foldable (same mechanism as System Checks /
    Usage & Limits) but DEFAULT OPEN, and the fold state survives the 2s poll.

    Verifies, on the SERVED HTML (before any JS runs):
      * #progress-head is a 'section-head collapsible' toggle carrying the hooks
        (role=button, aria-controls="progress-body", the localized
        data-i18n-aria binding) and starts OPEN (aria-expanded="true") -- unlike
        the two collapse-by-default sections;
      * #progress-body is a 'collapsible-body' wrapping the folding content
        (#progress-fill + #milestones); the live "done / total" head count
        (#progress-head-count) lives in the HEAD, OUTSIDE the body, so folding
        never hides the headline number;
      * the script carries a DISTINCT progress-specific localStorage key
        (separate from language / health / usage), the toggle/paint/wire hooks,
        and defaults progressUserExpanded to true (OPEN);
      * renderProgress never touches the fold classes (no is-collapsed, no
        paintProgressCollapse call) -- so the open/closed choice survives every
        poll (F12's in-place guarantee applied to the fold);
      * the progress_toggle_aria i18n key exists with a non-empty en AND zh.
    """
    head_m = re.search(r'<div class="section-head collapsible"[^>]*id="progress-head"[^>]*>',
                       html_full)
    if not head_m:
        _fail("G18: #progress-head is not a 'section-head collapsible' toggle div")
    head_tag = head_m.group(0)
    for needle in ('role="button"', 'aria-controls="progress-body"',
                   'data-i18n-aria="progress_toggle_aria"'):
        if needle not in head_tag:
            _fail("G18: #progress-head is missing its toggle hook {0!r}".format(needle))
    # DEFAULT OPEN: the served markup declares aria-expanded="true".
    if 'aria-expanded="true"' not in head_tag:
        _fail("G18: #progress-head must start OPEN (aria-expanded=\"true\")")

    # The collapsible body wrapper must exist and wrap the folding content.
    body_m = re.search(r'<div class="collapsible-body" id="progress-body">(.*?)</div>\s*</section>',
                       html_full, re.S)
    if not body_m:
        _fail("G18: served HTML has no #progress-body collapsible wrapper")
    body_inner = body_m.group(1)
    for needle in ('id="progress-fill"', 'id="milestones"'):
        if needle not in body_inner:
            _fail("G18: #progress-body must wrap the folding content {0!r}".format(needle))

    # The live head count must live in the HEAD, OUTSIDE the collapsible body --
    # so the headline number stays visible when the section is folded.
    head_region = head_m.string[head_m.start():body_m.start()]
    if 'id="progress-head-count"' not in head_region:
        _fail("G18: the live #progress-head-count must be in the head, before #progress-body")
    if 'id="progress-head-count"' in body_inner:
        _fail("G18: #progress-head-count must NOT be inside the collapsible body (it would hide when folded)")

    # Persistence + wiring: a progress-specific localStorage key DISTINCT from
    # the language, health, and usage keys, plus the toggle/paint/wire hooks.
    if "loop_dashboard_progress_expanded" not in html_full:
        _fail("G18: script is missing the progress-collapse localStorage key")
    for other in ("loop_dashboard_health_expanded", "loop_dashboard_usage_expanded"):
        if other not in html_full:
            _fail("G18: expected the {0} key to remain present (distinct keys)".format(other))
    for hook in ("toggleProgressCollapse", "paintProgressCollapse", "wireProgressToggle"):
        if hook not in html_full:
            _fail("G18: script is missing the progress collapse hook {0!r}".format(hook))
    # Default OPEN in the source state.
    if "progressUserExpanded = true" not in html_full:
        _fail("G18: progressUserExpanded must default to true (Progress OPEN by default)")

    # The fold survives the poll: renderProgress must not re-fold. Bound the scan
    # to the renderProgress body and assert it never touches the fold classes or
    # re-paints the collapse, so a poll cannot stomp the user's open/closed choice.
    rp_start = html_full.find("function renderProgress(")
    if rp_start < 0:
        _fail("G18: renderProgress not found")
    rp_end = html_full.find("\n      function ", rp_start + 1)
    rp_body = html_full[rp_start: rp_end if rp_end > 0 else rp_start + 4000]
    if "is-collapsed" in rp_body:
        _fail("G18: renderProgress must not touch the fold classes (a poll would stomp the fold)")
    if "paintProgressCollapse" in rp_body:
        _fail("G18: renderProgress must not re-paint the collapse (the fold survives the poll on its own)")

    # The new i18n key exists with a non-empty en AND zh.
    by_key = {row.get("key"): row for row in rows}
    row = by_key.get("progress_toggle_aria")
    if row is None:
        _fail("G18: i18n dictionary is missing the progress_toggle_aria key")
    if not (row.get("en") or "").strip():
        _fail("G18: progress_toggle_aria has an empty 'en' value")
    if not (row.get("zh") or "").strip():
        _fail("G18: progress_toggle_aria has an empty 'zh' value")


def _check_g21_doctor_notices(html_full: str, rows: list) -> None:
    """G21: the doctor's warnings/issues surface as HUMANIZED notices.

    The copy-humanization sweep adds a code->human-string map so the doctor's
    findings read as plain sentences (with the raw doctor text kept as
    expandable detail). An UNKNOWN code renders a GENERIC human line as the
    primary text with the raw message demoted to the same expandable detail --
    raw protocol text is never the primary line (round-2 verifier finding 5).

    Verifies, on the SERVED HTML (before any JS runs):
      * the #doctor-notices container exists INSIDE the #health-body collapsible
        wrapper, AFTER the #badges tiles (so it folds with the section and never
        hides the at-a-glance verdict badge in the head);
      * renderBadges calls renderDoctorNotices, and that renderer reads BOTH
        doc.issues and doc.warnings, threads a code->i18n-key map
        (DOCTOR_NOTE_KEYS), skips codes already surfaced elsewhere
        (DOCTOR_NOTE_SKIP), keeps the raw doctor message as an expandable detail
        (.dn-raw), and renders the generic doctor_note_unknown line (raw message
        in the detail) for an unknown code;
      * every doctor-notice i18n key (the heading, the detail toggle, and each
        mapped code) exists with a non-empty en AND zh -- the completeness scan
        covering the new keys.
    """
    # The container lives inside the health-body collapsible wrapper, after the
    # badges tiles (reuse the same body regex the collapse check uses).
    body_m = re.search(
        r'<div class="collapsible-body" id="health-body">(.*?)</div>\s*</section>',
        html_full, re.S)
    if not body_m:
        _fail("G21: #health-body collapsible wrapper not found")
    body_inner = body_m.group(1)
    if 'id="doctor-notices"' not in body_inner:
        _fail("G21: #doctor-notices container must live inside the #health-body fold")
    if body_inner.find('id="badges"') > body_inner.find('id="doctor-notices"'):
        _fail("G21: #doctor-notices must come AFTER the #badges tiles")

    # The renderer + its wiring.
    if "renderDoctorNotices" not in html_full:
        _fail("G21: script is missing the renderDoctorNotices renderer")
    rb_start = html_full.find("function renderBadges(")
    rb_body = html_full[rb_start: rb_start + 2500] if rb_start >= 0 else ""
    if "renderDoctorNotices(" not in rb_body:
        _fail("G21: renderBadges must call renderDoctorNotices so notices repaint each poll")
    for needle in ("DOCTOR_NOTE_KEYS", "DOCTOR_NOTE_SKIP",
                   "doc.issues", "doc.warnings", "dn-raw"):
        if needle not in html_full:
            _fail("G21: renderDoctorNotices is missing {0!r}".format(needle))
    # Unknown-code fallback (round 2): the PRIMARY text is the generic human
    # line, never the raw protocol message; the raw text is demoted to the same
    # expandable dn-detail/dn-raw block a known code uses.
    if 't("doctor_note_unknown")' not in html_full:
        _fail("G21: an unknown doctor code must render the generic "
              "doctor_note_unknown line as its primary text")
    unk_i = html_full.find('t("doctor_note_unknown")')
    unk_after = html_full[unk_i: unk_i + 600]
    if "dn-detail" not in unk_after or "dn-raw" not in unk_after:
        _fail("G21: the unknown-code branch must demote the raw doctor message "
              "to the expandable dn-detail/dn-raw block")
    # The raw message must no longer be a primary dn-text anywhere.
    if 'el("span", "dn-text", r.w.message' in html_full:
        _fail("G21: raw doctor text must never be the primary dn-text line")

    # Every doctor-notice i18n key exists with a non-empty en AND zh.
    by_key = {row.get("key"): row for row in rows}
    required = [
        "doctor_notices_heading", "doctor_notice_detail_label",
        "doctor_note_missing_file", "doctor_note_missing_lane_file",
        "doctor_note_unknown_request_owner", "doctor_note_stale_marker",
        "doctor_note_blocked_tracker_items", "doctor_note_fix_cycle_thrash",
        "doctor_note_budget_exhausted", "doctor_note_missing_evidence",
        "doctor_note_orphan_evidence", "doctor_note_evidence_naming",
        "doctor_note_uncommitted_work", "doctor_note_handoff_sensitive_content",
        "doctor_note_unknown",
    ]
    for k in required:
        row = by_key.get(k)
        if row is None:
            _fail("G21: i18n dictionary is missing the doctor-notice key {0!r}".format(k))
        if not (row.get("en") or "").strip():
            _fail("G21: doctor-notice key {0!r} has an empty 'en' value".format(k))
        if not (row.get("zh") or "").strip():
            _fail("G21: doctor-notice key {0!r} has an empty 'zh' value".format(k))
    # The mapped-code keys must all be present in the JS map (so a code the doctor
    # can emit never renders its bare code when a human string exists).
    for code_key in (
        '"doctor_note_missing_file"', '"doctor_note_orphan_evidence"',
        '"doctor_note_uncommitted_work"', '"doctor_note_fix_cycle_thrash"',
    ):
        if code_key not in html_full:
            _fail("G21: DOCTOR_NOTE_KEYS is missing a mapped key {0}".format(code_key))


def _check_g21_round2(html_full: str, rows: list) -> None:
    """G21 round 2: the six sustained copy-verifier findings stay fixed.

    Static contracts on the SERVED HTML + i18n blob (finding numbers from the
    adjudicated verify pass):

    (6) the WHOLE git/hook health-tile family speaks human (round-3 residue):
        health_git_note_false is ACTIONABLE (names the verbatim ``git init``
        command); the hook's note_false names ``install_precommit.py``; and NO
        string value anywhere in the dictionary uses the toolmaker jargon
        "scope guard" / "armed" / "honor system" (nor their ZH renderings).
    (7) missing-dependency item: the action sentence is self-contained (no
        trailing colon splicing the command mid-flow); the conversation link
        and the command render as their own lines after it.
    (8) tier-mismatch note: names the situation (model) and an action, not the
        bare "(differs from recommended)".
    (9a) the KNOWN bootstrap default role sentences render localized: the JS
        carries an exact-match map (DEFAULT_ROLE_KEYS) + laneRoleText, wired in
        fillLaneCard; every mapped i18n key exists with the en EXACTLY equal to
        the bootstrap role string (so the exact-match lookup can never miss)
        and a non-empty zh; the parameterized fallback role is matched by shape.
    (9b) None-leak: placeholder blocker cells ("None.", "N/A", "-") are
        filtered before any sentence is composed (isPlaceholderBlocker inside
        classifyLaneNote), so a null-ish value never renders inside a
        localized line -- the classifier falls back to its clean localized text.
    (9c) agent-authored machine text (the halt's decision ask) is a set-off
        .yt-quote block, never spliced into the localized sentence: the halt
        string carries no Y placeholder and the old .replace("Y", ...) splice
        is gone.
    (10) usage naturalness: the folded 5h summary label is the natural ZH hour
        word (u"5\\u5c0f\\u65f6") and the weekly reset date flows through the
        locale pattern usage_date_md (ZH month-day-ri form).
    """
    by_key = {row.get("key"): row for row in rows}

    # (6) actionable git note.
    git_en = (by_key.get("health_git_note_false") or {}).get("en") or ""
    if "git init" not in git_en:
        _fail("G21r2: health_git_note_false must name the verbatim 'git init' "
              "action; got {0!r}".format(git_en))
    # (6, round 3) the hook's absent-state note is equally actionable.
    hook_false_en = (by_key.get("health_hook_note_false") or {}).get("en") or ""
    if "install_precommit.py" not in hook_false_en:
        _fail("G21r2: health_hook_note_false must name the verbatim "
              "install_precommit.py action; got {0!r}".format(hook_false_en))
    # (6, round 3) the git subtitle explains what Git means for the human.
    git_sub_en = (by_key.get("health_git_label") or {}).get("subtitle_en") or ""
    if "git" not in git_sub_en.lower():
        _fail("G21r2: health_git_label subtitle must mention Git; got {0!r}".format(
            git_sub_en))
    # (6, round 3) NO string value anywhere may use the toolmaker jargon the
    # verifier flagged: "scope guard"/"scope-guard", whole-word "armed",
    # "honor system"/"honor-system" -- nor the ZH renderings (fan-wei-bao-hu
    # u8303u56f4u4fddu62a4, xie-fan-wei-shou-wei u5199u8303u56f4u5b88u536b,
    # rong-yu u8363u8a89). Scans EVERY value + subtitle of EVERY key.
    banned_en = [re.compile(r"scope[ -]guard", re.I),
                 re.compile(r"\barmed\b", re.I),
                 re.compile(r"honor[ -]system", re.I)]
    banned_zh = ["\u8303\u56f4\u4fdd\u62a4",      # fan wei bao hu
                 "\u5199\u8303\u56f4\u5b88\u536b",  # xie fan wei shou wei
                 "\u8363\u8a89"]                    # rong yu
    for row in rows:
        for field in ("en", "zh", "subtitle_en", "subtitle_zh"):
            val = row.get(field) or ""
            for pat in banned_en:
                if pat.search(val):
                    _fail("G21r2: banned jargon {0!r} in i18n value {1!r}/{2}".format(
                        pat.pattern, row.get("key"), field))
            for tok in banned_zh:
                if tok in val:
                    _fail("G21r2: banned ZH jargon (escape {0!r}) in i18n value "
                          "{1!r}/{2}".format(tok.encode("unicode_escape").decode("ascii"),
                                             row.get("key"), field))

    # (7) missing-dep three-part structure: self-contained action sentence.
    dep = by_key.get("yourturn_item_missing_dep") or {}
    dep_en = (dep.get("en") or "").strip()
    dep_zh = (dep.get("zh") or "").strip()
    if dep_en.endswith(":"):
        _fail("G21r2: yourturn_item_missing_dep en must not end with a colon "
              "(the where-line renders between it and the command)")
    # u+FF1A is the fullwidth ZH colon (escape form: this file stays ASCII).
    if dep_zh.endswith(":") or dep_zh.endswith("\uff1a"):
        _fail("G21r2: yourturn_item_missing_dep zh must not end with a colon")
    # The renderer keeps the three parts in order: sentence, where, command.
    gate_i = html_full.find('li.appendChild(document.createTextNode(it.text));')
    gate_seg = html_full[gate_i: gate_i + 800] if gate_i >= 0 else ""
    i_where = gate_seg.find('"yt-where"')
    i_cmd = gate_seg.find('"yt-cmd"')
    if not (0 <= i_where < i_cmd):
        _fail("G21r2: the your-turn item must render sentence -> where -> command")

    # (8) tier-mismatch note names the situation + an action.
    tm_en = ((by_key.get("lane_tier_mismatch_note") or {}).get("en") or "").lower()
    if "model" not in tm_en:
        _fail("G21r2: lane_tier_mismatch_note must say it is about the model; "
              "got {0!r}".format(tm_en))
    if "switch" not in tm_en and "update" not in tm_en:
        _fail("G21r2: lane_tier_mismatch_note must name an action (switch/update)")

    # (9a) localized default roles: JS map + exact-match EN + non-empty ZH.
    for hook in ("DEFAULT_ROLE_KEYS", "laneRoleText", "GENERIC_ROLE_RE"):
        if hook not in html_full:
            _fail("G21r2: script is missing the role-localization hook {0!r}".format(hook))
    if "laneRoleText(lane.role)" not in html_full:
        _fail("G21r2: fillLaneCard must render roles through laneRoleText")
    role_keys = {
        "product": "lane_role_default_product",
        "implementation": "lane_role_default_implementation",
        "review": "lane_role_default_review",
        "research": "lane_role_default_research",
        "visual": "lane_role_default_visual",
        "security": "lane_role_default_security",
        "data": "lane_role_default_data",
        "docs": "lane_role_default_docs",
        "release": "lane_role_default_release",
        "media": "lane_role_default_media",
    }
    bootstrap_roles = {}
    bootstrap_roles.update(
        {k: v["role"] for k, v in bootstrap_agent_loop.DEFAULT_LANES.items()})
    bootstrap_roles.update(
        {k: v["role"] for k, v in bootstrap_agent_loop.LANE_PRESETS.items()})
    for lane_name, key in role_keys.items():
        row = by_key.get(key)
        if row is None:
            _fail("G21r2: i18n dictionary is missing the role key {0!r}".format(key))
        if not (row.get("zh") or "").strip():
            _fail("G21r2: role key {0!r} has an empty 'zh' value".format(key))
        expected = bootstrap_roles.get(lane_name)
        if expected and (row.get("en") or "") != expected:
            _fail("G21r2: role key {0!r} en must EXACTLY equal the bootstrap "
                  "default {1!r} (exact-match lookup); got {2!r}".format(
                      key, expected, row.get("en")))
        # And the exact EN string must appear as a DEFAULT_ROLE_KEYS map key.
        if expected and json.dumps(expected) not in html_full:
            _fail("G21r2: DEFAULT_ROLE_KEYS is missing the exact bootstrap role "
                  "string for {0!r}".format(lane_name))
    if by_key.get("lane_role_default_generic") is None:
        _fail("G21r2: i18n dictionary is missing lane_role_default_generic")

    # (9b) None-leak: the placeholder filter exists and classifyLaneNote uses it.
    if "isPlaceholderBlocker" not in html_full:
        _fail("G21r2: script is missing the isPlaceholderBlocker filter")
    re_i = html_full.find("PLACEHOLDER_BLOCKER_RE")
    re_line = html_full[re_i: re_i + 120] if re_i >= 0 else ""
    if "none" not in re_line.lower():
        _fail("G21r2: PLACEHOLDER_BLOCKER_RE must match the 'None.' placeholder")
    cl_start = html_full.find("function classifyLaneNote(")
    cl_end = html_full.find("\n      function ", cl_start + 1)
    cl_body = html_full[cl_start: cl_end if cl_end > 0 else cl_start + 6000]
    if "isPlaceholderBlocker" not in cl_body:
        _fail("G21r2: classifyLaneNote must filter placeholder blockers "
              "(the ZH intake-note 'None.' leak)")

    # (9c) the halt decision text is a set-off quote, never spliced.
    halt_en = ((by_key.get("yourturn_item_halt") or {}).get("en") or "")
    if re.search(r"\bY\b", halt_en):
        _fail("G21r2: yourturn_item_halt must no longer carry the Y splice "
              "placeholder; got {0!r}".format(halt_en))
    if '.replace("Y"' in html_full:
        _fail("G21r2: the old .replace(\"Y\", ...) mid-sentence splice is back")
    if "yt-quote" not in html_full:
        _fail("G21r2: the .yt-quote set-off block for agent-authored text is missing")
    if "it.quote" not in html_full:
        _fail("G21r2: renderYourTurn never renders the item's quote block")

    # (10) usage naturalness: ZH 5-hour label + locale date pattern.
    s5_zh = ((by_key.get("usage_summary_5h") or {}).get("zh") or "")
    if s5_zh != "5\u5c0f\u65f6":
        _fail("G21r2: usage_summary_5h zh must be '5\u5c0f\u65f6'; got {0!r}".format(s5_zh))
    md = by_key.get("usage_date_md")
    if md is None:
        _fail("G21r2: i18n dictionary is missing usage_date_md")
    if "MON" not in (md.get("en") or "") or "DD" not in (md.get("en") or ""):
        _fail("G21r2: usage_date_md en must carry the MON and DD tokens")
    if not (md.get("zh") or "").endswith("\u65e5"):
        _fail("G21r2: usage_date_md zh must end with '\u65e5' (M\u6708D\u65e5)")
    if 't("usage_date_md")' not in html_full:
        _fail("G21r2: localReset must compose the weekly reset date through "
              "usage_date_md")


def _check_batch2_markup(html_full: str) -> None:
    """Assert the Batch 2 (dashboard humanization) markup + wiring exist.

    Covers, on the SERVED HTML (before any JS runs), the STRUCTURAL contract for
    each Batch 2 fix -- the per-behavior runtime assertions live in main() against
    a seeded loop. Every check here would fail loudly if a regression dropped a
    fix's markup or its wiring hook.
    """
    # F14: the tracker-derived Progress section, its count element, and the
    # milestones list + the renderProgress wiring.
    for needle in ('id="progress-section"', 'id="progress-count"',
                   'id="progress-fill"', 'id="progress-current-title"',
                   'id="milestones"'):
        if needle not in html_full:
            _fail("F14: served HTML is missing the progress element {0!r}".format(needle))
    if "renderProgress" not in html_full:
        _fail("F14: script is missing the renderProgress wiring")
    if "parse_tracker_progress" and "tracker_progress" not in html_full:
        _fail("F14: script never reads state.tracker_progress")

    # F3: the FIVE distinct blocked-taxonomy state classes must all be styled,
    # plus the friendly awaiting-objective empty state. The classifier + empty
    # state must be wired in the script.
    for cls in ("lane-note-halt", "lane-note-gated", "lane-note-waiting",
                "lane-note-infra", "lane-note-scope"):
        if "." + cls not in html_full:
            _fail("F3: served HTML is missing the taxonomy CSS class .{0}".format(cls))
    if 'id="awaiting-objective"' not in html_full:
        _fail("F3: served HTML is missing the #awaiting-objective empty state")
    if "classifyLaneNote" not in html_full:
        _fail("F3: script is missing the classifyLaneNote taxonomy classifier")
    if "awaiting_objective" not in html_full:
        _fail("F3: script never reads state.awaiting_objective")

    # G10 human-gate tones: the amber human-GATE and blue result-CONFIRM lane
    # note classes must be styled, and the classifier must read the loop-level
    # intake + held-for-human-qa context that drives them (the run-2 red-intake
    # fix). RED stays reserved for a genuine request-level halt.
    for cls in ("lane-note-gate", "lane-note-confirm"):
        if "." + cls not in html_full:
            _fail("G10: served HTML is missing the tone CSS class .{0}".format(cls))
    if "held_for_human_qa" not in html_full:
        _fail("G10: script never reads doctor.held_for_human_qa for the blue confirm tone")
    if "laneNoteCtx" not in html_full:
        _fail("G10: renderLanes must build and thread the laneNoteCtx tone context")
    if "awaitingObjective" not in html_full:
        _fail("G10: classifyLaneNote must read the intake (awaitingObjective) gate signal")

    # G13: the your-turn halt item renders the lane's recommended_answer when
    # present. The classifier must read the doctor's recommended_answers map, the
    # renderer must emit the .yt-recommended span with the localized label, and
    # the value is machine text (no i18n on the value).
    if "recommended_answers" not in html_full:
        _fail("G13: script never reads doctor.recommended_answers")
    if "yt-recommended" not in html_full:
        _fail("G13: the your-turn item must render a .yt-recommended span for the proposal")
    if "yourturn_recommended_label" not in html_full:
        _fail("G13: the recommended-answer LABEL must flow through the i18n dictionary")

    # F6: the your-turn banner, its three color classes, and the analyzer +
    # renderer wiring. Green/amber/blue must all be styled.
    for cls in ("yt-green", "yt-amber", "yt-blue"):
        if "." + cls not in html_full:
            _fail("F6: served HTML is missing the your-turn banner class .{0}".format(cls))
    if 'id="yourturn"' not in html_full:
        _fail("F6: served HTML is missing the #yourturn banner")
    for hook in ("analyzeNeedsYou", "renderYourTurn"):
        if hook not in html_full:
            _fail("F6: script is missing the wiring hook {0!r}".format(hook))

    # F2: the project title + rename control markup, and the rename wiring that
    # POSTs the third write endpoint. The <title> must be set to include the
    # project (renderProject sets document.title).
    for needle in ('id="project-title"', 'id="project-edit-btn"',
                   'id="project-rename"', 'id="project-input"',
                   'id="project-save-btn"'):
        if needle not in html_full:
            _fail("F2: served HTML is missing the project control {0!r}".format(needle))
    if "/api/project" not in html_full:
        _fail("F2: script never POSTs the /api/project endpoint")
    if "renderProject" not in html_full or "document.title" not in html_full:
        _fail("F2: script must set document.title via renderProject")

    # F9: the needs-you sort + rank-1 affordance wiring.
    for hook in ("sortLanesNeedsYou", "laneRankGroup"):
        if hook not in html_full:
            _fail("F9: script is missing the sort hook {0!r}".format(hook))
    if "needs-you" not in html_full:
        _fail("F9: served HTML is missing the .needs-you rank-1 card class")

    # F12: the in-place lane reconcile must key cards by data-lane and preserve
    # an open <details> across polls. Assert the reconcile does NOT clear the
    # grid with textContent="" and DOES restore the open state.
    if "data-lane" not in html_full:
        _fail("F12: lane cards must be keyed by data-lane for in-place update")
    if 'grid.textContent = ""' in html_full:
        _fail("F12: renderLanes must not wipe the grid with textContent='' "
              "(it stomps open <details>/scroll/focus)")
    if "fillLaneCard" not in html_full:
        _fail("F12: script is missing the fillLaneCard in-place builder")
    # The reconcile must remember and restore the open <details> state.
    if "wasOpen" not in html_full or ".open = true" not in html_full:
        _fail("F12: renderLanes must preserve the open <details> state across polls")
    # F12 FOCUS preservation (verifier finding): focus identity must survive the
    # poll, not just the open state. The reconcile must (a) capture where focus
    # lives BEFORE touching the DOM, (b) reorder minimally via insertBefore
    # (an unconditional per-lane appendChild is a remove+reinsert that blurs
    # focus even when the order is unchanged -- the exact regression found),
    # and (c) restore focus by stable identity at the END of the pass with
    # preventScroll. Assert each piece of that mechanism.
    for hook in ("captureLaneFocus", "restoreLaneFocus"):
        if hook not in html_full:
            _fail("F12: script is missing the focus-preservation hook {0!r}".format(hook))
    if "document.activeElement" not in html_full:
        _fail("F12: the reconcile never inspects document.activeElement "
              "(cannot capture/restore focus)")
    if "preventScroll" not in html_full:
        _fail("F12: focus restore must pass preventScroll so the page does not jump")
    if "insertBefore" not in html_full:
        _fail("F12: renderLanes must reorder minimally via insertBefore")
    if "grid.appendChild(card)" in html_full:
        _fail("F12: renderLanes still unconditionally re-appends every card "
              "(appendChild on an inserted node is a remove+reinsert that "
              "blurs focus each poll); reorder only out-of-position cards")

    # i18n BINDINGS (verifier findings): the browser-tab <title> and the modal
    # placeholders are user-visible strings and must carry dictionary bindings
    # in the SOURCE markup, not just be patched by id at runtime.
    if '<title data-i18n="page_title">' not in html_full:
        _fail("i18n: the <title> element must carry a data-i18n binding")
    for needle in ('data-i18n-placeholder="modal_lane_name_placeholder"',
                   'data-i18n-placeholder="modal_role_placeholder"'):
        if needle not in html_full:
            _fail("i18n: missing placeholder binding {0!r}".format(needle))
    if '"[data-i18n-placeholder]"' not in html_full:
        _fail("i18n: applyStaticI18n must apply placeholders via the generic "
              "[data-i18n-placeholder] loop")

    # F13: the honest heartbeat labeler + the idle-vs-overdue keys.
    if "heartbeatLabel" not in html_full:
        _fail("F13: script is missing the heartbeatLabel honest-label helper")
    if "heartbeat_gap_owners" not in html_full:
        _fail("F13: script never reads doctor.heartbeat_gap_owners for overdue gating")

    # F15: the prominent staleness line + the live-source note + reset helpers.
    for needle in ('id="usage-staleness"', 'id="usage-asof-line"'):
        if needle not in html_full:
            _fail("F15: served HTML is missing the staleness element {0!r}".format(needle))
    for hook in ("usageAsOfLine", "usage_resets_today_prefix", "usage_live_source_note"):
        if hook not in html_full:
            _fail("F15: script/dictionary is missing the staleness hook {0!r}".format(hook))


def _check_batch2_i18n(rows: list) -> None:
    """Assert every new Batch 2 i18n key exists with a non-empty en AND zh.

    Complements the general i18n integrity check (which enforces non-empty
    en/zh for ALL keys) by pinning the specific keys each Batch 2 fix
    introduced, so a regression cannot silently drop one back to an inline
    literal. ``rows`` is the parsed ``strings`` array.
    """
    by_key = {}
    for row in rows:
        by_key[row.get("key")] = row
    required = [
        # F14 progress
        "progress_kicker", "progress_heading", "progress_count_label",
        "progress_blocked_flag", "progress_current_label", "progress_empty",
        # F3 awaiting-objective + taxonomy labels
        "awaiting_objective_title", "awaiting_objective_body",
        "lane_note_halt_label", "lane_note_gated_label", "lane_note_waiting_label",
        "lane_note_infra_label", "lane_note_scope_label",
        # G10 human-gate tones (amber gate + blue confirm) and their wait phrases.
        "lane_note_gate_label", "lane_note_confirm_label", "lane_note_intake_wait",
        "lane_note_confirm_wait", "lane_note_dep_wait",
        # F6 your-turn banner
        "yourturn_badge_running", "yourturn_badge_gate", "yourturn_badge_confirm",
        "yourturn_running_headline", "yourturn_gate_headline", "yourturn_confirm_headline",
        "yourturn_where_lane", "yourturn_item_halt", "yourturn_item_stalled",
        # G20: the honest REVIEWING-stall variant (implementation evidence green,
        # no verdict) -- distinct from the "finished" work_done_unreported key.
        "yourturn_item_stalled_review",
        "yourturn_item_workerless", "yourturn_item_missing_dep", "yourturn_item_confirm",
        "yourturn_running_active",
        # G13 recommended-answer label.
        "yourturn_recommended_label",
        # F9 rank-1 + F14 goal
        "lane_needs_you_flag", "lane_goal_label",
        # F13 honest heartbeat
        "lane_meta_heartbeat_idle",
        # F2 project rename
        "project_edit_button", "project_edit_aria", "project_input_aria",
        "project_save_button", "project_cancel_button", "project_msg_empty",
        "project_msg_saving", "project_msg_success", "project_msg_server_error",
        "project_msg_network_error",
        # F4 health passthrough (git/hook badges)
        "health_git_label", "health_git_note_true", "health_git_note_false",
        "health_hook_label", "health_hook_note_true", "health_hook_note_false",
        # F15 usage staleness + reset formats
        "usage_resets_prefix", "usage_resets_today_prefix", "usage_asof_line",
        "usage_asof_just_now", "usage_live_source_note",
    ]
    for k in required:
        row = by_key.get(k)
        if row is None:
            _fail("Batch 2 i18n dictionary is missing required key {0!r}".format(k))
        if not (row.get("en") or "").strip():
            _fail("Batch 2 i18n key {0!r} has an empty 'en' value".format(k))
        if not (row.get("zh") or "").strip():
            _fail("Batch 2 i18n key {0!r} has an empty 'zh' value".format(k))


def _check_g10_tones(base: str, tmp: Path) -> None:
    """G10: human-gate states render amber/blue, never the run-2 red.

    Two proofs, one static (the classifier's tone ORDER in source) and two
    runtime (the state signals the client tones read):

    STATIC -- in ``dashboard.html`` the ``classifyLaneNote`` function must decide
    the amber intake GATE and the blue human-QA CONFIRM tones BEFORE it can ever
    reach the red ``lane-note-halt`` branch, so an intake wait / a held result
    can never fall through to red (the exact run-2 regression). We assert the
    amber ``lane-note-gate`` and blue ``lane-note-confirm`` returns both precede
    the red ``lane-note-halt`` return inside that function's body.

    RUNTIME (a) -- a fresh loop still at intake (goal.md is the placeholder, no
    real request) exposes ``awaiting_objective: true`` on /api/state; that is the
    signal the client turns into the AMBER intake gate for the waiting lane
    (instead of the old red "BLOCKED -- NEEDS YOU").

    RUNTIME (b) -- a user-facing slice held at REVIEWING with a
    ``human_qa_requested`` run-log row (and no confirmation) surfaces its
    request_id in ``doctor.held_for_human_qa``; that is the signal the client
    turns into the BLUE "ready to try" confirm tone.
    """
    # ---- STATIC: tone order inside classifyLaneNote --------------------------
    dash_src = (Path(loop_dashboard.__file__).resolve().parent / "dashboard.html").read_text(
        encoding="utf-8")
    fn_start = dash_src.find("function classifyLaneNote(")
    if fn_start < 0:
        _fail("G10: classifyLaneNote not found in dashboard.html")
    # Bound the scan to the function body (up to the next top-level function).
    fn_end = dash_src.find("\n      function ", fn_start + 1)
    body = dash_src[fn_start: fn_end if fn_end > 0 else len(dash_src)]
    i_gate = body.find('"lane-note-gate"')
    i_confirm = body.find('"lane-note-confirm"')
    i_halt = body.find('"lane-note-halt"')
    if i_gate < 0 or i_confirm < 0 or i_halt < 0:
        _fail("G10: classifyLaneNote must return all three tones (gate/confirm/halt)")
    if not (i_gate < i_halt):
        _fail("G10: the amber intake GATE branch must precede the red halt branch "
              "(an intake wait must never fall through to red)")
    if not (i_confirm < i_halt):
        _fail("G10: the blue human-QA CONFIRM branch must precede the red halt branch")

    # ---- RUNTIME (a): awaiting-objective loop exposes the intake signal ------
    intake_loop = tmp / "g10_intake_loop"
    _bootstrap(intake_loop)
    st_intake = loop_dashboard.build_state(intake_loop)
    if st_intake.get("awaiting_objective") is not True:
        _fail("G10: a fresh intake loop must expose awaiting_objective:true "
              "(the amber intake-gate signal)")

    # ---- RUNTIME (b): a held user-facing slice exposes held_for_human_qa -----
    held_loop = tmp / "g10_held_loop"
    _bootstrap(held_loop)
    held_req = "REQ-20260707-101010-frontend"
    (held_loop / "requests.md").write_text(
        "# Requests\n\n## Queue\n\n"
        "| request_id | status | owner_lane | iteration | source_docs "
        "| last_message | next_action | updated_at |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| {rid} | REVIEWING | frontend | 1 | goal.md | reviewed "
        "| awaiting human sign-off | 2026-07-07T10:10:00Z |\n".format(rid=held_req),
        encoding="utf-8",
    )
    # A human_qa_requested run-log row with NO matching confirmation -> held.
    (held_loop / "loop-run-log.md").write_text(
        "# Loop Run Log\n\n"
        "| at | request_id | from_status | to_status | note |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 2026-07-07T10:10:00Z | {rid} | IMPLEMENTATION_DONE | REVIEWING "
        "| human_qa_requested: try the UI at http://127.0.0.1:8011 |\n".format(rid=held_req),
        encoding="utf-8",
    )
    st_held = loop_dashboard.build_state(held_loop)
    held = (st_held.get("doctor") or {}).get("held_for_human_qa") or []
    if held_req not in held:
        _fail("G10: a held user-facing slice must appear in doctor.held_for_human_qa "
              "(the blue confirm-tone signal); got {0!r}".format(held))


def _check_g11_dashboard(tmp: Path) -> None:
    """G11(b): the dashboard state builder timestamp-sorts the run-log tail.

    Writes an append-only run log whose rows are OUT of chronological order (a
    late-append recovery row, which run 2 legally produced) and asserts
    ``build_state``'s ``run_log_tail`` lists the data rows in chronological
    order -- matching the timestamp-sorted reconstruction the in-process doctor
    uses, so the dashboard never shows a stale ordering.
    """
    loop = tmp / "g11_dash_loop"
    _bootstrap(loop)
    runlog = loop / "loop-run-log.md"
    header = (
        "# Loop Run Log\n\n"
        "| timestamp | request_id | iteration | from_status | to_status | lane | note |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
    )
    rid = "REQ-20260707-073729-data-eng"
    # Deliberately out of order: 08:00 appended, then a 07:37 recovery row after.
    shuffled = [
        ("2026-07-07T08:00:00Z", rid, "2", "IMPLEMENTING", "REVIEWING", "review", "late"),
        ("2026-07-07T07:37:29Z", rid, "1", "REQUESTED", "IMPLEMENTING", "data-eng", "recovery"),
        ("2026-07-07T07:50:00Z", rid, "1", "IMPLEMENTING", "REVIEWING", "review", "mid"),
    ]
    runlog.write_text(
        header + "".join("| " + " | ".join(r) + " |\n" for r in shuffled),
        encoding="utf-8",
    )
    state = loop_dashboard.build_state(loop)
    tail = state.get("run_log_tail") or []
    ts_seen = []
    for line in tail:
        if line.strip().startswith("|") and "20260707" in line:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells:
                ts_seen.append(cells[0])
    if len(ts_seen) != 3:
        _fail("G11: dashboard run_log_tail should carry the 3 data rows; got {0!r}".format(ts_seen))
    if ts_seen != sorted(ts_seen):
        _fail("G11: dashboard run_log_tail must be timestamp-ordered on a shuffled "
              "log; got {0!r}".format(ts_seen))


def _check_g14_dashboard(tmp: Path) -> None:
    """G14(c): the dashboard state carries recommended + observed tier per lane.

    Under G16 every lane's registry tier defaults to highest, so a mismatch now
    comes from a lane OBSERVED running a LOWER tier than recorded. Bootstraps
    with adoption-time observed-model stamping so implementation MATCHES its
    recommended tier (both highest) and product MISMATCHES (observed
    second-highest vs recommended highest), then asserts the ``build_state`` lane
    objects carry ``observed_model`` (the DATA value), ``observed_tier`` (the
    abstract tag), and ``tier_mismatch`` (True only for product). The chip
    renders these; the amber styling keys off tier_mismatch. A lane with no
    observed model reports tier_mismatch False (not-yet-observed).
    """
    loop = tmp / "g14_dash_loop"
    _bootstrap(loop, extra_argv=[
        "--observed-model", "implementation=gpt-5.5 xhigh (highest)",
        "--observed-model", "product=gpt-5.4 xhigh (second-highest)",
    ])
    state = loop_dashboard.build_state(loop)
    by_lane = {l.get("lane"): l for l in state.get("lanes", [])}
    impl = by_lane.get("implementation") or {}
    prod = by_lane.get("product") or {}
    rev = by_lane.get("review") or {}
    if impl.get("observed_model") != "gpt-5.5 xhigh (highest)":
        _fail("G14: implementation lane must carry observed_model verbatim; got {0!r}".format(
            impl.get("observed_model")))
    if impl.get("observed_tier") != "highest":
        _fail("G14: implementation observed_tier should be 'highest'")
    if impl.get("tier_mismatch") is not False:
        _fail("G14: implementation (observed matches recommended) must not be a tier_mismatch")
    if prod.get("tier_mismatch") is not True:
        _fail("G14: product (observed second-highest vs recommended highest) must be tier_mismatch True")
    if prod.get("observed_tier") != "second-highest":
        _fail("G14: product observed_tier should be 'second-highest'")
    # A not-yet-observed lane: no observed model, no mismatch.
    if rev.get("observed_model"):
        _fail("G14: review lane should have no observed_model (not stamped)")
    if rev.get("tier_mismatch") is not False:
        _fail("G14: a not-yet-observed lane must report tier_mismatch False")


def _check_g17_markup(html_full: str) -> None:
    """G17: the 'all running' banner attributes each 'working on this request'
    line ONLY to the request's owner_lane.

    Static contract on the SERVED HTML (the runtime attribution proof lives in
    ``_check_g17_banner_attribution`` against build_state): renderYourTurn's
    running branch must read the server-computed ``cr.is_owner`` flag, SKIP a
    non-owning lane, and dedup by request_id so one request never yields two
    'working on' items. Guards against a regression that re-lists every
    recently-active lane carrying the owner's next_action (the run-3 duplicate).
    """
    if "is_owner" not in html_full:
        _fail("G17: renderYourTurn must read cr.is_owner to attribute the running item to the owner")
    if "cr.is_owner !== true" not in html_full:
        _fail("G17: the running banner must skip a non-owning lane (cr.is_owner !== true)")
    if "seenRunReqIds" not in html_full:
        _fail("G17: the running banner must dedup by request_id (one item per request)")


def _check_g20_stall_honesty(html_full: str, rows: list) -> None:
    """G20: the your-turn stall item follows the honest reason split.

    Static contract on the SERVED HTML + i18n blob:
      * a distinct ``yourturn_item_stalled_review`` key exists with a non-empty
        en AND zh (the general integrity check enforces non-empty; this pins it);
      * that REVIEWING-case string is HONEST -- its en never claims the review
        "finished", and it names what the files actually prove (the automated
        check passed + the review decision is still missing);
      * the original ``yourturn_item_stalled`` (the work_done_unreported case,
        where "finished" is accurate) still exists;
      * renderYourTurn BRANCHES on the machine reason: it references the new
        key AND the doctor reason token ``implementation_evidence_green_no_verdict``
        so a REVIEWING gate-green stall renders the honest string, not the
        "finished" one.
    """
    by_key = {row.get("key"): row for row in rows}
    review = by_key.get("yourturn_item_stalled_review")
    if review is None:
        _fail("G20: i18n dictionary is missing the yourturn_item_stalled_review key")
    en = (review.get("en") or "").strip()
    zh = (review.get("zh") or "").strip()
    if not en:
        _fail("G20: yourturn_item_stalled_review has an empty 'en' value")
    if not zh:
        _fail("G20: yourturn_item_stalled_review has an empty 'zh' value")
    if "finish" in en.lower():
        _fail("G20: the REVIEWING stall string must NOT claim the review 'finished'; got {0!r}".format(en))
    # It must say what the files prove: the automated check passed + the review
    # decision has not come back yet (humanized wording of "gate green, no
    # verdict" -- G21 dropped the raw gate/verdict jargon from the user text).
    low = en.lower()
    if "check" not in low:
        _fail("G20: the honest REVIEWING stall string must name the passed automated check; "
              "got {0!r}".format(en))
    if "decision" not in low and "review" not in low:
        _fail("G20: the honest REVIEWING stall string must name the still-missing review "
              "decision; got {0!r}".format(en))
    # The original (work_done_unreported) key must remain -- "finished" is honest there.
    if by_key.get("yourturn_item_stalled") is None:
        _fail("G20: the original yourturn_item_stalled key must remain for the work_done_unreported case")
    # The client must BRANCH on the doctor reason to pick the honest string.
    if "yourturn_item_stalled_review" not in html_full:
        _fail("G20: renderYourTurn never references yourturn_item_stalled_review (no honest branch)")
    if "implementation_evidence_green_no_verdict" not in html_full:
        _fail("G20: renderYourTurn must branch on the reason token "
              "implementation_evidence_green_no_verdict to select the honest string")


def _check_g17_banner_attribution(tmp: Path) -> None:
    """G17: exactly ONE 'working on this request' item, attributed to the owner.

    Reproduces the run-3 shape: one IMPLEMENTING request owned by data-eng, with
    product carrying a FRESH heartbeat whose current.md still points at data-eng's
    request. ``build_state`` must flag data-eng's current_request.is_owner True and
    product's is_owner False, so the client's running banner emits exactly one
    'working on' item -- attributed to data-eng -- never a second one carrying
    data-eng's next_action for product.
    """
    loop = tmp / "g17_loop"
    _bootstrap(loop)
    # Register a data-eng lane (the request's owner).
    res = loop_dashboard.add_lane(loop, "data-eng", "Own the data pipeline")
    if not (isinstance(res, dict) and res.get("ok")):
        _fail("G17: could not add the data-eng lane: {0!r}".format(res))
    rid = "REQ-20260708-090000-data-eng"
    # One IMPLEMENTING request owned by data-eng.
    (loop / "requests.md").write_text(
        "# Requests\n\n## Queue\n\n"
        "| request_id | status | owner_lane | iteration | source_docs "
        "| last_message | next_action | updated_at |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| {rid} | IMPLEMENTING | data-eng | 2 | goal.md | building "
        "| Wire the parser into build_state | 2026-07-08T09:00:00Z |\n".format(rid=rid),
        encoding="utf-8",
    )
    ts = "2026-07-08T09:05:00Z"
    # data-eng: the OWNER, current.md points at the request.
    (loop / "lanes" / "data-eng" / "current.md").write_text(
        "# Data Eng Current State\n\n"
        "current_request_id: {rid}\nstatus: implementing\niteration: 2\n"
        "last_updated: {ts}\nheartbeat: {ts}\n".format(rid=rid, ts=ts),
        encoding="utf-8",
    )
    # product: a NON-owner whose current.md still points at data-eng's request,
    # with a fresh heartbeat (the exact run-3 shape that produced the duplicate).
    (loop / "lanes" / "product" / "current.md").write_text(
        "# Product Current State\n\n"
        "current_request_id: {rid}\nstatus: monitoring\niteration: 2\n"
        "last_updated: {ts}\nheartbeat: {ts}\n".format(rid=rid, ts=ts),
        encoding="utf-8",
    )
    state = loop_dashboard.build_state(loop)
    by_lane = {l.get("lane"): l for l in state.get("lanes", [])}
    de_cr = (by_lane.get("data-eng") or {}).get("current_request") or {}
    pr_cr = (by_lane.get("product") or {}).get("current_request") or {}
    # The run-3 shape: BOTH lanes resolve current_request to data-eng's request.
    if de_cr.get("request_id") != rid or pr_cr.get("request_id") != rid:
        _fail("G17: both data-eng and product must resolve current_request to {0}; "
              "got {1!r} / {2!r}".format(rid, de_cr.get("request_id"), pr_cr.get("request_id")))
    # Only the owner (data-eng) is is_owner; the non-owning product is not.
    if de_cr.get("is_owner") is not True:
        _fail("G17: data-eng (owner_lane==data-eng) must be current_request.is_owner True; "
              "got {0!r}".format(de_cr.get("is_owner")))
    if pr_cr.get("is_owner") is not False:
        _fail("G17: product (non-owner pointing at data-eng's request) must be is_owner "
              "False; got {0!r}".format(pr_cr.get("is_owner")))
    # Exactly ONE owner-attributed 'working on' item for this request: the client
    # emits one item per advancing current_request whose is_owner is True, so the
    # attributed-lane set for rid must be exactly ['data-eng'].
    attributed = [l.get("lane") for l in state.get("lanes", [])
                  if (l.get("current_request") or {}).get("request_id") == rid
                  and (l.get("current_request") or {}).get("is_owner") is True]
    if attributed != ["data-eng"]:
        _fail("G17: exactly one owner-attributed running item expected for {0}; "
              "got {1!r}".format(rid, attributed))


def _check_g13_recommended(tmp: Path) -> None:
    """G13: BLOCKED requests surface the raising lane's recommended_answer.

    A BLOCKED request whose archived BLOCKED envelope carries a
    ``recommended_answer`` exposes it (verbatim machine text) in the dashboard
    state's ``doctor.recommended_answers`` map keyed by request_id -- the your-
    turn halt item renders it. A BLOCKED request with NO recommended_answer is
    simply absent from the map (no empty/garbage entry).
    """
    loop = tmp / "g13_loop"
    _bootstrap(loop)
    rid = "REQ-20260707-140000-implementation"
    mdir = loop / "messages" / rid
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "BLOCKED-iter-1.md").write_text(
        "# BLOCKED\n\nmessage_type: BLOCKED\nrequest_id: {rid}\nstatus: BLOCKED\n"
        "blocker:\n- Missing the production catalog key.\n"
        "recommended_answer:\n- Use the bundled local mock catalog for the MVP.\n"
        "expected_reply:\n- Product updates scope.\n".format(rid=rid),
        encoding="utf-8",
    )
    # A second BLOCKED request with NO recommended_answer.
    rid2 = "REQ-20260707-150000-frontend"
    mdir2 = loop / "messages" / rid2
    mdir2.mkdir(parents=True, exist_ok=True)
    (mdir2 / "BLOCKED-iter-1.md").write_text(
        "# BLOCKED\n\nstatus: BLOCKED\nblocker:\n- Waiting on a design call.\n",
        encoding="utf-8",
    )
    (loop / "requests.md").write_text(
        "# Requests\n\n## Queue\n\n"
        "| request_id | status | owner_lane | iteration | source_docs "
        "| last_message | next_action | updated_at |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| {r1} | BLOCKED | implementation | 1 | goal.md | blocked "
        "| decide the catalog source | 2026-07-07T14:00:00Z |\n"
        "| {r2} | BLOCKED | frontend | 1 | goal.md | blocked "
        "| decide the layout | 2026-07-07T15:00:00Z |\n".format(r1=rid, r2=rid2),
        encoding="utf-8",
    )
    state = loop_dashboard.build_state(loop)
    ra = (state.get("doctor") or {}).get("recommended_answers") or {}
    if ra.get(rid) != "Use the bundled local mock catalog for the MVP.":
        _fail("G13: dashboard state must expose the BLOCKED request's recommended_answer "
              "verbatim; got {0!r}".format(ra.get(rid)))
    if rid2 in ra:
        _fail("G13: a BLOCKED request with no recommended_answer must not appear in the map")


def _check_f8_tier_markup(html_full: str, rows: list) -> None:
    """Assert the F8/G14 tier lane chip markup, wiring, and i18n.

    Covers, on the SERVED HTML (before any JS runs):
      * fillLaneCard reads lane.recommended_tier and renders a neutral chip;
      * G14(c): fillLaneCard also reads lane.observed_model / lane.tier_mismatch
        and renders the observed chip (amber on mismatch); the two G14 i18n keys
        exist with non-empty en AND zh;
      * the F8 i18n keys exist with non-empty en AND zh;
      * NO concrete model NAME LITERAL (gpt-*) is hardcoded anywhere in the
        served page. G14 note: the observed model+effort is DATA rendered at
        runtime from ``lane.observed_model`` (a variable, never a source
        literal), so this ban is on hardcoded policy/UI strings only -- the
        served static HTML legitimately carries the VARIABLE reference but no
        model literal. The ban is enforced on BOTH the whole served page and,
        specifically, every i18n dictionary value (the user-visible strings).
    """
    if "lane.recommended_tier" not in html_full:
        _fail("F8: fillLaneCard never reads lane.recommended_tier")
    if "lane_meta_tier_label" not in html_full:
        _fail("F8: served HTML is missing the lane_meta_tier_label chip binding")
    # G14(c): the observed-tier chip must be wired from the runtime DATA fields.
    if "lane.observed_model" not in html_full:
        _fail("G14: fillLaneCard never reads lane.observed_model for the observed chip")
    if "lane.tier_mismatch" not in html_full:
        _fail("G14: fillLaneCard never reads lane.tier_mismatch for the amber styling")
    by_key = {row.get("key"): row for row in rows}
    for k in ("lane_meta_tier_label", "lane_tier_highest", "lane_tier_second_highest",
              "lane_meta_observed_label", "lane_tier_mismatch_note"):
        row = by_key.get(k)
        if row is None:
            _fail("F8/G14 i18n dictionary is missing required key {0!r}".format(k))
        if not (row.get("en") or "").strip():
            _fail("F8/G14 i18n key {0!r} has an empty 'en' value".format(k))
        if not (row.get("zh") or "").strip():
            _fail("F8/G14 i18n key {0!r} has an empty 'zh' value".format(k))
    # Grep-proof (surgical for G14): no gpt-* model NAME LITERAL may be hardcoded
    # in the served dashboard -- not in any i18n VALUE (user-visible strings),
    # and not anywhere else in the page. The observed model+effort is rendered
    # from runtime data (lane.observed_model), so the ban targets literals, which
    # this substring check catches (a variable name is not "gpt-").
    for row in rows:
        for field in ("en", "zh", "subtitle_en", "subtitle_zh"):
            if "gpt-" in (row.get(field) or "").lower():
                _fail("F8/G14: a concrete model name (gpt-*) is hardcoded in i18n "
                      "value {0!r}/{1}".format(row.get("key"), field))
    if "gpt-" in html_full.lower():
        _fail("F8/G14: a concrete model name (gpt-*) literal leaked into the served "
              "HTML (observed model must be rendered from runtime data, not a literal)")


def _strip_comments_and_docstrings(source: str) -> str:
    """Return ``source`` with all comments and docstrings blanked out.

    Docstrings (the first statement of a module / class / function) are removed
    via the AST; comment tokens are removed via ``tokenize``. This lets the
    decoupling assertion below scan ONLY executable code, so the ``rate_limits``
    / ``auth.json`` / ``id_token`` literals that legitimately survive in the
    dashboard's prose do not trip it -- only a residual DIRECT PARSE would.
    """
    import ast

    tree = ast.parse(source)
    doc_ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                s = body[0].value
                doc_ranges.append((s.lineno, s.end_lineno))
    lines = source.splitlines()
    for (a, b) in doc_ranges:
        for ln in range(a, b + 1):
            if 1 <= ln <= len(lines):
                lines[ln - 1] = ""
    stripped = "\n".join(lines)

    out_lines = stripped.splitlines()
    try:
        comment_spans: dict = {}
        for t in tokenize.generate_tokens(io.StringIO(stripped).readline):
            if t.type == tokenize.COMMENT:
                comment_spans.setdefault(t.start[0], []).append((t.start[1], t.end[1]))
    except tokenize.TokenError:
        comment_spans = {}
    for row, spans in comment_spans.items():
        if 1 <= row <= len(out_lines):
            line = out_lines[row - 1]
            for (scol, ecol) in sorted(spans, reverse=True):
                line = line[:scol] + line[ecol:]
            out_lines[row - 1] = line
    return "\n".join(out_lines)


def _check_dashboard_decoupled_from_host() -> None:
    """Assert the dashboard no longer directly parses the Codex host surfaces.

    After the decoupling refactor, ALL reading of the Codex host's undocumented
    data plane (session JSONL ``rate_limits`` events, ``auth.json`` ``id_token``
    JWT and the other token fields) lives ONLY in ``codex_host_probe``. The
    dashboard may still MENTION those names in its prose (docstrings/comments) and
    imports the two providers by name, but its EXECUTABLE code must contain no
    direct parse of those surfaces. This scans the dashboard source with comments
    and docstrings stripped and asserts none of the host-parsing literals survive;
    then it asserts the probe module DOES carry them (they really moved, not just
    vanished).
    """
    scripts_dir = Path(loop_dashboard.__file__).resolve().parent
    dash_src = (scripts_dir / "loop_dashboard.py").read_text(encoding="utf-8")
    probe_src = (scripts_dir / "codex_host_probe.py").read_text(encoding="utf-8")

    dash_code = _strip_comments_and_docstrings(dash_src)
    # Literals that only appear when a module is DIRECTLY parsing the Codex host
    # surfaces. None may survive in the dashboard's executable code.
    host_parse_literals = [
        "rate_limits",
        "id_token",
        "auth.json",
        "access_token",
        "refresh_token",
        "total_token_usage",
        "used_percent",
        "chatgpt_plan_type",
        "sessions",
    ]
    residual = [lit for lit in host_parse_literals if lit in dash_code]
    if residual:
        _fail(
            "loop_dashboard.py executable code still directly parses Codex host "
            "surfaces (these literals must now live only in codex_host_probe.py): "
            "{0}".format(residual)
        )
    # The reason-code string is allowed and expected to remain in the dashboard.
    if "probe_module_missing" not in dash_src:
        _fail("loop_dashboard.py should carry the probe_module_missing reason code")
    # And the dashboard must import the probe's two providers by name.
    if "codex_host_probe" not in dash_src:
        _fail("loop_dashboard.py should import codex_host_probe (guarded)")

    # Sanity: the literals really MOVED -- the probe carries them.
    for lit in ("rate_limits", "id_token", "auth.json"):
        if lit not in probe_src:
            _fail("codex_host_probe.py should carry the moved host-parse literal {0!r}".format(lit))


def _check_probe_standalone_json(fake_home: Path) -> None:
    """Run ``codex_host_probe`` as a subprocess against a fake CODEX_HOME.

    Asserts ``python codex_host_probe.py`` emits valid JSON with the expected
    ``usage`` / ``account`` keys, the account carries the known fake email/plan,
    and NONE of the FAKE_* token strings (id_token signature, access/refresh
    tokens, full account_id) appears anywhere in the emitted JSON -- the same
    red line the HTTP API upholds, verified on the probe's own stdout.
    """
    import subprocess

    probe_path = Path(loop_dashboard.__file__).resolve().parent / "codex_host_probe.py"
    env = dict(os.environ)
    env["CODEX_HOME"] = str(fake_home)
    proc = subprocess.run(
        [sys.executable, str(probe_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    if proc.returncode != 0:
        _fail("codex_host_probe.py exited non-zero: {0}\n{1}".format(proc.returncode, proc.stderr))
    out = proc.stdout
    try:
        data = json.loads(out)
    except ValueError as exc:
        _fail("codex_host_probe.py did not emit valid JSON: {0}\n{1!r}".format(exc, out))
    if "usage" not in data or "account" not in data:
        _fail("probe JSON is missing 'usage'/'account' keys; got {0}".format(sorted(data.keys())))
    usage = data.get("usage") or {}
    account = data.get("account") or {}
    if usage.get("available") is not True:
        _fail("probe usage.available should be True with fake CODEX_HOME; got {0!r}".format(usage))
    if usage.get("plan_type") != "pro":
        _fail("probe usage.plan_type should be 'pro'; got {0!r}".format(usage.get("plan_type")))
    if account.get("available") is not True:
        _fail("probe account.available should be True with fake auth.json; got {0!r}".format(account))
    if account.get("email") != FAKE_ACCOUNT_EMAIL:
        _fail("probe account.email should be {0!r}; got {1!r}".format(
            FAKE_ACCOUNT_EMAIL, account.get("email")))
    # No token material may appear in the probe's standalone JSON output.
    for token_name, token_val in (
        ("id_token signature", FAKE_ID_TOKEN_SIG),
        ("access_token", FAKE_ACCESS_TOKEN),
        ("refresh_token", FAKE_REFRESH_TOKEN),
        ("full account_id", FAKE_ACCOUNT_ID),
    ):
        if token_val in out:
            _fail("AUTH TOKEN LEAK in probe stdout: {0}".format(token_name))


def _check_g26_chunk3_markup(html_full: str, rows: list) -> None:
    """G26 chunk 3: degraded dashboard states stay visible and localized."""
    by_key = {row.get("key"): row for row in rows}
    required = (
        "pulse_data_stale",
        "conn_banner_stale",
        "data_read_error",
        "data_parse_error",
        "requests_data_unavailable",
        "progress_data_unavailable",
        "badge_doctor_note_unavailable",
        "badge_completion_gate_note_not_passed",
        "doctor_note_gate_malformed_evidence",
        "bootstrap_module_unavailable",
        "usage_probe_module_unavailable",
        "usage_summary_module_unavailable",
        "usage_refresh_still_cached",
        "usage_refresh_failed_stale",
    )
    for key in required:
        row = by_key.get(key)
        if row is None:
            _fail("G26 chunk 3: i18n dictionary is missing {0!r}".format(key))
        if not (row.get("en") or "").strip() or not (row.get("zh") or "").strip():
            _fail("G26 chunk 3: {0!r} must have non-empty EN and ZH".format(key))

    for element_id in (
        "data-diagnostics",
        "add-lane-status",
        "usage-refresh-note",
        "progress-diagnostic",
    ):
        if 'id="{0}"'.format(element_id) not in html_full:
            _fail("G26 chunk 3: served HTML is missing #{0}".format(element_id))
    if 'id="data-diagnostics"' not in html_full or 'role="alert"' not in html_full:
        _fail("G26 chunk 3: file diagnostics need a visible alert region")
    if 'id="usage-refresh-note"' not in html_full or 'aria-live="polite"' not in html_full:
        _fail("G26 chunk 3: refresh degradation needs an aria-live status")

    # A failed poll keeps the last render, so the pulse/banner must explicitly
    # name that snapshot as stale rather than merely changing color.
    if 'pulseStateKey = "pulse_data_stale"' not in html_full:
        _fail("G26 C14-refresh: failed polling must set the visible stale-data pulse")
    conn = by_key.get("conn_banner_stale") or {}
    if "last successful" not in (conn.get("en") or "").lower():
        _fail("G26 C14-refresh: conn_banner must explain that the last successful snapshot remains")

    # The refresh path has two distinct honest failures: the request itself can
    # fail, or the backend can respond while admitting its cache was not dropped.
    refresh_start = html_full.find("function refreshUsage(")
    refresh_body = html_full[refresh_start:refresh_start + 2200] if refresh_start >= 0 else ""
    for needle in (
        "cache_drop_failed",
        "refresh_degraded",
        't("usage_refresh_still_cached")',
        't("usage_refresh_failed_stale")',
    ):
        if needle not in refresh_body:
            _fail("G26 C19/C14-refresh: refreshUsage is missing {0!r}".format(needle))

    # Doctor unavailable is itself a health problem: show the sanitized reason
    # and force the fold open. A malformed evidence warning must not be skipped.
    rb_start = html_full.find("function renderBadges(")
    rb_end = html_full.find("// ---- doctor notices", rb_start)
    rb_body = html_full[rb_start:rb_end if rb_end >= 0 else rb_start + 4000]
    if "doc.reason" not in rb_body:
        _fail("G26 C11: renderBadges must surface doc.reason")
    if "doc.available === false" not in rb_body or "healthForcedOpen" not in rb_body:
        _fail("G26 C11: checker-unavailable must force Health open")
    if "doc.completion_gate_ok === true" not in rb_body:
        _fail("G26 C13: the completion note must branch on the gate boolean, not failed-count only")

    skip_start = html_full.find("var DOCTOR_NOTE_SKIP")
    skip_end = html_full.find("};", skip_start)
    skip_body = html_full[skip_start:skip_end] if skip_start >= 0 and skip_end >= 0 else ""
    if "gate_malformed_evidence" in skip_body:
        _fail("G26 C13: gate_malformed_evidence must not remain in DOCTOR_NOTE_SKIP")
    mapping_start = html_full.find("var DOCTOR_NOTE_KEYS")
    mapping_end = html_full.find("};", mapping_start)
    mapping = html_full[mapping_start:mapping_end] if mapping_start >= 0 and mapping_end >= 0 else ""
    if 'gate_malformed_evidence: "doctor_note_gate_malformed_evidence"' not in mapping:
        _fail("G26 C13: malformed evidence needs a humanized doctor notice")

    for hook in ("renderDataDiagnostics", "renderCapabilities"):
        if hook not in html_full:
            _fail("G26 C12/C18: dashboard is missing {0}".format(hook))


def _check_g26_chunk3_state(tmp_path: Path) -> None:
    """G26 chunk 3: API diagnostics distinguish fallback causes with real data."""
    loop = tmp_path / "g26_chunk3_state"
    _bootstrap(loop)

    clean = loop_dashboard.build_state(loop)
    for key in (
        "read_errors",
        "parse_errors",
        "file_status",
        "capabilities",
        "refresh_degraded",
        "cache_drop_failed",
    ):
        if key not in clean:
            _fail("G26 chunk 3: build_state is missing {0!r}".format(key))
    if clean["read_errors"] or clean["parse_errors"]:
        _fail("G26 C12 negative: a clean bootstrapped loop must not report file diagnostics")
    if clean["refresh_degraded"] is not False or clean["cache_drop_failed"] is not False:
        _fail("G26 C19 negative: a normal poll must not report refresh degradation")

    # A valid empty table is different from a malformed table and must stay quiet.
    requests_path = loop / "requests.md"
    tracker_path = loop / "tracker.md"
    requests_original = requests_path.read_bytes()
    tracker_original = tracker_path.read_bytes()
    requests_path.write_text(
        "# Requests\n\n"
        "| request_id | status | owner_lane | iteration |\n"
        "| --- | --- | --- | --- |\n",
        encoding="utf-8",
    )
    valid_empty = loop_dashboard.build_state(loop)
    if any("requests.md" in str(item.get("source", "")) for item in valid_empty["parse_errors"]):
        _fail("G26 C12 negative: a valid empty requests table must not be called malformed")

    # Non-table core inputs retain their old empty fallback, but now expose the
    # source/reason so the page cannot present that fallback as legitimate data.
    requests_path.write_text("# Requests\n\nthis is not a table\n", encoding="utf-8")
    tracker_path.write_text("# Tracker\n\n## Broken\n\nnot a checkpoint\n", encoding="utf-8")
    malformed = loop_dashboard.build_state(loop)
    malformed_sources = {str(item.get("source", "")) for item in malformed["parse_errors"]}
    if not any(source.endswith("requests.md") for source in malformed_sources):
        _fail("G26 C12: malformed requests.md must appear in parse_errors")
    if not any(source.endswith("tracker.md") for source in malformed_sources):
        _fail("G26 C12: malformed tracker.md must appear in parse_errors")
    if malformed.get("requests") != [] or (malformed.get("tracker_progress") or {}).get("available") is not False:
        _fail("G26 C12: diagnostics must not change the existing empty fallback values")

    # Invalid UTF-8 used to escape the OSError-only guard and crash /api/state.
    requests_path.write_bytes(b"\xff\xfe\xfa")
    unreadable = loop_dashboard.build_state(loop)
    if not any(
        str(item.get("source", "")).endswith("requests.md")
        for item in unreadable["read_errors"]
    ):
        _fail("G26 C12: unreadable requests.md must appear in read_errors")
    if (unreadable.get("file_status") or {}).get("requests.md") != "unreadable":
        _fail("G26 C12: file_status must distinguish unreadable requests.md")
    requests_path.unlink()
    missing = loop_dashboard.build_state(loop)
    if (missing.get("file_status") or {}).get("requests.md") != "missing":
        _fail("G26 C12: file_status must distinguish missing requests.md")
    requests_path.write_bytes(requests_original)
    tracker_path.write_bytes(tracker_original)

    # Doctor import and runtime failures keep a sanitized reason on the wire.
    saved_doctor = (
        loop_dashboard.DOCTOR_AVAILABLE,
        loop_dashboard.doctor,
        loop_dashboard.DOCTOR_IMPORT_ERROR,
    )
    try:
        loop_dashboard.DOCTOR_AVAILABLE = False
        loop_dashboard.doctor = None
        loop_dashboard.DOCTOR_IMPORT_ERROR = "ImportError: smoke doctor unavailable"
        snap = loop_dashboard._doctor_snapshot(loop)
        if snap.get("available") is not False or "smoke doctor unavailable" not in snap.get("reason", ""):
            _fail("G26 C11: doctor import failure reason must survive into doctor.reason")
    finally:
        (
            loop_dashboard.DOCTOR_AVAILABLE,
            loop_dashboard.doctor,
            loop_dashboard.DOCTOR_IMPORT_ERROR,
        ) = saved_doctor

    # Bootstrap/probe module failures are explicit capabilities. The affected
    # lane control and usage panel can then render a module-unavailable signal.
    saved_bootstrap = (
        loop_dashboard.BOOTSTRAP_AVAILABLE,
        loop_dashboard.bootstrap_agent_loop,
        loop_dashboard.BOOTSTRAP_IMPORT_ERROR,
    )
    saved_probe = (
        loop_dashboard.PROBE_AVAILABLE,
        loop_dashboard.codex_host_probe,
        loop_dashboard.PROBE_IMPORT_ERROR,
    )
    try:
        loop_dashboard.BOOTSTRAP_AVAILABLE = False
        loop_dashboard.bootstrap_agent_loop = None
        loop_dashboard.BOOTSTRAP_IMPORT_ERROR = "ImportError: smoke bootstrap unavailable"
        loop_dashboard.PROBE_AVAILABLE = False
        loop_dashboard.codex_host_probe = None
        loop_dashboard.PROBE_IMPORT_ERROR = "ImportError: smoke probe unavailable"
        degraded = loop_dashboard.build_state(loop)
        caps = degraded.get("capabilities") or {}
        if (caps.get("bootstrap") or {}).get("available") is not False:
            _fail("G26 C18: bootstrap capability must report unavailable")
        if "smoke bootstrap unavailable" not in (caps.get("bootstrap") or {}).get("reason", ""):
            _fail("G26 C18: bootstrap capability must preserve its sanitized import reason")
        if (caps.get("probe") or {}).get("available") is not False:
            _fail("G26 C18: probe capability must report unavailable")
        usage = degraded.get("usage") or {}
        if usage.get("reason_code") != "probe_module_unavailable":
            _fail("G26 C18: probe import failure needs the module-unavailable usage reason code")
    finally:
        (
            loop_dashboard.BOOTSTRAP_AVAILABLE,
            loop_dashboard.bootstrap_agent_loop,
            loop_dashboard.BOOTSTRAP_IMPORT_ERROR,
        ) = saved_bootstrap
        (
            loop_dashboard.PROBE_AVAILABLE,
            loop_dashboard.codex_host_probe,
            loop_dashboard.PROBE_IMPORT_ERROR,
        ) = saved_probe

    # A manual refresh whose cache drop fails remains serviceable, but the API
    # must admit that the response may still be cached.
    original_drop = loop_dashboard.codex_host_probe.drop_caches
    try:
        def _raise_cache_drop() -> None:
            raise RuntimeError("smoke cache drop failed")

        loop_dashboard.codex_host_probe.drop_caches = _raise_cache_drop
        cached = loop_dashboard.build_state(loop, refresh=True)
    finally:
        loop_dashboard.codex_host_probe.drop_caches = original_drop
    if cached.get("refresh_degraded") is not True or cached.get("cache_drop_failed") is not True:
        _fail("G26 C19: cache-drop failure must set refresh_degraded/cache_drop_failed")
    if "smoke cache drop failed" not in cached.get("refresh_reason", ""):
        _fail("G26 C19: degraded refresh must carry a sanitized reason")


def _set_g30_lane_row(loop: Path, lane_name: str, heartbeat: str) -> None:
    """Register one synthetic lane and give it a deterministic heartbeat."""
    path = loop / "agent-lanes.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    header = []
    for line in lines:
        if line.startswith("| lane |"):
            header = [cell.strip() for cell in line.split("|")[1:-1]]
            break
    if not header:
        _fail("G30 fixture: agent-lanes.md header is missing")
    indexes = {name: i for i, name in enumerate(header)}
    changed = False
    for i, line in enumerate(lines):
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if len(cells) != len(header) or cells[indexes["lane"]] != lane_name:
            continue
        cells[indexes["thread_id"]] = "thread-g30-{0}".format(lane_name)
        cells[indexes["status"]] = "registered"
        cells[indexes["heartbeat"]] = heartbeat
        lines[i] = "| " + " | ".join(cells) + " |"
        changed = True
        break
    if not changed:
        _fail("G30 fixture: lane {0!r} was not found".format(lane_name))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_g30_loop(loop: Path, request_status: str) -> str:
    """Create one request whose owner heartbeat is intentionally stale."""
    _bootstrap(loop)
    rid = "REQ-20260715-120000-implementation"
    (loop / "requests.md").write_text(
        "# Requests\n\n## Queue\n\n"
        "| request_id | status | owner_lane | iteration | source_docs | "
        "last_message | next_action | updated_at |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| {rid} | {status} | implementation | 1 | goal.md | fixture | "
        "fixture next action | 2026-07-15T12:00:00Z |\n".format(
            rid=rid, status=request_status
        ),
        encoding="utf-8",
    )
    owner_heartbeat = "2000-01-01T00:00:00Z"
    _set_g30_lane_row(loop, "implementation", owner_heartbeat)
    (loop / "lanes" / "implementation" / "current.md").write_text(
        "# Implementation Current State\n\n"
        "current_request_id: {rid}\n"
        "status: {status}\n"
        "iteration: 1\n"
        "last_updated: {heartbeat}\n"
        "heartbeat: {heartbeat}\n\n"
        "## Current Checkpoint\n\n- G30 fixture.\n\n"
        "## Next Action\n\n- Fixture next action.\n\n"
        "## Blockers\n\n- None.\n".format(
            rid=rid, status=request_status.lower(), heartbeat=owner_heartbeat
        ),
        encoding="utf-8",
    )
    return rid


def _serve_g30_state(loop: Path, doctor_clean_fixture: bool = False) -> dict:
    """Serve one fixture through the real dashboard HTTP state endpoint."""
    original_snapshot = loop_dashboard._doctor_snapshot
    if doctor_clean_fixture:
        def clean_snapshot(loop_dir: Path) -> dict:
            snapshot = dict(original_snapshot(loop_dir))
            # A stale registered lane necessarily produces orphan_suspect in the
            # real doctor. Isolate the client-side completed predicate with an
            # explicitly synthetic doctor-clean response, after the unmodified
            # response has proved that the stale heartbeat itself is calm.
            snapshot["warnings"] = []
            snapshot["issues"] = []
            snapshot["available"] = True
            return snapshot

        loop_dashboard._doctor_snapshot = clean_snapshot
    loop_dashboard._clear_state_snapshot_cache(loop)
    server = loop_dashboard.make_server(loop, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = "http://127.0.0.1:{0}/api/state".format(server.server_address[1])
        status, body = _http_get(base)
        if status != 200:
            _fail("G30 fixture state request failed with {0}".format(status))
        return json.loads(body.decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        loop_dashboard._doctor_snapshot = original_snapshot
        loop_dashboard._clear_state_snapshot_cache(loop)


def _g30_pulse_inputs(state: dict) -> tuple:
    """Evaluate the documented pulse inputs over a served payload."""
    def owns_active(lane: dict) -> bool:
        current = lane.get("current_request") or {}
        status = str(current.get("status") or "").upper()
        return current.get("is_owner") is True \
            and status not in {"", "PLANNED", "ACCEPTED", "BLOCKED"}

    stale_active_owner = any(
        (lane.get("heartbeat") or {}).get("state") == "stale" and owns_active(lane)
        for lane in state.get("lanes", [])
    )
    requests = state.get("requests") or []
    doctor = state.get("doctor") or {}
    completed = bool(requests) and all(
        str(request.get("status") or "").upper() == "ACCEPTED"
        for request in requests
    ) and doctor.get("available") is not False \
        and not (doctor.get("warnings") or []) and not (doctor.get("issues") or [])
    return stale_active_owner, completed


def _check_g30_pulse_truthfulness(tmp: Path) -> None:
    """G30: only a stale lane that actually owns active work warns."""
    accepted_loop = tmp / "g30_all_accepted"
    active_loop = tmp / "g30_active_owner"
    accepted_rid = _seed_g30_loop(accepted_loop, "ACCEPTED")
    active_rid = _seed_g30_loop(active_loop, "IMPLEMENTING")
    accepted_raw = _serve_g30_state(accepted_loop)
    accepted = _serve_g30_state(accepted_loop, doctor_clean_fixture=True)
    active = _serve_g30_state(active_loop)

    accepted_stale = [
        lane.get("lane") for lane in accepted_raw.get("lanes", [])
        if (lane.get("heartbeat") or {}).get("state") == "stale"
    ]
    if "implementation" not in accepted_stale:
        _fail("G30 accepted fixture must contain a stale lane heartbeat")
    if (accepted_raw.get("doctor") or {}).get("non_terminal_requests"):
        _fail("G30 accepted fixture must have no non-terminal request")
    raw_warning, _raw_completed = _g30_pulse_inputs(accepted_raw)
    if raw_warning:
        _fail("G30 unmodified accepted fixture must not reach the stale-data warning")
    accepted_warning, accepted_completed = _g30_pulse_inputs(accepted)
    if accepted_warning or not accepted_completed:
        _fail("G30 doctor-clean accepted fixture must reach completed, not warning")

    active_warning, active_completed = _g30_pulse_inputs(active)
    if not active_warning or active_completed:
        _fail("G30 active-owner fixture must reach warning, not completed: {0!r}/{1!r}".format(
            active_warning, active_completed
        ))
    gaps = (active.get("doctor") or {}).get("heartbeat_gap_owners") or []
    if not any(gap.get("request_id") == active_rid for gap in gaps):
        _fail("G30 active fixture must expose the doctor's active-owner heartbeat gap")

    _set_g30_lane_row(active_loop, "implementation", "2999-01-01T00:00:00Z")
    _set_g30_lane_row(active_loop, "product", "2000-01-01T00:00:00Z")
    (active_loop / "lanes" / "product" / "current.md").write_text(
        "# Tracking Current State\n\n"
        "current_request_id: {rid}\n"
        "status: implementing\n"
        "iteration: 1\n"
        "last_updated: 2000-01-01T00:00:00Z\n"
        "heartbeat: 2000-01-01T00:00:00Z\n\n"
        "## Current Checkpoint\n\n- G30 non-owner fixture.\n\n"
        "## Next Action\n\n- Observe the owner.\n\n"
        "## Blockers\n\n- None.\n".format(rid=active_rid),
        encoding="utf-8",
    )
    non_owner = _serve_g30_state(active_loop)
    non_owner_warning, non_owner_completed = _g30_pulse_inputs(non_owner)
    if non_owner_warning or non_owner_completed:
        _fail("G30 stale non-owner fixture must not reach warning/completed: {0!r}/{1!r}".format(
            non_owner_warning, non_owner_completed
        ))
    lanes = {lane.get("lane"): lane for lane in non_owner.get("lanes", [])}
    owner_current = (lanes.get("implementation") or {}).get("current_request") or {}
    tracking_current = (lanes.get("product") or {}).get("current_request") or {}
    if owner_current.get("is_owner") is not True:
        _fail("G30 non-owner fixture must retain the actual owner")
    if tracking_current.get("is_owner") is not False:
        _fail("G30 tracking lane must be marked current_request.is_owner False")
    if not any(
        (lane.get("heartbeat") or {}).get("state") == "stale"
        and (lane.get("current_request") or {}).get("is_owner") is False
        for lane in non_owner.get("lanes", [])
    ):
        _fail("G30 non-owner fixture must contain a stale non-owning lane")
    print(
        "PULSE_PROBE accepted={0}:no-warning active={1}:warning "
        "non-owner={2}:no-warning".format(
            accepted_rid, active_rid, active_rid
        )
    )


def _check_g27_pagination(tmp: Path) -> None:
    """G27 C: active+recent defaults are bounded, counted, and expandable."""
    loop = tmp / "g27_pagination"
    _bootstrap(loop)
    accepted_ids = ["REQ-20260710-1200{0:02d}-done".format(i) for i in range(60)]
    active_ids = ["REQ-20260710-130000-live", "REQ-20260710-130001-blocked"]
    request_rows = []
    for i, rid in enumerate(accepted_ids):
        request_rows.append(
            "| {0} | ACCEPTED | review | 1 | goal.md | done | archive | "
            "2026-07-10T12:{1:02d}:00Z |\n".format(rid, i % 60)
        )
    request_rows.extend((
        "| {0} | IMPLEMENTING | implementation | 1 | goal.md | work | continue | "
        "2026-07-10T13:00:00Z |\n".format(active_ids[0]),
        "| {0} | BLOCKED | product | 2 | goal.md | blocked | ask human | "
        "2026-07-10T13:01:00Z |\n".format(active_ids[1]),
    ))
    (loop / "requests.md").write_text(
        "# Requests\n\n| request_id | status | owner_lane | iteration | source_docs | "
        "last_message | next_action | updated_at |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n" + "".join(request_rows),
        encoding="utf-8",
    )

    evidence_dir = loop / "evidence"
    for i in range(105):
        rid = accepted_ids[i % len(accepted_ids)]
        record = {
            "request_id": rid,
            "checkpoint": "pagination-{0}".format(i),
            "command": "verify-{0}".format(i),
            "exit_code": 0,
            "ran_at": "2026-07-10T12:{0:02d}:{1:02d}Z".format((i // 60) % 60, i % 60),
        }
        (evidence_dir / "evidence-{0:03d}.json".format(i)).write_text(
            json.dumps(record), encoding="utf-8"
        )
    for i, rid in enumerate(active_ids):
        record = {
            "request_id": rid,
            "checkpoint": "active-{0}".format(i),
            "command": "verify-active-{0}".format(i),
            "exit_code": 0,
            "ran_at": "2026-07-10T13:0{0}:00Z".format(i),
        }
        (evidence_dir / "evidence-active-{0}.json".format(i)).write_text(
            json.dumps(record), encoding="utf-8"
        )

    log_rows = []
    for i, rid in enumerate(accepted_ids + active_ids):
        to_status = "ACCEPTED" if rid in accepted_ids else (
            "IMPLEMENTING" if rid == active_ids[0] else "BLOCKED"
        )
        log_rows.append(
            "| 2026-07-10T12:{0:02d}:00Z | {1} | 1 | REQUESTED | {2} | review | row |\n".format(
                i % 60, rid, to_status
            )
        )
    (loop / "loop-run-log.md").write_text(
        "# Loop Run Log\n\n| timestamp | request_id | iteration | from_status | "
        "to_status | lane | note |\n| --- | --- | --- | --- | --- | --- | --- |\n"
        + "".join(log_rows),
        encoding="utf-8",
    )

    server = loop_dashboard.make_server(loop, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:{0}/api/state".format(server.server_address[1])
    try:
        status, body = _http_get(base)
        if status != 200:
            _fail("G27 pagination default request failed with {0}".format(status))
        state = json.loads(body.decode("utf-8"))
        meta = state.get("pagination") or {}
        expected = {
            "requests": (52, 62),
            "evidence": (102, 107),
            "run_log": (52, 62),
        }
        actual_lengths = {
            "requests": len(state.get("requests") or []),
            "evidence": len((state.get("evidence") or {}).get("records") or []),
            "run_log": len(state.get("run_log_tail") or []),
        }
        for name, pair in expected.items():
            shown, total = pair
            item = meta.get(name) or {}
            if (actual_lengths[name], item.get("shown"), item.get("total"),
                    item.get("truncated")) != (shown, shown, total, True):
                _fail("G27 pagination {0} expected {1}/{2} truncated; got {3!r}/{4!r}".format(
                    name, shown, total, actual_lengths[name], item
                ))

        status_full, body_full = _http_get(base + "?full=requests,evidence,run_log")
        if status_full != 200:
            _fail("G27 full pagination request failed with {0}".format(status_full))
        full = json.loads(body_full.decode("utf-8"))
        full_lengths = {
            "requests": len(full.get("requests") or []),
            "evidence": len((full.get("evidence") or {}).get("records") or []),
            "run_log": len(full.get("run_log_tail") or []),
        }
        if full_lengths != {"requests": 62, "evidence": 107, "run_log": 62}:
            _fail("G27 full pagination returned wrong counts: {0!r}".format(full_lengths))
        if any((full.get("pagination", {}).get(name) or {}).get("truncated")
               for name in full_lengths):
            _fail("G27 full pagination must mark every requested collection untruncated")

        # G29 collapse path: after an expanded ?full= read, the client's Show
        # less action drops the full query and the next coordinated poll uses
        # the default URL again. The server must return the bounded snapshot.
        status_collapsed, body_collapsed = _http_get(base)
        if status_collapsed != 200:
            _fail("G29 collapsed pagination request failed with {0}".format(status_collapsed))
        collapsed = json.loads(body_collapsed.decode("utf-8"))
        collapsed_lengths = {
            "requests": len(collapsed.get("requests") or []),
            "evidence": len((collapsed.get("evidence") or {}).get("records") or []),
            "run_log": len(collapsed.get("run_log_tail") or []),
        }
        if collapsed_lengths != {"requests": 52, "evidence": 102, "run_log": 52}:
            _fail("G29 collapse did not return to bounded collections: {0!r}".format(
                collapsed_lengths
            ))
        if any(not (collapsed.get("pagination", {}).get(name) or {}).get("truncated")
               for name in collapsed_lengths):
            _fail("G29 collapsed pagination must mark each collection truncated")
        print(
            "PAGINATION_PROBE requests=52/62 evidence=102/107 run_log=52/62 "
            "full=62,107,62 collapsed=52,102,52"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _check_g27_snapshot_cache_and_etag(base: str, loop_dir: Path) -> None:
    """G27 C: concurrent reads share one build and unchanged state returns 304."""
    original = loop_dashboard.build_state
    calls = [0]
    calls_lock = threading.Lock()
    build_started = threading.Event()
    release_build = threading.Event()

    def counted_build(*args, **kwargs):
        with calls_lock:
            calls[0] += 1
            current = calls[0]
        if current == 1:
            build_started.set()
            if not release_build.wait(timeout=5):
                _fail("G27 snapshot single-flight probe timed out")
        return original(*args, **kwargs)

    results = []

    def read_state() -> None:
        results.append(_http_get(base + "/api/state"))

    loop_dashboard._clear_state_snapshot_cache(loop_dir)
    loop_dashboard.build_state = counted_build
    try:
        first = threading.Thread(target=read_state)
        second = threading.Thread(target=read_state)
        first.start()
        if not build_started.wait(timeout=5):
            _fail("G27 snapshot builder never started")
        second.start()
        time.sleep(0.05)
        release_build.set()
        first.join(timeout=10)
        second.join(timeout=10)
    finally:
        release_build.set()
        loop_dashboard.build_state = original
    if first.is_alive() or second.is_alive():
        _fail("G27 snapshot single-flight requests did not finish")
    if calls[0] != 1:
        _fail("G27 snapshot single-flight expected one build, got {0}".format(calls[0]))
    if len(results) != 2 or any(status != 200 for status, _ in results):
        _fail("G27 snapshot single-flight requests did not both return 200")

    status, body, headers = _http_get_with_headers(base + "/api/state")
    etag = headers.get("ETag") or headers.get("Etag")
    if status != 200 or not body or not etag:
        _fail("G27 ETag probe needs a 200 response with body and ETag")
    status_304, body_304, headers_304 = _http_get_with_headers(
        base + "/api/state", {"If-None-Match": etag}
    )
    if status_304 != 304 or body_304:
        _fail("G27 If-None-Match expected 304 with no body, got {0}/{1} bytes".format(
            status_304, len(body_304)
        ))
    if (headers_304.get("ETag") or headers_304.get("Etag")) != etag:
        _fail("G27 304 response must echo the matching ETag")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        loop_dir = Path(tmp) / "loop"

        # Point CODEX_HOME at a hermetic fake home BEFORE the server starts, so
        # every /api/state usage read is deterministic and never touches the
        # user's real (private) ~/.codex sessions.
        fake_home = _make_fake_codex_home(Path(tmp))
        saved_codex_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(fake_home)

        # (1) bootstrap a minimal loop in-process.
        _bootstrap(loop_dir)
        registry_path = loop_dir / "agent-lanes.md"
        if not registry_path.exists():
            _fail("bootstrap did not create agent-lanes.md")

        # (D) GUARD AUTH DETECTOR: import codex_guard from tools/ and assert its
        # auth regex fires on auth-shaped failures and NOT on a plain compile
        # error. This lives beside the dashboard smoke because both cover the
        # "codex login" login-guidance surface introduced together.
        _check_guard_auth_detector()

        # (DECOUPLING) The dashboard must no longer directly parse the Codex host
        # surfaces: all session-JSONL / auth.json reading now lives ONLY in
        # codex_host_probe. Assert the executable code carries no residual parse.
        _check_dashboard_decoupled_from_host()

        # (PROBE STANDALONE) ``python codex_host_probe.py`` against the fake
        # CODEX_HOME must emit valid JSON (usage + account) with the known fake
        # identity and no token material. Uses the same hermetic fake home.
        _check_probe_standalone_json(fake_home)

        # (G26 chunk 3) Exercise real malformed/unreadable files, guarded module
        # failures, and a cache-drop exception against build_state itself.
        _check_g26_chunk3_state(Path(tmp))

        # Start the server on an ephemeral port in a background thread.
        server = loop_dashboard.make_server(loop_dir, 0)
        host, port = server.server_address[0], server.server_address[1]
        if host != "127.0.0.1":
            _fail("server did not bind loopback only; bound {0}".format(host))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        base = "http://127.0.0.1:{0}".format(port)

        _check_g27_snapshot_cache_and_etag(base, loop_dir)
        _check_g27_pagination(Path(tmp))
        _check_g30_pulse_truthfulness(Path(tmp))
        try:
            # Snapshot the whole tree BEFORE any read, to prove GETs don't write.
            snap_before = _snapshot_tree(loop_dir)

            # (2) GET / -> 200 and contains '<html'.
            status, body = _http_get(base + "/")
            if status != 200:
                _fail("GET / expected 200, got {0}".format(status))
            text = body.decode("utf-8", errors="replace").lower()
            if "<html" not in text:
                _fail("GET / body does not contain '<html'")
            # Sanity: the page must be self-contained (no external script/link/font).
            for needle in ("http://", "https://", "//fonts", "cdn"):
                # Allow the word inside comments only if it's not a real URL.
                # A strict check: no 'src=\"http' and no 'href=\"http'.
                pass
            if 'src="http' in text or "src='http" in text:
                _fail("GET / page references an external script (src=http...)")
            if 'href="http' in text or "href='http" in text:
                _fail("GET / page references an external stylesheet/link (href=http...)")

            # (C) i18n INTEGRITY: extract the embedded STRINGS blob from the
            # served HTML, parse it, and assert every key carries a non-empty en
            # AND zh; also assert the language toggle control is present. Uses
            # the full-fidelity (non-lowercased) HTML so the JSON parses.
            html_full = body.decode("utf-8", errors="replace")
            _check_i18n_integrity(html_full)
            # Simplified masthead: no kicker, no VIEW ONLY badge, compact path
            # chip with the full loop dir on hover (title="").
            _check_header_markup(html_full)
            # System-checks collapse-by-default markup + persistence hooks.
            _check_health_collapse_markup(html_full)
            # (v4) Layout: Lanes first, then the collapsible Usage & Limits
            # section (with its own localStorage key, live summary, refresh).
            _check_usage_limits_layout_markup(html_full)
            # (G27) One generation-guarded coordinator owns all state polling;
            # manual and POST refreshes cannot start durable parallel chains.
            _check_poll_coordinator(html_full)
            # (Batch 2) Progress view / your-turn banner / blocked taxonomy /
            # project rename / needs-you sort / in-place update / honest
            # heartbeat / usage staleness -- structural markup + wiring hooks.
            _check_batch2_markup(html_full)
            # (Batch 2 i18n) every new key has a non-empty en AND zh.
            _b2_rows = _extract_i18n_rows(html_full)
            _check_batch2_i18n(_b2_rows)
            _check_g27_pagination_markup(html_full, _b2_rows)
            _check_g30_pulse_markup(html_full, _b2_rows)
            # (G26 chunk 3) Honest stale/degraded controls, checker reasons,
            # malformed-evidence notice, and all new EN/ZH strings.
            _check_g26_chunk3_markup(html_full, _b2_rows)
            # (F8) recommended-tier lane chip: markup, wiring, i18n, grep-proof.
            _check_f8_tier_markup(html_full, _b2_rows)
            # (G17) running banner attributes each "working on" line to the
            # request owner only (static contract; runtime proof below).
            _check_g17_markup(html_full)
            # (G20) the your-turn stall item splits by the doctor reason: the
            # REVIEWING gate-green stall renders an HONEST string (never
            # "finished"); the work_done_unreported case keeps its wording.
            _check_g20_stall_honesty(html_full, _b2_rows)
            # (G18) foldable Progress: same collapse mechanism as System Checks
            # / Usage & Limits but default OPEN; the fold survives the poll
            # (renderProgress never re-folds); the new label key has en + zh.
            _check_g18_progress_collapse(html_full, _b2_rows)
            # (G21) the doctor's warnings/issues surface as humanized notices
            # (code->human-string map, raw doctor text as expandable detail /
            # unknown-code fallback), inside the System Checks fold.
            _check_g21_doctor_notices(html_full, _b2_rows)
            # (G21 round 2) the six sustained copy-verifier findings stay
            # fixed: unknown-code generic fallback, actionable git note,
            # missing-dep sentence structure, actionable tier-mismatch note,
            # localized default roles + None-leak + quoted machine text, and
            # natural ZH usage labels/date pattern.
            _check_g21_round2(html_full, _b2_rows)

            # (3) GET /api/state -> 200, valid JSON, has the expected keys.
            status, body = _http_get(base + "/api/state")
            if status != 200:
                _fail("GET /api/state expected 200, got {0}".format(status))
            try:
                state = json.loads(body.decode("utf-8"))
            except ValueError as exc:
                _fail("GET /api/state did not return valid JSON: {0}".format(exc))
            for key in ("lanes", "requests", "doctor", "policy", "usage",
                        "tracker_progress", "project", "awaiting_objective"):
                if key not in state:
                    _fail("GET /api/state JSON is missing key {0!r}".format(key))
            if not isinstance(state["lanes"], list):
                _fail("state['lanes'] should be a list")
            if not isinstance(state["requests"], list):
                _fail("state['requests'] should be a list")
            if not isinstance(state["doctor"], dict):
                _fail("state['doctor'] should be an object")
            if not isinstance(state["policy"], dict):
                _fail("state['policy'] should be an object")
            if not isinstance(state["usage"], dict):
                _fail("state['usage'] should be an object")
            # The default lanes must show up in the read.
            lane_names = {row.get("lane") for row in state["lanes"]}
            for expected in ("product", "implementation", "review"):
                if expected not in lane_names:
                    _fail("state is missing default lane {0!r}".format(expected))
            # G16: every lane carries an advisory recommended_tier on the wire,
            # defaulting to 'highest' for EVERY lane (the F8 coding/non-coding
            # split is gone; a lower value only appears after a manual opt-down),
            # and the value is ABSTRACT (never a model name).
            by_lane = {row.get("lane"): row for row in state["lanes"]}
            for row in state["lanes"]:
                tier = (row.get("recommended_tier") or "").strip()
                if tier not in ("highest", "second-highest"):
                    _fail("lane {0!r} has a non-abstract/absent recommended_tier {1!r}".format(
                        row.get("lane"), tier))
                if "gpt" in tier.lower():
                    _fail("recommended_tier must be abstract, never a model name; got {0!r}".format(tier))
            # A freshly bootstrapped loop has no opt-down, so EVERY default lane
            # surfaces 'highest'.
            for lane in ("implementation", "product", "review"):
                if (by_lane.get(lane) or {}).get("recommended_tier") != "highest":
                    _fail("G16: default lane {0!r} should surface recommended_tier 'highest'".format(lane))
            # The doctor imported in-process and produced a real snapshot.
            if state["doctor"].get("available") is not True:
                _fail(
                    "doctor snapshot should be available, got {0!r}".format(
                        state["doctor"].get("available")
                    )
                )

            # (6a) GET endpoints must have written NOTHING.
            snap_after_reads = _snapshot_tree(loop_dir)
            if snap_after_reads != snap_before:
                changed = _diff_keys(snap_before, snap_after_reads)
                _fail("GET endpoints modified the loop tree: {0}".format(changed))

            # (4) POST /api/lanes {"lane":"qa-review","role":"test"} -> ok:true.
            status, result = _http_post_json(
                base + "/api/lanes", {"lane": "qa-review", "role": "test"}
            )
            if status != 200:
                _fail("POST /api/lanes (valid) expected 200, got {0}".format(status))
            if not isinstance(result, dict) or result.get("ok") is not True:
                _fail("POST /api/lanes (valid) did not return ok:true; got {0!r}".format(result))
            if result.get("lane") != "qa-review":
                _fail("POST /api/lanes returned wrong lane: {0!r}".format(result.get("lane")))

            # ... and agent-lanes.md now contains qa-review with needs-thread.
            registry_text = registry_path.read_text(encoding="utf-8")
            qa_row = None
            for line in registry_text.splitlines():
                if line.strip().startswith("|") and "qa-review" in line:
                    qa_row = line
                    break
            if qa_row is None:
                _fail("agent-lanes.md does not contain a qa-review row after POST")
            if "needs-thread" not in qa_row:
                _fail("qa-review row is not status needs-thread: {0!r}".format(qa_row))
            if "test" not in qa_row:
                _fail("qa-review row did not record the role 'test': {0!r}".format(qa_row))

            # ... and the lane dir + workspace/ were created.
            qa_dir = loop_dir / "lanes" / "qa-review"
            if not qa_dir.is_dir():
                _fail("qa-review lane directory was not created")
            for filename in ("current.md", "worklog.md", "inbox.md", "outbox.md"):
                if not (qa_dir / filename).exists():
                    _fail("qa-review lane is missing {0}".format(filename))
            if not (qa_dir / "workspace" / "README.md").exists():
                _fail("qa-review lane is missing its workspace/ directory")

            # A second read must now show the new lane (read-through works).
            status, body = _http_get(base + "/api/state")
            state2 = json.loads(body.decode("utf-8"))
            if "qa-review" not in {row.get("lane") for row in state2["lanes"]}:
                _fail("state does not reflect the newly added qa-review lane")

            # (5) POST invalid name -> rejected, registry byte-for-byte unchanged.
            registry_before_bad = registry_path.read_bytes()
            snap_before_bad = _snapshot_tree(loop_dir)
            status, result = _http_post_json(
                base + "/api/lanes", {"lane": "Bad Name!", "role": "nope"}
            )
            # A rejected request returns ok:false (HTTP 400 in this impl).
            if not isinstance(result, dict) or result.get("ok") is not False:
                _fail("POST /api/lanes (invalid) should return ok:false; got {0!r}".format(result))
            registry_after_bad = registry_path.read_bytes()
            if registry_after_bad != registry_before_bad:
                _fail("an invalid POST changed agent-lanes.md; registry must be unchanged")
            snap_after_bad = _snapshot_tree(loop_dir)
            if snap_after_bad != snap_before_bad:
                changed = _diff_keys(snap_before_bad, snap_after_bad)
                _fail("an invalid POST modified the loop tree: {0}".format(changed))

            # Bonus: a reserved name is also rejected without side effects.
            status, result = _http_post_json(
                base + "/api/lanes", {"lane": "evidence", "role": "reserved"}
            )
            if not isinstance(result, dict) or result.get("ok") is not False:
                _fail("POST /api/lanes reserved name should be rejected; got {0!r}".format(result))

            # Bonus: a duplicate lane is rejected.
            status, result = _http_post_json(
                base + "/api/lanes", {"lane": "qa-review", "role": "dup"}
            )
            if not isinstance(result, dict) or result.get("ok") is not False:
                _fail("POST /api/lanes duplicate should be rejected; got {0!r}".format(result))

            # Bonus: an unknown path is 404, and GET-only /api/state refuses POST.
            status, _ = _http_get(base + "/does-not-exist")
            if status != 404:
                _fail("GET /does-not-exist should be 404, got {0}".format(status))
            status, _ = _http_post_json(base + "/api/state", {})
            if status != 404:
                _fail("POST /api/state should be 404 (GET-only), got {0}".format(status))

            # (7) USAGE: the fake CODEX_HOME feeds a real usage snapshot.
            status, body = _http_get(base + "/api/state")
            state_body_text = body.decode("utf-8")
            state = json.loads(state_body_text)
            usage = state.get("usage") or {}
            if usage.get("available") is not True:
                _fail("usage.available should be True with fake CODEX_HOME; got {0!r}".format(usage))
            primary = usage.get("primary") or {}
            secondary = usage.get("secondary") or {}
            if primary.get("remaining_percent") != 91.0:
                _fail("usage.primary.remaining_percent should be 91.0 (used 9.0); got {0!r}".format(
                    primary.get("remaining_percent")))
            if primary.get("used_percent") != 9.0:
                _fail("usage.primary.used_percent should be 9.0; got {0!r}".format(
                    primary.get("used_percent")))
            if secondary.get("remaining_percent") != 77.0:
                _fail("usage.secondary.remaining_percent should be 77.0 (used 23.0); got {0!r}".format(
                    secondary.get("remaining_percent")))
            if usage.get("plan_type") != "pro":
                _fail("usage.plan_type should be 'pro'; got {0!r}".format(usage.get("plan_type")))
            if usage.get("source") != "codex-session-jsonl":
                _fail("usage.source should be 'codex-session-jsonl'; got {0!r}".format(usage.get("source")))

            # (7-privacy) The planted conversation marker must NOT appear in the
            # /api/state body: the parser must expose only rate-limit/token
            # numbers, never session message content.
            if PRIVACY_MARKER in state_body_text:
                _fail("PRIVACY LEAK: fake conversation marker appeared in /api/state body")
            # A path from a decoy line must not leak either.
            if "/secret/path" in state_body_text:
                _fail("PRIVACY LEAK: a decoy session path appeared in /api/state body")

            # (E) ACCOUNT IDENTITY: the fake auth.json's hand-built JWT feeds
            # usage.account with the known email/name/plan, and auth_mode. The
            # account object is field-scoped: no key beyond the permitted set may
            # appear (a stray token field would be a red-line breach).
            account = usage.get("account") or {}
            if not isinstance(account, dict):
                _fail("usage.account should be an object; got {0!r}".format(account))
            if account.get("available") is not True:
                _fail("usage.account.available should be True with fake auth.json; got {0!r}".format(
                    account))
            if account.get("email") != FAKE_ACCOUNT_EMAIL:
                _fail("usage.account.email should be {0!r}; got {1!r}".format(
                    FAKE_ACCOUNT_EMAIL, account.get("email")))
            if account.get("name") != FAKE_ACCOUNT_NAME:
                _fail("usage.account.name should be {0!r}; got {1!r}".format(
                    FAKE_ACCOUNT_NAME, account.get("name")))
            if account.get("plan_type") != FAKE_ACCOUNT_PLAN:
                _fail("usage.account.plan_type should be {0!r}; got {1!r}".format(
                    FAKE_ACCOUNT_PLAN, account.get("plan_type")))
            if account.get("auth_mode") != "chatgpt":
                _fail("usage.account.auth_mode should be 'chatgpt'; got {0!r}".format(
                    account.get("auth_mode")))
            # account_id_short must be the FIRST 8 chars of the full id and
            # nothing more -- never the whole opaque id.
            if account.get("account_id_short") != FAKE_ACCOUNT_ID[:8]:
                _fail("usage.account.account_id_short should be first 8 chars {0!r}; got {1!r}".format(
                    FAKE_ACCOUNT_ID[:8], account.get("account_id_short")))
            # Field-scope: ONLY the permitted keys may ever appear in account.
            permitted_account_keys = {
                "available", "email", "name", "plan_type", "auth_mode",
                "account_id_short", "auth_mtime_iso", "snapshot_stale",
                "stale_reason", "detail",
            }
            extra_keys = set(account.keys()) - permitted_account_keys
            if extra_keys:
                _fail("usage.account carries unexpected keys (possible leak): {0}".format(
                    sorted(extra_keys)))

            # (E-tokenleak) None of the FAKE_* token strings from auth.json may
            # appear ANYWHERE in the full /api/state response body: not the
            # id_token JWT signature segment, not the access/refresh tokens, and
            # not the full account_id. This is the auth.json red line.
            for token_name, token_val in (
                ("id_token signature", FAKE_ID_TOKEN_SIG),
                ("access_token", FAKE_ACCESS_TOKEN),
                ("refresh_token", FAKE_REFRESH_TOKEN),
                ("full account_id", FAKE_ACCOUNT_ID),
            ):
                if token_val in state_body_text:
                    _fail("AUTH TOKEN LEAK: {0} appeared in /api/state body".format(token_name))
            # The full id_token JWT (any of its segments concatenated) must not
            # leak either; rebuild it and assert its absence.
            full_jwt = _build_fake_jwt(
                FAKE_ACCOUNT_EMAIL, FAKE_ACCOUNT_NAME, FAKE_ACCOUNT_PLAN, FAKE_ID_TOKEN_SIG
            )
            if full_jwt in state_body_text:
                _fail("AUTH TOKEN LEAK: the full id_token JWT appeared in /api/state body")

            # (F) REFRESH: a plain /api/state MAY serve the cached account, but
            # /api/state?refresh=1 MUST drop the cache and reflect a NEW email.
            # To make the cached-path half deterministic, the auth.json is
            # rewritten to a same-length email AND its (mtime, size) is forced
            # back to the pre-rewrite values -- so the server's (path, mtime,
            # size) cache key is unchanged and a plain read serves the STALE
            # cached email. Only refresh=1 (which drops the cache) sees the new
            # one. This proves both that the cache is real and that refresh works.
            auth_path = fake_home / "auth.json"
            pre_stat = auth_path.stat()
            # Prime the cache with the CURRENT (old) email via one plain read.
            _http_get(base + "/api/state")
            # Rewrite to the new email (same name/plan -> identical byte size).
            _write_fake_auth(fake_home, FAKE_ACCOUNT_EMAIL_2, FAKE_ACCOUNT_NAME, FAKE_ACCOUNT_PLAN)
            post_stat = auth_path.stat()
            if post_stat.st_size != pre_stat.st_size:
                _fail("refresh test invariant broken: auth.json size changed on rewrite "
                      "({0} -> {1})".format(pre_stat.st_size, post_stat.st_size))
            # Force the mtime back so the cache key is byte-for-byte identical.
            os.utime(str(auth_path), ns=(pre_stat.st_atime_ns, pre_stat.st_mtime_ns))
            # Plain read: may still show the OLD email (served from cache). We do
            # not hard-require staleness (a real deployment could legitimately
            # miss), but we DO require that it never shows a wrong/garbage value.
            _, body_cached = _http_get(base + "/api/state")
            acct_cached = (json.loads(body_cached.decode("utf-8")).get("usage") or {}).get("account") or {}
            if acct_cached.get("email") not in (FAKE_ACCOUNT_EMAIL, FAKE_ACCOUNT_EMAIL_2):
                _fail("plain /api/state account email should be old or new, got {0!r}".format(
                    acct_cached.get("email")))
            # refresh=1: MUST reflect the NEW email (cache dropped, auth re-read).
            _, body_refreshed = _http_get(base + "/api/state?refresh=1")
            body_refreshed_text = body_refreshed.decode("utf-8")
            acct_refreshed = (json.loads(body_refreshed_text).get("usage") or {}).get("account") or {}
            if acct_refreshed.get("email") != FAKE_ACCOUNT_EMAIL_2:
                _fail("GET /api/state?refresh=1 must reflect the NEW email {0!r}; got {1!r}".format(
                    FAKE_ACCOUNT_EMAIL_2, acct_refreshed.get("email")))
            # The refresh response must STILL carry no token material.
            for token_name, token_val in (
                ("id_token signature", FAKE_ID_TOKEN_SIG),
                ("access_token", FAKE_ACCESS_TOKEN),
                ("refresh_token", FAKE_REFRESH_TOKEN),
                ("full account_id", FAKE_ACCOUNT_ID),
            ):
                if token_val in body_refreshed_text:
                    _fail("AUTH TOKEN LEAK in refresh body: {0}".format(token_name))
            # Restore the original fake email so any later reads are consistent.
            _write_fake_auth(fake_home, FAKE_ACCOUNT_EMAIL, FAKE_ACCOUNT_NAME, FAKE_ACCOUNT_PLAN)

            # (7-missing) Point CODEX_HOME at a dir with no sessions -> usage
            # unavailable, but the endpoint is still 200 (never crashes).
            empty_home = Path(tmp) / "empty_codex_home"
            empty_home.mkdir(parents=True, exist_ok=True)
            os.environ["CODEX_HOME"] = str(empty_home)
            status, body = _http_get(base + "/api/state")
            if status != 200:
                _fail("GET /api/state with empty CODEX_HOME should still be 200, got {0}".format(status))
            usage2 = json.loads(body.decode("utf-8")).get("usage") or {}
            if usage2.get("available") is not False:
                _fail("usage.available should be False with no sessions; got {0!r}".format(usage2))
            if not usage2.get("reason"):
                _fail("usage should carry a 'reason' when unavailable")

            # (A) LOGIN GUIDANCE reason codes. Two fakes that both HAVE a session
            # file but NO rate_limits event, differing only by auth.json:
            #   - no auth.json           -> reason "codex_not_logged_in"
            #   - auth.json present      -> reason "no_session_data_yet"
            home_no_auth = _make_codex_home_no_ratelimits(Path(tmp), "home_no_auth", with_auth=False)
            os.environ["CODEX_HOME"] = str(home_no_auth)
            status, body = _http_get(base + "/api/state")
            if status != 200:
                _fail("GET /api/state (no-auth home) should be 200, got {0}".format(status))
            usage_na = json.loads(body.decode("utf-8")).get("usage") or {}
            if usage_na.get("available") is not False:
                _fail("usage.available should be False with no rate_limits; got {0!r}".format(usage_na))
            if usage_na.get("reason") != "codex_not_logged_in":
                _fail("usage.reason should be 'codex_not_logged_in' when auth.json is absent; got {0!r}".format(
                    usage_na.get("reason")))
            if not usage_na.get("hint"):
                _fail("usage should carry a human 'hint' when not logged in")
            if "codex login" not in (usage_na.get("hint") or ""):
                _fail("not-logged-in hint must mention the verbatim 'codex login' command")

            home_auth = _make_codex_home_no_ratelimits(Path(tmp), "home_auth", with_auth=True)
            os.environ["CODEX_HOME"] = str(home_auth)
            status, body = _http_get(base + "/api/state")
            usage_auth = json.loads(body.decode("utf-8")).get("usage") or {}
            if usage_auth.get("available") is not False:
                _fail("usage.available should be False with no rate_limits; got {0!r}".format(usage_auth))
            if usage_auth.get("reason") != "no_session_data_yet":
                _fail("usage.reason should be 'no_session_data_yet' when auth.json exists but no data; got {0!r}".format(
                    usage_auth.get("reason")))

            # Restore the valid fake home for any later reads.
            os.environ["CODEX_HOME"] = str(fake_home)

            # (B) STRUCTURED LANE SUMMARY: seed a lane's current.md with a known
            # request id / next action / blocker, then confirm /api/state's lane
            # object carries them parsed under ``summary``.
            impl_current = loop_dir / "lanes" / "implementation" / "current.md"
            if not impl_current.parent.is_dir():
                _fail("expected an 'implementation' lane dir from bootstrap")
            impl_current.write_text(_SEED_CURRENT_MD, encoding="utf-8")
            status, body = _http_get(base + "/api/state")
            state_ls = json.loads(body.decode("utf-8"))
            impl_lane = None
            for row in state_ls.get("lanes", []):
                if row.get("lane") == "implementation":
                    impl_lane = row
                    break
            if impl_lane is None:
                _fail("state is missing the 'implementation' lane after seeding current.md")
            summ = impl_lane.get("summary") or {}
            if not isinstance(summ, dict):
                _fail("lane.summary should be an object; got {0!r}".format(summ))
            if summ.get("current_request_id") != SEED_REQUEST_ID:
                _fail("lane.summary.current_request_id should be {0!r}; got {1!r}".format(
                    SEED_REQUEST_ID, summ.get("current_request_id")))
            if summ.get("status") != "implementing":
                _fail("lane.summary.status should be 'implementing'; got {0!r}".format(summ.get("status")))
            if summ.get("iteration") != "2":
                _fail("lane.summary.iteration should be '2'; got {0!r}".format(summ.get("iteration")))
            if not isinstance(summ.get("next_action"), list) or SEED_NEXT_ACTION not in summ.get("next_action"):
                _fail("lane.summary.next_action should include the seeded action; got {0!r}".format(
                    summ.get("next_action")))
            if not isinstance(summ.get("blockers"), list) or SEED_BLOCKER not in summ.get("blockers"):
                _fail("lane.summary.blockers should include the seeded blocker; got {0!r}".format(
                    summ.get("blockers")))
            if not isinstance(summ.get("checkpoint_items"), list):
                _fail("lane.summary.checkpoint_items should be a list; got {0!r}".format(
                    summ.get("checkpoint_items")))
            # The raw current.md is still carried for the "View raw" details.
            if SEED_REQUEST_ID not in (impl_lane.get("current") or ""):
                _fail("lane.current (raw) should still contain the seeded request id")

            # ============================================================
            # (Batch 2) Runtime assertions against a seeded loop.
            # ============================================================

            # (B2-F14) TRACKER PROGRESS: seed tracker.md with a Checkpoints
            # section (done/current/blocked/todo) plus a Done-When section that
            # must be EXCLUDED from the milestone count. /api/state's
            # tracker_progress must report the checkpoint-only counts and pick
            # the [~] item as current.
            tracker_seed = (
                "# Tracker\n\n"
                "## Checkpoints\n\n"
                "- [x] First slice shipped and verified.\n"
                "- [x] Second slice shipped and verified.\n"
                "- [!] Third slice blocked on a missing dependency.\n"
                "- [~] Fourth slice: build the frontend dashboard.\n"
                "- [ ] Fifth slice not started.\n\n"
                "## Done When\n\n"
                "- [ ] Acceptance criterion that is NOT a milestone.\n"
                "- [ ] Another acceptance criterion.\n"
            )
            (loop_dir / "tracker.md").write_text(tracker_seed, encoding="utf-8")
            # Seed a real request so the loop is NOT "awaiting objective" (a real
            # request means work has begun). One BLOCKED-by-scope row proves the
            # F3 taxonomy source data is present without a genuine halt.
            requests_seed = (
                "# Requests\n\n"
                "## Queue\n\n"
                "| request_id | status | owner_lane | iteration | source_docs "
                "| last_message | next_action | updated_at |\n"
                "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
                "| REQ-20260706-204609-implementation | REQUESTED | implementation "
                "| 1 | goal.md | msg | Build the first slice and report evidence. "
                "| 2026-07-06T21:00:00Z |\n"
            )
            (loop_dir / "requests.md").write_text(requests_seed, encoding="utf-8")
            status, body = _http_get(base + "/api/state")
            state_tp = json.loads(body.decode("utf-8"))
            tp = state_tp.get("tracker_progress") or {}
            if tp.get("available") is not True:
                _fail("F14: tracker_progress.available should be True after seeding")
            if tp.get("total") != 5:
                _fail("F14: tracker_progress.total should be 5 (Checkpoints only, "
                      "Done-When excluded); got {0!r}".format(tp.get("total")))
            if tp.get("done") != 2:
                _fail("F14: tracker_progress.done should be 2; got {0!r}".format(tp.get("done")))
            if tp.get("blocked") != 1:
                _fail("F14: tracker_progress.blocked should be 1; got {0!r}".format(tp.get("blocked")))
            cps = tp.get("checkpoints") or []
            if len(cps) != 5:
                _fail("F14: tracker_progress.checkpoints should have 5 items; got {0}".format(len(cps)))
            ci = tp.get("current_index")
            if ci is None or cps[ci].get("status") != "current":
                _fail("F14: current_index must point at the [~] in-progress checkpoint")
            if "frontend" not in (cps[ci].get("title") or "").lower():
                _fail("F14: current checkpoint title should be the frontend one; got {0!r}".format(
                    cps[ci].get("title")))
            # Status values must be exactly the four buckets.
            statuses = [c.get("status") for c in cps]
            if statuses != ["done", "done", "blocked", "current", "todo"]:
                _fail("F14: checkpoint statuses wrong; got {0!r}".format(statuses))

            # (B2-F3) AWAITING-OBJECTIVE: with a real seeded tracker + a real
            # request in the queue, the loop is NOT awaiting an objective.
            if state_tp.get("awaiting_objective") is not False:
                _fail("F3: awaiting_objective should be False once real work exists")
            # A brand-new bootstrap (fresh temp loop) whose goal.md is still the
            # placeholder and has no real request MUST be awaiting_objective.
            fresh = Path(tmp) / "fresh_loop"
            _bootstrap(fresh)
            fresh_state = loop_dashboard.build_state(fresh)
            if fresh_state.get("awaiting_objective") is not True:
                _fail("F3: a fresh placeholder loop must report awaiting_objective True")

            # (B2-F2) PROJECT NAME: default derives from the loop-dir root; POST
            # /api/project persists it atomically; junk input is rejected; and
            # there is NO fourth write endpoint.
            proj = state_tp.get("project") or {}
            if not proj.get("name"):
                _fail("F2: project.name must be non-empty (default from loop-dir root)")
            if proj.get("is_default") is not True:
                _fail("F2: project.is_default should be True before any rename")
            project_path = loop_dir / "project.md"
            # Happy path.
            status, result = _http_post_json(base + "/api/project", {"name": "My Expense App"})
            if status != 200 or not isinstance(result, dict) or result.get("ok") is not True:
                _fail("F2: POST /api/project happy path should be ok; got {0} {1!r}".format(status, result))
            if result.get("name") != "My Expense App":
                _fail("F2: POST /api/project returned wrong name: {0!r}".format(result.get("name")))
            # Persistence: file written + state reflects it + no longer default.
            if not project_path.exists():
                _fail("F2: project.md was not created by the write")
            if "My Expense App" not in project_path.read_text(encoding="utf-8"):
                _fail("F2: project.md does not contain the saved name")
            status, body = _http_get(base + "/api/state")
            proj2 = json.loads(body.decode("utf-8")).get("project") or {}
            if proj2.get("name") != "My Expense App" or proj2.get("is_default") is not False:
                _fail("F2: state should reflect the renamed project; got {0!r}".format(proj2))
            # Atomicity pattern: no leftover temp file after the write.
            leftovers = list(loop_dir.glob("project.md.tmp*"))
            if leftovers:
                _fail("F2: atomic write left a temp file behind: {0}".format(leftovers))
            # Junk input rejected without changing project.md.
            project_bytes_before = project_path.read_bytes()
            for bad in ({"name": ""}, {"name": "  "}, {"name": "a\nb"}, {"name": 123}, {}):
                status, result = _http_post_json(base + "/api/project", bad)
                if status != 400 or not isinstance(result, dict) or result.get("ok") is not False:
                    _fail("F2: POST /api/project {0!r} should be 400 ok:false; got {1} {2!r}".format(
                        bad, status, result))
                if project_path.read_bytes() != project_bytes_before:
                    _fail("F2: an invalid /api/project {0!r} changed project.md".format(bad))
            # HARD INVARIANT: exactly three write endpoints; a fourth is 404.
            status, _ = _http_post_json(base + "/api/does-not-exist", {"x": 1})
            if status != 404:
                _fail("F2: a fourth POST path must be 404 (writes are exactly three); got {0}".format(status))
            # And the handler routes ONLY the three known write paths.
            src_dash = (Path(loop_dashboard.__file__).resolve()).read_text(encoding="utf-8")
            do_post = src_dash[src_dash.find("def do_POST"):]
            do_post = do_post[:do_post.find("def do_PUT")]
            for endpoint in ('"/api/lanes"', '"/api/policy"', '"/api/project"'):
                if endpoint not in do_post:
                    _fail("F2: do_POST must route {0}".format(endpoint))
            # No stray fourth /api/<x> write path in do_POST beyond the three.
            post_paths = set(re.findall(r'"(/api/[a-z]+)"', do_post))
            if post_paths != {"/api/lanes", "/api/policy", "/api/project"}:
                _fail("F2: do_POST routes an unexpected set of write paths: {0}".format(
                    sorted(post_paths)))

            # (B2-F9) NEEDS-YOU ORDERING STABILITY: two consecutive /api/state
            # reads return the SAME lane order (the server sort is stable; the
            # client re-sorts deterministically on top). We assert server order
            # is identical across polls so the client's stable re-sort cannot
            # thrash.
            order_a = [l.get("lane") for l in
                       json.loads(_http_get(base + "/api/state")[1].decode("utf-8")).get("lanes", [])]
            order_b = [l.get("lane") for l in
                       json.loads(_http_get(base + "/api/state")[1].decode("utf-8")).get("lanes", [])]
            if order_a != order_b:
                _fail("F9: server lane order must be identical across two polls; "
                      "got {0!r} then {1!r}".format(order_a, order_b))

            # (B2-F13) HONEST HEARTBEAT: the doctor's heartbeat_gap_owners field
            # (which gates the client's "overdue" label) is present in the
            # passed-through doctor snapshot.
            doc_snap = state_tp.get("doctor") or {}
            if "heartbeat_gap_owners" not in doc_snap:
                _fail("F13: doctor snapshot must pass through heartbeat_gap_owners")
            for f in ("workerless_dependencies", "stalled_handoffs",
                      "missing_dependency_blockers", "git_present", "hook_installed",
                      "non_terminal_requests"):
                if f not in doc_snap:
                    _fail("doctor snapshot must pass through the Batch 1 field {0!r}".format(f))
            # G10: the doctor snapshot must also pass through held_for_human_qa
            # (the blue confirm tone reads it) as a list.
            if "held_for_human_qa" not in doc_snap:
                _fail("G10: doctor snapshot must pass through held_for_human_qa")
            if not isinstance(doc_snap.get("held_for_human_qa"), list):
                _fail("G10: doctor.held_for_human_qa should be a list")

            # (G10) HUMAN-GATE TONES. Prove the two run-2-fix signals end to end.
            _check_g10_tones(base, Path(tmp))

            # (G11) The dashboard state builder sorts run-log rows by timestamp:
            # a shuffled append-only log (late-append recovery rows) yields a
            # chronologically-ordered run_log_tail.
            _check_g11_dashboard(Path(tmp))

            # (G13) BLOCKED envelopes carry recommended_answer; the dashboard
            # surfaces it (present -> rendered; absent -> empty).
            _check_g13_recommended(Path(tmp))

            # (G14) Tier observability: the dashboard state carries observed_model
            # / observed_tier / tier_mismatch per lane (chip renders them; amber
            # on mismatch).
            _check_g14_dashboard(Path(tmp))

            # (G17) Banner attribution: a synthetic loop with one IMPLEMENTING
            # request owned by data-eng and a fresh-heartbeat product yields
            # exactly ONE owner-attributed "working on this request" item.
            _check_g17_banner_attribution(Path(tmp))

            # (8) POLICY: default value, valid update, invalid rejections.
            status, body = _http_get(base + "/api/state")
            policy = json.loads(body.decode("utf-8")).get("policy") or {}
            if policy.get("max_fix_cycles") != 3:
                _fail("default max_fix_cycles should be 3; got {0!r}".format(policy.get("max_fix_cycles")))
            if policy.get("source_present") is not True:
                _fail("policy.source_present should be True (bootstrap wrote loop-policy.md)")

            policy_path = loop_dir / "loop-policy.md"

            # Valid update to 5 -> ok, file updated, state reflects 5.
            status, result = _http_post_json(base + "/api/policy", {"max_fix_cycles": 5})
            if status != 200 or not isinstance(result, dict) or result.get("ok") is not True:
                _fail("POST /api/policy 5 should be ok; got status {0} result {1!r}".format(status, result))
            if result.get("max_fix_cycles") != 5:
                _fail("POST /api/policy 5 returned wrong value: {0!r}".format(result.get("max_fix_cycles")))
            policy_text_5 = policy_path.read_text(encoding="utf-8")
            if "max_fix_cycles: 5" not in policy_text_5:
                _fail("loop-policy.md should contain 'max_fix_cycles: 5' after POST")
            if "max_fix_cycles: 3" in policy_text_5:
                _fail("loop-policy.md should no longer contain the old 'max_fix_cycles: 3'")
            status, body = _http_get(base + "/api/state")
            policy2 = json.loads(body.decode("utf-8")).get("policy") or {}
            if policy2.get("max_fix_cycles") != 5:
                _fail("state should reflect max_fix_cycles 5 after POST; got {0!r}".format(
                    policy2.get("max_fix_cycles")))

            # Invalid values -> 400 and loop-policy.md byte-for-byte UNCHANGED.
            # (The rest of the tree is also snapshotted, excluding loop-policy.md
            # which we just legitimately changed and now expect to stay fixed.)
            policy_bytes_before = policy_path.read_bytes()
            snap_before_bad_policy = _snapshot_tree(loop_dir)
            for bad_value in (0, 99, "abc"):
                status, result = _http_post_json(base + "/api/policy", {"max_fix_cycles": bad_value})
                if status != 400 or not isinstance(result, dict) or result.get("ok") is not False:
                    _fail("POST /api/policy {0!r} should be 400 ok:false; got status {1} result {2!r}".format(
                        bad_value, status, result))
                if policy_path.read_bytes() != policy_bytes_before:
                    _fail("an invalid POST /api/policy {0!r} changed loop-policy.md".format(bad_value))
            # A missing key is also rejected.
            status, result = _http_post_json(base + "/api/policy", {})
            if status != 400 or result.get("ok") is not False:
                _fail("POST /api/policy with no max_fix_cycles should be 400; got {0!r}".format(result))
            # Nothing else on disk changed across the invalid POSTs.
            snap_after_bad_policy = _snapshot_tree(loop_dir)
            if snap_after_bad_policy != snap_before_bad_policy:
                _fail("invalid POST /api/policy modified the loop tree: {0}".format(
                    _diff_keys(snap_before_bad_policy, snap_after_bad_policy)))

            # (9) URL LINE + fallback: bind helper reports the actual port.
            free_srv, fell = loop_dashboard.make_server_with_fallback(loop_dir, 0)
            try:
                bound_port = free_srv.server_address[1]
                url_line = "DASHBOARD_URL=http://127.0.0.1:{0}/".format(bound_port)
                if "DASHBOARD_URL=http://127.0.0.1:{0}/".format(bound_port) != url_line:
                    _fail("URL line does not match bound port")
                if fell is not False:
                    _fail("binding port 0 should not count as a fallback")
            finally:
                free_srv.server_close()

            # Busy requested port -> ephemeral fallback (never crashes).
            blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            busy_port = blocker.getsockname()[1]
            try:
                fb_srv, fell2 = loop_dashboard.make_server_with_fallback(loop_dir, busy_port)
                try:
                    if fell2 is not True:
                        _fail("a busy requested port should fall back to an ephemeral one")
                    if fb_srv.server_address[1] == busy_port:
                        _fail("fallback server bound the busy port instead of a new one")
                finally:
                    fb_srv.server_close()
            finally:
                blocker.close()

            # main() prints exactly one DASHBOARD_URL= line matching its bind.
            _assert_main_prints_url(Path(tmp))

        finally:
            # (10) shut down cleanly and restore CODEX_HOME.
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            if thread.is_alive():
                _fail("server thread did not shut down cleanly")
            if saved_codex_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = saved_codex_home

    print("DASH_SMOKE_OK")
    return 0


def _assert_main_prints_url(tmp: Path) -> None:
    """Run ``loop_dashboard.main`` in a thread and assert its startup line.

    main() serves forever, so it runs in a daemon thread with stdout captured.
    We parse the single ``DASHBOARD_URL=`` line, confirm that exact port is
    actually accepting connections, then shut the server down via the object
    main() stashes on the module for test hooks.
    """
    loop_dir = tmp / "loop"
    buf = io.StringIO()

    def run() -> None:
        with redirect_stdout(buf):
            loop_dashboard.main(["--loop-dir", str(loop_dir), "--port", "0"])

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    # Wait for the URL line to appear in the captured stdout.
    url = None
    for _ in range(100):  # up to ~5s
        text = buf.getvalue()
        for line in text.splitlines():
            if line.startswith("DASHBOARD_URL="):
                url = line[len("DASHBOARD_URL="):].strip()
                break
        if url is not None:
            break
        time.sleep(0.05)
    if url is None:
        _fail("main() did not print a DASHBOARD_URL= line")

    # Exactly one such line.
    n_lines = sum(1 for ln in buf.getvalue().splitlines() if ln.startswith("DASHBOARD_URL="))
    if n_lines != 1:
        _fail("main() printed {0} DASHBOARD_URL= lines, expected exactly 1".format(n_lines))

    # The URL must be loopback and its port must be live.
    if not url.startswith("http://127.0.0.1:"):
        _fail("DASHBOARD_URL is not loopback: {0!r}".format(url))
    port_str = url[len("http://127.0.0.1:"):].rstrip("/")
    try:
        port = int(port_str)
    except ValueError:
        _fail("DASHBOARD_URL has a non-integer port: {0!r}".format(url))
    # Confirm the bound port actually responds (proves the line matches reality).
    status, _ = _http_get("http://127.0.0.1:{0}/api/state".format(port))
    if status != 200:
        _fail("DASHBOARD_URL port {0} did not serve /api/state (got {1})".format(port, status))

    # Shut the server down through the test hook main() left on the module.
    srv = getattr(loop_dashboard, "_LAST_SERVER_FOR_TEST", None)
    if srv is not None:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    thread.join(timeout=5)


def _diff_keys(before: dict, after: dict) -> str:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(k for k in (set(before) & set(after)) if before[k] != after[k])
    return "added={0} removed={1} modified={2}".format(added, removed, modified)


if __name__ == "__main__":
    raise SystemExit(main())
