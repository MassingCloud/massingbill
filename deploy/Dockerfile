# Massing Bill container image.
#
# Two stages so the runtime layer carries no build toolchain. The WeasyPrint
# native stack (pango/cairo) is installed because document rendering is the
# product; without it the PDF path degrades silently, which is worse than a
# larger image.

FROM python:3.14-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY massingbill ./massingbill

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install ".[postgres,server,render]"


FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    MASSINGBILL_ENV=production \
    MASSINGBILL_INSTANCE_PATH=/data

# Runtime libraries only -- these are what WeasyPrint loads at import time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libffi8 \
        shared-mime-info \
        fonts-dejavu-core \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Never run as root; /data is the only writable path the app needs.
RUN useradd --create-home --uid 10001 massingbill \
    && mkdir -p /data \
    && chown -R massingbill:massingbill /data

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=massingbill:massingbill massingbill ./massingbill
COPY --chown=massingbill:massingbill migrations ./migrations
COPY --chown=massingbill:massingbill deploy/gunicorn.conf.py deploy/docker-entrypoint.sh wsgi.py ./
COPY --chown=massingbill:massingbill pyproject.toml README.md SPEC.md LICENSE ./

RUN chmod +x /app/docker-entrypoint.sh

USER massingbill
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
