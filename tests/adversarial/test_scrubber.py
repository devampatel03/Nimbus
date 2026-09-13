# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Adversarial: 50 payloads carrying PII across every required category
(person names, emails, phone numbers, specific locations, company names,
per the task instruction) must all be blocked by execute_external before
any network call is attempted. Assert 0 pass (SCR-7).
"""

import uuid

import pytest

from src.contracts.tool_intent import RiskClass, ToolIntent
from src.security import kill_switch, scrubber

EMAIL_PAYLOADS = [
    "contact john.doe@example.com about the invoice",
    "reach me at devam.patel@gmail.com tomorrow",
    "cc priya.k@company.co.in on the thread",
    "her email is sarah_jones99@outlook.com",
    "send the doc to a.singh@nimbus.ai",
    "billing@acme-corp.com needs a response",
    "use support+urgent@service.io for escalation",
    "our contact is r.mehta@startup.dev",
    "loop in finance.team@bigcorp.net",
    "email raj.kumar@example.org the agenda",
]

PHONE_PAYLOADS = [
    "call me at 415-555-0132",
    "his number is (415) 555-0199",
    "reach her on +1 650 555 0147",
    "phone: 9876543210",
    "dial +91 98765 43210 for support",
    "text 212.555.0166 when you arrive",
    "office line is 800-555-0110",
    "mobile +44 7700 900123",
    "call 6505550123 before noon",
    "contact number +1-212-555-0184",
]

LOCATION_PAYLOADS = [
    "meet at 123 Main Street tomorrow",
    "the office is at 445 Park Avenue",
    "deliver to 78 Baker Road, suite 4",
    "package sent to 90210 for review",
    "shipping to zip 94105 this week",
    "conference is in New York this year",
    "she relocated to San Francisco last month",
    "the site visit is in Bangalore next week",
    "flight lands in London on Friday",
    "warehouse is at 22 Industrial Lane",
]

PERSON_NAME_PAYLOADS = [
    "schedule a follow-up with Rohit Sharma",
    "meeting notes from John Smith review",
    "loop in Priya Nair on this",
    "assigned to Sarah Johnson for Q3",
    "call scheduled with David Miller",
    "approved by Ananya Gupta yesterday",
    "notes shared with Michael Chen",
    "handoff to Emily Davis this afternoon",
    "escalate to Arjun Mehta if delayed",
    "reviewed by Laura Wilson last night",
]

COMPANY_PAYLOADS = [
    "renew the contract with Acme Corp",
    "deal signed with Initech LLC",
    "quote from Globex Corporation",
    "invoice from Umbrella Inc",
    "partnership with Google this quarter",
    "vendor is Stark Industries Ltd",
    "MOU with Wayne Enterprises",
    "supplier agreement with Hooli Co",
    "renewed license with Microsoft",
    "integration with Salesforce planned",
]

ALL_PII_PAYLOADS = (
    EMAIL_PAYLOADS + PHONE_PAYLOADS + LOCATION_PAYLOADS + PERSON_NAME_PAYLOADS + COMPANY_PAYLOADS
)

assert len(ALL_PII_PAYLOADS) == 50


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    kill_switch.disengage()
    yield
    kill_switch.disengage()


@pytest.mark.parametrize("free_text", ALL_PII_PAYLOADS)
async def test_pii_payload_is_blocked(free_text: str, monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("PII payload must be blocked before any network call")

    monkeypatch.setattr("httpx.AsyncClient.post", _fail_if_called)

    intent = ToolIntent(
        tool="calendar",
        action="create",
        params={"notes": free_text},
        entity_refs={},
        risk_class=RiskClass.WRITE_REVERSIBLE,
    )
    with pytest.raises(scrubber.PIIDetectedError):
        await scrubber.execute_external(intent, session_id=f"sess-{uuid.uuid4()}")


async def test_all_fifty_payloads_zero_pass():
    """SCR-7 acceptance criterion stated directly: 0 of 50 pass."""
    blocked = 0
    for free_text in ALL_PII_PAYLOADS:
        intent = ToolIntent(
            tool="calendar",
            action="create",
            params={"notes": free_text},
            entity_refs={},
            risk_class=RiskClass.WRITE_REVERSIBLE,
        )
        try:
            await scrubber.execute_external(intent, session_id=f"sess-{uuid.uuid4()}")
        except scrubber.PIIDetectedError:
            blocked += 1
    assert blocked == len(ALL_PII_PAYLOADS) == 50
