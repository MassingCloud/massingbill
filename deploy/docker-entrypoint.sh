#!/bin/sh
# Container entrypoint.
#
# Migrations run here rather than in a separate job so a single `docker compose
# up` on a fresh volume produces a working system -- the P0 acceptance criterion.
# `alembic upgrade head` is idempotent, so a restart or a replica is harmless.

set -eu

if [ "${MASSINGBILL_SKIP_MIGRATIONS:-0}" != "1" ]; then
    echo "massingbill: applying database migrations"
    flask --app wsgi:app db upgrade
fi

if [ "${MASSINGBILL_ENV:-production}" = "production" ] && [ -z "${MASSINGBILL_SECRET_KEY:-}" ]; then
    echo "massingbill: MASSINGBILL_SECRET_KEY must be set in production." >&2
    echo "             Generate one with: docker run --rm <image> massingbill gen-secret" >&2
    exit 1
fi

exec "$@"
