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

# Remove any previous install so stale files never linger, then copy fresh.
rm -rf "$TARGET"
mkdir -p "$TARGET"
cp -R "$SOURCE/." "$TARGET/"

echo "Installed $SKILL_NAME -> $TARGET"
