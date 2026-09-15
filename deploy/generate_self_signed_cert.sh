#!/usr/bin/env bash
set -Eeuo pipefail

deploy_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cert_dir="${deploy_dir}/certs"
public_host=${1:-101.42.90.142}
cert_path="${cert_dir}/assistant.crt"
key_path="${cert_dir}/assistant.key"

if [[ -f "${cert_path}" && -f "${key_path}" ]]; then
    echo "Existing self-signed certificate retained."
    exit 0
fi

if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required to generate the TLS certificate." >&2
    exit 1
fi

if [[ "${public_host}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    subject_alt_name="IP:${public_host},IP:127.0.0.1,DNS:localhost"
else
    subject_alt_name="DNS:${public_host},DNS:localhost,IP:127.0.0.1"
fi

install -d -m 700 "${cert_dir}"
umask 077
openssl req \
    -x509 \
    -nodes \
    -newkey rsa:3072 \
    -sha256 \
    -days 825 \
    -subj "/C=CN/O=Personal AI Assistant/CN=${public_host}" \
    -addext "subjectAltName=${subject_alt_name}" \
    -addext "keyUsage=digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth" \
    -keyout "${key_path}" \
    -out "${cert_path}" >/dev/null 2>&1

chmod 600 "${key_path}"
chmod 644 "${cert_path}"
echo "Generated self-signed certificate for ${public_host}."
