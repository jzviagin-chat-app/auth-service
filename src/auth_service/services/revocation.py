"""Signing a device out: the one place that does it (decided 2026-11-02, ARCHITECTURE §5.1).

    revoke_device(session, revoked, device_id, now, reason)   then the caller's session.commit()

A device is signed out (`devices.revoked_at`) on logout, when the primary removes a linked
device, when a new SMS login replaces the primary, and on refresh-token reuse. All of them call
revoke_device(); it never commits, because the revocation may be part of a larger transaction
(a login also creates the new device and its refresh token, all committed together).

Postgres is the truth: auth refuses a signed-out device's access token at once
(api/deps.CurrentDevice). chat-service can't read our database, so after the commit we also
write the device into the Redis db shared by the services (SHARED_REDIS_URL, db 2 in the
cluster). **auth only writes these keys; it never reads them.**

Keys (the contract chat-service reads):
  revoked:device:<device_id>  "1", expires at revoked_at + REVOKED_KEY_TTL_SECONDS (17 min): by
                              then every access token of the device has expired anyway
  revoked:synced              "1", rewritten by every pod each minute, expires after 3 minutes.
                              Missing = Redis lost its data (restart, flush), not yet resynced

How the Redis write follows the commit, with nothing for the caller to do:
  1. revoke_device() remembers the id in `session.info` (a dict SQLAlchemy keeps on every
     session for our own use, like `res.locals` in Express).
  2. SQLAlchemy calls our `after_commit` listener (below) once the transaction is committed.
     Listeners are plain functions (they can't `await`), so it starts a background task that
     writes the keys (3 quick tries, ~1 s in total). The reply doesn't wait for it.
  3. If the transaction is rolled back instead, the remembered ids are dropped: nothing is
     written.
A failed write never fails the request: the resync loop rewrites the keys of every recent
revocation each minute, which also refills Redis after a restart. Redis down: nothing is
published (chat-service accepts the risk).
"""

import asyncio
import logging
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from auth_service.db import repos
from auth_service.db.engine import Database

log = logging.getLogger(__name__)

KEY_PREFIX = "revoked:device:"
MARKER = "revoked:synced"
MARKER_TTL_SECONDS = 180
WRITE_TRIES = 3
WRITE_BUDGET_SECONDS = 1.0  # all tries together, so a dead Redis can't pile up slow tasks
PENDING = "revoked_devices"  # our key in session.info: (RevokedDevices, [device ids])


def device_key(device_id: uuid.UUID) -> str:
    return f"{KEY_PREFIX}{device_id}"


class RevokedDevices:
    """Writes revoked devices to the shared Redis. With no Redis configured, does nothing."""

    def __init__(self, redis: Redis | None, ttl: timedelta) -> None:
        self.redis = redis
        self.ttl = ttl
        # Running publish tasks. asyncio keeps only a weak reference to a task, so without this
        # set a task could be garbage-collected before it finishes.
        self._tasks: set[asyncio.Task[None]] = set()

    def _expires_in_ms(self, revoked_at: datetime, now: datetime) -> int:
        return int((revoked_at + self.ttl - now).total_seconds() * 1000)

    def publish_in_background(self, devices: list[tuple[uuid.UUID, datetime]]) -> None:
        """Start writing the keys without waiting (called from the after_commit listener)."""
        if self.redis is None or not devices:
            return
        self._start(self.publish(devices))

    def _start(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for the running publish tasks (shutdown, tests)."""
        while self._tasks:
            await asyncio.gather(*self._tasks)

    async def publish(self, devices: list[tuple[uuid.UUID, datetime]]) -> None:
        """Write the keys of (device_id, revoked_at) pairs. Never raises."""
        if self.redis is None:
            return
        now = datetime.now(UTC)
        pending = [(d, ms) for d, at in devices if (ms := self._expires_in_ms(at, now)) > 0]
        try:
            async with asyncio.timeout(WRITE_BUDGET_SECONDS):
                for attempt in range(WRITE_TRIES):
                    try:
                        async with self.redis.pipeline(transaction=False) as pipe:
                            for device_id, expires_ms in pending:
                                pipe.set(device_key(device_id), "1", px=expires_ms)
                            await pipe.execute()
                        return
                    except RedisError:
                        if attempt < WRITE_TRIES - 1:
                            await asyncio.sleep(0.1)
        except TimeoutError:
            pass
        log.warning(
            "revoked device not written to redis; the resync will",
            extra={"device_ids": [str(d) for d, _ in pending]},
        )

    async def resync(self, db: Database) -> int:
        """Rewrite the key of every device revoked within the TTL, then the marker. Returns how
        many keys were written. Raises RedisError (the loop logs it)."""
        if self.redis is None:
            return 0
        now = datetime.now(UTC)
        async with db.session() as session:
            revoked = await repos.devices_revoked_since(session, now - self.ttl)
        async with self.redis.pipeline(transaction=False) as pipe:
            written = 0
            for device_id, revoked_at in revoked:
                expires_ms = self._expires_in_ms(revoked_at, now)
                if expires_ms > 0:
                    pipe.set(device_key(device_id), "1", px=expires_ms)
                    written += 1
            pipe.set(MARKER, "1", ex=MARKER_TTL_SECONDS)  # last: the keys are in place first
            await pipe.execute()
        return written


async def revoke_device(
    session: AsyncSession,
    revoked: RevokedDevices,
    device_id: uuid.UUID,
    now: datetime,
    reason: str,
) -> bool:
    """Sign the device out in Postgres; False if it already was (or doesn't exist). The caller
    commits; the Redis write follows the commit by itself. `reason` is for the logs."""
    if not await repos.revoke_device(session, device_id, now):
        return False
    log.info("device signed out", extra={"device_id": str(device_id), "reason": reason})
    if PENDING not in session.info:
        session.info[PENDING] = (revoked, [])
    session.info[PENDING][1].append((device_id, now))
    return True


# Registered once, when this module is imported, for every session (AsyncSession runs a plain
# `Session` underneath, and that is what the events fire on; both share the same `info` dict).


@event.listens_for(Session, "after_commit")
def _publish_after_commit(session: Session) -> None:
    """Fires on every commit, also when a savepoint is released (tested). Only the real one
    counts: while a savepoint's commit runs, the session is still in a nested transaction."""
    if session.in_nested_transaction():
        return  # a savepoint was released; the outer transaction can still roll back
    pending = session.info.pop(PENDING, None)
    if pending is not None:
        revoked, devices = pending
        revoked.publish_in_background(devices)


@event.listens_for(Session, "after_soft_rollback")
def _forget_after_rollback(session: Session, previous_transaction: SessionTransaction) -> None:
    """The transaction was rolled back: those revocations never happened. A rolled-back savepoint
    (replace_primary's retry) doesn't count: revocations are never made inside one."""
    if previous_transaction.parent is None:
        session.info.pop(PENDING, None)


async def resync_loop(db: Database, revoked: RevokedDevices, interval_seconds: int) -> None:
    """Runs for the life of the process (started in main.lifespan); every pod runs it."""
    while True:
        try:
            await revoked.resync(db)
        except Exception:
            # Keep the loop alive (Redis or Postgres restarting); try again next round.
            log.exception("revoked devices resync failed")
        await asyncio.sleep(interval_seconds)
