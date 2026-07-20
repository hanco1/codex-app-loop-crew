# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Gate holes closed: the doctor now blocks auto-chaining when a request is
  BLOCKED or the run log is missing, tables in state files are parsed against
  their required headers (a preface table can no longer shadow the real one),
  and `max_fix_cycles` is read consistently in both list-marker and plain form.
  The pre-commit scope guard fails closed when the lease table is unreadable
  or malformed.
- Concurrency: shared state files (lane registry, decision log, inboxes) are
  now updated under a cross-process file lock with atomic replacement, so
  concurrent writers no longer lose each other's changes.
- Dashboard server hardening: stricter request handling and shared,
  drift-proof parsing between the dashboard and the doctor; terminal request
  states (including abandoned requests) are no longer shown as running.
- `deliver_message.py` contract: message metadata (type, iteration, sender)
  is required via flags or the message body's envelope, preventing silent
  message-id collisions and skipped heartbeats; archiving requires a request
  id; all documented protocol message types are recognized.
- Installers: both installers honor `CODEX_HOME`, probe for a Python 3.9+
  launcher before copying, and refuse a skills directory that points into the
  repository's own `skills/` folder (self-delete guard).
- Core state readers (completion gate, host probe, bootstrap registry, decision
  log, message bodies) report non-UTF-8 corruption with clear errors instead of
  tracebacks; lane and request-id inputs are validated against path traversal.
- Unreadable state files are surfaced, not hidden: the doctor reports them as
  visible issues instead of crashing, and dashboard lane cards now say when a
  lane's `current.md` is unreadable instead of rendering it like an empty one.
- The generated pre-commit hook shell-quotes the paths it embeds and is written
  atomically, so repositories at unusual paths and concurrent commits are safe.

### Changed

- Documentation now describes human involvement honestly (you are needed
  whenever a gate asks, not at a single moment), documents install-path
  precedence and the Python 3.9+ requirement, and points recovery at the
  canonical `inbox/new/` pending surface.

## [1.0.0] - 2026-07-07

### Added

- Initial public release: multi-agent loop orchestrator skill for Codex with
  bootstrap, message delivery, decision recording, completion gate, pre-commit
  scope guard, loop doctor, and a local dashboard; installers for POSIX and
  Windows; English and Simplified Chinese documentation.
