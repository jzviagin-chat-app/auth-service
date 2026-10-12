"""Business logic: plain async functions/classes, no HTTP and no SQL (they call db.repos).

Planned modules (README.md, "Layers"):
- otp.py      create/verify codes: hashing, expiry, attempt limits
- tokens.py   issue/verify JWTs, issue/rotate/revoke refresh tokens, reuse detection
- devices.py  register/list/remove devices
- sms.py      SmsSender interface + FakeSmsSender (dev, free) + TwilioSmsSender (later, flagged)
- keys.py     signing key pair management, JWKS document
"""
