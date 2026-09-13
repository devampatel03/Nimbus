# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Named-entity / PII leak detector for outbound payloads (SCR-7).

Runs synchronously inside `scrubber.execute_external`, on the fully
serialised, post-substitution payload, before it ever touches the
network. A hard fail (any finding) blocks the send.

No NLP/ML dependency is wired into Nimbus yet (see MEMORY.md AD-7, AD-11
for the same constraint elsewhere), and this check must be deterministic,
dependency-light, and fast enough to run inline on every egress call — so
detection here is regex/heuristic, not a model. Because a false negative
here is a privacy leak and a false positive only blocks one tool call,
every category is deliberately tuned toward high recall over precision.
"""

import re
from dataclasses import dataclass
from typing import Any

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}\b")

# Phone numbers: a permissive candidate regex (any digit/separator run),
# filtered by digit count in `_looks_like_phone`. This deliberately covers
# both US-style (3-3-4) and international groupings (e.g. UK's 4-3-3),
# matching the "high recall" posture above — a false positive here just
# blocks one tool call; a false negative is a privacy leak.
_PHONE_CANDIDATE_RE = re.compile(r"(?<!\w)\+?\(?\d[\d().\-\s]{5,}\d(?!\w)")

# ISO 8601 date/datetime fragments (e.g. calendar params like
# "2026-09-15" or the date part of "2026-09-15T15:00:00+05:30") look like
# phone candidates under the loose regex above but aren't PII — excluded
# explicitly rather than dropping the separators the phone regex needs.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _looks_like_phone(candidate: str) -> bool:
    if _ISO_DATE_RE.match(candidate.strip()):
        return False
    digits = re.sub(r"\D", "", candidate)
    return 7 <= len(digits) <= 15


STREET_ADDRESS_RE = re.compile(
    r"\b\d{1,5}\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,3}\s+"
    r"(Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|"
    r"Court|Ct|Place|Pl|Way|Circle|Cir|Terrace|Ter|Square|Sq)\b\.?",
    re.IGNORECASE,
)

ZIP_CODE_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")

# Two or three consecutive Capitalized Words: catches person names AND
# multi-word place names (e.g. "New Delhi") — both count as PII here.
PROPER_NAME_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}\b")

COMPANY_SUFFIX_RE = re.compile(
    r"\b[A-Z][\w&.,'-]*(?:\s+[A-Z][\w&.,'-]*){0,3}\s+"
    r"(Inc|LLC|Ltd|Corp|Corporation|Co|GmbH|plc|LLP|Pvt\.?\s*Ltd)\.?\b"
)

# A short, curated list of well-known place/company names that wouldn't
# otherwise trip the structural regexes above (single-word or lowercase-suffix
# cases). Not exhaustive — defense-in-depth, not the primary mechanism.
KNOWN_LOCATIONS = {
    "mumbai",
    "delhi",
    "bangalore",
    "bengaluru",
    "hyderabad",
    "chennai",
    "kolkata",
    "pune",
    "london",
    "paris",
    "tokyo",
    "singapore",
    "dubai",
    "new york",
    "san francisco",
    "los angeles",
    "chicago",
    "seattle",
    "berlin",
    "toronto",
    "sydney",
    "beijing",
    "shanghai",
}
KNOWN_COMPANIES = {
    "google",
    "microsoft",
    "amazon",
    "apple",
    "meta",
    "openai",
    "anthropic",
    "netflix",
    "tesla",
    "uber",
    "airbnb",
    "salesforce",
}


@dataclass(frozen=True)
class PIIFinding:
    category: str  # "email" | "phone" | "person_name" | "location" | "company"
    field_path: str
    snippet: str


def detect_pii(text: str, field_path: str = "") -> list[PIIFinding]:
    """Scan a single string for PII. Returns every finding (not short-circuit)."""
    findings: list[PIIFinding] = []

    for m in EMAIL_RE.finditer(text):
        findings.append(PIIFinding("email", field_path, m.group()))
    for m in _PHONE_CANDIDATE_RE.finditer(text):
        if _looks_like_phone(m.group()):
            findings.append(PIIFinding("phone", field_path, m.group()))
    for m in STREET_ADDRESS_RE.finditer(text):
        findings.append(PIIFinding("location", field_path, m.group()))
    for m in ZIP_CODE_RE.finditer(text):
        findings.append(PIIFinding("location", field_path, m.group()))
    for m in COMPANY_SUFFIX_RE.finditer(text):
        findings.append(PIIFinding("company", field_path, m.group()))
    for m in PROPER_NAME_RE.finditer(text):
        findings.append(PIIFinding("person_name", field_path, m.group()))

    lowered = text.lower()
    for loc in KNOWN_LOCATIONS:
        if loc in lowered:
            findings.append(PIIFinding("location", field_path, loc))
    for company in KNOWN_COMPANIES:
        if company in lowered:
            findings.append(PIIFinding("company", field_path, company))

    return findings


def scan_payload(payload: Any, _path: str = "$") -> list[PIIFinding]:
    """Recursively scan every string leaf of a JSON-like payload."""
    findings: list[PIIFinding] = []
    if isinstance(payload, str):
        findings.extend(detect_pii(payload, _path))
    elif isinstance(payload, dict):
        for key, value in payload.items():
            findings.extend(scan_payload(value, f"{_path}.{key}"))
    elif isinstance(payload, (list, tuple)):
        for i, value in enumerate(payload):
            findings.extend(scan_payload(value, f"{_path}[{i}]"))
    return findings
