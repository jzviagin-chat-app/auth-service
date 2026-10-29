"""Table definitions (SQLAlchemy 2.0 typed ORM). Alembic generates migrations from these.

Conventions:
- Primary keys are UUIDv7: random enough to be unguessable, but time-ordered, so new rows land at
  the end of the index (like an auto-increment) instead of all over it.
- All timestamps are `timestamptz` (stored in UTC), set by Postgres (`now()`).
- `Mapped[X | None]` means the column is nullable; `Mapped[X]` means NOT NULL.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Predictable constraint names, so Alembic migrations can rename/drop them reliably.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# E.164: "+", country code (no leading 0), up to 15 digits in total. Example: +972501234567
E164_REGEX = r"^\+[1-9][0-9]{6,14}$"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """A person, identified by their phone number."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(f"phone_e164 ~ '{E164_REGEX}'", name="phone_e164_format"),
        CheckConstraint("status IN ('active', 'blocked')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    phone_e164: Mapped[str] = mapped_column(String(16), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(50))  # set during onboarding
    # Object-storage key of the profile picture, e.g. "avatars/<user_id>/<uuid>.jpg" (not a URL:
    # the bucket is private; clients get short-lived signed URLs generated from this key).
    avatar_key: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    created_at: Mapped[datetime] = _now()

    devices: Mapped[list[Device]] = relationship(back_populates="user")


class Device(Base):
    """One installed app. WhatsApp model (PLAN.md section 6): one `primary` device per user, a
    phone logged in by SMS; a new SMS login revokes the old primary. `linked` (QR) in step 10."""

    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint("platform IN ('ios', 'android', 'web')", name="platform_valid"),
        CheckConstraint("role IN ('primary', 'linked')", name="role_valid"),
        # At most one active primary per user, enforced by Postgres: two SMS logins racing can't
        # both win (the second INSERT fails). Revoked or linked rows don't count.
        Index(
            "uq_devices_one_active_primary",
            "user_id",
            unique=True,
            postgresql_where=text("role = 'primary' AND revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(16))
    role: Mapped[str] = mapped_column(String(16), server_default="primary")
    name: Mapped[str | None] = mapped_column(String(100))  # e.g. "Jacob's iPhone"
    created_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # signed out

    user: Mapped[User] = relationship(back_populates="devices")


class OtpChallenge(Base):
    """One SMS code sent to a phone number. Valid for a few minutes, a few attempts, once."""

    __tablename__ = "otp_challenges"
    __table_args__ = (
        CheckConstraint(f"phone_e164 ~ '{E164_REGEX}'", name="phone_e164_format"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        # "latest challenge for this phone" is the hot query
        Index("ix_otp_challenges_phone_created", "phone_e164", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    phone_e164: Mapped[str] = mapped_column(String(16))
    code_hash: Mapped[bytes] = mapped_column(LargeBinary(32))  # HMAC-SHA256, never the code
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_ip: Mapped[str | None] = mapped_column(INET)
    # Set when a correct code arrives for a phone with no account yet: SHA-256 of the registration
    # token the app must send to POST /auth/register (with a display name). Cleared once used.
    registration_token_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), unique=True)
    registration_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SmsDailyUsage(Base):
    """How many SMS were sent on each UTC day: the global daily cap, a money guard.

    Kept in Postgres (not Redis) so a restart can't reset it, and counted with one atomic
    UPDATE ... WHERE sent < cap, so two pods racing can never go over the cap.
    """

    __tablename__ = "sms_daily_usage"
    __table_args__ = (CheckConstraint("sent >= 0", name="sent_non_negative"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)  # UTC
    sent: Mapped[int] = mapped_column(Integer, server_default="0")
