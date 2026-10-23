"""Small pure functions of the OTP flow (no database, no Redis)."""

import re

import pytest

from auth_service.core.masking import mask_phone
from auth_service.services.otp import clean_ip, hash_code, is_israeli_mobile, new_code, phone_ref

PEPPER = b"test-pepper"


def test_mask_phone() -> None:
    assert mask_phone("+972501234567") == "+97250***4567"
    assert mask_phone("+1234") == "***"  # too short to show anything


@pytest.mark.parametrize("phone", ["+972501234567", "+972549999999"])
def test_israeli_mobile_accepted(phone: str) -> None:
    assert is_israeli_mobile(phone)


@pytest.mark.parametrize(
    "phone",
    [
        "0501234567",  # local format
        "972501234567",  # no "+"
        "+972 50 123 4567",  # spaces
        "+97250123456",  # too short
        "+9725012345678",  # too long
        "+97221234567",  # landline (Jerusalem)
        "+14155550100",  # not Israel
        "+972" + "".join(chr(0x0660 + int(d)) for d in "501234567"),  # Arabic-Indic digits
        "+972501234567\n",  # trailing newline
    ],
)
def test_other_numbers_rejected(phone: str) -> None:
    assert not is_israeli_mobile(phone)


def test_new_code_is_six_digits() -> None:
    codes = {new_code() for _ in range(200)}
    assert all(re.fullmatch(r"[0-9]{6}", c) for c in codes)
    assert len(codes) > 150  # random, not a constant


def test_hash_code() -> None:
    h = hash_code(PEPPER, "+972501234567", "123456")
    assert len(h) == 32
    assert h == hash_code(PEPPER, "+972501234567", "123456")  # deterministic
    assert h != hash_code(PEPPER, "+972501234568", "123456")  # tied to the phone
    assert h != hash_code(b"other-pepper", "+972501234567", "123456")  # depends on the secret


def test_phone_ref_hides_the_number() -> None:
    ref = phone_ref(PEPPER, "+972501234567")
    assert "972501234567" not in ref
    assert ref == phone_ref(PEPPER, "+972501234567")
    assert ref != phone_ref(PEPPER, "+972501234568")


def test_clean_ip() -> None:
    assert clean_ip("147.235.199.4") == "147.235.199.4"
    assert clean_ip("2001:db8::1") == "2001:db8::1"
    assert clean_ip("testclient") is None
    assert clean_ip(None) is None
