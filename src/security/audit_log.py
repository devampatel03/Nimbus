# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Local, user-inspectable egress audit log (SCR-4).

Every call to `scrubber.execute_external` writes one line here before it
sends anything, and a second line once the response (or failure) is
known. Format is JSON Lines so a user can read it with any text tool or
`jq` without special tooling.

Entries only ever contain the *already-scrubbed* payload (post entity
substitution, post PII-detector pass) or, for a blocked attempt, a
redacted summary — never the raw pre-scrub params, so the audit log
itself can't become a leak vector.
"""

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

AUDIT_LOG_PATH = Path("data/audit/egress.jsonl")

_write_lock = threading.Lock()

Status = Literal["sent", "response_received", "blocked_pii", "blocked_schema", "error"]


@dataclass(frozen=True)
class AuditLogEntry:
    session_id: str
    tool: str
    action: str
    status: Status
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    payload: dict[str, Any] | None = None  # only ever the scrubbed payload
    response_summary: str | None = None
    blocked_categories: tuple[str, ...] | None = None


def write_entry(entry: AuditLogEntry, path: Path = AUDIT_LOG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(entry), sort_keys=True)
    with _write_lock, path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_entries(path: Path = AUDIT_LOG_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
