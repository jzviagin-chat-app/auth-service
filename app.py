import os

from fastapi import FastAPI

# Placeholder: proves the build/deploy pipeline works. Login, devices and tokens come next.
app = FastAPI()
POD_NAME = os.environ.get("POD_NAME", "local")


@app.get("/healthz")  # used by Kubernetes probes (inside the cluster)
def healthz():
    return {"ok": True, "service": "auth-service", "instance": POD_NAME}


@app.get("/auth/healthz")  # the same check, reachable from outside through the ingress
def public_healthz():
    return healthz()
