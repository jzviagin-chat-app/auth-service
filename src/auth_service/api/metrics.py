"""GET /metrics for Prometheus (core/metrics.py). At the root, not under /auth: the ingress sends
only /auth/* to this service, so it is reachable inside the cluster only."""

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["metrics"])


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    # Formats the in-memory values: fast, no I/O, so fine on the event loop.
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
