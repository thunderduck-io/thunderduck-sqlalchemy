"""Flattening the /catalog/schema tree.

The payload shape is the API's `CatalogSchema`.
Nessie catalogs populate `tables`; flat-file catalogs populate `namespaces`.
"""

from thunderduck_sqlalchemy.catalog import TableRef, flatten, split_schema

TREE = {
    "catalogs": [
        {
            # 2-level: a nessie catalog, tables hang directly off the catalog.
            "name": "thunder_duck_demo",
            "type": "nessie_iceberg",
            "tables": [
                {
                    "name": "iris",
                    "kind": "table",
                    "columns": [
                        {"name": "sepal_length", "type": "DOUBLE"},
                        {"name": "species", "type": "VARCHAR"},
                    ],
                }
            ],
            "namespaces": None,
        },
        {
            # 3-level: a flat-files catalog with named namespaces.
            "name": "lego",
            "type": "flatfiles_minio",
            "tables": None,
            "namespaces": [
                {
                    "name": "public",
                    "tables": [
                        {
                            "name": "lego_sets",
                            "kind": "table",
                            "columns": [{"name": "set_num", "type": "VARCHAR"}],
                        },
                        {"name": "themes_v", "kind": "view", "columns": []},
                    ],
                }
            ],
        },
    ]
}


def test_two_level_catalog_yields_a_bare_catalog_schema():
    refs = flatten(TREE)
    iris = next(r for r in refs if r.table == "iris")
    assert iris.schema == "thunder_duck_demo"


def test_three_level_catalog_yields_a_composite_schema():
    refs = flatten(TREE)
    sets = next(r for r in refs if r.table == "lego_sets")
    assert sets.schema == "lego.public"


def test_flatten_returns_every_table_and_view():
    refs = flatten(TREE)
    assert {(r.schema, r.table) for r in refs} == {
        ("thunder_duck_demo", "iris"),
        ("lego.public", "lego_sets"),
        ("lego.public", "themes_v"),
    }


def test_columns_are_carried_through():
    refs = flatten(TREE)
    iris = next(r for r in refs if r.table == "iris")
    assert [c["name"] for c in iris.columns] == ["sepal_length", "species"]
    assert iris.columns[0]["type"] == "DOUBLE"


def test_kind_is_normalised():
    refs = flatten(TREE)
    kinds = {r.table: r.kind for r in refs}
    assert kinds["lego_sets"] == "table"
    assert kinds["themes_v"] == "view"


def test_a_catalog_that_failed_to_introspect_is_skipped_not_fatal():
    """`error` is set when the API could not reach a catalog."""
    tree = {
        "catalogs": [
            {"name": "broken", "error": "connection refused", "tables": None, "namespaces": None},
            {"name": "ok", "tables": [{"name": "t", "kind": "table", "columns": []}]},
        ]
    }
    refs = flatten(tree)
    assert [r.schema for r in refs] == ["ok"]


def test_missing_and_empty_payloads_are_tolerated():
    assert flatten({}) == []
    assert flatten({"catalogs": []}) == []
    assert flatten({"catalogs": [{"name": "empty"}]}) == []


def test_a_table_without_columns_still_appears():
    tree = {"catalogs": [{"name": "c", "tables": [{"name": "t"}]}]}
    assert flatten(tree) == [TableRef(schema="c", table="t", kind="table", columns=[])]


def test_split_schema():
    assert split_schema("lego.public") == ["lego", "public"]
    assert split_schema("thunder_duck_demo") == ["thunder_duck_demo"]
    assert split_schema("") == []
