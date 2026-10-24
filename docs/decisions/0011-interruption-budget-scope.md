# 0011 — User-requested reminders are outside the interruption budget and quiet hours

- Status: Proposed default. The human may override (open question Q5).

## Context
R11: "≤ 6 proactive interruptions/day, with quiet hours; reminders delivered within 60 s of due." Read literally, a user-set 06:30 alarm during quiet hours, or a 7th reminder, would be deferred, which breaks the 60 s promise.

## Decision
- `JobSpec.proactive` decides the treatment.
- **Not proactive** (`proactive=False`): reminders and tasks the user explicitly set, approval prompts, and spend/backup/error alerts. These are always delivered on time and do not count against the budget or quiet hours.
- **Proactive** (`proactive=True`): morning brief, check-ins, commitment nudges, weekly review and any system-initiated suggestion. These count toward ≤ 6/day (IST calendar day), respect quiet hours (default 22:30–07:30 IST, configurable) and support snooze.
- When over budget, proactive items fold into the next allowed digest instead of being dropped.

## Consequences
- The `proactive_interruptions_per_day_max: 6` gate measures only `proactive=True` sends.
- If the user creates very many reminders, they still all fire, because the user asked for them.

## Alternatives
- **Everything counts.** Rejected: it breaks the R11 delivery-time clause.
- **Reminders exempt from the budget but not from quiet hours.** Possible. It would surprise a user who deliberately set an early reminder.
