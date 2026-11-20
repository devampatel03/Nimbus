# 0007 — Gmail "drafts only" cannot be enforced by OAuth scope; enforce in code and disclose

- Status: **Proposed. Needs a human decision (open question Q2).** This is a gap in the Brief's R6 assumption.

## Context
R6 says "Gmail: drafts only. No `gmail.send` scope requested. Ever." Verified 2026-10-24:
- Google's scope list describes `gmail.compose` as "Manage drafts and send emails". `users.drafts.create` and `users.drafts.send` both accept `gmail.compose`, `gmail.modify` and `https://mail.google.com/`.
- **Gmail has no drafts-only scope.** Sources: [Google OAuth scopes](https://developers.google.com/identity/protocols/oauth2/scopes), [Zenn write-up](https://zenn.dev/alma65/articles/gmail-draft-only-scope?locale=en).

So any token that can create a draft can also send one. The literal R6 (no `gmail.send` scope) can be met, but its intent (the system *cannot* send) cannot be met by scope.

## Decision (proposed default)
- Request `gmail.readonly` + `gmail.compose`. Never request `gmail.send`, `gmail.modify` or `mail.google.com`. The scope-lint test asserts this exact set.
- No send method exists in `src/integrations/google/gmail_api.py`. An AST/registry test proves it.
- `net` denies the request paths `*/messages/send`, `*/drafts/send`, `*/messages/import` and `*/messages/insert` for `Service.GMAIL`, regardless of caller. A test covers this.
- Draft creation is WRITE_EXTERNAL, so it needs approval (policy). The draft is visible in Gmail before any human sends it.
- SECURITY.md and the bot's `/privacy` reply state it plainly: "the Gmail token technically permits sending; Nimbus has no code path that sends, and its network layer blocks the send endpoints."

## Consequences
- R6's intent is met by deterministic code at two layers, not by Google. Residual risk: an attacker who achieves code execution inside the container and steals the token.
- `quality/thresholds.yaml: gmail_send_scope_present: false` is still a true and testable statement.

## Alternatives
- **`gmail.readonly` only, no drafts.** The model writes draft text into Telegram for the user to copy-paste. This is the strictest option and drops UAT 6. **Recommended if the human considers the residual risk unacceptable.**
- **Google Workspace add-on scope (`gmail.addons.current.action.compose`).** Rejected: it only works inside an add-on UI session.
- **Notion or Drive as the draft destination.** Possible, but changes the UX the Brief describes.
