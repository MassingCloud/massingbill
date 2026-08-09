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
    from massingbill.services import accounts, seeding

    password = args.password or getpass.getpass("Password: ")

    app = create_app()
    with app.app_context():
        try:
            user = accounts.create_user(args.email, password, name=args.name or "")
            organization = accounts.create_organization(args.organization, user)
            seeded = seeding.seed_cost_codes(organization)
            db.session.commit()

            # Read the attributes before the context closes: after commit the
            # instances expire, and outside the app context refreshing them
            # raises DetachedInstanceError.
            summary = (
                f"Created {user.email} as owner of {organization.name} "
                f"({organization.slug}).\nSeeded {seeded} CSI MasterFormat division(s)."
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
    if args.command == "audit":
        if args.audit_command == "verify":
            return _audit_verify(args)
        audit_parser.print_help()
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
