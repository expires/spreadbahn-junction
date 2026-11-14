#!/bin/sh
# Load the team credentials and a working CA bundle, then use .venv/bin/python.
#   . ./env.sh
# Secrets live in .env (gitignored); nothing here is secret.
set -a
. "$(dirname "$0")/.env"
set +a
ROOT=$(cd "$(dirname "$0")" && pwd)
# python.org builds ship no CA store; certifi's bundle is in the venv.
export SSL_CERT_FILE="$ROOT/.venv/lib/python3.12/site-packages/certifi/cacert.pem"
export PATH="$ROOT/.venv/bin:$PATH"
