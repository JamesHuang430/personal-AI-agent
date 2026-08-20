# Current deployment

- Host: `VM-0-8-ubuntu`
- Project path: `/home/ubuntu/code/ai-agent`
- Compose project: `personal-ai-assistant`
- Application image: `personal-ai-assistant-api:0.1.0`
- HTTPS ingress: Nginx on `0.0.0.0:18000` and `0.0.0.0:19000`
- Internal upstreams: `assistant-api:8000` and `admin-api:8000`
- Running profiles: base only
- Base services: `nginx`, `assistant-api`, `admin-api`, `postgres`, `redis`
- Deferred profiles: `storage` (MinIO), `graph` (Neo4j)
- Alembic head: `20260820_0005`
- pgvector: enabled

## Legacy service status

Stopped on 2026-08-20 after the new base stack passed its health checks:

- `deploy-api-1` — exited with code 0
- `deploy-web-1` — exited with code 0
- `deploy-mailpit-1` — exited with code 0

The old `deploy-mongo-1` and `deploy-redis-1` containers remain healthy because
their data and other consumers have not yet been audited. No legacy container,
image, volume, or database has been deleted. The stopped services can be
restored with `docker start deploy-api-1 deploy-web-1 deploy-mailpit-1`.

Verification after cutover:

- API liveness: OK
- PostgreSQL and Redis readiness: OK
- Alembic head: `20260820_0005`
- pgvector: `0.8.6`
- Nginx is the only public Web entry point. Application APIs, PostgreSQL and
  Redis remain internal.

## Update command

```bash
cd /home/ubuntu/code/ai-agent
./deploy/deploy.sh
```

The deployment script generates a self-signed certificate when needed,
validates Compose, rebuilds the application image, starts the base stack,
applies Alembic migrations, and waits for the HTTPS readiness endpoint.
