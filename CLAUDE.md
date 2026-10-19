# auth-service — Claude Code context

Phone-number signup/login with SMS codes, devices, and the tokens every other service trusts.
Part of the chat system in GitHub org `jzviagin-chat-app`; the **system design lives in chat-infra** and is
kept in `.context/chat-infra/` by the SessionStart hook (`.claude/hooks/load-design.sh`).

## System design (shared, loaded from chat-infra)
@.context/chat-infra/docs/ARCHITECTURE.md
@.context/chat-infra/docs/PLAN.md

## This service (architecture, API, data model, security rules)
@README.md

## Working with the owner
- The owner is learning Python (coming from Node.js): explain the why, with Node equivalents and trade-offs.
  **Don't guess** facts or UI; say when unsure. One step at a time; do only what was asked; explain a step
  before building it.
- `main` is protected (PRs only, admins included): work on a branch, open a PR; the owner merges.
- Commands for the owner: root-anchored (`cd "$(git rev-parse --show-toplevel)"`) and say which machine.

## Rules for this repo
- Async everywhere: inside `async def` only async libraries (SQLAlchemy async/asyncpg, httpx, redis.asyncio);
  blocking or CPU-heavy work via `asyncio.to_thread`. Layers: `api/` → `services/` → `db/`, never upwards.
- Before every commit run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
  (database tests need `TEST_DATABASE_URL`, see README).
- Schema changes: edit `db/models.py`, then `uv run alembic revision --autogenerate -m "..." --rev-id 000N`,
  review the generated file. Never edit a migration that is already deployed. Migrations must work with the
  previous app version still running (rolling deploys).
- Never log OTP codes, tokens or full phone numbers. Secrets only from env vars (Kubernetes Secrets).
- Deploys: merging to `main` builds the image and opens a "Deploy auth-service" PR in chat-infra.
  Changes to `k8s/auth.yaml` (env vars, init containers) are chat-infra PRs.

## Keeping the design current
If a change affects another service, a contract (tokens, claims, JWKS, API conventions), a database, the infra
or a decision in PLAN.md, update `.context/chat-infra/docs/ARCHITECTURE.md` / `PLAN.md` on a branch there
(with a change-log row) and tell the owner how to push it. Run `/sync-design` before finishing a task.
