# Project: <name>

<one line: language, framework, ORM, deploy target>

## Build
<the exact commands — e.g. npm run build && npm test>

## Project identity
- Framework: <e.g. Next.js 15 + TypeScript>
- Package manager: <pnpm | npm | uv | poetry>
- Architecture: <one line>

## Constraints (each from a real failure)

NEVER use `git push --force`. Use `--force-with-lease`.
# 2026-03-12: force push overwrote a teammate's commits on feature/auth

Run `npm test` before ANY git commit.
# 2026-02-28: broken import shipped to main, CI caught it 20 min later

Schema migrations: always generate with `drizzle-kit generate`.
# 2026-03-05: hand-written migration missed NOT NULL, broke staging

API routes: validate input with zod schemas, never trust req.body.
# 2026-03-18: unvalidated input caused 500s for 2 hours

## What NOT to do
- Don't refactor without atomic commits
- Don't add dependencies without checking bundle impact
- Don't commit .env files