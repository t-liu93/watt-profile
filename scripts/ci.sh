#!/usr/bin/env bash
# Full synthetic CI. Requires installed dev dependencies, Chromium and Docker Compose.
set -euo pipefail
cd "$(dirname "$0")/.."
python_bin=${WATT_CI_PYTHON:-python}
ci_root=$(mktemp -d "${TMPDIR:-/tmp}/watt-ci.XXXXXXXX")
export TMPDIR="$ci_root" PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH
export RUFF_CACHE_DIR="$ci_root/ruff"
unset WATT_SERVER_DATABASE_URL WATT_SERVER_DATABASE_URL_FILE
unset WATT_LOGGER_IMAGE WATT_SERVER_IMAGE
export WATT_TEST_POSTGRES=1 WATT_TEST_SERVER_COMPOSE=1
logger_project=""
logger_image=""
owned_resources() {
    if [[ "$1" == container ]]; then
        docker container ls --all -q --filter "label=com.docker.compose.project=$2"
    else
        docker "$1" ls -q --filter "label=com.docker.compose.project=$2"
    fi
}
cleanup() {
    result=$?
    trap - EXIT
    if [[ -n "$logger_project" ]]; then
        if ! docker compose -p "$logger_project" -f docker-compose.dev.yml \
            down --volumes --remove-orphans --timeout 30; then result=1; fi
        if docker image inspect "$logger_image" >/dev/null 2>&1; then
            if ! docker image rm "$logger_image"; then result=1; fi
        fi
        for kind in container network volume; do
            remaining=$(owned_resources "$kind" "$logger_project")
            if [[ -n "$remaining" ]]; then
                echo "CI cleanup left $kind resources for $logger_project" >&2
                result=1
            fi
        done
    fi
    rm -rf -- "$ci_root"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Local entry builds once; reusable CI supplies its trusted artifact digest.
if [[ -z "${WATT_FRONTEND_DIGEST:-}" ]]; then
    "$python_bin" scripts/frontend_artifact.py build
    WATT_FRONTEND_DIGEST=$("$python_bin" -c \
        'import json; print(json.load(open("frontend/dist/artifact.json"))["digest"])')
fi
export WATT_FRONTEND_DIGEST
"$python_bin" scripts/frontend_artifact.py stage --expected "$WATT_FRONTEND_DIGEST"
"$python_bin" -m ruff check .
"$python_bin" -m ruff format --check .
"$python_bin" -m pytest -q -o cache_dir="$ci_root/pytest-cache" --basetemp="$ci_root/unit"
# Explicit selectors opt in to services/browser acceptance; default pytest skips these.
"$python_bin" -m pytest tests/browser -q -o cache_dir="$ci_root/pytest-cache" \
    --basetemp="$ci_root/browser"
nonce=$("$python_bin" -c 'import secrets; print(secrets.token_hex(8))')
export WATT_TEST_COMPOSE_PROJECT="watt-m3-ci-$nonce"
export WATT_DEV_IMAGE_TAG="$WATT_TEST_COMPOSE_PROJECT"
"$python_bin" -m pytest tests/integration/test_server_compose.py -q \
    -o cache_dir="$ci_root/pytest-cache" --basetemp="$ci_root/server-compose"
export WATT_TEST_COMPOSE_PROJECT="watt-m3-ci-recovery-$nonce"
export WATT_DEV_IMAGE_TAG="$WATT_TEST_COMPOSE_PROJECT"
"$python_bin" -m pytest tests/integration/test_server_recovery.py -q \
    -o cache_dir="$ci_root/pytest-cache" --basetemp="$ci_root/recovery"

candidate="watt-m2-ci-$nonce"
candidate_image="watt-profile-logger-dev:$candidate"
# Check stopped containers too. Never adopt or remove pre-existing resources.
for kind in container network volume; do
    existing=$(owned_resources "$kind" "$candidate")
    if [[ -n "$existing" ]]; then echo "Refusing existing project: $candidate" >&2; exit 1; fi
done
if docker image inspect "$candidate_image" >/dev/null 2>&1; then
    echo "Refusing existing image: $candidate_image" >&2
    exit 1
fi
logger_project=$candidate
logger_image=$candidate_image
export WATT_TEST_COMPOSE_PROJECT="$logger_project" WATT_DEV_IMAGE_TAG="$logger_project"
export WATT_DEV_PORT
WATT_DEV_PORT=$("$python_bin" -c \
    'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
docker compose -p "$logger_project" -f docker-compose.dev.yml \
    up --build -d --wait --wait-timeout 120 logger
"$python_bin" -m pytest tests/integration/test_logger_compose.py -q \
    -o cache_dir="$ci_root/pytest-cache" --basetemp="$ci_root/logger-compose"

# M4 automatic Logger-to-Server delivery and self-owned combined project acceptance.
"$python_bin" -m pytest tests/test_m4_pipeline.py -q -o cache_dir="$ci_root/pytest-cache" \
    --basetemp="$ci_root/m4-pipeline"
export WATT_TEST_M4_CAPACITY=1
"$python_bin" -m pytest tests/integration/test_logger_capacity.py -q \
    -o cache_dir="$ci_root/pytest-cache" --basetemp="$ci_root/m4-capacity"
export WATT_TEST_M4_COMPOSE=1
export WATT_TEST_COMPOSE_PROJECT="watt-m4-ci-$nonce"
export WATT_DEV_IMAGE_TAG="$WATT_TEST_COMPOSE_PROJECT"
read -r WATT_DEV_PORT WATT_DEV_SERVER_PORT < <("$python_bin" -c '
import socket
sockets = []
ports = []
for _ in range(2):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sockets.append(sock)
    ports.append(sock.getsockname()[1])
print(*ports)
for sock in sockets:
    sock.close()
')
export WATT_DEV_PORT WATT_DEV_SERVER_PORT
"$python_bin" -m pytest tests/integration/test_m4_compose.py -q \
    -o cache_dir="$ci_root/pytest-cache" --basetemp="$ci_root/m4-compose"
