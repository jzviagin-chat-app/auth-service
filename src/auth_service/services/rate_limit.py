"""Per-phone and per-IP limits for sending codes, counted in Redis (PLAN.md, decided 2026-10-21).

Fixed windows: each counter is a Redis key that INCR creates and that expires after its window
(EXPIRE ... NX sets the expiry only when the key is new). Every group of commands runs as one
MULTI/EXEC transaction, so two pods can't interleave between "count" and "set expiry".
Like `rate-limiter-flexible` on ioredis in Node, written out by hand.

Keys (Redis db 1 is auth's; all keys start with "auth:rl:"):
  auth:rl:phone:<phone hmac>:cooldown   one code per phone per cooldown (SET NX EX)
  auth:rl:phone:<phone hmac>:h / :d     codes per phone per hour / day
  auth:rl:ip:<ip>:h / :d                codes per client IP per hour / day

The phone appears only as an HMAC (see otp.phone_ref): no phone numbers in a Redis that chat-service
also uses, and none in Redis error messages.

Redis down -> LimitsUnavailable: the caller refuses to send codes (fail closed).
"""

import logging
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

HOUR = 3600
DAY = 86400


@dataclass(frozen=True)
class Limits:
    cooldown_seconds: int
    phone_per_hour: int
    phone_per_day: int
    ip_per_hour: int
    ip_per_day: int


class RateLimited(Exception):
    def __init__(self, limit: str, retry_after: int) -> None:
        super().__init__(limit)
        self.limit = limit  # which limit, e.g. "phone_hour" (for logs)
        self.retry_after = retry_after  # seconds until it resets (Retry-After header)


class LimitsUnavailable(Exception):
    """Redis is unreachable or failed: we can't check the limits."""


async def check_and_count(redis: Redis, limits: Limits, phone_ref: str, ip: str) -> None:
    """Raise RateLimited if this phone/IP may not get a code now; otherwise count this request."""
    try:
        await _check_and_count(redis, limits, phone_ref, ip)
    except RedisError as e:
        raise LimitsUnavailable(type(e).__name__) from e


async def _check_and_count(redis: Redis, limits: Limits, phone_ref: str, ip: str) -> None:
    phone_key = f"auth:rl:phone:{phone_ref}"

    # 1. Cooldown first, on its own: a quick "resend" tap doesn't use up the hourly/daily quota.
    #    It starts even if step 2 then rejects the request, on purpose: retries during that minute
    #    stop here, so hammering the button can't keep counting against the phone's or IP's limits.
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(f"{phone_key}:cooldown", 1, nx=True, ex=limits.cooldown_seconds)
        pipe.ttl(f"{phone_key}:cooldown")
        was_set, ttl = await pipe.execute()
    if not was_set:
        raise RateLimited("phone_cooldown", max(int(ttl), 1))

    # 2. Count this request in every window, then see whether any went over.
    windows = [
        ("phone_hour", f"{phone_key}:h", HOUR, limits.phone_per_hour),
        ("phone_day", f"{phone_key}:d", DAY, limits.phone_per_day),
        ("ip_hour", f"auth:rl:ip:{ip}:h", HOUR, limits.ip_per_hour),
        ("ip_day", f"auth:rl:ip:{ip}:d", DAY, limits.ip_per_day),
    ]
    async with redis.pipeline(transaction=True) as pipe:
        for _, key, seconds, _ in windows:
            pipe.incr(key)
            pipe.expire(key, seconds, nx=True)
            pipe.ttl(key)
        results = await pipe.execute()

    exceeded = []
    for i, (name, _, seconds, maximum) in enumerate(windows):
        count, _, ttl = results[i * 3 : i * 3 + 3]
        if int(count) > maximum:
            exceeded.append((name, int(ttl) if int(ttl) > 0 else seconds))
    if exceeded:
        # Wait for the window that resets last, otherwise the next try fails again.
        name, retry_after = max(exceeded, key=lambda x: x[1])
        raise RateLimited(name, retry_after)
