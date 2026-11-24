"""Flatten the API's `/catalog/schema` tree into `(schema, table)` pairs.

## Two catalog shapes, one rule

thunderduck names are not uniformly three-part. Nessie catalogs are 2-level
(`catalog.table`) and put their tables directly under the catalog; flat-file
catalogs are 3-level (`catalog.schema.table`) and put them under named
namespaces. SQLAlchemy and Superset both model exactly two levels.

The rule that reconciles them: **the composite `schema` is the whole dotted
prefix and the `table` is the leaf.** So `iris` in the nessie catalog
`thunder_duck_demo` has schema `"thunder_duck_demo"`, while `lego_sets` has
schema `"lego.public"`. The dialect's identifier preparer later splits that
prefix back apart and quotes each segment, so both compile correctly.

A catalog carrying an `error` (the API could not introspect it) is skipped
rather than raised on: one unreachable catalog must not break reflection of
every other one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TableRef:
    schema: str
    table: str
    kind: str = "table"
    columns: list[dict[str, str]] = field(default_factory=list)


def flatten(tree: dict[str, Any] | None) -> list[TableRef]:
    refs: list[TableRef] = []
    for catalog in (tree or {}).get("catalogs") or []:
        if catalog.get("error"):
            continue
        catalog_name = catalog.get("name")
        if not catalog_name:
            continue
        for table in catalog.get("tables") or []:
            ref = _make_ref(catalog_name, table)
            if ref:
                refs.append(ref)
        for namespace in catalog.get("namespaces") or []:
            namespace_name = namespace.get("name")
            if not namespace_name:
                continue
            schema = f"{catalog_name}.{namespace_name}"
            for table in namespace.get("tables") or []:
                ref = _make_ref(schema, table)
                if ref:
                    refs.append(ref)
    return refs


def _make_ref(schema: str, table: dict[str, Any]) -> TableRef | None:
    name = table.get("name")
    if not name:
        return None
    kind = str(table.get("kind") or "table").lower()
    columns = [
        {"name": str(c.get("name")), "type": str(c.get("type") or "")}
        for c in (table.get("columns") or [])
        if c.get("name")
    ]
    return TableRef(schema=schema, table=str(name), kind=kind, columns=columns)


def split_schema(schema: str) -> list[str]:
    """Split a composite schema into its segments (`"lego.public"` -> 2)."""
    if not schema:
        return []
    return schema.split(".")
