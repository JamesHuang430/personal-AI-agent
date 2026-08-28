#!/usr/bin/env bash
set -Eeuo pipefail

deploy_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "${deploy_dir}/.." && pwd)
env_path="${deploy_dir}/.env"

if [[ ! -f "${env_path}" ]]; then
    "${deploy_dir}/bootstrap_env.sh"
fi

set -a
# shellcheck disable=SC1090
source "${env_path}"
set +a

"${deploy_dir}/generate_self_signed_cert.sh" "${ASSISTANT_PUBLIC_IP:-101.42.90.142}"

compose_cmd=(
    docker compose
    --project-name personal-ai-assistant
    --env-file "${env_path}"
    --file "${deploy_dir}/compose.yaml"
)

cd "${project_dir}"

echo "Validating Compose configuration..."
"${compose_cmd[@]}" config --quiet

echo "Building application and PostgreSQL extension images..."
"${compose_cmd[@]}" build --pull postgres assistant-api

echo "Starting data services..."
"${compose_cmd[@]}" up -d postgres redis

echo "Applying database migrations..."
"${compose_cmd[@]}" run --rm --no-deps assistant-api alembic upgrade head

echo "Starting application services..."
"${compose_cmd[@]}" up -d assistant-api admin-api nginx
# Refresh Nginx's resolved upstream addresses after application containers are recreated.
"${compose_cmd[@]}" restart nginx

echo "Waiting for API health..."
for attempt in $(seq 1 30); do
    if curl --silent --show-error --fail \
        --insecure "https://127.0.0.1:${ASSISTANT_API_PORT:-18000}/api/v1/health/ready" >/dev/null \
        && curl --silent --show-error --fail \
        --insecure "https://127.0.0.1:${ASSISTANT_ADMIN_PORT:-19000}/api/v1/health/ready" >/dev/null; then
        echo "User API and operations console are ready."
        "${compose_cmd[@]}" ps
        exit 0
    fi
    if [[ "${attempt}" -eq 30 ]]; then
        echo "API did not become ready." >&2
        "${compose_cmd[@]}" ps
        "${compose_cmd[@]}" logs --tail=200 nginx assistant-api admin-api postgres redis
        exit 1
    fi
    sleep 2
done
