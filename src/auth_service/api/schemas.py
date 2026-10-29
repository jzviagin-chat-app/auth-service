"""Request/response bodies shared by several routes."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from auth_service.services.devices import DeviceInfo, LoggedIn


class DeviceIn(BaseModel):
    """The app installation that is logging in (one `devices` row per login). SMS login takes only
    ios/android (answered 400 unsupported_platform); "web" is for linked devices later."""

    platform: Literal["ios", "android", "web"]
    name: str | None = Field(default=None, max_length=100, examples=["Jacob's iPhone"])

    def to_info(self) -> DeviceInfo:
        return DeviceInfo(platform=self.platform, name=self.name)


class LoggedInOut(BaseModel):
    """Refresh tokens come in build step 6 (this gains `refresh_token` then)."""

    status: Literal["logged_in"] = "logged_in"
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 (the scheme name, not a secret)
    expires_in: int  # seconds
    user_id: uuid.UUID
    device_id: uuid.UUID

    @classmethod
    def from_result(cls, r: LoggedIn) -> LoggedInOut:
        return cls(
            access_token=r.access_token,
            expires_in=r.expires_in,
            user_id=r.user_id,
            device_id=r.device_id,
        )
