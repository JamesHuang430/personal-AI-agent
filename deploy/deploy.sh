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

compose_cmd=(
    docker compose
    --project-name personal-ai-assistant
    --env-file "${env_path}"
    --file "${deploy_dir}/compose.yaml"
)

cd "${project_dir}"

echo "Validating Compose configuration..."
"${compose_cmd[@]}" config --quiet

echo "Building application image..."
"${compose_cmd[@]}" build --pull assistant-api

echo "Starting lightweight base stack..."
"${compose_cmd[@]}" up -d postgres redis assistant-api admin-api

echo "Applying database migrations..."
"${compose_cmd[@]}" exec -T assistant-api alembic upgrade head

echo "Waiting for API health..."
for attempt in $(seq 1 30); do
    if curl --silent --show-error --fail \
        "http://127.0.0.1:${ASSISTANT_API_PORT:-18000}/api/v1/health/ready" >/dev/null \
        && curl --silent --show-error --fail \
        "http://127.0.0.1:${ASSISTANT_ADMIN_PORT:-19000}/api/v1/health/ready" >/dev/null; then
        echo "User API and operations console are ready."
        "${compose_cmd[@]}" ps
        exit 0
    fi
    if [[ "${attempt}" -eq 30 ]]; then
        echo "API did not become ready." >&2
        "${compose_cmd[@]}" ps
        "${compose_cmd[@]}" logs --tail=200 assistant-api admin-api postgres redis
        exit 1
    fi
    sleep 2
done
