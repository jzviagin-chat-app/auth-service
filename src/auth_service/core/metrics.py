"""Metrics: counters and timings kept in memory, read by Prometheus from GET /metrics.

Traces (core/tracing.py) show what happened to one request; metrics are totals over time
(requests per minute, how slow, how many codes sent, how many logins failed), for dashboards and
later alerts. Prometheus (on the ops node) reads every pod's /metrics every 60 s (pull model,
like prom-client + an Express route in Node); the pod annotations in k8s/auth.yaml tell it where.

Each pod counts on its own (one uvicorn process per pod); Prometheus adds the pods up. Counters
only go up, and start from 0 when a pod restarts: queries use rate()/increase(), which handle that.

Labels only ever hold fixed values (a route template, a status, an outcome): never a phone number,
code, token or id. That keeps them private, and keeps the number of series (one per combination of
label values) small. tests/test_metrics.py checks both.
"""

import time

from prometheus_client import Counter, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Not counted: the probes (every few seconds) and Prometheus' own reads.
UNCOUNTED_PATHS = {"/healthz", "/auth/healthz", "/metrics"}
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests answered", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds", "Time to answer an HTTP request", ["method", "route"]
)

OTP_REQUESTS = Counter(
    "auth_otp_requests_total",
    "POST /auth/otp/request by outcome (sent, invalid_phone, rate_limited_<limit>, "
    "limiter_unavailable, daily_cap, send_failed)",
    ["outcome"],
)
SMS_SENT = Counter(
    "auth_sms_sent_total",
    "SMS send attempts by provider and result (ok, or the SmsSendError status)",
    ["provider", "result"],
)
LOGINS = Counter(
    "auth_logins_total",
    "otp/verify and register by outcome (logged_in, registration_required, registered, "
    "invalid_code, too_many_attempts, code_expired, blocked, already_registered, "
    "invalid_registration_token)",
    ["outcome"],
)
REFRESHES = Counter(
    "auth_refresh_total",
    "POST /auth/token/refresh by outcome (rotated, retried, reused, blocked, or the reject reason)",
    ["outcome"],
)
DEVICES_REVOKED = Counter(
    "auth_devices_revoked_total",
    "Devices signed out (counted after the commit), by reason",
    ["reason"],
)
JOB_RUNS = Counter(
    "auth_job_runs_total",
    "Rounds of the background loops (cleanup, revoked_resync) by result (ok, error)",
    ["job", "result"],
)


class MetricsMiddleware:
    """Counts and times every request by its route template (`/auth/devices/{id}`, never the real
    path with an id in it). A plain ASGI middleware (like an Express middleware, one level lower).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in UNCOUNTED_PATHS:
            await self.app(scope, receive, send)
            return
        status = 500  # if the app fails before answering
        start = time.perf_counter()

        async def send_and_remember(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_remember)
        finally:
            route = route_template(scope)
            method = scope["method"] if scope["method"] in METHODS else "OTHER"
            HTTP_REQUESTS.labels(method, route, str(status)).inc()
            HTTP_DURATION.labels(method, route).observe(time.perf_counter() - start)


def route_template(scope: Scope) -> str:
    """`/auth/devices/{device_id}` for `/auth/devices/0190…`. The router writes the matched route
    into `scope` (the same dict the middleware passed down). Its `path` (`/devices/{device_id}`) is
    the right end of the template but lacks the /auth prefix in this FastAPI version, so the same
    number of pieces is taken off the end of the real path and the route's path put in their place.
    The values are never looked at, so equal values can't confuse it. Assumes prefixes hold no
    parameters and no `{name:path}` parameter (one containing `/`): we have neither.
    No route matched (404): one shared value, not a series per URL."""
    route_path: str | None = getattr(scope.get("route"), "path", None)
    if route_path is None:
        return "unmatched"
    pieces = scope["path"].split("/")
    keep = len(pieces) - len(route_path.split("/")) + 1  # the prefix pieces: "", "auth"
    return "/".join(pieces[:keep]) + route_path
