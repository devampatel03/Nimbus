#!/usr/bin/env bash
# Append a git/PR snapshot to docs/pivot/reports/checkpoints.log.
# Safe to run from a hook (Stop / PreCompact) or manually. Never prints secrets.
set -u
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT" || exit 0
LOG="docs/pivot/reports/checkpoints.log"
mkdir -p "$(dirname "$LOG")"
{
  echo "=== checkpoint $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  echo "branch: $(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  echo "head:   $(git log -1 --oneline 2>/dev/null)"
  echo "-- status (short) --"
  git status --short 2>/dev/null | head -50
  echo "-- worktrees --"
  git worktree list 2>/dev/null
  if command -v gh >/dev/null 2>&1; then
    echo "-- open PRs --"
    gh pr list --state open --limit 20 2>/dev/null || echo "(gh unavailable)"
  fi
  echo "-- STATE header --"
  sed -n '1,20p' docs/pivot/STATE.md 2>/dev/null
  echo
} >> "$LOG"
echo "checkpoint appended to $LOG"
