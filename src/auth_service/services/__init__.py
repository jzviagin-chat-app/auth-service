"""Business logic: plain async functions/classes, no HTTP and no SQL (they call db.repos).

Modules (README.md, "Project layout"):
- otp.py           request codes: validation, hashing, the flow
- login.py         otp/verify: check a code -> logged in / registration required
- registration.py  registration token, register (new user + primary device)
- devices.py       device info, phones only, replace the primary, logout, list, remove
- refresh.py       refresh tokens: issue at login, rotate, lost replies, reuse detection
- revocation.py    signing a device out (the one place) + publishing it in the shared Redis, resync
- cleanup.py       hourly job: old OTP rows, long-revoked devices, old refresh tokens
- rate_limit.py    per-phone / per-IP limits in Redis
- keys.py          Ed25519 signing keys, kid, JWKS document
- tokens.py        issue / verify access tokens (JWT)
- sms.py           SmsSender interface, FakeSmsSender, GlobalSmsSender
"""
