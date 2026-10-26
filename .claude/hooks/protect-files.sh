#!/bin/bash
# .claude/hooks/protect-files.sh
# PreToolUse (matcher: Write|Edit). Belt-and-suspenders alongside the
# permissions.deny block in settings.json — catches paths a glob might miss.
#
# LIMIT (accepted in ADR-0015): this only guards the Write/Edit tools. A Bash
# redirect, `python -c`, `sed -i`, etc. can still write these files; the real
# backstop is CODEOWNERS + branch protection (human) and the sha256 pin test.

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$FILE_PATH" ] && exit 0

if echo "$FILE_PATH" | grep -qE '(^|/)(\.env(\.[a-zA-Z0-9_]+)?$|.*secrets?.*|.*credentials?.*|id_rsa$|.*\.pem$)'; then
  echo "BLOCKED: $FILE_PATH looks like a secrets/credentials file. Ask the human to edit it directly." >&2
  exit 2
fi

# R9 / ADR-0015: the character card and its human-approval pin are human-only.
# Normalise first so case, separators, ./ .. // , trailing dots/spaces, NTFS
# alternate data streams (::$DATA) and 8.3 short names cannot dodge the match.
NORM=$(printf '%s' "$FILE_PATH" | tr 'A-Z' 'a-z' | tr '\\' '/' | sed -E \
  -e 's#^[a-z]:##' \
  -e 's#:[^/]*##g' \
  -e 's#/+#/#g')
# resolve "/./" and "x/../" until stable
while :; do
  NEXT=$(printf '%s' "$NORM" | sed -E -e 's#/\./#/#g' -e 's#^\./##' -e 's#/\.$##' \
    -e 's#(^|/)([^/]|[^/.][^/]|\.[^/.]|[^/]{3,})/\.\.(/|$)#\1#')
  [ "$NEXT" = "$NORM" ] && break
  NORM=$NEXT
done
NORM=$(printf '%s' "$NORM" | sed -E -e 's#[. ]+(/|$)#\1#g')

# Second, deliberately broad net: any path naming both the personality dir and the
# card/pin (covers odd segments like "..." that Windows resolves textually).
if printf '%s' "$NORM" | grep -qE '(^|/)(personality|person~[0-9]+)/(character_card\.(md|sha256)|charac~[0-9]+)'   || printf '%s' "$NORM" | grep -qE '(personality|person~[0-9]+).*(character_card\.(md|sha256)|charac~[0-9]+)'; then
  echo "BLOCKED: $FILE_PATH is the human-authored character card / approval pin (R9, HUMAN_TODO H1). Ask the human to edit it directly." >&2
  exit 2
fi

exit 0
