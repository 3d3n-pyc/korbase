#!/usr/bin/env bash
# Start a throwaway Postgres for the test suite and print the DSN to use.
# Safe to run more than once: an existing container is reused.
set -euo pipefail

NAME=korbase-test
VERSION="${KORBASE_TEST_PG_VERSION:-18}"
PORT="${KORBASE_TEST_PORT:-55433}"
IMAGE="postgres:${VERSION}"
DSN="postgresql://korbase:korbase@localhost:${PORT}/korbase"

# Create the container on first run, or start it again if it is stopped.
if ! docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    docker run -d --name "$NAME" \
        -e POSTGRES_USER=korbase \
        -e POSTGRES_PASSWORD=korbase \
        -e POSTGRES_DB=korbase \
        -p "${PORT}:5432" \
        "$IMAGE" >/dev/null
elif [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" != "true" ]; then
    docker start "$NAME" >/dev/null
fi

# Wait up to 60 seconds for the server to accept connections.
for _ in $(seq 1 60); do
    if docker exec "$NAME" pg_isready -U korbase -d korbase >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
if ! docker exec "$NAME" pg_isready -U korbase -d korbase >/dev/null 2>&1; then
    echo "Postgres in container $NAME is not ready after 60 seconds." >&2
    exit 1
fi

echo "export KORBASE_TEST_DSN=${DSN}"
if [ -n "${GITHUB_ENV:-}" ]; then
    echo "KORBASE_TEST_DSN=${DSN}" >>"$GITHUB_ENV"
fi
