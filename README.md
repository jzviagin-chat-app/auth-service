# auth-service

Identity for the chat app: users, devices, login, tokens (coming next). For now a placeholder
with health endpoints, to prove the build/deploy pipeline.

- `GET /healthz` — Kubernetes probes (inside the cluster)
- `GET /auth/healthz` — same, from outside: `curl http://<lb_public_ip>/auth/healthz`

Deploys exactly like chat-service: push to `main` → GitHub Actions builds
`ghcr.io/jzviagin-chat-app/auth-service:sha-<commit>` → opens a "Deploy auth-service sha-…"
PR in chat-infra → merge it → Argo CD rolls it out.

Needs the `INFRA_REPO_TOKEN` secret (organization secret shared with chat-service).
