"""Health checks (infrastructure, not features): Kubernetes probes and the ingress use them."""

from fastapi import APIRouter

from auth_service.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/healthz")  # Kubernetes probes, inside the cluster
async def healthz() -> dict[str, str | bool]:
    s = get_settings()
    return {"ok": True, "service": s.service_name, "instance": s.pod_name}


@router.get("/auth/healthz")  # the same check, reachable from outside through the ingress
async def public_healthz() -> dict[str, str | bool]:
    return await healthz()
