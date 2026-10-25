"""Sending SMS. The rest of the code only knows the SmsSender interface; which provider is behind it
is a setting (SMS_PROVIDER). Like a TypeScript `interface SmsSender` with several implementations.

Adding a provider: a class with `name`, `send()` and `aclose()`, its settings block in config.py,
and one `case` in make_sms_sender(). Nothing else changes.

Logging rule: a provider's errors can contain the request (API key, phone number, code), so senders
raise SmsSendError with a short, fixed status only, never the original error text.
"""

import logging
import math
import secrets
from typing import Protocol

import httpx

from auth_service.config import Settings
from auth_service.core.masking import mask_phone

log = logging.getLogger(__name__)


class SmsSendError(Exception):
    """The provider refused or failed. `status` is safe to log (e.g. "http_403", "timeout")."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


class SmsSender(Protocol):
    """Any class with these members is an SmsSender (structural typing: no `implements` needed)."""

    name: str

    async def send(self, phone_e164: str, text: str) -> str:
        """Send `text` to the phone; return a message id for the logs. Raises SmsSendError."""
        ...

    async def aclose(self) -> None:
        """Release connections (called once at shutdown)."""
        ...


# ---- fake ----


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

    async def aclose(self) -> None:
        return None


# ---- Global SMS (api.itnewsletter.co.il) ----

GLOBALSMS_SEND_URL = "https://api.itnewsletter.co.il/api/restApiSms/sendSmsToRecipients"

# The provider answers in plain text: a number (credits charged) on success, otherwise one of these.
# Keys are lower-cased; values are our status codes, safe to log.
_GLOBALSMS_ERRORS = {
    "invalid login": "invalid_login",
    "e 1": "bad_key_format",
    "api-not strongly identified": "not_strongly_identified",  # account KYC not completed
    "unapproved originator number": "unapproved_originator",
    "unapporved originator number": "unapproved_originator",  # spelled like this in their docs
    "originator length is 0": "bad_originator",
    "originator length is greater than 11": "bad_originator",
    "empty message": "empty_message",
    "no valid mobile numbers": "no_valid_mobile_numbers",
    "not enough credit in your account": "not_enough_credit",
    "system error": "system_error",
    "wrong date format (dtetodeliver)": "bad_date",
}


def to_local_israeli(phone_e164: str) -> str:
    """'+972501234567' -> '0501234567', the recipient format in Global SMS's docs example."""
    if not phone_e164.startswith("+972"):
        raise SmsSendError("unsupported_number")
    return "0" + phone_e164.removeprefix("+972")


def parse_credits(text: str) -> float | None:
    """The success answer: credits charged ("1", "2.5"). None if `text` isn't a number."""
    try:
        credits = float(text)
    except ValueError:
        return None
    return credits if math.isfinite(credits) and credits >= 0 else None


class GlobalSmsSender:
    """Sends through Global SMS's REST API. JSON body: the API key travels in the body, not the URL.

    One HTTP client (connection pool) for the life of the process, like a shared axios instance.
    Their API only accepts calls from approved IP addresses (HTTP 403 otherwise).
    """

    name = "globalsms"

    def __init__(
        self,
        api_key: str,
        originator: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,  # tests pass a mock here
    ) -> None:
        self._api_key = api_key
        self._originator = originator
        self._client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)

    async def send(self, phone_e164: str, text: str) -> str:
        # Our own id for this message (max 15 chars allowed); it appears as "Inf" in their
        # delivery reports, so a report row can be matched to our "sms sent" log line.
        message_id = secrets.token_hex(7)
        payload = {
            "ApiKey": self._api_key,
            "txtOriginator": self._originator,
            "destinations": to_local_israeli(phone_e164),
            "txtSMSmessage": text,
            "txtAddInf": message_id,
        }
        # `from None` drops httpx's original exception, whose text can include the request.
        try:
            response = await self._client.post(GLOBALSMS_SEND_URL, json=payload)
        except httpx.TimeoutException:
            raise SmsSendError("timeout") from None
        except httpx.HTTPError:
            raise SmsSendError("network_error") from None

        if response.status_code == 403:
            raise SmsSendError("http_403")  # caller IP not approved, or no API permission
        if response.status_code != 200:
            raise SmsSendError(f"http_{response.status_code // 100}xx")

        answer = response.text.strip()
        credits = parse_credits(answer)
        if credits is None:
            raise SmsSendError(_GLOBALSMS_ERRORS.get(answer.lower(), "unexpected_response"))
        log.info("globalsms accepted", extra={"message_id": message_id, "credits": credits})
        return message_id

    async def aclose(self) -> None:
        await self._client.aclose()


# ---- choosing the provider ----


def make_sms_sender(settings: Settings) -> SmsSender | None:
    """The one place that knows the provider names. None = not configured: /auth/otp/request then
    answers 503 (we never silently fall back to the fake sender)."""
    match settings.sms_provider:
        case "fake":
            return FakeSmsSender(show_text=settings.environment == "dev")
        case "globalsms":
            cfg = settings.globalsms
            if cfg.api_key is None or not cfg.originator:
                log.error(
                    "globalsms not configured (GLOBALSMS_API_KEY or originator missing): "
                    "/auth/otp/request will answer 503"
                )
                return None
            return GlobalSmsSender(
                cfg.api_key.get_secret_value(), cfg.originator, cfg.timeout_seconds
            )
