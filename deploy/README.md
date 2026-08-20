# Production deployment

This directory contains the Ubuntu deployment assets for the personal assistant.

```bash
cd /home/ubuntu/code/ai-agent
./deploy/bootstrap_env.sh
# Edit deploy/.env to configure the real model provider and domain.
./deploy/deploy.sh
```

The base deployment publishes the user application on `0.0.0.0:18000`, the
operations console on `0.0.0.0:19000`, and starts PostgreSQL and Redis. It does
not start MinIO, Neo4j, or Caddy. This lets it run next to
the existing `deploy-*` containers without a port cutover. MinIO is kept in the
`storage` profile because the current API does not expose document uploads yet.

The cloud security group must allow TCP `18000` for users and TCP `19000` for
operators. Restrict `19000` to trusted source IPs whenever possible. Configure
HTTPS before production use because the initial IP-based deployment uses HTTP.

Do not enable the `graph` profile on the current 3.6 GiB server while the old
stack is still running. Do not enable `ingress` until a real domain has been
configured and ports 80/443 are confirmed free.

When document ingestion is implemented, enable object storage with:

```bash
docker compose --project-name personal-ai-assistant --env-file deploy/.env -f deploy/compose.yaml --profile storage up -d minio
```

Useful commands:

```bash
docker compose --project-name personal-ai-assistant --env-file deploy/.env -f deploy/compose.yaml ps
docker compose --project-name personal-ai-assistant --env-file deploy/.env -f deploy/compose.yaml logs -f assistant-api
curl --fail http://127.0.0.1:18000/api/v1/health/ready
curl --fail http://127.0.0.1:19000/api/v1/health/ready
```
