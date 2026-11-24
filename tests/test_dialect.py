"""The SQLAlchemy dialect: URL parsing, quoting and reflection."""

import httpx
import pytest
from sqlalchemy import Column, MetaData, Table, create_engine, select
from sqlalchemy.engine.url import make_url

from thunderduck_sqlalchemy import exceptions as exc
from thunderduck_sqlalchemy.dialect import ThunderduckDialect

from .test_catalog import TREE

TOKEN = "tdk_test_token"


def connect_args(url: str):
    dialect = ThunderduckDialect()
    args, kwargs = dialect.create_connect_args(make_url(url))
    return args, kwargs


# ---- URL handling ----


def test_token_comes_from_the_url_password():
    _, kwargs = connect_args(f"thunderduck://:{TOKEN}@api.example.test/")
    assert kwargs["token"] == TOKEN
    assert kwargs["host"] == "api.example.test"
    assert kwargs["use_ssl"] is True


def test_token_can_come_from_a_query_parameter():
    _, kwargs = connect_args(f"thunderduck://api.example.test/?token={TOKEN}")
    assert kwargs["token"] == TOKEN


def test_port_is_passed_through():
    _, kwargs = connect_args(f"thunderduck://:{TOKEN}@db.example.test:8000/")
    assert kwargs["port"] == 8000


def test_ssl_false_disables_tls():
    _, kwargs = connect_args(f"thunderduck://:{TOKEN}@db.example.test:8000/?ssl=false")
    assert kwargs["use_ssl"] is False


def test_poll_interval_and_timeout_are_configurable():
    _, kwargs = connect_args(
        f"thunderduck://:{TOKEN}@api.example.test/?poll_ms=250&timeout_ms=60000"
    )
    assert kwargs["poll_interval"] == 0.25
    assert kwargs["timeout"] == 60.0


def test_arraysize_is_configurable():
    """The page size for result fetching, documented in the module docstring."""
    _, kwargs = connect_args(f"thunderduck://:{TOKEN}@api.example.test/?arraysize=500")
    assert kwargs["arraysize"] == 500


def test_a_missing_token_fails_clearly():
    with pytest.raises(exc.InterfaceError, match="token"):
        connect_args("thunderduck://api.example.test/")


def test_the_token_never_appears_in_the_connect_args_url():
    args, kwargs = connect_args(f"thunderduck://:{TOKEN}@api.example.test/")
    assert TOKEN not in repr(args)
    assert kwargs["token"] == TOKEN  # only as its own keyword


# ---- quoting ----


def test_quote_schema_splits_a_composite_schema():
    preparer = ThunderduckDialect().identifier_preparer
    assert preparer.quote_schema("lego.public") == '"lego"."public"'


def test_quote_schema_handles_a_single_segment():
    preparer = ThunderduckDialect().identifier_preparer
    assert preparer.quote_schema("thunder_duck_demo") == '"thunder_duck_demo"'


def test_compiled_sql_for_a_three_level_table():
    table = Table("lego_sets", MetaData(), Column("set_num"), schema="lego.public")
    sql = str(select(table.c.set_num).compile(dialect=ThunderduckDialect()))
    assert '"lego"."public"."lego_sets"' in sql


def test_compiled_sql_for_a_two_level_table():
    table = Table("iris", MetaData(), Column("species"), schema="thunder_duck_demo")
    sql = str(select(table.c.species).compile(dialect=ThunderduckDialect()))
    assert '"thunder_duck_demo"."iris"' in sql


# ---- reflection ----


def catalog_engine(tree=TREE, extra_handler=None):
    """An engine whose /catalog/schema returns `tree`."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/catalog/schema":
            return httpx.Response(200, json=tree)
        if extra_handler is not None:
            return extra_handler(request)
        return httpx.Response(404, json={"detail": f"unexpected {request.url.path}"})

    engine = create_engine(
        f"thunderduck://:{TOKEN}@api.example.test/",
        connect_args={"transport": httpx.MockTransport(handler), "poll_interval": 0},
    )
    return engine, calls


def test_get_schema_names_lists_both_catalog_shapes():
    engine, _ = catalog_engine()
    with engine.connect() as conn:
        assert sorted(engine.dialect.get_schema_names(conn)) == [
            "lego.public",
            "thunder_duck_demo",
        ]


def test_get_table_names_is_scoped_to_the_schema():
    engine, _ = catalog_engine()
    with engine.connect() as conn:
        assert engine.dialect.get_table_names(conn, schema="lego.public") == ["lego_sets"]
        assert engine.dialect.get_table_names(conn, schema="thunder_duck_demo") == ["iris"]


def test_get_view_names_returns_views_only():
    engine, _ = catalog_engine()
    with engine.connect() as conn:
        assert engine.dialect.get_view_names(conn, schema="lego.public") == ["themes_v"]


def test_get_columns_maps_types():
    from sqlalchemy import types as sqltypes

    engine, _ = catalog_engine()
    with engine.connect() as conn:
        columns = engine.dialect.get_columns(conn, "iris", schema="thunder_duck_demo")
    assert [c["name"] for c in columns] == ["sepal_length", "species"]
    assert isinstance(columns[0]["type"], sqltypes.Float)
    assert columns[0]["nullable"] is True


def test_has_table():
    engine, _ = catalog_engine()
    with engine.connect() as conn:
        assert engine.dialect.has_table(conn, "iris", schema="thunder_duck_demo") is True
        assert engine.dialect.has_table(conn, "nope", schema="thunder_duck_demo") is False


def test_reflection_reuses_one_catalog_fetch_per_connection():
    engine, calls = catalog_engine()
    with engine.connect() as conn:
        engine.dialect.get_schema_names(conn)
        engine.dialect.get_table_names(conn, schema="lego.public")
        engine.dialect.get_columns(conn, "iris", schema="thunder_duck_demo")
    assert calls.count("GET /catalog/schema") == 1


def test_empty_reflection_helpers_do_not_query():
    engine, _ = catalog_engine()
    with engine.connect() as conn:
        assert engine.dialect.get_pk_constraint(conn, "iris", schema="thunder_duck_demo") == {
            "constrained_columns": [],
            "name": None,
        }
        assert engine.dialect.get_foreign_keys(conn, "iris", schema="thunder_duck_demo") == []
        assert engine.dialect.get_indexes(conn, "iris", schema="thunder_duck_demo") == []


# ---- ping ----


def test_do_ping_makes_no_http_call_at_all():
    """A SELECT 1 here would start a Kubernetes Job. It must never happen."""
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    engine = create_engine(
        f"thunderduck://:{TOKEN}@api.example.test/",
        connect_args={"transport": httpx.MockTransport(handler), "poll_interval": 0},
    )
    with engine.connect() as conn:
        # 1.4 and 2.0 differ on how the raw DBAPI connection is reached.
        raw = getattr(conn.connection, "dbapi_connection", conn.connection)
        calls.clear()
        assert engine.dialect.do_ping(raw) is True
    assert calls == []


def test_dialect_reports_itself_as_read_only():
    dialect = ThunderduckDialect()
    assert dialect.name == "thunderduck"
    assert dialect.driver == "rest"
    assert dialect.supports_statement_cache is False
