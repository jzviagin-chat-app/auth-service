# auth-service

Sign-up and login with a phone number and an SMS code. It issues the tokens that every other
service (chat-service first) uses to know **who** is calling and **from which device**.

> **Status: skeleton.** The structure, tooling, logging, database connection, migrations setup,
> tests and CI are in place. Only the health checks answer; feature endpoints are added step by step
> (see [Build plan](#13-build-plan)).

---

## Contents
1. [Where it fits](#1-where-it-fits)
2. [Technology choices](#2-technology-choices)
3. [Async: how it works here and the rules](#3-async-how-it-works-here-and-the-rules)
4. [Project layout](#4-project-layout)
5. [Layers and how a request flows](#5-layers-and-how-a-request-flows)
6. [App lifecycle, settings, logging](#6-app-lifecycle-settings-logging)
7. [Sessions: access + refresh tokens](#7-sessions-access--refresh-tokens)
8. [API (planned)](#8-api-planned)
9. [Data model (planned)](#9-data-model-planned)
10. [Security rules](#10-security-rules)
11. [Configuration](#11-configuration)
12. [Everyday commands](#12-everyday-commands)
13. [Build plan](#13-build-plan)
14. [Deployment](#14-deployment)

---

## 1. Where it fits

```
 mobile app ──HTTPS──► HAProxy ingress ──/auth/*──► auth-service (2 pods, workers) ──► Postgres (ops node, db "auth")
     │                                                   │
     │                                                   └── publishes public keys: /auth/.well-known/jwks.json
     │                                                                     │ (fetched once, cached)
     └──WSS + access token──► chat-service ── verifies the token locally ◄─┘   (no call to auth-service)
```

- auth-service **owns** users, devices, SMS codes and refresh tokens, in its own Postgres database.
  No other service reads that database.
- Other services trust auth-service only through **signed access tokens**, which they verify with its
  public key.
- It's stateless between requests. All state is in Postgres (and Redis for rate limits), so any pod can
  serve any request and we scale by adding pods.

## 2. Technology choices

| Concern | Choice | Node.js equivalent | Why |
|---|---|---|---|
| Language | **Python 3.14** | Node 22 LTS | Current stable version with a year of fixes; ready-built ARM packages exist for all our libraries |
| Packages / env | **uv** (`pyproject.toml` + `uv.lock`) | npm + nvm + npx | Fast, reproducible (lock file), installs Python itself |
| Web framework | **FastAPI** (on Starlette, served by **uvicorn**) | Express + zod + Swagger | Async, validation from type hints, automatic OpenAPI docs, WebSockets (chat-service uses the same stack) |
| Validation / schemas | **Pydantic v2** | zod | Validates requests, responses and settings at runtime |
| Postgres | **SQLAlchemy 2.0 (async)** + **asyncpg** driver | Drizzle / Prisma on `pg` | Mature, async, typed; we use the ORM for simple tables |
| Migrations | **Alembic** | Prisma migrate / Knex | Versioned schema changes, generated from the models |
| Tests | **pytest** + pytest-asyncio + httpx | jest / vitest + supertest | `async def` tests; the app is called in-process |
| Lint + format | **ruff** | eslint + prettier | One fast tool for both |
| Type check | **mypy** (strict) | `tsc --noEmit` | Catches type errors before runtime |
| Later | PyJWT + cryptography, redis-py (asyncio), httpx, OpenTelemetry | jose, ioredis, fetch, @opentelemetry | Added in the step that needs them |

**Python vs TypeScript types:** type hints are *not* checked when the code runs, just like TS types
disappear after compilation. `mypy` checks them before running (in CI); **Pydantic** is what actually
validates data at runtime, like zod.

## 3. Async: how it works here and the rules

FastAPI runs on **one event loop per process**, the same model as Node: `async def`, `await`,
thousands of concurrent requests on one thread. Unlike Node, the Python ecosystem is mixed: many
libraries are **blocking** (sync). A blocking call inside `async def` **freezes the whole process**
until it returns, and Python gives no warning.

**Rules for this codebase:**
1. Inside `async def`, use only async libraries: SQLAlchemy async (asyncpg), `httpx.AsyncClient`,
   `redis.asyncio`, `asyncio.sleep`. **Never** `requests`, `time.sleep`, `psycopg2`, or a sync SDK.
2. Something blocking or CPU-heavy that can't be avoided → `await asyncio.to_thread(func, *args)`
   (runs it on a worker thread, like offloading to a worker in Node).
3. Always `await` coroutines. Calling `do_x()` without `await` creates the coroutine but **never runs
   it**, the same as forgetting `await` on a promise. mypy and ruff flag most of these.
4. More throughput = more pods (one CPU core per process), not threads.

`ruff`'s `ASYNC` rules catch common blocking calls inside `async def`.

## 4. Project layout

```
auth-service/
├── pyproject.toml          # dependencies + tool settings (≈ package.json)
├── uv.lock                 # exact versions of everything (≈ package-lock.json) — committed
├── .python-version         # 3.14 (uv uses it, ≈ .nvmrc)
├── Dockerfile              # 2-stage build with uv, runs as non-root
├── compose.yaml            # local Postgres for development
├── .env.example            # copy to .env for local settings (.env is gitignored)
├── alembic.ini             # migration tool settings (no credentials in here)
├── alembic/
│   ├── env.py              # reads DATABASE_URL, uses our models' metadata
│   ├── script.py.mako      # template for new migration files
│   └── versions/           # one file per migration (empty for now)
├── src/auth_service/
│   ├── main.py             # app factory: create_app(), lifespan (startup/shutdown), routers
│   ├── config.py           # Settings from env vars (pydantic-settings), get_settings()
│   ├── api/                # HTTP layer
│   │   ├── health.py       #   /healthz, /auth/healthz (the only routes so far)
│   │   ├── router.py       #   mounts all feature routers under /auth
│   │   ├── deps.py         #   dependencies routes receive (DbSession, later: current user)
│   │   ├── otp.py          #   (empty) /auth/otp/request, /auth/otp/verify
│   │   ├── tokens.py       #   (empty) /auth/token/refresh, /auth/logout
│   │   ├── devices.py      #   (empty) /auth/devices
│   │   └── jwks.py         #   (empty) /auth/.well-known/jwks.json
│   ├── services/           # business logic (planned modules listed in __init__.py)
│   ├── db/
│   │   ├── engine.py       #   connection pool + sessions (Database class)
│   │   ├── models.py       #   tables (Base only, for now)
│   │   └── repos.py        #   data access functions (empty)
│   └── core/
│       └── logging.py      #   JSON logs to stdout
└── tests/
    ├── conftest.py         # shared fixtures: in-process HTTP client
    └── test_skeleton.py    # health checks, docs hidden in prod, URL conversion
```

**Why `src/auth_service/` and not just `app.py`:** with the `src/` layout, tests import the
*installed* package the same way production does, so a missing file is caught in tests rather than in
production. `auth_service` is the import name: `from auth_service.config import get_settings`.

## 5. Layers and how a request flows

```
HTTP request
   │
   ▼
api/<feature>.py   route: parse + validate input (Pydantic), call a service, shape the response
   │                       knows HTTP (status codes, headers); no SQL, no business rules
   ▼
services/<x>.py    business rules: "is this code valid?", "rotate this token", "is this a reuse?"
   │                       no HTTP, no SQL; easy to unit-test
   ▼
db/repos.py        small async functions: get_user_by_phone(), insert_refresh_token()...
   │                       the only place with queries
   ▼
db/models.py       table definitions  ──►  Postgres
```

Dependencies only point **downwards**: `api → services → db`. `services` never imports from `api`,
and `db` never imports from `services`. This is the controllers → services → repositories split
you'd use in an Express or Nest app.

**Dependency injection (`Depends`).** A route declares what it needs as typed parameters, and FastAPI
provides them for each request (similar to Express middleware attaching `req.db`, but typed and explicit):

```python
@router.post("/otp/verify")
async def verify_otp(body: VerifyOtpIn, db: DbSession) -> TokensOut: ...
```

`DbSession` (in `api/deps.py`) opens one database session per request, and closes or rolls it back
automatically. Later `CurrentDevice` will decode the access token the same way.

## 6. App lifecycle, settings, logging

**Startup** (`main.py`):
1. `create_app()` reads `Settings`, sets up JSON logging and mounts the routers.
2. `lifespan()` runs once per process. Before `yield`: create the DB pool (it connects lazily, on
   the first query). After `yield` (shutdown, e.g. a rolling deploy): close the pool cleanly.

**Settings** (`config.py`): every field comes from an env var with the same name (`DATABASE_URL` →
`database_url`). The types are validated at startup, so a typo fails immediately with a clear error.
`ENVIRONMENT` defaults to **`prod`**, so a forgotten variable never turns on dev features.

**Logging** (`core/logging.py`): one JSON object per line on stdout, for example:
```json
{"ts": "2026-10-12T21:12:38.523+00:00", "level": "info", "logger": "auth_service.main",
 "msg": "started", "service": "auth-service", "pod": "auth-6d9f...", "environment": "prod"}
```
Alloy ships these to Loki, so in Grafana you query `{app="auth"} | json | level="error"`.
Extra fields: `log.info("otp sent", extra={"phone": masked})`. Health-check access lines are dropped.
Later each line also gets `trace_id`, linked to its trace in Tempo.

## 7. Sessions: access + refresh tokens

| | Access token | Refresh token |
|---|---|---|
| What | JWT signed by auth-service (Ed25519 / `EdDSA`) | Random 256-bit value, base64url |
| Lifetime | **15 min** | **60 days**, renewed on every use |
| Stored server-side | No (anyone verifies it with the public key) | Yes: only its **SHA-256 hash**, one chain per device |
| Sent | `Authorization: Bearer …` on API calls; first message on WebSocket connect | Only to `POST /auth/token/refresh` |
| On the phone | Secure storage (`expo-secure-store`: iOS Keychain / Android Keystore) | Same |

**Access token claims:** `iss=auth-service`, `sub=<user_id>`, `did=<device_id>`, `iat`, `exp`, `jti`,
`kid` in the header (which key signed it).

**Flows**
- **Sign up / log in:** `otp/request {phone}` → SMS code → `otp/verify {phone, code, device info}`
  → find or create the user, register the device → `{access, refresh}`.
- **Refresh (rotation):** `token/refresh {refresh}` → the old token is marked used → returns a **new**
  pair in the same chain.
- **Reuse detection:** if an already-used refresh token is presented again, it was copied. Revoke the
  **whole chain** for that device. Both the attacker and the user must log in again.
- **Logout / remove a device:** revoke that device's chain. Its access token still works for up to
  15 minutes. chat-service can also check a Redis "revoked devices" set when the socket connects.
- **WebSocket (chat-service):** connect, then send `{"type":"auth","token":"<access>"}` within 5 s.
  The token is never put in the URL, because URLs end up in logs.
- **Signing keys:** an Ed25519 key pair from a SealedSecret. `jwks.json` publishes the current and the
  previous public key, so keys can be rotated without logging anyone out.
- **Web client later:** the refresh token goes in an `HttpOnly; Secure; SameSite=Strict` cookie scoped
  to `/auth/token/refresh`, and the access token is kept in memory only.

Why not server sessions: every service would have to look up a shared session store on every request,
and mobile apps don't handle cookies well. Why not one long-lived JWT: it can't be revoked.

## 8. API (planned)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz`, `/auth/healthz` | – | Health checks ✅ |
| POST | `/auth/otp/request` | – | Send an SMS code. Always `202`, so it never reveals whether a number is registered. Rate limited |
| POST | `/auth/otp/verify` | – | Check the code → user + device + tokens |
| POST | `/auth/token/refresh` | refresh token | Rotate → new pair |
| POST | `/auth/logout` | access token | Revoke this device's refresh chain |
| GET | `/auth/devices` | access token | List my devices |
| DELETE | `/auth/devices/{id}` | access token | Sign a device out |
| GET | `/auth/.well-known/jwks.json` | – | Public keys for verifying access tokens |
| GET | `/auth/docs` | – | Interactive API docs, **dev only** |

Errors use one shape everywhere: `{"error": "<code>", "message": "<human text>"}`. Clients branch on
`error`, never on `message`.

## 9. Data model (planned)

| Table | Key columns | Notes |
|---|---|---|
| `users` | `id uuid PK`, `phone_e164 text UNIQUE`, `created_at`, `status` | Phone normalised to E.164 (`+972501234567`) |
| `devices` | `id uuid PK`, `user_id FK`, `platform`, `name`, `public_key` (later, E2E), `created_at`, `last_seen_at`, `revoked_at` | One row per installed app |
| `otp_challenges` | `id`, `phone_e164`, `code_hash`, `expires_at`, `attempts`, `consumed_at`, `ip`, `created_at` | Code valid 5 min, max 5 attempts, single use |
| `refresh_tokens` | `id`, `device_id FK`, `family_id`, `token_hash UNIQUE`, `parent_id`, `expires_at`, `used_at`, `revoked_at`, `created_at` | `family_id` = the chain; reuse ⇒ revoke the family |
| `sms_daily_usage` | `day PK`, `sent` | Global daily SMS cap. Kept in Postgres, **not** Redis, so a Redis restart can't reset the money guard |

Per-phone and per-IP rate limits live in Redis (losing them on a restart is acceptable).
Expired OTP rows and refresh tokens are cleaned up by a periodic job (later).

## 10. Security rules
- **Never log** OTP codes, tokens or full phone numbers. Mask them: `+97250***4567`.
- OTP codes: 6 digits, stored as `HMAC-SHA256(server_pepper, phone + code)`. They are compared in
  constant time (`hmac.compare_digest`), expire after 5 minutes, are invalidated after 5 wrong tries,
  and work once.
- Refresh tokens: only the SHA-256 hash is stored, so a database leak doesn't hand out usable tokens.
- `otp/request` always answers `202` the same way (no account enumeration), with a 60 s resend cooldown.
- SMS cost guards (before any paid provider is switched on): Israel-only numbers, a per-phone and
  per-IP limit, a **global daily cap**, and on the provider side prepaid credit with auto-recharge off.
- Secrets (DB URL, signing key, OTP pepper, SMS credentials) come only from Kubernetes Secrets
  (SealedSecrets in git), never from code or images.

## 11. Configuration

| Env var | Default | Meaning |
|---|---|---|
| `ENVIRONMENT` | `prod` | `dev` enables `/auth/docs` |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `POD_NAME` | `local` | Set by Kubernetes; added to every log line |
| `DATABASE_URL` | – | `postgresql://user:pass@host:5432/db`. Optional until the first DB feature |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `5` / `5` | Connections per pod: at most 10 × 2 pods, against Postgres' `max_connections=50` |

More settings are added with the steps that need them (token lifetimes, key file, OTP pepper,
Redis URL, SMS provider).

## 12. Everyday commands

One-time setup (Mac): `brew install uv`, then in this folder:
```bash
uv sync                          # creates .venv with Python 3.14 + all packages (≈ npm install)
cp .env.example .env             # local settings
docker compose up -d             # local Postgres on localhost:5432
```

| Task | Command | Node equivalent |
|---|---|---|
| Run with auto-reload | `uv run uvicorn auth_service.main:app --reload` | `nodemon` |
| API docs (dev) | http://localhost:8000/auth/docs | Swagger UI |
| Tests | `uv run pytest` | `npm test` |
| Lint / auto-fix | `uv run ruff check .` / `uv run ruff check --fix .` | `eslint` |
| Format | `uv run ruff format .` | `prettier --write` |
| Type check | `uv run mypy` | `tsc --noEmit` |
| Add a package | `uv add <pkg>` / `uv add --dev <pkg>` | `npm i` / `npm i -D` |
| New migration | `uv run alembic revision --autogenerate -m "add users"` | `prisma migrate dev` |
| Apply migrations | `uv run alembic upgrade head` | `prisma migrate deploy` |

CI runs lint, format check, type check and tests on every push and pull request; any failure stops
the build. Run them locally before pushing:
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

## 13. Build plan
Each step is small, tested and deployed before the next.

1. ✅ **Skeleton**: structure, tooling, JSON logs, DB pool, Alembic, tests, CI, Docker.
2. First tables (`users`, `devices`, `otp_challenges`) + migration; run migrations in the cluster.
3. `POST /auth/otp/request` with a **fake SMS sender** (₪0) + rate limits.
4. `POST /auth/otp/verify` → user + device.
5. Signing keys + access tokens + `jwks.json`.
6. Refresh tokens: issue, rotate, reuse detection, logout, devices list/remove.
7. Tracing (OpenTelemetry → Tempo) + metrics.
8. Dummy Expo app against it (PLAN.md phase 8).
9. Real SMS provider behind a flag, with all cost guards (only when you decide).

## 14. Deployment
- Push to `main` → GitHub Actions (ARM runner): checks → image
  `ghcr.io/jzviagin-chat-app/auth-service:sha-<12>` → a PR in `chat-infra` bumps `k8s/auth.yaml` →
  merging it deploys (Argo CD).
- The image is built in 2 stages: dependencies are installed with `uv sync --frozen` (exactly the lock
  file), and the runtime image contains only Python, `.venv`, our code and the migrations, and runs
  as an unprivileged user.
- `DATABASE_URL` comes from the Secret `auth-db` (namespace `default`, sealed in chat-infra). It's
  wired into `k8s/auth.yaml` in build step 2, together with running migrations before the app starts.
