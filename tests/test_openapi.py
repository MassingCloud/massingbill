"""The OpenAPI document must describe the API that actually exists.

A specification that drifts from the code is worse than none: it is a
confident, wrong answer. So this compares the document against Flask's own URL
map in both directions -- an undocumented route and a documented route that
does not exist are both failures.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from flask import Flask

SPEC_PATH = Path(__file__).resolve().parents[1] / "docs" / "openapi" / "massingbill-v1.yaml"

#: Flask writes ``<converter:name>``; OpenAPI writes ``{name}``.
_PARAM = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")

METHODS = {"get", "post", "put", "patch", "delete"}


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


def flask_operations(app: Flask) -> set[tuple[str, str]]:
    found = set()
    for rule in app.url_map.iter_rules():
        path = str(rule)
        if not path.startswith("/api/massingbill/v1"):
            continue
        for method in rule.methods or set():
            if method.lower() in METHODS:
                found.add((method.lower(), _PARAM.sub(r"{\1}", path)))
    return found


def spec_operations(spec: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, operations in spec["paths"].items()
        for method in operations
        if method in METHODS
    }


def test_the_document_parses_as_openapi_31(spec: dict[str, Any]) -> None:
    assert spec["openapi"].startswith("3.1")
    assert spec["info"]["title"]
    assert spec["info"]["version"]


def test_every_route_is_documented(app: Flask, spec: dict[str, Any]) -> None:
    undocumented = flask_operations(app) - spec_operations(spec)
    assert not undocumented, f"routes missing from the OpenAPI document: {sorted(undocumented)}"


def test_every_documented_route_exists(app: Flask, spec: dict[str, Any]) -> None:
    """The direction people forget, and the one that misleads a client author."""
    phantom = spec_operations(spec) - flask_operations(app)
    assert not phantom, f"documented routes that do not exist: {sorted(phantom)}"


def test_every_operation_has_an_id(spec: dict[str, Any]) -> None:
    """Generators name client methods after these; a missing one becomes junk."""
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method in METHODS:
                assert operation.get("operationId"), f"{method.upper()} {path} has no operationId"


def test_operation_ids_are_unique(spec: dict[str, Any]) -> None:
    ids = [
        operation["operationId"]
        for operations in spec["paths"].values()
        for method, operation in operations.items()
        if method in METHODS
    ]
    assert len(ids) == len(set(ids))


def test_every_reference_resolves(spec: dict[str, Any]) -> None:
    """A dangling ``$ref`` renders the document unusable to every generator."""

    def walk(node: Any) -> list[str]:
        if isinstance(node, dict):
            refs = [node["$ref"]] if "$ref" in node else []
            return refs + [r for value in node.values() for r in walk(value)]
        if isinstance(node, list):
            return [r for item in node for r in walk(item)]
        return []

    for ref in walk(spec):
        assert ref.startswith("#/"), f"external $ref not allowed: {ref}"
        target: Any = spec
        for part in ref[2:].split("/"):
            assert part in target, f"unresolvable $ref: {ref}"
            target = target[part]


def test_the_error_codes_match_the_ones_the_app_raises(spec: dict[str, Any]) -> None:
    """The error-code table is a massing convention (SPEC.md 3.1), so it is not
    allowed to quietly grow a code the document does not list."""
    from massingbill import errors

    documented = set(spec["components"]["schemas"]["Error"]["properties"]["error"]["enum"])
    raised = {
        value.code
        for value in vars(errors).values()
        if isinstance(value, type)
        and issubclass(value, errors.MassingBillError)
        and value is not errors.MassingBillError
    }
    assert raised <= documented, f"undocumented error codes: {sorted(raised - documented)}"


def test_every_money_field_is_documented_as_money(spec: dict[str, Any]) -> None:
    """The whole product rests on amounts never being floats.

    A schema that describes an amount as ``number`` would invite exactly the
    client-side float the engine refuses to use.
    """
    schemas = spec["components"]["schemas"]
    money_ref = "#/components/schemas/Money"

    for name, schema in schemas.items():
        for field, definition in (schema.get("properties") or {}).items():
            looks_monetary = (
                field.endswith(("_sum", "_cents"))
                # line1..line9, not the "lines" collection.
                or (len(field) > 4 and field.startswith("line") and field[4].isdigit())
                or field
                in {"amount", "scheduled_value", "certified_payment", "original_contract_sum"}
                or field.startswith("col_")
            )
            if not looks_monetary or name == "Money":
                continue
            assert definition.get("$ref") == money_ref, (
                f"{name}.{field} looks like an amount but is not a Money object"
            )
