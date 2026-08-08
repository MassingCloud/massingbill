# Security Policy

## Reporting a vulnerability

Report privately through GitHub's
[security advisory form](https://github.com/MassingCloud/massingbill/security/advisories/new).
Please do not open a public issue for a suspected vulnerability.

We aim to acknowledge within 3 business days and to ship a fix or a mitigation
within 30 days for anything rated high or critical.

Include what you have: affected version or commit, reproduction steps, and the
impact you believe it has. A proof of concept helps but is not required.

## Scope

In scope: this repository — the application, its container image, its CI
configuration and its dependencies as pinned here.

Out of scope: findings that require an attacker to already hold administrator
credentials; denial of service through sheer volume; missing hardening headers on
a deployment the operator configured themselves; and vulnerabilities in
massing.cloud (report those to that project).

## Supported versions

Until v1.0.0, only `main` is supported. After v1.0.0 the latest minor release
receives security fixes.

## What this software handles

Massing Bill stores construction financial records: contract values, schedules of
values, applications for payment, and signed lien waivers. Those are commercially
sensitive and, in a dispute, evidentiary. Design decisions that follow from that:

- Submitted applications are immutable and carry a hashed snapshot.
- Financial records are never hard-deleted; `void` is a state.
- The audit log is hash-chained and verifiable.
- Signatures bind the SHA-256 of the exact rendered document, so re-rendering
  invalidates them.

If you find a way to mutate a submitted application, forge an audit chain, or
detach a signature from the bytes it signed, treat it as critical.
