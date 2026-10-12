"""SMS one-time codes. Planned endpoints (README.md, "API"):

POST /auth/otp/request   {phone}                 -> send a code (rate limited); always 202
POST /auth/otp/verify    {phone, code, device}   -> create/find user + device, return tokens
"""

from fastapi import APIRouter

router = APIRouter(prefix="/otp", tags=["otp"])
