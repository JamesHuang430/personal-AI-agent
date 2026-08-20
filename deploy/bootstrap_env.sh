#!/usr/bin/env bash
set -Eeuo pipefail

deploy_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
env_path="${deploy_dir}/.env"

if [[ -f "${env_path}" ]]; then
    echo "Existing ${env_path} retained."
    exit 0
fi

if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required to generate deployment secrets." >&2
    exit 1
fi

umask 077
postgres_secret=$(openssl rand -hex 24)
redis_secret=$(openssl rand -hex 24)

cat >"${env_path}" <<EOF
ASSISTANT_IMAGE_TAG=0.1.0
ASSISTANT_LOG_LEVEL=INFO
ASSISTANT_API_PORT=18000
ASSISTANT_CORS_ORIGINS=https://assistant.example.com
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
PIP_DEFAULT_TIMEOUT=120

POSTGRES_DB=assistant
POSTGRES_USER=assistant
POSTGRES_PASSWORD=${postgres_secret}
REDIS_PASSWORD=${redis_secret}
EOF

chmod 600 "${env_path}"
echo "Generated ${env_path} with mode 600."
