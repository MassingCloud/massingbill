"""The ``massingbill`` command-line entry point."""

from __future__ import annotations

import argparse
import secrets
import sys

from massingbill import __version__


def _gen_secret() -> int:
    print(secrets.token_urlsafe(48))
    return 0


def _check() -> int:
    """Boot the app with the ambient configuration and report what it resolved.

    Useful as a deployment smoke test: it proves the config parses and the
    adapters resolve without serving a request.
    """
    from massingbill import create_app

    app = create_app()
    print(f"massingbill {__version__}")
    print(f"  env                  {app.config['ENV']}")
    print(f"  database             {app.config['SQLALCHEMY_DATABASE_URI']}")
    print(f"  entitlement provider {app.config['MASSINGBILL_ENTITLEMENT_PROVIDER']}")
    print(f"  storage backend      {app.config['MASSINGBILL_STORAGE_BACKEND']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="massingbill", description="Massing Bill CLI")
    parser.add_argument("--version", action="version", version=f"massingbill {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gen-secret", help="Print a fresh MASSINGBILL_SECRET_KEY value")
    sub.add_parser("check", help="Boot the app and report the resolved configuration")

    args = parser.parse_args(argv)
    if args.command == "gen-secret":
        return _gen_secret()
    if args.command == "check":
        return _check()

    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
