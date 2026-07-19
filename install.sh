#!/usr/bin/env bash
[ -n "${BASH_VERSION:-}" ] || { echo "Please run with bash: bash install.sh" >&2; exit 1; }
# Install the codex-agent-loop-orchestrator skill into the local Codex skills dir.
#
# Copies the skill folder that ships beside this script into
# ~/.codex/skills/codex-agent-loop-orchestrator, overwriting any previous
# install. Idempotent: re-running refreshes the installed copy so it never
# lags the source.
set -euo pipefail

SKILL_NAME="codex-agent-loop-orchestrator"
# Precedence: CODEX_SKILLS_DIR > CODEX_HOME/skills > ~/.codex/skills.
SKILLS_DIR="${CODEX_SKILLS_DIR:-${CODEX_HOME:-$HOME/.codex}/skills}"

# Probe for a Python >= 3.9 launcher (the skill's scripts require it). This is
# a warning, not a gate: the copy still proceeds so docs-only hosts install fine.
PYTHON_LAUNCHER=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 &&
     "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    PYTHON_LAUNCHER="$cand"
    break
  fi
done
if [ -n "$PYTHON_LAUNCHER" ]; then
  echo "Found Python launcher: $PYTHON_LAUNCHER"
else
  echo "warning: no Python 3.9+ launcher found (tried: python3, python)." >&2
  echo "warning: the skill's scripts require Python 3.9+; installing the files anyway." >&2
fi

# Resolve the directory this script lives in (repo root).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# Locate the skill source folder. The skill ships at skills/<name>/ (plugin
# layout: the repo root is the plugin root); fall back to the legacy nested
# plugin/skills/ layout, a flat layout, or running from inside the skill folder.
if [ -d "$SCRIPT_DIR/skills/$SKILL_NAME" ]; then
  SOURCE="$SCRIPT_DIR/skills/$SKILL_NAME"
elif [ -d "$SCRIPT_DIR/plugin/skills/$SKILL_NAME" ]; then
  SOURCE="$SCRIPT_DIR/plugin/skills/$SKILL_NAME"
elif [ -d "$SCRIPT_DIR/$SKILL_NAME" ]; then
  SOURCE="$SCRIPT_DIR/$SKILL_NAME"
elif [ -f "$SCRIPT_DIR/SKILL.md" ]; then
  # Allow running the installer from inside the skill folder itself.
  SOURCE="$SCRIPT_DIR"
else
  echo "error: cannot find skill source folder '$SKILL_NAME' under skills/, plugin/skills/, or next to install.sh (looked in $SCRIPT_DIR)" >&2
  exit 1
fi

TARGET="$SKILLS_DIR/$SKILL_NAME"

mkdir -p "$SKILLS_DIR"

# Self-delete guard: refuse when the install target resolves to (or overlaps)
# the skill source, or the "rm -rf" below would destroy the source itself.
# Portable realpath: macOS lacks readlink -f, so resolve via cd + pwd -P.
resolve_dir() { (cd "$1" 2>/dev/null && pwd -P); }
SOURCE_REAL="$(resolve_dir "$SOURCE")"
SKILLS_REAL="$(resolve_dir "$SKILLS_DIR")"
if [ -z "$SOURCE_REAL" ] || [ -z "$SKILLS_REAL" ]; then
  echo "error: cannot resolve source ($SOURCE) or skills dir ($SKILLS_DIR) to a real path" >&2
  exit 1
fi
TARGET_REAL="$SKILLS_REAL/$SKILL_NAME"
if [ -d "$TARGET_REAL" ]; then
  TARGET_REAL="$(resolve_dir "$TARGET_REAL")"
fi
CMP_SOURCE="$SOURCE_REAL"
CMP_TARGET="$TARGET_REAL"
case "$(uname -s 2>/dev/null || true)" in
  MINGW*|MSYS*|CYGWIN*)
    # Windows filesystems are case-insensitive; compare lowercased.
    CMP_SOURCE="$(printf '%s' "$SOURCE_REAL" | tr '[:upper:]' '[:lower:]')"
    CMP_TARGET="$(printf '%s' "$TARGET_REAL" | tr '[:upper:]' '[:lower:]')"
    ;;
esac
case "$CMP_TARGET/" in
  "$CMP_SOURCE/"*)
    echo "error: refusing to install: the skills dir cannot point into the repo's own skills/ folder (target $TARGET_REAL is the skill source $SOURCE_REAL or inside it)" >&2
    exit 1
    ;;
esac
case "$CMP_SOURCE/" in
  "$CMP_TARGET/"*)
    echo "error: refusing to install: the skills dir cannot point into the repo's own skills/ folder (skill source $SOURCE_REAL is inside target $TARGET_REAL, so installing would delete it)" >&2
    exit 1
    ;;
esac

# Remove any previous install so stale files never linger, then copy fresh.
rm -rf "$TARGET"
mkdir -p "$TARGET"
cp -R "$SOURCE/." "$TARGET/"

echo "Installed $SKILL_NAME -> $TARGET"
