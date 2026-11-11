"""Per-phone and per-IP limits for sending codes, counted in Redis (PLAN.md, decided 2026-10-21).

Fixed windows: each counter is a Redis key that INCR creates and that expires after its window
(EXPIRE ... NX sets the expiry only when the key is new), so it starts at the first request and is
deleted as a whole when the window ends. Like `rate-limiter-flexible` on ioredis in Node, written
out by hand.

Check first, count only what is sent (decided 2026-11-11): one Lua script checks the cooldown and
every window, and only if nothing is full starts the cooldown and counts the request everywhere.
A refused request changes nothing, so tapping "resend" while blocked can't use up the quota.
Redis runs a script as one step (nothing else in between), so two pods can't both take the
last slot (like a database transaction).

Keys (Redis db 1 is auth's; all keys start with "auth:rl:"):
  auth:rl:phone:<phone hmac>:cooldown   one code per phone per cooldown
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
        self.limit = limit  # resend_cooldown, phone_hour, phone_day, ip_hour or ip_day (public)
        self.retry_after = retry_after  # seconds until it resets (Retry-After header)


class LimitsUnavailable(Exception):
    """Redis is unreachable or failed: we can't check the limits."""


async def check_and_count(redis: Redis, limits: Limits, phone_ref: str, ip: str) -> int:
    """Raise RateLimited if this phone/IP may not get a code now; otherwise count this request
    and return the seconds until the next one may be requested."""
    try:
        return await _check_and_count(redis, limits, phone_ref, ip)
    except RedisError as e:
        raise LimitsUnavailable(type(e).__name__) from e


# KEYS[1] = the cooldown key, KEYS[2..5] = the windows. ARGV[1] = cooldown seconds, then for
# window j (1..4): ARGV[2j] = its length in seconds, ARGV[2j+1] = its maximum.
# Refused: {0, wait} for the cooldown, {j, wait} for window j: whichever frees up last, so the
# wait is the real one (a running cooldown doesn't hide a full window behind it).
# Allowed (and counted): {-1, next}, next = seconds until another code is possible: the cooldown,
# or longer when this code used a window's last slot.
# A Redis Cluster would need every key on one node (a shared {hash tag} in the key names).
_CHECK_AND_COUNT = """
local blocker, wait = -1, 0
local cooldown = redis.call('TTL', KEYS[1])
if cooldown ~= -2 then
  blocker, wait = 0, math.max(cooldown, 1)
end
for j = 1, #KEYS - 1 do
  local key, seconds = KEYS[j + 1], tonumber(ARGV[2 * j])
  local count = tonumber(redis.call('GET', key) or '0')
  if count >= tonumber(ARGV[2 * j + 1]) then
    local ttl = redis.call('TTL', key)
    if ttl < 1 then ttl = seconds end
    if ttl > wait then blocker, wait = j, ttl end
  end
end
if blocker ~= -1 then
  return {blocker, wait}
end
redis.call('SET', KEYS[1], 1, 'EX', ARGV[1])
local next_ok = tonumber(ARGV[1])
for j = 1, #KEYS - 1 do
  local key = KEYS[j + 1]
  local count = redis.call('INCR', key)
  redis.call('EXPIRE', key, ARGV[2 * j], 'NX')
  if count >= tonumber(ARGV[2 * j + 1]) then
    next_ok = math.max(next_ok, redis.call('TTL', key))
  end
end
return {-1, next_ok}
"""


# Takes back what _CHECK_AND_COUNT counted for a request that then sent nothing (same KEYS):
# the cooldown goes, and each window goes down by 1 (a window back at 0 is deleted).
_UNCOUNT = """
redis.call('DEL', KEYS[1])
for j = 2, #KEYS do
  if redis.call('EXISTS', KEYS[j]) == 1 and redis.call('DECR', KEYS[j]) <= 0 then
    redis.call('DEL', KEYS[j])
  end
end
return 0
"""


def _cooldown_key(phone_ref: str) -> str:
    return f"auth:rl:phone:{phone_ref}:cooldown"


def _windows(limits: Limits, phone_ref: str, ip: str) -> list[tuple[str, str, int, int]]:
    """(name, key, seconds, maximum) of the 4 windows."""
    phone_key = f"auth:rl:phone:{phone_ref}"
    return [
        ("phone_hour", f"{phone_key}:h", HOUR, limits.phone_per_hour),
        ("phone_day", f"{phone_key}:d", DAY, limits.phone_per_day),
        ("ip_hour", f"auth:rl:ip:{ip}:h", HOUR, limits.ip_per_hour),
        ("ip_day", f"auth:rl:ip:{ip}:d", DAY, limits.ip_per_day),
    ]


async def _check_and_count(redis: Redis, limits: Limits, phone_ref: str, ip: str) -> int:
    windows = _windows(limits, phone_ref, ip)
    args: list[int] = [limits.cooldown_seconds]
    for _, _, seconds, maximum in windows:
        args += [seconds, maximum]
    script = redis.register_script(_CHECK_AND_COUNT)  # sent by its hash; the text only once
    blocker, wait = await script(
        keys=[_cooldown_key(phone_ref), *(key for _, key, _, _ in windows)], args=args
    )
    if blocker == 0:
        raise RateLimited("resend_cooldown", int(wait))
    if blocker > 0:
        raise RateLimited(windows[blocker - 1][0], int(wait))
    return int(wait)  # allowed: when the next code may be requested


async def uncount(redis: Redis, limits: Limits, phone_ref: str, ip: str) -> None:
    """Take back a counted request that sent nothing because the global daily SMS cap was full,
    so a day with the cap reached doesn't use up anyone's own limits. Not for a failed send: the
    SMS may have gone out anyway (a timeout), so that one stays counted. A Redis error is only
    logged: the request is refused either way."""
    keys = [_cooldown_key(phone_ref), *(key for _, key, _, _ in _windows(limits, phone_ref, ip))]
    try:
        await redis.register_script(_UNCOUNT)(keys=keys)
    except RedisError as e:
        log.warning("rate limit uncount failed", extra={"error_type": type(e).__name__})


async def cooldown_left(redis: Redis, phone_ref: str) -> int | None:
    """Seconds until this phone may request a new code (0: now). Only reads; None if Redis fails."""
    try:
        ttl = int(await redis.ttl(_cooldown_key(phone_ref)))
    except RedisError as e:
        log.warning("cooldown unreadable", extra={"error_type": type(e).__name__})
        return None
    return max(ttl, 0)  # -2: no key (over), -1: no expiry (never set by us)
