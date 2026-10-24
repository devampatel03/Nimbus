# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Shared column vocabulary for the pivot schema (P1.2c).

Provenance (R7/R8): every derived row carries `lane` (trusted|untrusted),
`confidence` in [0,1], a `created_at`, and a source event. Untrusted rows are
never auto-promoted: `promoted` may only be true for an untrusted row if a
`promoted_approval_id` is recorded (CHECK `ck_*_untrusted_promotion`).
"""

from sqlalchemy import CheckConstraint

LANES = ("trusted", "untrusted")
LANE_CHECK_SQL = "lane IN ('trusted', 'untrusted')"
CONFIDENCE_CHECK_SQL = "confidence BETWEEN 0 AND 1"
PROMOTION_CHECK_SQL = "lane = 'trusted' OR promoted = false OR promoted_approval_id IS NOT NULL"


def lane_check(table: str) -> CheckConstraint:
    return CheckConstraint(LANE_CHECK_SQL, name=f"ck_{table}_lane")


def confidence_check(table: str) -> CheckConstraint:
    return CheckConstraint(CONFIDENCE_CHECK_SQL, name=f"ck_{table}_confidence")


def promotion_check(table: str) -> CheckConstraint:
    return CheckConstraint(PROMOTION_CHECK_SQL, name=f"ck_{table}_untrusted_promotion")
