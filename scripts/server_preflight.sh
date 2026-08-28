#!/usr/bin/env bash
set -Eeuo pipefail

echo "== System =="
uname -a

echo "== CPU =="
nproc

echo "== Memory =="
free -h

echo "== Disk =="
df -h / /var/lib/docker 2>/dev/null || df -h /

echo "== Docker =="
docker version --format 'client={{.Client.Version}} server={{.Server.Version}}'
docker compose version

echo "== Running containers =="
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

echo "== Listening TCP ports =="
ss -lntp

echo "== Candidate port check =="
for port in 80 443 8000 9000 9001 18000; do
    if ss -lnt "sport = :${port}" | grep -q LISTEN; then
        echo "port ${port}: in use"
    else
        echo "port ${port}: available"
    fi
done

