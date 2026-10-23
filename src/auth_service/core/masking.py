"""Masking for logs: full phone numbers never reach the logs (README, "Security rules")."""


def mask_phone(phone: str) -> str:
    """'+972501234567' -> '+97250***4567': enough to recognise a number while debugging."""
    if len(phone) < 10:
        return "***"
    return f"{phone[:6]}***{phone[-4:]}"
