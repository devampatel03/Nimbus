# HUMAN TODO (things only you can do)

Agents build everything that can be built without these (using FakeLLM, recorded fixtures, staging stubs). Tick items (`[x]`) and the orchestrator unblocks the tasks that depend on them on its next loop. Never paste secrets into chat or commit them — put them in `.env` (git-ignored) on the machine/VM.

| # | Item | Blocks | Needed by wave | Done |
|---|------|--------|---------------|------|
| H1 | Write the **character card** (`personality/character_card.md` (the path the code loads; ADR-0015)): who the assistant is — voice, values, quirks, what it will push back on. Hand-authored; add the line `human-authored: true` at the top. | P2.6 (real card), P5.3 evals meaning | W2 | [ ] |
| H2 | Create Telegram bot via @BotFather → `TELEGRAM_BOT_TOKEN` (prod). Create a **second** bot for staging → `TELEGRAM_BOT_TOKEN_STAGING`. Find your numeric user id (e.g. via @userinfobot) → `TELEGRAM_ALLOWED_USER_ID`. Enable Telegram 2-step verification + a passcode lock. | P2.1 live test, P6.5 | W2 / W6 | [ ] |
| H3 | Model provider account with **billing enabled** (paid tier; not AI Studio/free tier) → `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`. For the bake-off, 2–3 providers' keys (e.g. Gemini paid, Anthropic Haiku-class, OpenAI nano/mini-class). Set a provider-side budget alert. | P5.4, P5.5 | W5 | [ ] |
| H4 | Google Cloud project + OAuth consent screen set to **In production** (unverified; personal use; you'll click through the warning once) → `GOOGLE_CLIENT_ID/SECRET`. Scopes: Calendar, Gmail **compose/readonly only (no send)**, Drive readonly/file. Do the one-time consent when the agent asks. | P3.4–P3.8 live | W3 | [ ] |
| H5 | Notion internal integration token → `NOTION_TOKEN`; share the specific pages/DBs with the integration. | P3.7 live | W3 | [ ] |
| H6 | Oracle Cloud account → create Always Free Ampere A1 instance (check actual shape in console: likely 2 OCPU/12 GB), Ubuntu arm64, add your SSH public key, note the IP/region. Capacity errors are common — retry later or try another AD. | P6.x live deploy | W6 | [ ] |
| H7 | Off-VM backup target decision (e.g. a Drive folder or second free object store) + credentials. | P6.3 live | W6 | [ ] |
| H8 | Uptime monitor (free, e.g. a healthchecks-style ping URL) → `HEALTHCHECK_PING_URL`. | P6.4 | W6 | [ ] |
| H9 | Run UAT items marked **[human]** in `reports/UAT.md` (dictation quality, Pixel Recorder share, real calendar). | P6.6 | W6 | [ ] |
| H10 | Review & merge the final PR `pivot` → `main`. | P7.4 | W7 | [ ] |
| H11 | Confirm: your real repo path & `gh auth status` OK; GitHub repo has Actions enabled. | P1.1 | W1 | [ ] |

## Added by agents during the run
_(append below: id, what, why, which task it blocks, interim workaround)_
- **H12** (W0, orchestrator): `.claude/settings.json` in the working tree was overwritten by the pivot kit's template (original hooks pre-tool-guard/protect-files/enforce-boundary/graphify removed; broad allow list added). I left it uncommitted and untouched; the original is in git HEAD. Decide: merge by hand, or `git checkout .claude/settings.json` to restore. Blocks nothing. Also: `docs/EXECUTION_PLAN.md` has 1015 uncommitted changed lines not made by me, left untouched.
- **H13** (W0): envcheck shows all optional credentials unset (see H2-H8); nothing blocks W0-W2 build with fakes.
- **H14** (W0, architect Q1): ONE batch approval to edit protected `tests/eval/**` (CLAUDE.md rule 4): INV-1 rewrite, delete INV-2/INV-4, rename INV-5 -> R7 provenance + new INV-5/6/7, LoCoMo harness port off Redis/Qdrant, replace `red_team.py`. Blocks PROT tasks: P1.2c, P1.3a/b, P1.4c, P1.5, P2.4, P2.5b, P3.0, P3.1, P4.7, P5.3. Workaround: all non-PROT tasks proceed.
- **H15** (W0): start Docker Desktop (daemon was down; 26 integration/INV-5 tests error without it). Needed before P1.2b and for verifying any Redis/Qdrant removal.
- **H16** (architect Q2, ADR-0007): Gmail has no drafts-only scope; `gmail.compose` also permits sending. Default: request compose, ship no send code, block send endpoints in `src/net`, disclose. Or readonly-only (loses drafts / UAT 6). Brief R6 wording ("no send scope") is stricter than what Google offers - your call. Blocks P3.4, P3.6.
- **H17** (Q3/Q4/Q5, defaults applied unless you object): egress = app-level hostname allowlist + nftables port rules (no squid); Drive scope `drive.readonly`; user-set reminders bypass quiet hours/6-per-day cap.
