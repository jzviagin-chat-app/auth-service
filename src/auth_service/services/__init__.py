"""Business logic: plain async functions/classes, no HTTP and no SQL (they call db.repos).

Modules (README.md, "Layers"):
- otp.py         request codes: validation, hashing, the flow, cleanup job (verify: step 4)
- rate_limit.py  per-phone / per-IP limits in Redis
- sms.py         SmsSender interface + FakeSmsSender (real provider: once chosen)
- tokens.py      (planned) issue/verify JWTs, issue/rotate/revoke refresh tokens, reuse detection
- devices.py     (planned) register/list/remove devices
- keys.py        (planned) signing key pair management, JWKS document
"""
