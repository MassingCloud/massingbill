"""Massing Bill API client.

Dependency-light by design: HTTP via the standard library, nothing to install
beyond Python itself. It mirrors ``massing_data/client.py`` in shape so a team
that has integrated one has already learned the other.

Example
-------
    from massingbill_client import MassingBillClient

    client = MassingBillClient(api_key="mbil_...", base_url="http://localhost:8000")

    for application in client.applications(status="submitted"):
        report = client.tieout(application["id"])
        if not report["ok"]:
            print(application["number"], report["summary"])

**Amounts are integer cents.** Every money field is a dict with ``cents`` (an
int, authoritative), ``amount`` (a decimal *string*) and ``currency``. Use
:func:`cents` to read one. Do not do arithmetic on ``amount``, and never put an
amount into a float -- the whole engine exists to avoid that.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

__all__ = [
    "MassingBillClient",
    "MassingBillError",
    "cents",
    "verify_webhook",
]

DEFAULT_BASE_URL = "http://localhost:8000"
API_ROOT = "/api/massingbill/v1"


class MassingBillError(Exception):
    """An API error. ``status`` and ``code`` are set when the server said so."""

    def __init__(self, message: str, status: int | None = None, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def cents(money: dict[str, Any] | None) -> int:
    """The authoritative integer value of a money object.

    Provided so that reading an amount is a one-liner that cannot accidentally
    reach for the decimal string instead.
    """
    if money is None:
        return 0
    return int(money["cents"])


def verify_webhook(body: bytes, signature: str, secret: str) -> bool:
    """Check an ``X-Massing-Signature`` header against the raw request body.

    Pass the **raw bytes** your web framework received, not a re-serialization
    of the parsed JSON: the signature covers bytes, and ``json.dumps`` of a
    parsed payload will not reproduce them.
    """
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class MassingBillClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("An API key is required.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ── Plumbing ────────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        url = f"{self.base_url}{API_ROOT}{path}"
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url = f"{url}?{urllib.parse.urlencode(query)}"

        request = urllib.request.Request(  # noqa: S310 - base_url is caller-supplied
            url,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "*/*" if raw else "application/json",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise self._error(exc) from exc
        except urllib.error.URLError as exc:
            raise MassingBillError(f"Could not reach {self.base_url}: {exc.reason}") from exc

        return payload if raw else json.loads(payload)

    @staticmethod
    def _error(exc: urllib.error.HTTPError) -> MassingBillError:
        """Turn the server's error envelope back into a usable exception."""
        try:
            body = json.loads(exc.read())
            return MassingBillError(
                body.get("message", str(exc)), status=exc.code, code=body.get("error", "")
            )
        except (ValueError, OSError):
            return MassingBillError(str(exc), status=exc.code)

    def _paginate(self, path: str, **params: Any) -> Iterator[dict[str, Any]]:
        """Walk every page.

        Iterating rather than returning a list: a contractor with ten years of
        applications should not need all of them in memory to find one.
        """
        page = 1
        while True:
            response = self._request("GET", path, params={**params, "page": page})
            yield from response["data"]

            meta = response.get("meta", {})
            if page >= meta.get("pages", 1):
                return
            page += 1

    # ── Resources ───────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Confirm the key works, and report its scopes and available formats."""
        return self._request("GET", "/status")["data"]

    def projects(self, **params: Any) -> Iterator[dict[str, Any]]:
        return self._paginate("/projects", **params)

    def project(self, project_id: str) -> dict[str, Any]:
        return self._request("GET", f"/projects/{project_id}")["data"]

    def schedule_of_values(self, project_id: str) -> dict[str, Any]:
        return self._request("GET", f"/projects/{project_id}/schedule-of-values")["data"]

    def applications(
        self, *, project_id: str | None = None, status: str | None = None, **params: Any
    ) -> Iterator[dict[str, Any]]:
        return self._paginate("/applications", project_id=project_id, status=status, **params)

    def application(self, application_id: str) -> dict[str, Any]:
        return self._request("GET", f"/applications/{application_id}")["data"]

    def tieout(self, application_id: str) -> dict[str, Any]:
        """The reconciliation: does this application balance, and if not, why."""
        return self._request("GET", f"/applications/{application_id}/tieout")["data"]

    def document(self, application_id: str, fmt: str = "pdf") -> bytes:
        """The rendered application, as bytes."""
        return self._request("GET", f"/applications/{application_id}/document.{fmt}", raw=True)

    def submit(self, application_id: str) -> dict[str, Any]:
        """Freeze and submit.

        Raises :class:`MassingBillError` with ``code == "validation_error"`` if
        the application does not tie out; the blocking findings are in the
        server's ``details``.
        """
        return self._request("POST", f"/applications/{application_id}/submit")["data"]

    def change_orders(self, **params: Any) -> Iterator[dict[str, Any]]:
        return self._paginate("/change-orders", **params)
