"""Gunicorn configuration.

Sized for a document-rendering workload: PDF generation is CPU-bound and can run
for seconds, so the worker timeout is generous and workers are recycled to keep
any renderer leak bounded.
"""

from __future__ import annotations

import multiprocessing
import os

bind = os.environ.get("MASSINGBILL_BIND", "0.0.0.0:8000")
workers = int(os.environ.get("WEB_CONCURRENCY", multiprocessing.cpu_count() * 2 + 1))
threads = int(os.environ.get("WEB_THREADS", 4))
worker_class = "gthread"

# PDF rendering is slow but not unbounded; 120 s catches a genuine hang without
# killing a legitimately large continuation sheet.
timeout = 120
graceful_timeout = 30
keepalive = 5

# Recycle workers so a slow leak in a native renderer never accumulates.
max_requests = 1000
max_requests_jitter = 100

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("MASSINGBILL_LOG_LEVEL", "info").lower()

# Trust only the immediate proxy for X-Forwarded-*; the container is expected to
# sit behind exactly one.
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
proxy_protocol = False
