"""The ``massingbill`` command-line entry point."""

from __future__ import annotations

import argparse
import secrets
import sys

from massingbill import __version__


def _gen_secret(_: argparse.Namespace) -> int:
    print(secrets.token_urlsafe(48))
    return 0


def _check(_: argparse.Namespace) -> int:
    """Boot the app with the ambient configuration and report what it resolved.

    A deployment smoke test: it proves the config parses and the adapters
    resolve without serving a request.
    """
    from massingbill import create_app

    app = create_app()
    print(f"massingbill {__version__}")
    print(f"  env                  {app.config['ENV']}")
    print(f"  database             {app.config['SQLALCHEMY_DATABASE_URI']}")
    print(f"  entitlement provider {app.config['MASSINGBILL_ENTITLEMENT_PROVIDER']}")
    print(f"  storage backend      {app.config['MASSINGBILL_STORAGE_BACKEND']}")
    return 0


def _audit_verify(args: argparse.Namespace) -> int:
    """Walk the audit chains and report any break.

    Exits non-zero when a chain is broken, so it can run from cron or a
    pre-backup hook and actually alert someone.
    """
    from massingbill import create_app
    from massingbill.services import audit

    app = create_app()
    with app.app_context():
        verdicts = [audit.verify(args.organization)] if args.organization else audit.verify_all()

        if not verdicts:
            print("No audit events recorded yet.")
            return 0

        broken = 0
        for verdict in verdicts:
            print(verdict.describe())
            if not verdict.ok:
                broken += 1

        if broken:
            print(f"\n{broken} chain(s) failed verification.", file=sys.stderr)
            return 1

        print(f"\n{len(verdicts)} chain(s) verified.")
        return 0


def _create_admin(args: argparse.Namespace) -> int:
    """Create the first user and organization without a browser."""
    import getpass

    from massingbill import create_app
    from massingbill.extensions import db
    from massingbill.services import accounts, deadlines, seeding
    from massingbill.services import waivers as waiver_service

    password = args.password or getpass.getpass("Password: ")

    app = create_app()
    with app.app_context():
        try:
            user = accounts.create_user(args.email, password, name=args.name or "")
            organization = accounts.create_organization(args.organization, user)
            seeded = seeding.seed_cost_codes(organization)
            # Both ship deliberately empty and refuse until verified, so
            # seeding them is how a contractor finds out the obligations
            # exist at all rather than discovering the gap later.
            templates = waiver_service.seed_templates(organization)
            rules = deadlines.seed_rules(organization)
            db.session.commit()

            # Read the attributes before the context closes: after commit the
            # instances expire, and outside the app context refreshing them
            # raises DetachedInstanceError.
            summary = (
                f"Created {user.email} as owner of {organization.name} "
                f"({organization.slug}).\n"
                f"Seeded {seeded} CSI MasterFormat division(s), {templates} lien-waiver "
                f"template(s) and {rules} statutory deadline rule(s).\n"
                "The statutory waiver forms and every deadline rule ship EMPTY and "
                "refuse to render or compute until someone reads the statute and "
                "verifies them. That is deliberate; see docs/legal-forms-policy.md."
            )
        except Exception as exc:  # noqa: BLE001 - report cleanly, do not traceback
            db.session.rollback()
            print(f"Could not create the account: {exc}", file=sys.stderr)
            return 1

    print(summary)
    return 0


def _demo(args: argparse.Namespace) -> int:
    """Seed the demo project into the configured database.

    Six periods on a $4,850,000 job, exercising a change order, stored material
    that later installs, a deductive change order and a partial certification.
    Sign in with the printed credentials to walk through it.
    """
    from massingbill import create_app
    from massingbill.extensions import db
    from massingbill.services import demo as demo_service

    app = create_app()
    with app.app_context():
        try:
            built = demo_service.build(email=args.email, password=args.password)

            # Read every attribute before the context closes: after commit the
            # instances expire, and refreshing them outside the app context
            # raises DetachedInstanceError.
            lines = [
                f"Seeded {built.project.number} — {built.project.name}",
                f"  organization  {built.organization.name}",
                f"  sign in as    {args.email}",
                f"  password      {args.password}",
            ]
            if built.waiver_refusal:
                lines += [
                    "",
                    "The statutory waiver form refused to render, which is correct",
                    "for a California project:",
                    f"  {built.waiver_refusal}",
                ]
            summary = "\n".join(lines)
            db.session.commit()
        except Exception as exc:  # noqa: BLE001 - report cleanly, do not traceback
            db.session.rollback()
            print(f"Could not seed the demo: {exc}", file=sys.stderr)
            return 1

    print(summary)
    return 0


def _apikey_mint(args: argparse.Namespace) -> int:
    """Mint an API key and print the token exactly once."""
    from massingbill import create_app
    from massingbill.extensions import db
    from massingbill.models import Organization
    from massingbill.services import apikeys

    app = create_app()
    with app.app_context():
        organization = db.session.get(Organization, args.organization)
        if organization is None:
            print(f"No organization with id {args.organization!r}.", file=sys.stderr)
            return 1

        try:
            minted = apikeys.mint(
                organization,
                name=args.name,
                scopes=set(args.scope) if args.scope else None,
                rate_limit_per_minute=args.rate_limit,
            )
            # Read everything before the commit expires the instance.
            summary = "\n".join(
                [
                    f"Minted {minted.key.name} for {organization.name}.",
                    f"  scopes  {' '.join(sorted(minted.key.scope_set))}",
                    "",
                    "  " + minted.token,
                    "",
                    "That token is shown once and is not recoverable -- only its",
                    "digest is stored. Copy it now, or mint another.",
                ]
            )
            db.session.commit()
        except Exception as exc:  # noqa: BLE001 - report cleanly, do not traceback
            db.session.rollback()
            print(f"Could not mint the key: {exc}", file=sys.stderr)
            return 1

    print(summary)
    return 0


def _apikey_list(args: argparse.Namespace) -> int:
    from massingbill import create_app
    from massingbill.services import apikeys

    app = create_app()
    with app.app_context():
        keys = apikeys.for_organization(args.organization, include_revoked=args.all)
        if not keys:
            print("No API keys.")
            return 0

        rows = [
            f"{key.masked:<40} {key.name:<24} "
            f"{'revoked' if key.is_revoked else 'expired' if key.is_expired else 'active':<8} "
            f"last used {key.last_used_at.date().isoformat() if key.last_used_at else 'never'}"
            for key in keys
        ]
    print("\n".join(rows))
    return 0


def _webhooks_drain(args: argparse.Namespace) -> int:
    """Attempt every webhook delivery that is due.

    Run from cron. Exits non-zero when anything was abandoned, so a dead
    subscriber is noticed by the thing that runs it rather than by nobody.
    """
    from massingbill import create_app
    from massingbill.services import webhooks

    app = create_app()
    with app.app_context():
        tally = webhooks.drain(limit=args.limit)

    print(
        f"delivered {tally['delivered']}, failed {tally['failed']} "
        f"(will retry), abandoned {tally['abandoned']}"
    )
    return 1 if tally["abandoned"] else 0


def _tieout_sweep(args: argparse.Namespace) -> int:
    """Check every open period and announce the ones that will not submit.

    Run from cron. Exits non-zero when anything fails, so the schedule itself
    reports the problem rather than waiting for someone to look.
    """
    from massingbill import create_app
    from massingbill.services import events

    app = create_app()
    with app.app_context():
        failures = events.sweep_open_periods(args.organization)
        lines = [
            f"#{application.number} ({application.period_end.isoformat()}): "
            + "; ".join(f.rule_id for f in report.blocking)
            for application, report in failures
        ]

    if not lines:
        print("Every open period ties out.")
        return 0

    print(f"{len(lines)} open period(s) will not submit:")
    print("\n".join(f"  {line}" for line in lines))
    return 1


def _statutory_status(args: argparse.Namespace) -> int:
    """How much statutory content is still unverified."""
    from massingbill import create_app
    from massingbill.services import statutory

    app = create_app()
    with app.app_context():
        waivers, deadlines = statutory.outstanding(args.organization)

    print(f"{waivers} lien-waiver form(s) and {deadlines} deadline rule(s) unverified.")
    if waivers or deadlines:
        print(
            "\nThese ship empty on purpose and refuse to render or compute until\n"
            "somebody reads the statute and enters them. Export a worksheet with:\n"
            f"  massingbill statutory export --organization {args.organization} "
            "--out statutory.csv"
        )
    return 0


def _statutory_export(args: argparse.Namespace) -> int:
    """Write a worksheet of everything outstanding, with citations."""
    from pathlib import Path

    from massingbill import create_app
    from massingbill.services import statutory

    app = create_app()
    with app.app_context():
        content = statutory.export_worksheet(args.organization, state=args.state)

    rows = content.count("\r\n") - 1
    if args.out:
        Path(args.out).write_text(content, encoding="utf-8")
        print(f"Wrote {rows} outstanding item(s) to {args.out}.")
        print(
            "\nFill the `verbatim_text` column for waivers and the `days` column for\n"
            "deadlines, from the statute named in `citation`. Leave a row blank to\n"
            "skip it. Then:\n"
            f"  massingbill statutory import {args.out} --organization {args.organization}"
        )
    else:
        print(content, end="")
    return 0


def _statutory_import(args: argparse.Namespace) -> int:
    """Read a filled worksheet back, verifying each row that has content."""
    from pathlib import Path

    from massingbill import create_app
    from massingbill.extensions import db
    from massingbill.services import statutory

    source = Path(args.worksheet)
    if not source.exists():
        print(f"No such file: {source}", file=sys.stderr)
        return 1

    app = create_app()
    with app.app_context():
        try:
            result = statutory.import_worksheet(
                args.organization, source.read_text(encoding="utf-8")
            )
            summary = result.describe()
            problems = list(result.errors or [])
            db.session.commit()
        except Exception as exc:  # noqa: BLE001 - report cleanly, do not traceback
            db.session.rollback()
            print(f"Could not import the worksheet: {exc}", file=sys.stderr)
            return 1

    print(summary)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)

    if problems:
        return 1

    print(
        "\nVerified entries render and compute from now on. Check them against the\n"
        "statute once more before you rely on them -- this tool moved your text,\n"
        "it did not check it."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="massingbill", description="Massing Bill CLI")
    parser.add_argument("--version", action="version", version=f"massingbill {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("gen-secret", help="Print a fresh secret-key value")
    sub.add_parser("check", help="Boot the app and report the resolved configuration")

    audit_parser = sub.add_parser("audit", help="Audit-log tools")
    audit_sub = audit_parser.add_subparsers(dest="audit_command")
    verify = audit_sub.add_parser("verify", help="Verify the hash chain")
    verify.add_argument("--organization", help="Limit to one organization id")

    demo_parser = sub.add_parser("demo", help="Seed a worked six-period demo project")
    demo_parser.add_argument("--email", default="demo@massingbill.example")
    demo_parser.add_argument("--password", default="demo-account-not-for-production")

    apikey_parser = sub.add_parser("apikey", help="API key management")
    apikey_sub = apikey_parser.add_subparsers(dest="apikey_command")

    key_mint = apikey_sub.add_parser("mint", help="Mint a key and print it once")
    key_mint.add_argument("--organization", required=True, help="Organization id")
    key_mint.add_argument("--name", required=True, help="What this key is for")
    key_mint.add_argument(
        "--scope",
        action="append",
        default=[],
        help="Repeatable. Omit for a read-only key.",
    )
    key_mint.add_argument("--rate-limit", type=int, default=0, help="Requests/minute, 0 = default")

    key_list = apikey_sub.add_parser("list", help="List keys")
    key_list.add_argument("--organization", required=True)
    key_list.add_argument("--all", action="store_true", help="Include revoked keys")

    tieout_parser = sub.add_parser("tieout", help="Reconciliation tools")
    tieout_sub = tieout_parser.add_subparsers(dest="tieout_command")
    sweep = tieout_sub.add_parser("sweep", help="Check every open period")
    sweep.add_argument("--organization", required=True)

    hook_parser = sub.add_parser("webhooks", help="Outbound webhook tools")
    hook_sub = hook_parser.add_subparsers(dest="webhooks_command")
    drain = hook_sub.add_parser("drain", help="Send every delivery that is due")
    drain.add_argument("--limit", type=int, default=100)

    statutory_parser = sub.add_parser(
        "statutory", help="Enter the statutory text and deadlines that ship empty"
    )
    statutory_sub = statutory_parser.add_subparsers(dest="statutory_command")

    status = statutory_sub.add_parser("status", help="How much is still unverified")
    status.add_argument("--organization", required=True)

    export = statutory_sub.add_parser("export", help="Write a worksheet of what is outstanding")
    export.add_argument("--organization", required=True)
    export.add_argument("--state", default=None, help="Limit to one two-letter state code")
    export.add_argument("--out", default="", help="Write here instead of to stdout")

    import_parser = statutory_sub.add_parser("import", help="Read a filled worksheet back")
    import_parser.add_argument("worksheet")
    import_parser.add_argument("--organization", required=True)

    admin = sub.add_parser("create-admin", help="Create the first user and organization")
    admin.add_argument("--email", required=True)
    admin.add_argument("--organization", required=True, help="Company name")
    admin.add_argument("--name", default="")
    admin.add_argument(
        "--password",
        default="",
        help="Prompted for if omitted, which keeps it out of your shell history",
    )

    args = parser.parse_args(argv)

    if args.command == "gen-secret":
        return _gen_secret(args)
    if args.command == "check":
        return _check(args)
    if args.command == "create-admin":
        return _create_admin(args)
    if args.command == "demo":
        return _demo(args)
    if args.command == "apikey":
        if args.apikey_command == "mint":
            return _apikey_mint(args)
        if args.apikey_command == "list":
            return _apikey_list(args)
        apikey_parser.print_help()
        return 1
    if args.command == "tieout":
        if args.tieout_command == "sweep":
            return _tieout_sweep(args)
        tieout_parser.print_help()
        return 1
    if args.command == "webhooks":
        if args.webhooks_command == "drain":
            return _webhooks_drain(args)
        hook_parser.print_help()
        return 1
    if args.command == "statutory":
        if args.statutory_command == "status":
            return _statutory_status(args)
        if args.statutory_command == "export":
            return _statutory_export(args)
        if args.statutory_command == "import":
            return _statutory_import(args)
        statutory_parser.print_help()
        return 1
    if args.command == "audit":
        if args.audit_command == "verify":
            return _audit_verify(args)
        audit_parser.print_help()
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
