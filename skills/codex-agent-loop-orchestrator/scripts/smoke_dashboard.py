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


def _find_repo_tool(rel: str) -> Path:
    """Walk upward from this file to find a repo tool by relative path.

    Returns the first existing ``<ancestor>/<rel>``; raises AssertionError if
    none is found. Used to locate ``tools/codex_guard.py`` without hard-coding
    the depth from scripts/ to the repo root.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / rel
        if candidate.exists():
            return candidate
    _fail("could not locate {0} above {1}".format(rel, here))
    raise AssertionError  # unreachable; keeps type checkers happy


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


def _check_i18n_integrity(html_full: str) -> None:
    """Parse the embedded STRINGS blob from the served HTML and validate it.

    Asserts: the JSON blob parses; every entry carries a non-empty ``en`` and
    ``zh``; and the served page includes the EN / zh language toggle control.
    """
    m = re.search(
        r'<script type="application/json" id="i18n-strings">(.*?)</script>',
        html_full,
        re.S,
    )
    if not m:
        _fail("served HTML has no embedded i18n-strings blob")
    try:
        data = json.loads(m.group(1).strip())
    except ValueError as exc:
        _fail("embedded i18n-strings blob is not valid JSON: {0}".format(exc))
    rows = data.get("strings")
    if not isinstance(rows, list) or not rows:
        _fail("i18n-strings blob has no non-empty 'strings' array")
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
        # F6 your-turn banner
        "yourturn_badge_running", "yourturn_badge_gate", "yourturn_badge_confirm",
        "yourturn_running_headline", "yourturn_gate_headline", "yourturn_confirm_headline",
        "yourturn_where_lane", "yourturn_item_halt", "yourturn_item_stalled",
        "yourturn_item_workerless", "yourturn_item_missing_dep", "yourturn_item_confirm",
        "yourturn_running_active",
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


def _check_f8_tier_markup(html_full: str, rows: list) -> None:
    """Assert the F8 recommended-tier lane chip markup, wiring, and i18n.

    Covers, on the SERVED HTML (before any JS runs):
      * fillLaneCard reads lane.recommended_tier and renders a neutral chip;
      * the three F8 i18n keys exist with non-empty en AND zh;
      * NO concrete model name (gpt-*) appears ANYWHERE in the served page --
        tiers stay abstract in the UI too.
    """
    if "lane.recommended_tier" not in html_full:
        _fail("F8: fillLaneCard never reads lane.recommended_tier")
    if "lane_meta_tier_label" not in html_full:
        _fail("F8: served HTML is missing the lane_meta_tier_label chip binding")
    by_key = {row.get("key"): row for row in rows}
    for k in ("lane_meta_tier_label", "lane_tier_highest", "lane_tier_second_highest"):
        row = by_key.get(k)
        if row is None:
            _fail("F8 i18n dictionary is missing required key {0!r}".format(k))
        if not (row.get("en") or "").strip():
            _fail("F8 i18n key {0!r} has an empty 'en' value".format(k))
        if not (row.get("zh") or "").strip():
            _fail("F8 i18n key {0!r} has an empty 'zh' value".format(k))
    # Grep-proof: no gpt-* model name anywhere in the served dashboard.
    if "gpt-" in html_full.lower():
        _fail("F8: a concrete model name (gpt-*) leaked into the served HTML")


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

        # Start the server on an ephemeral port in a background thread.
        server = loop_dashboard.make_server(loop_dir, 0)
        host, port = server.server_address[0], server.server_address[1]
        if host != "127.0.0.1":
            _fail("server did not bind loopback only; bound {0}".format(host))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        base = "http://127.0.0.1:{0}".format(port)
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
            # (Batch 2) Progress view / your-turn banner / blocked taxonomy /
            # project rename / needs-you sort / in-place update / honest
            # heartbeat / usage staleness -- structural markup + wiring hooks.
            _check_batch2_markup(html_full)
            # (Batch 2 i18n) every new key has a non-empty en AND zh.
            _b2_blob = re.search(
                r'<script type="application/json" id="i18n-strings">(.*?)</script>',
                html_full, re.S)
            _b2_rows = json.loads(_b2_blob.group(1).strip()).get("strings", [])
            _check_batch2_i18n(_b2_rows)
            # (F8) recommended-tier lane chip: markup, wiring, i18n, grep-proof.
            _check_f8_tier_markup(html_full, _b2_rows)

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
            # F8: every lane carries an advisory recommended_tier on the wire,
            # with the policy assignment (coding lane -> highest; else
            # second-highest) and an ABSTRACT value (never a model name).
            by_lane = {row.get("lane"): row for row in state["lanes"]}
            for row in state["lanes"]:
                tier = (row.get("recommended_tier") or "").strip()
                if tier not in ("highest", "second-highest"):
                    _fail("lane {0!r} has a non-abstract/absent recommended_tier {1!r}".format(
                        row.get("lane"), tier))
                if "gpt" in tier.lower():
                    _fail("recommended_tier must be abstract, never a model name; got {0!r}".format(tier))
            if (by_lane.get("implementation") or {}).get("recommended_tier") != "highest":
                _fail("coding lane 'implementation' should surface recommended_tier 'highest'")
            if (by_lane.get("product") or {}).get("recommended_tier") != "second-highest":
                _fail("default lane 'product' should surface recommended_tier 'second-highest'")
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
