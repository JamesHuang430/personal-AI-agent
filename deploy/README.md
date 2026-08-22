# Production deployment

This directory contains the Ubuntu deployment assets for the personal assistant.

```bash
cd /home/ubuntu/code/ai-agent
./deploy/bootstrap_env.sh
# Edit deploy/.env and configure the production secrets and public IP.
./deploy/deploy.sh
```

The deployment starts PostgreSQL, Redis, the user API, the operations API and
Nginx. Only Nginx publishes host ports: HTTPS `18000` for the user application
and HTTPS `19000` for the operations console. Backend services stay on the
internal Compose network. pgvector and Apache AGE run inside PostgreSQL; MinIO
remains an optional profile.

The cloud security group must allow TCP `18000` for users and TCP `19000` for
operators. Restrict `19000` to trusted source IPs whenever possible.

After the first operations login, open **邮件服务** and configure the sender
mailbox, SMTP host/port, authorization code and TLS mode. The authorization
code is encrypted in PostgreSQL and is never returned by the API. Use the test
mail action before enabling public registration and password reset.

`deploy/deploy.sh` creates an IP-aware self-signed certificate in
`deploy/certs/` when no certificate exists. Browsers will warn until the public
certificate is explicitly trusted. Replace it with a CA-issued certificate as
soon as a domain is available; never commit the private key.

Knowledge-graph queries do not expose a separate database port. All access goes
through the authenticated application API and is scoped to the current user.

When document ingestion is implemented, enable object storage with:

```bash
docker compose --project-name personal-ai-assistant --env-file deploy/.env -f deploy/compose.yaml --profile storage up -d minio
```

Useful commands:

```bash
docker compose --project-name personal-ai-assistant --env-file deploy/.env -f deploy/compose.yaml ps
docker compose --project-name personal-ai-assistant --env-file deploy/.env -f deploy/compose.yaml logs -f assistant-api
curl --insecure --fail https://127.0.0.1:18000/api/v1/health/ready
curl --insecure --fail https://127.0.0.1:19000/api/v1/health/ready
```
