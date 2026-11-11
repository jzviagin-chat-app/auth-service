# auth-service

Sign-up and login with a phone number and an SMS code. It issues the tokens that every other
service (chat-service first) uses to know **who** is calling and **from which device**.

> **Status: sign-up, login, sessions.** `POST /auth/otp/request` sends real SMS codes (Global SMS),
> `otp/verify` + `register` log users in with access + refresh tokens, `token/refresh` keeps them logged in,
> `logout` and `devices` sign devices out, every request is traced (OpenTelemetry → Tempo) and counted
> (`/metrics` for Prometheus). Next: the dummy Expo app (see [Build plan](#13-build-plan)).

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
| Tracing | **OpenTelemetry** SDK + one instrumentation package per library (FastAPI, SQLAlchemy, redis, httpx) | @opentelemetry/sdk-node + auto-instrumentations | Vendor-neutral standard; Alloy/Tempo already speak it |
| Metrics | **prometheus_client** (the official Prometheus library) | prom-client | Prometheus reads `/metrics` (pull); counters live in memory |
| Also | PyJWT + cryptography, redis-py (asyncio), httpx | jose, ioredis, fetch | Tokens, Redis, the SMS provider's API |

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
│   └── versions/           # one file per migration (0001 tables, 0002 sms_daily_usage,
│                           #   0003 registration token, 0004 device role)
├── src/auth_service/
│   ├── main.py             # app factory: create_app(), lifespan (startup/shutdown), routers
│   ├── config.py           # Settings from env vars (pydantic-settings), get_settings()
│   ├── api/                # HTTP layer
│   │   ├── health.py       #   /healthz, /auth/healthz
│   │   ├── metrics.py      #   /metrics (Prometheus; not under /auth, so not public)
│   │   ├── router.py       #   mounts all feature routers under /auth
│   │   ├── errors.py       #   the {"error", "message"} shape for every error
│   │   ├── deps.py         #   dependencies routes receive (DbSession, OtpContextDep, LoginContextDep,
│   │   │                   #   SessionContextDep, CurrentDevice, TokenDevice, RevokedDep)
│   │   ├── otp.py          #   /auth/otp/request ✅, /auth/otp/verify ✅
│   │   ├── register.py     #   /auth/register ✅
│   │   ├── schemas.py      #   bodies shared by routes (DeviceIn, LoggedInOut)
│   │   ├── tokens.py       #   /auth/token/refresh ✅, /auth/logout ✅
│   │   ├── devices.py      #   GET /auth/devices ✅, DELETE /auth/devices/{id} ✅
│   │   └── jwks.py         #   /auth/.well-known/jwks.json ✅
│   ├── services/           # business logic
│   │   ├── otp.py          #   request a code: validation, hashing, the flow
│   │   ├── login.py        #   otp/verify: check a code → logged in / registration required
│   │   ├── registration.py #   registration token, register (new user + primary device)
│   │   ├── devices.py      #   device info, phones only, replace the primary, logout, list, remove
│   │   ├── refresh.py      #   refresh tokens: issue at login, rotate, lost replies, reuse detection
│   │   ├── cleanup.py      #   hourly job: old OTP rows, long-revoked devices, old refresh tokens
│   │   ├── rate_limit.py   #   per-phone / per-IP limits in Redis
│   │   ├── revocation.py   #   signing a device out (the one place), publishing it in the shared Redis, resync
│   │   ├── keys.py         #   Ed25519 signing keys, kid (RFC 7638 thumbprint), JWKS document
│   │   ├── tokens.py       #   issue / verify access tokens (JWT, EdDSA)
│   │   └── sms.py          #   SmsSender interface, FakeSmsSender, GlobalSmsSender, make_sms_sender()
│   ├── db/
│   │   ├── engine.py       #   connection pool + sessions (Database class)
│   │   ├── models.py       #   tables
│   │   └── repos.py        #   data access functions (the only SQL)
│   └── core/
│       ├── logging.py      #   JSON logs to stdout (with trace_id inside a request)
│       ├── tracing.py      #   OpenTelemetry: provider, exporter to Alloy, library instrumentations
│       ├── metrics.py      #   all metrics (counters, histogram) + the request-counting middleware
│       ├── hashing.py      #   random secret tokens + their SHA-256 (registration, refresh)
│       └── masking.py      #   mask_phone() for logs
└── tests/
    ├── conftest.py         # shared fixtures, needs_db / needs_redis markers, alembic()
    ├── test_skeleton.py    # health checks, docs hidden in prod, URL conversion
    ├── test_errors.py      # error shape
    ├── test_client_ip.py   # X-Forwarded-For handling behind the ingress
    ├── test_otp_units.py   # pure OTP helpers
    ├── test_otp_request.py # POST /auth/otp/request against real Postgres + Redis
    ├── test_otp_verify.py  # POST /auth/otp/verify + /auth/register against real Postgres
    ├── test_refresh.py     # POST /auth/token/refresh: rotation, lost replies, reuse, cleanup
    ├── test_devices.py     # CurrentDevice, logout, devices list/remove, revoked devices in Redis
    ├── test_tokens.py      # signing keys, access tokens, jwks.json
    ├── test_tracing.py     # spans per request/query/command, trace_id in logs, no secrets in spans
    ├── test_metrics.py     # /metrics, counts by route template and outcome, no secrets in metrics
    └── test_migrations.py  # migrations + constraints against real Postgres
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
automatically. `CurrentDevice` gives a route the logged-in caller the same way:
`async def list_devices(me: CurrentDevice, db: DbSession)` (like an Express auth middleware setting
`req.user`, but per route and typed).

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
A line written while a request is handled also has `trace_id` and `span_id` (tracing on), so in Grafana a log
line links to its trace in Tempo, and back.

**Tracing** (`core/tracing.py`): with `OTEL_EXPORTER_OTLP_ENDPOINT` set, every request becomes a trace: a span
for the HTTP request and, inside it, one per Postgres query, Redis command and outgoing HTTP call (Global
SMS), with their durations. We don't write these spans: one OpenTelemetry *instrumentation* package per
library hooks into it (FastAPI: a middleware; SQLAlchemy: its query events; redis and httpx: their methods are
wrapped at startup). The current span lives in a `contextvar` (≈ `AsyncLocalStorage`), so every query made
for a request becomes its child. The background loops (cleanup, revoked-devices resync) make one span per
round (`tracer` in `core/tracing.py`). Spans are sent in batches by a background thread to Alloy
(`alloy.observability.svc:4317`), which passes them to Tempo; if Alloy is down they are dropped, requests never
wait. Health checks and `/metrics` aren't traced. Unset → no tracing (local default, CI).

**Metrics** (`core/metrics.py`): totals over time, where a trace is one request. Each pod keeps counters in
memory and serves them as text at `GET /metrics`; Prometheus reads every pod's page every 60 s (the pod
annotations in `k8s/auth.yaml` tell it to) and Grafana graphs them (≈ prom-client + an Express route). Counters
only go up and restart from 0 with the pod; queries use `rate()` / `increase()`, which handle that.
- `http_requests_total{method, route, status}`, `http_request_duration_seconds{method, route}` (histogram):
  every request except health checks and `/metrics`, by route template (`/auth/devices/{device_id}`).
- `auth_otp_requests_total{outcome}` (`sent`, `rate_limited_<limit>`, `daily_cap`, `send_failed`, …),
  `auth_sms_sent_total{provider, result}`, `auth_logins_total{outcome}` (verify + register),
  `auth_refresh_total{outcome}` (`rotated`, `retried`, `reused`, reject reasons),
  `auth_devices_revoked_total{reason}` (counted after the commit), `auth_job_runs_total{job, result}`
  (cleanup, resync: a loop that keeps failing shows up).
- Plus the library's own `process_*` (memory, CPU, open files) and `python_gc_*`.

Example queries: `sum by (route) (rate(http_requests_total{app="auth"}[5m]))`,
`increase(auth_otp_requests_total[1d])`, `histogram_quantile(0.95, sum by (le, route)
(rate(http_request_duration_seconds_bucket[5m])))`.

## 7. Sessions: access + refresh tokens

| | Access token | Refresh token |
|---|---|---|
| What | JWT signed by auth-service (Ed25519 / `EdDSA`) | Random 256-bit value, base64url |
| Lifetime | **15 min** | **60 days**, renewed on every use |
| Stored server-side | No (anyone verifies it with the public key) | Yes: only its **SHA-256 hash**; the device is the chain |
| Sent | `Authorization: Bearer …` on API calls; first message on WebSocket connect | Only to `POST /auth/token/refresh` |
| On the phone | Secure storage (`expo-secure-store`: iOS Keychain / Android Keystore) | Same |

**Access token claims:** `iss=auth-service`, `sub=<user_id>`, `did=<device_id>`, `iat`, `exp`, `jti`,
`kid` in the header (which key signed it).

**Flows**
- **Devices (WhatsApp model, PLAN.md §6):** each user has **one primary device**, a phone (`ios` / `android`)
  logged in by SMS. **A new SMS login replaces it:** in one transaction the old primary is revoked
  (`revoked_at`, which also ends its refresh tokens) and the new device is created. A re-login on the same
  phone (reinstall, wiped app) is the same case: the server can't recognise a reinstalled app, so it is a new
  device. Why: whoever gets hold of a code (SIM swap, phishing) signs the real owner out, so a takeover is
  noticed instead of an extra device silently reading along. Browsers and tablets will be **linked** devices,
  added by QR from the primary (step 10; up to 4).
- **Log in (known number):** `otp/request {phone}` → SMS code → `otp/verify {phone, code, device}` →
  the old primary revoked, a new primary device → `logged_in` + access and refresh tokens.
- **Sign up (new number, Telegram-like):** the same `otp/verify` answers `registration_required` with a
  registration token (random 256 bits, only its SHA-256 stored on the `otp_challenges` row, 10 min, single
  use) → `register {registration_token, display_name, device}` → user + device → `logged_in`. No user row
  exists before a display name is sent. The app keeps the registration token only in secure storage
  (this device only) and deletes it on use, expiry, back or sign-in, so a restart doesn't cost a new SMS.
- **Refresh (`services/refresh.py`):** `token/refresh {refresh_token}` → a new access token and a new
  refresh token. Every login creates a new device, so **the device is the chain**: no family ids, and
  "kill the chain" = sign the device out (`devices.revoked_at`). Each refresh locks the device row, so
  refreshes of one device run one at a time (on any pod). A token is *waiting* (handed out, never used),
  *current* (used) or *discarded* (replaced):
  - waiting token → it becomes current; every other token of the device is discarded;
  - current token again → the reply was lost (a tunnel, a crash): one more waiting token, the others stay
    usable, so replies arriving out of order are fine too (at most 5 wait; beyond that the oldest is
    discarded);
  - discarded token → someone holds a copy: the device is signed out, and both the thief and the user
    must log in again;
  - unknown, expired or device signed out → `401`.

  So a lost reply never logs anyone out, after any delay. Each new token lives 60 days, so the session
  slides with use. Replaced tokens are deleted 7 days after being discarded (`REFRESH_REPLACED_RETENTION_DAYS`):
  until then a copy signs the device out, afterwards it just gets 401.
- **Rules for the app:** send one refresh at a time; save the new refresh token before using it; ignore
  replies to requests it has given up on. If the reply is lost, retry with the same token. Refresh a minute
  or so before the access token expires.
- **The logged-in caller (`api/deps.CurrentDevice`):** reads `Authorization: Bearer <access token>`,
  verifies it (`verify_access_token`), then reads the device in Postgres: a signed-out device gets `401`
  at once, not only when its token expires; a blocked user `403`. Logout uses `TokenDevice` (the token
  only), so it also succeeds for a device that is already signed out.
- **Signing a device out** (`devices.revoked_at`): logout, the primary removing a linked device, a new SMS
  login replacing the primary, refresh-token reuse. All four go through one function,
  `services/revocation.revoke_device` (the only caller of `repos.revoke_device`, the only SQL that sets
  `revoked_at`); it never commits, so a login can revoke the old phone in the same transaction that
  creates the new one. Its refresh tokens stop at once (refresh reads the device), auth's endpoints
  refuse its access token at once, and chat-service learns it from Redis:
- **Revoked devices for chat-service** (`services/revocation.py`, decided 2026-11-02): `revoke_device`
  remembers the device on the session (`session.info`); a SQLAlchemy `after_commit` listener then starts
  a background write (the reply doesn't wait; a rolled-back transaction writes nothing) of
  `revoked:device:<device_id>` into the Redis db shared by the services
  (`SHARED_REDIS_URL`, db 2), expiring 17 min after the revocation (by then every access token of the
  device has expired). auth only writes these keys, never reads them. A failed write (3 quick tries,
  ~1 s in total) doesn't fail the request: every pod rewrites the keys of all devices revoked in the
  last 17 min each minute, plus a marker `revoked:synced` (expires after 3 min), so a Redis restart is
  refilled within a minute and a missing marker tells chat-service the list may be incomplete.
- **Who manages devices (WhatsApp model):** only the primary lists devices and removes linked ones; a
  linked device can only log itself out; the primary leaves by logout or a new SMS login. What a primary
  logout or replacement does to linked devices is decided in step 10.
- **WebSocket (chat-service):** connect, then send `{"type":"auth","token":"<access>"}` within 5 s.
  The token is never put in the URL, because URLs end up in logs.
- **Signing keys** (`services/keys.py`): an Ed25519 private key (PEM) in `JWT_PRIVATE_KEY` from the sealed
  Secret `auth-jwt`. Each key's `kid` is its RFC 7638 thumbprint (a hash of the public key), so a new key
  gets a new id automatically. `jwks.json` publishes the current public key and, during a rotation, the
  previous one. A broken key stops the pod from starting; no key in prod → token endpoints and `jwks.json`
  answer 503; no key with `ENVIRONMENT=dev` → a throwaway key is generated at startup.
- **Key rotation:** generate a new key; set `JWT_PRIVATE_KEY` to it and `JWT_PREVIOUS_PUBLIC_KEY` to the old
  *public* key; deploy (new tokens use the new key, old tokens still verify). After 15 min plus consumers'
  JWKS cache (5 min), remove `JWT_PREVIOUS_PUBLIC_KEY`. Nobody is logged out (refresh tokens don't depend
  on the signing key).
- **Verifying** (`services/tokens.verify_access_token`, the reference for every consumer): algorithm fixed to
  `EdDSA`, key chosen by `kid` from the published keys only, `iss` checked, all claims required, 30 s leeway
  for clock differences. Never trust the token's own `alg` or embedded keys.
- **Web client later:** the refresh token goes in an `HttpOnly; Secure; SameSite=Strict` cookie scoped
  to `/auth/token/refresh`, and the access token is kept in memory only.

Why not server sessions: every service would have to look up a shared session store on every request,
and mobile apps don't handle cookies well. Why not one long-lived JWT: it can't be revoked.

## 8. API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz`, `/auth/healthz` | – | Health checks ✅ |
| GET | `/metrics` | – (inside the cluster only) | Prometheus metrics ✅ |
| POST | `/auth/otp/request` | – | Send an SMS code ✅. Same `202` for every number, registered or not. Rate limited (below) |
| POST | `/auth/otp/verify` | – | Check the code → logged in, or registration required ✅ |
| POST | `/auth/register` | registration token | New number: display name → user + device + token ✅ |
| POST | `/auth/token/refresh` | refresh token | New access + refresh token ✅ (retry with the same token if the reply is lost) |
| POST | `/auth/logout` | access token | Sign this device out ✅ (always `204`) |
| GET | `/auth/devices` | access token (primary) | List my active devices ✅ |
| DELETE | `/auth/devices/{id}` | access token (primary) | Sign one of my linked devices out ✅ |
| GET | `/auth/.well-known/jwks.json` | – | Public keys for verifying access tokens ✅ (`Cache-Control: public, max-age=300`) |
| GET | `/auth/docs` | – | Interactive API docs, **dev only** |

Errors use one shape everywhere: `{"error": "<code>", "message": "<human text>"}`. Clients branch on
`error`, never on `message`. Some errors add machine-readable fields (counts, seconds, fixed names; never
a phone number, code or token), listed with each answer below. Codes so far: `invalid_request` (422, malformed body; names the fields,
never echoes values), `not_found`, `method_not_allowed`, `internal_error` (500), and the ones below.

**`POST /auth/otp/request`** `{"phone": "+972501234567"}`

| Answer | When |
|---|---|
| `202 {"resend_after_seconds": 60, "expires_in_seconds": 300}` | Code sent. `resend_after_seconds`: when the next code may be requested: the 60 s cooldown, or longer when this code was the last one of an hour or day (number or IP) |
| `400 invalid_phone` | Not an Israeli mobile number in E.164 form (`+9725XXXXXXXX`) |
| `429 {"error": "rate_limited", "message", "limit", "retry_after_seconds"}` + `Retry-After: <seconds>` | A limit is full. `limit`: `resend_cooldown` (60 s between codes), `phone_hour` / `phone_day` (5 / 10 per number), `ip_hour` / `ip_day` (30 / 200 per client IP). A number's limits block only that number; an IP limit blocks every number from that IP. If several are full (the cooldown included), the one that frees up last, so the wait is the real one |
| `503 sms_unavailable` | Global daily SMS cap reached (+ `Retry-After`: seconds until midnight UTC), Redis unreachable (fail closed), SMS provider failed, or not configured |

The limits apply to registered and unknown numbers alike, and the endpoint never reads `users`, so no
answer reveals whether a number has an account.

Flow (`api/otp.py` → `services/otp.py`):
1. Validate the number (Israel-only is an SMS cost guard).
2. Redis limits (`services/rate_limit.py`): keys `auth:rl:phone:<HMAC of phone>:cooldown|h|d` and
   `auth:rl:ip:<ip>:h|d`. One Lua script (Redis runs it as one step, so two pods can't both take the last
   slot) checks the cooldown and the 4 windows, and only when none is full starts the cooldown and counts
   the request (`INCR` + `EXPIRE NX`). A refused request changes nothing, so tapping while blocked doesn't
   use up the quota. Each window starts at its first request and is deleted when it ends. The phone is an
   HMAC in the key, so no phone numbers sit in Redis.
3. Daily cap (`sms_daily_usage`, atomic `UPDATE … WHERE sent < cap`) and the `otp_challenges` row, in one
   transaction.
4. Send after the commit, so no code reaches a phone unless it can be verified.

**`POST /auth/otp/verify`** `{"phone": "+972501234567", "code": "123456", "device": {"platform": "ios", "name": "Jacob's iPhone"}}`
(`platform`: `ios` or `android`, `web` is answered 400; `name` optional, max 100)

| Answer | When |
|---|---|
| `200 {"status": "logged_in", "access_token", "token_type": "Bearer", "expires_in": 900, "refresh_token"}` (the user and device ids are in the access token: `sub`, `did`) | Right code, the number has an account: this phone becomes the primary device, the previous one is signed out |
| `200 {"status": "registration_required", "registration_token", "expires_in": 600}` | Right code, new number → call `register` |
| `400 invalid_code` + `attempts_left` (1–4), `resend_after_seconds` | Wrong code; retry |
| `400 too_many_attempts` + `resend_after_seconds` | The 5th wrong try, and any try after it (even the right code) → request a new one |
| `400 code_expired` + `resend_after_seconds` | No usable code: none sent, expired or already used → request a new one |
| `400 invalid_phone` / `422 invalid_request` | Bad number / malformed body (the code must be 6 digits) |
| `400 unsupported_platform` | `platform` is `web`: SMS login is for phones only (nothing is used up) |
| `403 account_blocked` | The user's status is `blocked` (the code is used up) |
| `409 login_conflict` | Another login of the same user won a race twice in a row; retry with the same code (nothing is used up) |
| `503 tokens_unavailable` | No signing key or OTP pepper configured (nothing is used up) |

Only the **newest** code for a number counts: a resend cancels the previous one. The challenge row is locked
(`SELECT … FOR UPDATE`), so parallel requests can't use one code twice or skip the attempt count. Guessing is
bounded by 5 tries per code × 10 codes a day per number (the `otp/request` limits).
`resend_after_seconds` is what's left of the number's resend cooldown (`0`: a new code can be requested now;
the hourly/daily limits show up only in `otp/request`'s 429). It is only read, nothing is counted, and it is
left out when Redis is unreachable (the app counts down on its own).

**`POST /auth/register`** `{"registration_token": "...", "display_name": "Jacob", "device": {...}}`
(`display_name` trimmed, 1–50 characters)

| Answer | When |
|---|---|
| `201` same body as `logged_in` | User and its primary device created |
| `400 unsupported_platform` | `platform` is `web` (the token stays usable) |
| `401 invalid_registration_token` | Unknown, expired (10 min) or already used → verify the number again |
| `409 already_registered` | The number got an account meanwhile (e.g. another device registered first) → log in |
| `503 tokens_unavailable` | As above |

**`POST /auth/token/refresh`** `{"refresh_token": "..."}`

| Answer | When |
|---|---|
| `200` same body as `logged_in` | Store the new `refresh_token` (replacing the old one) and use the new access token |
| `401 invalid_refresh_token` | Unknown, expired, reused (the device is now signed out) or the device was signed out → log in by SMS. One code for all: the app does the same in each case; the reason is in our logs |
| `403 account_blocked` | The user's status is `blocked` |
| `503 tokens_unavailable` | No signing key configured (nothing is changed) |

**Logged-in endpoints** send `Authorization: Bearer <access token>`. Every one may answer
`401 invalid_token` (missing, invalid, expired, or the device is signed out; with
`WWW-Authenticate: Bearer`): refresh, or log in again if that fails too. `403 account_blocked`: the user
is blocked. `503 tokens_unavailable`: no signing key configured.

**`POST /auth/logout`** → `204`, also when the device was already signed out. Delete the stored tokens.

**`GET /auth/devices`**

| Answer | When |
|---|---|
| `200 {"devices": [{"id", "platform", "name", "role", "created_at", "last_seen_at", "current"}]}` | The user's active devices, oldest first; `current` is the caller. `last_seen_at` = last login or refresh |
| `403 action_not_allowed` | The caller is a linked device: only the primary manages devices |

**`DELETE /auth/devices/{id}`**

| Answer | When |
|---|---|
| `204` | The linked device is signed out (or already was) |
| `403 action_not_allowed` | The caller isn't the primary, or `id` is the caller itself (log out instead) |
| `404 not_found` | No such device of this user (another user's device answers the same) |

**Client IP:** `request.client.host` comes from `X-Forwarded-For`, believed only from `TRUSTED_PROXIES`
(the ingress in the pod network `10.42.0.0/16`). HAProxy appends to whatever the client sent, so the app
takes the rightmost untrusted address (uvicorn's `ProxyHeadersMiddleware`, added in `create_app()`;
the Dockerfile runs uvicorn with `--no-proxy-headers`).

## 9. Data model

In `db/models.py`; migration `0001_initial.py` creates the first three tables, `0002_sms_daily_usage.py` the cap,
`0003_registration_token.py` the registration-token columns, `0004_device_role.py` `devices.role` + the
one-active-primary index, `0005_refresh_tokens.py` the refresh tokens.
IDs are UUIDv7 generated by the app (no database default). All times are `timestamptz`.

| Table | Key columns | Notes |
|---|---|---|
| `users` ✅ | `id uuid PK`, `phone_e164 varchar(16) UNIQUE`, `display_name varchar(50)`, `avatar_key varchar(200)`, `status` (`active`/`blocked`), `created_at` | E.164 enforced by a check constraint (`+972501234567`). `avatar_key` = object-storage key, served via short-lived signed URLs. Hard delete (cascades to devices) |
| `devices` ✅ | `id uuid PK`, `user_id FK → users ON DELETE CASCADE`, `platform` (`ios`/`android`/`web`), `role` (`primary`/`linked`, default `primary`), `name`, `created_at`, `last_seen_at`, `revoked_at`; partial unique index `(user_id) WHERE role = 'primary' AND revoked_at IS NULL` | One row per app installation (a login). At most one active primary per user, enforced by Postgres. `last_seen_at` is set at login and on every token refresh, never per API call (it is *not* chat's "last seen" presence, which chat-service will own). Revoked rows are deleted after 30 days. `public_key` added with E2E |
| `otp_challenges` ✅ | `id`, `phone_e164`, `code_hash bytea`, `created_at`, `expires_at`, `attempts`, `consumed_at`, `request_ip inet`, `registration_token_hash bytea UNIQUE`, `registration_expires_at`; index `(phone_e164, created_at)` | Keyed by phone, not user: the code is sent before the user exists. Valid 5 min, max 5 attempts, single use; rows deleted after 24 h. A right code for a new number stores the registration token's hash here (migration 0003) |
| `refresh_tokens` ✅ | `id uuid PK`, `device_id FK → devices ON DELETE CASCADE` (indexed), `token_hash bytea UNIQUE`, `created_at`, `expires_at`, `used_at`, `discarded_at` | SHA-256 of the token only. Waiting = `used_at` empty; current = used, not discarded; discarded = replaced. Rules in §7 |
| `sms_daily_usage` ✅ | `day date PK` (UTC), `sent int` (check `>= 0`) | Global daily SMS cap. Kept in Postgres, **not** Redis, so a Redis restart can't reset the money guard |

Per-phone and per-IP rate limits live in Redis db 1 (losing them on a restart is acceptable). Revoked
devices for chat-service live in the shared Redis db 2 (`services/revocation.py`, §7), rebuilt from
`devices.revoked_at` every minute.
An hourly loop in every pod (`services/cleanup.cleanup_loop`, started in `main.lifespan`; harmless to run
twice) deletes `otp_challenges` rows older than 24 h, devices revoked more than 30 days ago (their refresh
tokens go with them), expired refresh tokens and refresh tokens discarded more than 7 days ago.
Later: `device_links` for linking secondary devices by QR code (PLAN.md, "Later").

**Migration rules**
- Change `db/models.py`, then `uv run alembic revision --autogenerate -m "..." --rev-id 000N` and **read the
  generated file** (autogenerate misses some things, e.g. renames look like drop + add).
- Never edit a migration that has been deployed; add a new one.
- Rolling deploys run old and new pods side by side, so a migration must work with the previous app
  version: add nullable columns first, backfill, and only drop/rename in a later release (expand/contract).
- The test `test_models_match_migrations` fails if models and migrations disagree.

## 10. Security rules
- **Never log** OTP codes, tokens or full phone numbers. Mask them: `+97250***4567` (`core/masking.py`).
  Guards against accidental leaks: the DB engine has `hide_parameters=True` (query values never appear in
  error messages), 422 errors name fields but never echo values, Redis keys hold an HMAC instead of the
  phone, and SMS senders raise `SmsSendError` with a short status, never the provider's error text.
  `test_logs_never_contain_the_phone_or_code` checks every path. Only `ENVIRONMENT=dev` lets the fake
  sender log the message text (so you can read the code locally).
- **Traces** never hold them either: SQL is recorded with placeholders (`$1`), never values; Redis commands
  as `SET ? ?`; outgoing calls by URL (the Global SMS key is in the body); requests by method, route and
  status, never bodies. `test_spans_never_contain_the_phone_codes_or_tokens` checks a whole sign-up.
- **Metrics** labels hold only fixed values (route template, status, outcome), never a phone number, code,
  token or id (`test_sign_up_is_counted_and_never_exposed`). `/metrics` isn't under `/auth`, so the ingress
  never sends it here from the internet.
- OTP codes: 6 digits from `secrets`, stored as `HMAC-SHA256(OTP_PEPPER, "phone:code")`. They are compared in
  constant time (`hmac.compare_digest`), expire after 5 minutes, are invalidated after 5 wrong tries,
  and work once.
- Registration tokens: only the SHA-256 hash is stored, valid 10 min, single use.
- Refresh tokens: 256 random bits, only the SHA-256 hash is stored, so a database leak doesn't hand out
  usable tokens. A replaced token used again signs the device out. Never sent anywhere but `token/refresh`.
- `otp/request` answers the same way for registered and unknown numbers (no account enumeration), with a
  60 s resend cooldown.
- SMS cost guards (before any paid provider is switched on): Israel-only numbers, a per-phone and
  per-IP limit, a **global daily cap**, and on the provider side prepaid credit with auto-recharge off.
- Secrets (DB URL, signing key, OTP pepper, SMS credentials) come only from Kubernetes Secrets
  (SealedSecrets in git), never from code or images.

## 11. Configuration

| Env var | Default | Meaning |
|---|---|---|
| `ENVIRONMENT` | `prod` | `dev` enables `/auth/docs` |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `POD_NAME` | `local` | Set by Kubernetes; added to every log line and trace |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | – | Where traces go (OTLP over gRPC): `http://alloy.observability.svc:4317` in the cluster. Unset → no tracing |
| `DATABASE_URL` | – | `postgresql://user:pass@host:5432/db` (Secret `auth-db` in the cluster) |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `5` / `5` | Connections per pod: at most 10 × 2 pods, against Postgres' `max_connections=50` |
| `REDIS_URL` | – | `redis://redis:6379/1` in the cluster. Unset/unreachable → `otp/request` answers 503 |
| `SHARED_REDIS_URL` | – | `redis://redis:6379/2` in the cluster: the Redis db shared by the services (revoked devices for chat-service). Unset → nothing is published |
| `REVOKED_KEY_TTL_SECONDS` / `REVOKED_SYNC_INTERVAL_SECONDS` | `1020` / `60` | A revoked device's key lives 17 min after the revocation / how often every pod rewrites recent keys |
| `OTP_PEPPER` | – | Secret key for code hashes (Secret `auth-otp`). Unset → `otp/request` answers 503 |
| `TRUSTED_PROXIES` | `["10.42.0.0/16"]` | Who may set `X-Forwarded-For` (JSON list) |
| `OTP_TTL_SECONDS` / `OTP_RESEND_COOLDOWN_SECONDS` | `300` / `60` | Code lifetime / resend cooldown |
| `OTP_PHONE_MAX_PER_HOUR` / `_PER_DAY` | `5` / `10` | Codes per phone |
| `OTP_IP_MAX_PER_HOUR` / `_PER_DAY` | `30` / `200` | Codes per client IP (generous: carrier-grade NAT) |
| `OTP_RETENTION_HOURS` / `OTP_CLEANUP_INTERVAL_SECONDS` | `24` / `3600` | Cleanup job: OTP rows kept / how often it runs |
| `DEVICE_RETENTION_DAYS` | `30` | Cleanup job: revoked devices kept |
| `SMS_PROVIDER` | `fake` | `fake` (sends nothing) or `globalsms` |
| `FAKE_SMS_DAILY_CAP` | `100` | Daily SMS cap while the fake provider is active |
| `GLOBALSMS_API_KEY` | – | Global SMS key. In the cluster: sealed Secret `auth-sms` |
| `GLOBALSMS_ORIGINATOR` | `ZviaginChat` | Approved sender name/number, max 11 chars |
| `GLOBALSMS_BASE_URL` | `https://sapi.itnewsletter.co.il` | Their host without an IP allow-list |
| `GLOBALSMS_DAILY_CAP` / `GLOBALSMS_TIMEOUT_SECONDS` | `100` / `5` | Daily cap / whole-request timeout |

**SMS providers.** Each provider has its own settings block in `config.py` (`FakeSmsSettings`,
`GlobalSmsSettings`) with its own env prefix. Plain values (sender, cap, timeout) have their defaults there;
secret fields only point to an env var that Kubernetes fills from a sealed Secret. The daily count in
`sms_daily_usage` is shared; the cap is the active provider's. `make_sms_sender()` in `services/sms.py` is
the only place that knows provider names; a provider that isn't fully configured gives no sender, and
`otp/request` answers 503 (never a silent fallback to fake). Adding a provider = a class with `name`,
`send()` and `aclose()` (the `SmsSender` Protocol), its settings block, and one `case`.

**Global SMS** (`GlobalSmsSender`): `POST <GLOBALSMS_BASE_URL>/api/restApiSms/sendSmsToRecipients`
with a JSON body (`ApiKey`, `txtOriginator`, `destinations` as `05XXXXXXXX`, `txtSMSmessage`, `txtAddInf` =
our 14-char message id, which shows up in their delivery reports). The endpoint answers in JSON,
`{"success":true,"result":1,"errDesc":""}` (`result` = credits charged), although their docs describe plain
text (`1`, or an error text); both forms are read (`read_answer()`). Error texts (`errDesc`, or the plain text)
are mapped to fixed status codes (`invalid_login`, `not_enough_credit`,
`unapproved_originator`, `http_403`, `timeout`, …) that are safe to log. We use their `sapi` host, which
has no IP allow-list (their `api` host only accepts approved IPs), so the API key alone authorizes sending:
keep it secret. An English code message is 1 credit (up to 160 characters). Verified with a real send on
2026-10-26: JSON body on `sapi`, sender `ZviaginChat`, recipient format `05XXXXXXXX`.

| `JWT_PRIVATE_KEY` | – | Ed25519 private key (PEM) that signs access tokens. Sealed Secret `auth-jwt` |
| `JWT_PREVIOUS_PUBLIC_KEY` | – | Previous public key (PEM), only during a key rotation |
| `JWT_ISSUER` / `ACCESS_TOKEN_TTL_SECONDS` | `auth-service` / `900` | `iss` claim / access-token lifetime |
| `REFRESH_TOKEN_TTL_DAYS` | `60` | Refresh-token lifetime, from the last refresh |
| `REFRESH_MAX_PENDING` | `5` | Waiting tokens per device from retries; beyond that the oldest is discarded |
| `REFRESH_REPLACED_RETENTION_DAYS` | `7` | Cleanup job: discarded refresh tokens kept (a copy used meanwhile signs the device out) |

## 12. Everyday commands

One-time setup (Mac): `brew install uv`, then in this folder:
```bash
uv sync                          # creates .venv with Python 3.14 + all packages (≈ npm install)
cp .env.example .env             # local settings
docker compose up -d             # local Postgres :5432 (databases auth + auth_test) and Redis :6379
uv run alembic upgrade head      # bring your dev database to the latest migration
```
Database tests run only when `TEST_DATABASE_URL` is set, and the OTP tests also need `TEST_REDIS_URL` (both are
in `.env.example`; CI sets them too). They wipe that database and Redis db 15, so these must never be the ones
you develop against. Without them they are skipped (`uv run pytest -rs` shows why).

To see traces locally: `docker compose --profile tracing up -d jaeger`, set
`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317` in `.env`, restart the app, open http://localhost:16686
(Jaeger: a small trace viewer standing in for Tempo + Grafana).

Try the endpoint locally (with `ENVIRONMENT=dev` the fake sender logs the code):
```bash
curl -i -X POST localhost:8000/auth/otp/request -H 'content-type: application/json' -d '{"phone":"+972501234567"}'
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
2. ✅ First tables (`users`, `devices`, `otp_challenges`) + migration 0001 + database tests (CI runs a Postgres);
   migrations run in the cluster by the `migrate` init container (`k8s/auth.yaml`).
3. ✅ `POST /auth/otp/request` + per-phone/per-IP limits (Redis) + daily SMS cap (Postgres) + OTP cleanup
   job + error shape + client-IP fix. Real SMS via Global SMS (`SMS_PROVIDER=globalsms` in prod; `fake`
   locally and in CI). Verified in prod 2026-10-26: code SMS received, spoofed `X-Forwarded-For` ignored.
5. ✅ Signing keys + access tokens + `jwks.json` (built before step 4, so verify returns real tokens).
4. ✅ `POST /auth/otp/verify` + `POST /auth/register` (Telegram-like: a new phone gets a registration token
   and must send a display name before a user exists) + WhatsApp device model (one primary phone per user,
   a new SMS login replaces it; `devices.role`, migrations 0003 + 0004). Deployed 2026-10-29.
6. Refresh tokens, in two parts:
   - ✅ 6a: `refresh_tokens` (migration 0005), tokens returned at login/register, `POST /auth/token/refresh`
     (rotation, lost replies, reuse detection), cleanup. Deployed 2026-10-30.
   - ✅ 6b: access-token dependency (`CurrentDevice`), `POST /auth/logout`, `GET/DELETE /auth/devices`,
     revoked devices published in the shared Redis (db 2) for chat-service. Deployed 2026-11-02.
7. Observability, in two parts:
   - ✅ 7a: tracing (OpenTelemetry → Alloy → Tempo), `trace_id` on log lines. Deployed 2026-11-04.
   - 🔨 7b: metrics (`/metrics` for Prometheus).
8. Dummy Expo app against it (PLAN.md phase 8).
9. ✅ Real SMS provider: done as part of step 3 (Global SMS, per-provider settings in `config.py`).
10. Linking devices by QR (after step 6): `device_links`, `POST /auth/link/start` + `/link/approve`, up to 4
    `linked` devices; then decide whether a new primary login (or a primary logout) also signs out
    linked devices.

## 14. Deployment
- Push to `main` → GitHub Actions (ARM runner): checks → image
  `ghcr.io/jzviagin-chat-app/auth-service:sha-<12>` → a PR in `chat-infra` bumps `k8s/auth.yaml` →
  merging it deploys (Argo CD).
- The image is built in 2 stages: dependencies are installed with `uv sync --frozen` (exactly the lock
  file), and the runtime image contains only Python, `.venv`, our code and the migrations, and runs
  as an unprivileged user.
- `DATABASE_URL` comes from the Secret `auth-db` (namespace `default`, sealed in chat-infra). It's
  wired into `k8s/auth.yaml` in build step 2, together with a `migrate` init container that runs
  `alembic upgrade head` before the app starts. `alembic/env.py` retries the first connection (the
  NetworkPolicy blocks new pods for a few seconds) and holds a Postgres advisory lock, so two pods starting
  together never migrate at the same time.
