"""The Global SMS sender and provider selection. No network: httpx.MockTransport answers instead of
the provider (like nock / msw in Node)."""

import contextlib
import json
import logging
import re

import httpx
import pytest
from pydantic import SecretStr

from auth_service.config import GlobalSmsSettings, Settings
from auth_service.core.logging import JsonFormatter, setup_logging
from auth_service.services.sms import (
    GLOBALSMS_SEND_PATH,
    FakeSmsSender,
    GlobalSmsSender,
    SmsSendError,
    make_sms_sender,
    parse_credits,
    read_answer,
    to_local_israeli,
)

API_KEY = "test-api-key-123"
BASE_URL = "https://sms.test"
PHONE = "+972501234567"


def make_sender(transport: httpx.MockTransport) -> GlobalSmsSender:
    return GlobalSmsSender(
        api_key=API_KEY,
        originator="MyApp",
        base_url=BASE_URL,
        timeout_seconds=5,
        transport=transport,
    )


def sender_answering(
    status: int = 200, text: str = "1", seen: list[httpx.Request] | None = None
) -> GlobalSmsSender:
    """A sender whose 'provider' answers with this status and text, recording the requests."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, text=text)

    return make_sender(httpx.MockTransport(handler))


def sender_raising(exc: type[httpx.HTTPError]) -> GlobalSmsSender:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc("simulated", request=request)

    return make_sender(httpx.MockTransport(handler))


async def send_status(sender: GlobalSmsSender) -> str:
    """Send once and return the SmsSendError status."""
    with pytest.raises(SmsSendError) as err:
        await sender.send(PHONE, "123456 is your verification code.")
    await sender.aclose()
    return err.value.status


# ---- success ----


async def test_request_sent_as_documented() -> None:
    seen: list[httpx.Request] = []
    sender = sender_answering(seen=seen)
    message_id = await sender.send(PHONE, "123456 is your verification code.")
    await sender.aclose()

    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == BASE_URL + GLOBALSMS_SEND_PATH  # the key is not in the URL
    assert json.loads(request.content) == {
        "ApiKey": API_KEY,
        "txtOriginator": "MyApp",
        "destinations": "0501234567",
        "txtSMSmessage": "123456 is your verification code.",
        "txtAddInf": message_id,
    }
    assert re.fullmatch(r"[0-9a-f]{14}", message_id)  # max 15 chars allowed by the provider


# The real answer of the REST endpoint on sapi (captured 2026-10-26).
REAL_SUCCESS = '{"success":true,"result":1,"resultJSON":"[]","errDesc":""}'


@pytest.mark.parametrize(
    "answer",
    [
        REAL_SUCCESS,
        '{"success":true,"result":2.5,"resultJSON":"[]","errDesc":""}',
        '{"success":true,"result":"1","errDesc":""}',
        '{"success":true}',  # sent, even without a cost: never report a sent SMS as failed
        '"1"',  # a JSON-quoted plain answer
        "1",  # the plain-text form from their docs
        "2.5",
        " 1\r\n",
    ],
)
async def test_numeric_answer_is_success(answer: str) -> None:
    sender = sender_answering(text=answer)
    assert await sender.send(PHONE, "hi")
    await sender.aclose()


# ---- provider errors ----


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        ("invalid login", "invalid_login"),
        ("Invalid Login", "invalid_login"),  # case doesn't matter
        ("e 1", "bad_key_format"),
        ("api-Not strongly identified", "not_strongly_identified"),
        ("Unapproved originator number", "unapproved_originator"),
        ("Unapporved originator number", "unapproved_originator"),  # their docs' spelling
        ("Originator Length Is 0", "bad_originator"),
        ("Originator Length Is Greater Than 11", "bad_originator"),
        ("empty message", "empty_message"),
        ("No valid mobile numbers", "no_valid_mobile_numbers"),
        ("Not enough credit in your account", "not_enough_credit"),
        ("system error", "system_error"),
        ("Wrong Date Format (dteToDeliver)", "bad_date"),
        # JSON failures carry the docs' error text in errDesc. The real answer to a wrong key
        # (captured 2026-10-26):
        ('{"success":false,"result":0,"resultJSON":"[]","errDesc":"e 1"}', "bad_key_format"),
        ('{"success":false,"result":0,"errDesc":"invalid login"}', "invalid_login"),
        ('{"success":false,"errDesc":"Not enough credit in your account"}', "not_enough_credit"),
        ('{"success":false,"errDesc":""}', "unexpected_response"),
        ('{"success":"yes","result":1}', "unexpected_response"),  # only a real true counts
        ("something new", "unexpected_response"),
        ("", "unexpected_response"),
        ("-1", "unexpected_response"),
        ("nan", "unexpected_response"),
    ],
)
async def test_error_answers(answer: str, status: str) -> None:
    assert await send_status(sender_answering(text=answer)) == status


@pytest.mark.parametrize(
    ("http_status", "status"),
    [(403, "http_403"), (400, "http_4xx"), (500, "http_5xx"), (502, "http_5xx")],
)
async def test_http_errors(http_status: int, status: str) -> None:
    assert await send_status(sender_answering(status=http_status, text="1")) == status


async def test_timeout() -> None:
    assert await send_status(sender_raising(httpx.ReadTimeout)) == "timeout"


async def test_network_error() -> None:
    assert await send_status(sender_raising(httpx.ConnectError)) == "network_error"


async def test_network_error_details_are_dropped() -> None:
    sender = sender_raising(httpx.ConnectError)
    with pytest.raises(SmsSendError) as err:
        await sender.send(PHONE, "hi")
    await sender.aclose()
    assert err.value.__suppress_context__  # httpx's exception (may contain the request) not chained


async def test_non_israeli_number_is_never_sent() -> None:
    seen: list[httpx.Request] = []
    sender = sender_answering(seen=seen)
    with pytest.raises(SmsSendError, match="unsupported_number"):
        await sender.send("+14155550100", "hi")
    await sender.aclose()
    assert seen == []  # no request reached the provider


# ---- helpers ----


def test_to_local_israeli() -> None:
    assert to_local_israeli("+972501234567") == "0501234567"


def test_parse_credits() -> None:
    assert parse_credits("1") == 1
    assert parse_credits("2.5") == 2.5
    assert parse_credits("invalid login") is None
    assert parse_credits("inf") is None


# ---- logs ----


async def test_logs_never_contain_key_phone_or_text(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fixed message id: a random hex id could contain "123456" by chance and fail the test.
    monkeypatch.setattr("auth_service.services.sms.secrets.token_hex", lambda _: "ffffffffffffff")
    caplog.set_level(logging.DEBUG)
    for sender in (
        sender_answering(text="1"),
        sender_answering(text="invalid login"),
        sender_answering(status=403),
        sender_raising(httpx.ConnectError),
    ):
        with contextlib.suppress(SmsSendError):  # = try/except SmsSendError: pass
            await sender.send(PHONE, "123456 is your verification code.")
        await sender.aclose()

    output = "\n".join(JsonFormatter("auth-service", "test").format(r) for r in caplog.records)
    assert "globalsms accepted" in output
    assert API_KEY not in output
    assert "501234567" not in output
    assert "123456" not in output


def test_httpx_request_lines_are_silenced() -> None:
    # httpx logs every request URL at INFO; a URL may carry an API key.
    setup_logging("DEBUG", "auth-service", "test")
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING


# ---- choosing the provider ----


async def test_factory_fake() -> None:
    assert isinstance(make_sms_sender(Settings(sms_provider="fake")), FakeSmsSender)


async def test_factory_globalsms() -> None:
    settings = Settings(
        sms_provider="globalsms",
        globalsms=GlobalSmsSettings(api_key=SecretStr("k"), originator="MyApp"),
    )
    sender = make_sms_sender(settings)
    assert isinstance(sender, GlobalSmsSender)
    await sender.aclose()


@pytest.mark.parametrize(
    "cfg",
    [
        GlobalSmsSettings(api_key=None, originator="MyApp"),
        GlobalSmsSettings(api_key=SecretStr("k"), originator=None),
        GlobalSmsSettings(api_key=SecretStr("k"), originator=""),
    ],
)
def test_factory_globalsms_not_configured(cfg: GlobalSmsSettings) -> None:
    # Never falls back to the fake sender: no sender -> /auth/otp/request answers 503.
    assert make_sms_sender(Settings(sms_provider="globalsms", globalsms=cfg)) is None


def test_daily_cap_is_the_active_providers() -> None:
    settings = Settings(sms_provider="globalsms", globalsms=GlobalSmsSettings(daily_cap=7))
    assert settings.sms_daily_cap == 7
    assert Settings(sms_provider="fake").sms_daily_cap == settings.fake_sms.daily_cap


def test_originator_max_11_chars() -> None:
    with pytest.raises(ValueError, match="at most 11 characters"):
        GlobalSmsSettings(originator="TwelveChars1")


def test_globalsms_defaults() -> None:
    # Plain provider settings live here, in code (only the API key comes from the cluster).
    cfg = GlobalSmsSettings()
    assert cfg.base_url == "https://sapi.itnewsletter.co.il"  # their host without an IP allow-list
    assert cfg.originator == "ZviaginChat"  # the approved sender


def test_read_answer() -> None:
    assert read_answer(REAL_SUCCESS) == (True, 1.0, "")
    assert read_answer("1") == (True, 1.0, "")
    assert read_answer(" invalid login ") == (False, None, "invalid login")
    failure = '{"success":false,"errDesc":"empty message"}'
    assert read_answer(failure) == (False, None, "empty message")


async def test_unexpected_answer_is_logged_safely(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    odd = '{"success":false,"errDesc":"weird thing for 0584654123"}'
    assert await send_status(sender_answering(text=odd)) == "unexpected_response"
    [record] = [r for r in caplog.records if r.getMessage() == "globalsms unexpected answer"]
    preview = record.__dict__["preview"]
    assert "weird thing" in preview  # enough to see what they answered...
    assert "0584654123" not in preview  # ...with long digit runs masked
