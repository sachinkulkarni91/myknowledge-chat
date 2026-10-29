#!/usr/bin/env bash
# Render start command. Render mounts secret files read-only under /etc/secrets,
# but MSAL rewrites the token cache whenever it refreshes, so copy it somewhere writable.
set -euo pipefail

CACHE_PATH="${TOKEN_CACHE_PATH:-./.secrets/msal_cache.bin}"
SEED="/etc/secrets/msal_cache.bin"

mkdir -p "$(dirname "$CACHE_PATH")"
chmod 700 "$(dirname "$CACHE_PATH")"
if [ ! -f "$CACHE_PATH" ]; then
  if [ ! -f "$SEED" ]; then
    echo "No token cache: upload .secrets/msal_cache.bin as the Render secret file 'msal_cache.bin'." >&2
    exit 1
  fi
  cp "$SEED" "$CACHE_PATH"
fi
chmod 600 "$CACHE_PATH"

export FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-*}"  # trust Render's proxy for client IP and https
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}" --proxy-headers
