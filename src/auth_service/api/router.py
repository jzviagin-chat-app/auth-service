"""All feature routes, mounted under /auth (the ingress sends /auth/* to this service)."""

from fastapi import APIRouter

from auth_service.api import devices, jwks, otp, register, tokens

api_router = APIRouter(prefix="/auth")
for module in (otp, register, tokens, devices, jwks):
    api_router.include_router(module.router)
