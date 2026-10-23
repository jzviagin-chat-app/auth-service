"""Sending SMS. The rest of the code only knows the SmsSender interface; which provider is behind it
is a setting (SMS_PROVIDER). Like a TypeScript `interface SmsSender` with several implementations.

Logging rule: a provider's errors can contain the request (phone number + code), so senders raise
SmsSendError with a short status only, never the original error text.
"""

import logging
from typing import Protocol

from auth_service.config import Settings
from auth_service.core.masking import mask_phone

log = logging.getLogger(__name__)


class SmsSendError(Exception):
    """The provider refused or failed. `status` is safe to log (e.g. "http_500", "timeout")."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


class SmsSender(Protocol):
    name: str

    async def send(self, phone_e164: str, text: str) -> str:
        """Send `text` to the phone; return the provider's message id. Raises SmsSendError."""
        ...


class FakeSmsSender:
    """Sends nothing and costs nothing (development, tests, CI).

    With ENVIRONMENT=dev it logs the message text, so you can read the code on your machine.
    Never in prod (the default), where codes must not reach the logs.
    """

    name = "fake"

    def __init__(self, show_text: bool) -> None:
        self._show_text = show_text
        self._count = 0

    async def send(self, phone_e164: str, text: str) -> str:
        self._count += 1
        if self._show_text:
            log.info("fake sms (dev only)", extra={"phone": mask_phone(phone_e164), "text": text})
        return f"fake-{self._count}"


def make_sms_sender(settings: Settings) -> SmsSender:
    # Only "fake" exists for now; the real provider is added here once chosen.
    return FakeSmsSender(show_text=settings.environment == "dev")
