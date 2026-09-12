#!/bin/bash
# .claude/hooks/observability.sh
# PostToolUse (matcher: "*"). Never blocks — always exits 0.
# Reads the hook JSON from stdin (Claude Code does NOT set $TOOL/$TOKENS
# as environment variables; that's a common copy-paste bug).

INPUT=$(cat)
LOG_DIR=".claude/logs"
LOG="$LOG_DIR/decisions.log"
mkdir -p "$LOG_DIR"

TOOL=$(echo "$INPUT"      | jq -r '.tool_name // "unknown"')
SESSION=$(echo "$INPUT"   | jq -r '.session_id // "unknown"')
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // .tool_input.command // empty' | tr '\n' ' ' | head -c 120)

# PostToolUse payloads vary by tool; treat a reported error as FAILED.
if echo "$INPUT" | jq -e '.tool_response.error // .tool_response.is_error // empty' >/dev/null 2>&1; then
  STATUS="FAILED"
else
  STATUS="ok"
fi

printf '%s | session=%s | tool=%-10s | status=%-6s | %s\n' \
  "$(date '+%Y-%m-%d %H:%M:%S')" "$SESSION" "$TOOL" "$STATUS" "$FILE_PATH" >> "$LOG"

# Error-rate anomaly: 5+ failures in this session
FAILS=$(grep -c "session=$SESSION.*FAILED" "$LOG" 2>/dev/null || echo 0)
if [ "$FAILS" -ge 5 ] && [ ! -f "$LOG_DIR/.$SESSION.errflag" ]; then
  touch "$LOG_DIR/.$SESSION.errflag"
  echo "{\"additionalContext\": \"$FAILS tool calls have failed this session. Stop and reconsider the approach rather than retrying — repeated failures usually mean a wrong assumption, not a flaky tool.\"}"
fi

exit 0