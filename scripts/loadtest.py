"""Concurrency and throughput harness.

Answers one question the unit suite structurally cannot: **what happens when two
people click the same button at the same moment?** Every test in `tests/` runs
one session against one connection, so a time-of-check/time-of-use gap is
invisible there by construction.

The scenario that matters is `open`. `application.open_period` reads the current
open application, decides there is not one, computes ``max(number) + 1`` and
inserts. Two callers interleaved between the read and the insert both pass the
check and both compute the same number. The unique constraint on
``(prime_contract_id, number)`` is what stops that from producing two live
periods -- so the data is safe either way, and the question this answers is what
the *loser* sees. A clean "that period is already open" is right; a 500 from an
unhandled ``IntegrityError`` is not, and no unit test can tell them apart.

## Running it

    python scripts/loadtest.py open --workers 8

By default this builds a throwaway **file-backed SQLite** database (WAL, so
readers do not block the writer). Point it at anything else with
``MASSINGBILL_DATABASE_URL``:

    MASSINGBILL_DATABASE_URL=postgresql://... python scripts/loadtest.py open

**Read the SQLite result with care.** SQLite takes a single write lock per
database, so concurrent writers are serialised by the engine rather than by
anything this application does. That still exercises the read-then-insert gap --
the reads genuinely overlap -- but it does not reproduce the contention a busy
Postgres deployment sees. A green run here is necessary, not sufficient. The
numbers to trust for capacity planning come from Postgres.

Exits non-zero when an invariant breaks, so it works from CI.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

# The application, not a copy of it. A load test that exercises a reimplementation
# of the thing under test measures the reimplementation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from massingbill.app import create_app  # noqa: E402
from massingbill.errors import ConflictError  # noqa: E402
from massingbill.extensions import db  # noqa: E402


@dataclass
class Outcome:
    """What every worker returned, and how long each took."""

    labels: Counter[str] = field(default_factory=Counter)
    durations: list[float] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, label: str, seconds: float) -> None:
        with self.lock:
            self.labels[label] += 1
            self.durations.append(seconds)

    def report(self) -> None:
        print("\n  outcomes")
        for label, count in sorted(self.labels.items(), key=lambda pair: -pair[1]):
            print(f"    {count:>5}  {label}")

        if not self.durations:
            return
        ordered = sorted(self.durations)
        print("\n  latency (ms)")
        print(f"    min    {ordered[0] * 1000:8.1f}")
        print(f"    median {statistics.median(ordered) * 1000:8.1f}")
        print(f"    p95    {ordered[int(len(ordered) * 0.95) - 1] * 1000:8.1f}")
        print(f"    max    {ordered[-1] * 1000:8.1f}")


def classify(error: BaseException) -> str:
    """Name the failure the way an operator would have to triage it."""
    from sqlalchemy.exc import IntegrityError, OperationalError

    if isinstance(error, ConflictError):
        return "refused cleanly (ConflictError)"
    if isinstance(error, IntegrityError):
        return "!! IntegrityError -- would be a 500"
    if isinstance(error, OperationalError):
        # On SQLite this is the engine's single write lock, not the application.
        return f"database busy (OperationalError: {str(error)[:60]})"
    return f"!! {type(error).__name__}: {str(error)[:80]}"


def build_app() -> Any:
    """A real application against a real file, not an in-memory database.

    ``:memory:`` gives each connection its own private database, so every worker
    would succeed against its own copy and the test would pass while proving
    nothing.
    """
    if not os.environ.get("MASSINGBILL_DATABASE_URL"):
        path = Path(tempfile.mkdtemp(prefix="massingbill-load-")) / "load.sqlite"
        os.environ["MASSINGBILL_DATABASE_URL"] = f"sqlite:///{path}"
        print(f"  database  {path}")
    else:
        print(f"  database  {os.environ['MASSINGBILL_DATABASE_URL']}")

    os.environ.setdefault("MASSINGBILL_SECRET_KEY", "load-test-not-a-secret-value-here")
    os.environ.setdefault("MASSINGBILL_ENCRYPTION_KEY", "load-test-not-a-secret-value-here")

    app = create_app()
    with app.app_context():
        db.create_all()
        if db.engine.dialect.name == "sqlite":
            # Readers must not block the writer, or the read-then-insert gap
            # this is trying to observe never opens.
            db.session.execute(db.text("PRAGMA journal_mode=WAL"))
            db.session.commit()
    return app


def seed(app: Any) -> tuple[str, str]:
    """One tenant with an approved schedule. Returns (contract id, org id)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from massingbill.models import Role
    from massingbill.services import sov as sov_service
    from tests.factories import add_balanced_lines, make_tenant

    with app.app_context():
        tenant = make_tenant("loadtest")
        add_balanced_lines(tenant)
        sov_service.approve(tenant.schedule, actor=tenant.user(Role.OWNER))
        db.session.commit()
        return tenant.contract.id, tenant.organization.id


def run_concurrent_open(app: Any, contract_id: str, workers: int) -> int:
    """Every worker races to open the same period. Exactly one may win."""
    from massingbill.models import PrimeContract
    from massingbill.services import application as application_service

    outcome = Outcome()
    # A barrier rather than staggered starts: the gap being probed is a few
    # microseconds wide, and threads that merely start "at about the same time"
    # miss it entirely.
    gate = threading.Barrier(workers)

    def worker() -> None:
        with app.app_context():
            contract = db.session.get(PrimeContract, contract_id)
            gate.wait()
            started = time.perf_counter()
            try:
                application_service.open_period(
                    contract,
                    period_start=date(2026, 6, 1),
                    period_end=date(2026, 6, 30),
                )
                db.session.commit()
                outcome.record("opened", time.perf_counter() - started)
            except BaseException as error:  # noqa: BLE001 - classifying, not handling
                db.session.rollback()
                outcome.record(classify(error), time.perf_counter() - started)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    outcome.report()

    from massingbill.models import Application

    with app.app_context():
        live = db.session.query(Application).filter_by(prime_contract_id=contract_id).count()

    print(f"\n  applications on the contract: {live}")

    failures = 0
    if live != 1:
        print(f"  FAIL  expected exactly 1 application, found {live}")
        failures += 1
    if outcome.labels["opened"] != 1:
        print(f"  FAIL  expected exactly 1 winner, found {outcome.labels['opened']}")
        failures += 1

    ugly = sum(count for label, count in outcome.labels.items() if label.startswith("!!"))
    if ugly:
        print(
            f"  FAIL  {ugly} worker(s) hit an error that reaches the user as a 500 "
            "rather than as a refusal"
        )
        failures += 1

    if not failures:
        print("  OK    one winner, every loser refused cleanly, one application on the contract")
    return failures


def run_read_throughput(app: Any, contract_id: str, workers: int, seconds: float) -> int:
    """How many reconciliations a second, with `workers` readers.

    The reconciliation panel is the most expensive read in the product and the
    one every user hits on every period. Reads take no locks, so this is a
    throughput number rather than a correctness check.
    """
    from massingbill.models import PrimeContract
    from massingbill.services import sov as sov_service

    outcome = Outcome()
    stop = threading.Event()

    def worker() -> None:
        with app.app_context():
            contract = db.session.get(PrimeContract, contract_id)
            schedule = sov_service.current_schedule(contract)
            while not stop.is_set():
                started = time.perf_counter()
                try:
                    sov_service.reconciliation(schedule)
                    outcome.record("reconciled", time.perf_counter() - started)
                except BaseException as error:  # noqa: BLE001
                    db.session.rollback()
                    outcome.record(classify(error), time.perf_counter() - started)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    started_at = time.perf_counter()
    for thread in threads:
        thread.start()
    time.sleep(seconds)
    stop.set()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started_at

    outcome.report()
    total = sum(outcome.labels.values())
    print(f"\n  {total / elapsed:,.0f} reconciliations/second across {workers} readers")

    ugly = sum(count for label, count in outcome.labels.items() if label.startswith("!!"))
    if ugly:
        print(f"  FAIL  {ugly} read(s) errored")
        return 1
    print("  OK    no read errored")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=("open", "read"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seconds", type=float, default=3.0, help="read scenario only")
    args = parser.parse_args()

    print(f"\n  scenario  {args.scenario}")
    print(f"  workers   {args.workers}")

    app = build_app()
    contract_id, _ = seed(app)

    if args.scenario == "open":
        failures = run_concurrent_open(app, contract_id, args.workers)
    else:
        failures = run_read_throughput(app, contract_id, args.workers, args.seconds)

    print()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
