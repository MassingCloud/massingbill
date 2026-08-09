#!/bin/sh
# Container entrypoint.
#
# Migrations run here rather than in a separate job so a single `docker compose
# up` on a fresh volume produces a working system -- the P0 acceptance
# criterion. `alembic upgrade head` is idempotent, so a restart or a replica is
# harmless.

set -eu

# Check the required production secrets BEFORE anything imports the app.
#
# The settings layer already refuses to start without these, but it does so as a
# pydantic ValidationError buried in a forty-line traceback -- which is what an
# operator following the README saw, and it tells them nothing they can act on.
# Failing here means the first thing in the log is the sentence that fixes it.
if [ "${MASSINGBILL_ENV:-production}" = "production" ]; then
    missing=""

    if [ -z "${MASSINGBILL_SECRET_KEY:-}" ]; then
        missing="${missing} MASSINGBILL_SECRET_KEY"
    fi

    # Kept separate from the session key on purpose: rotating SECRET_KEY must
    # not make every stored TOTP seed undecryptable and lock every user out of
    # two-factor. See massingbill/services/crypto.py.
    if [ -z "${MASSINGBILL_ENCRYPTION_KEY:-}" ]; then
        missing="${missing} MASSINGBILL_ENCRYPTION_KEY"
    fi

    if [ -n "${missing}" ]; then
        echo "massingbill: refusing to start. Missing required setting(s):${missing}" >&2
        echo "" >&2
        echo "  Generate a value for each with:" >&2
        echo "    docker run --rm <image> massingbill gen-secret" >&2
        echo "" >&2
        echo "  They are deliberately separate. SECRET_KEY signs sessions;" >&2
        echo "  ENCRYPTION_KEY encrypts TOTP seeds and integration tokens at rest," >&2
        echo "  so rotating one does not invalidate the other." >&2
        exit 1
    fi
fi

if [ "${MASSINGBILL_SKIP_MIGRATIONS:-0}" != "1" ]; then
    echo "massingbill: applying database migrations"
    flask --app wsgi:app db upgrade
fi

exec "$@"
