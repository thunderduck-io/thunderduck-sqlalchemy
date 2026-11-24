import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import types as sqltypes

from thunderduck_sqlalchemy import exceptions as exc
from thunderduck_sqlalchemy.types import render_literal, render_sql, sqlalchemy_type


@pytest.mark.parametrize(
    ("duckdb_type", "expected"),
    [
        ("BIGINT", sqltypes.BigInteger),
        ("INTEGER", sqltypes.Integer),
        ("SMALLINT", sqltypes.SmallInteger),
        ("TINYINT", sqltypes.SmallInteger),
        ("HUGEINT", sqltypes.BigInteger),
        ("DOUBLE", sqltypes.Float),
        ("FLOAT", sqltypes.Float),
        ("REAL", sqltypes.Float),
        ("VARCHAR", sqltypes.String),
        ("BOOLEAN", sqltypes.Boolean),
        ("DATE", sqltypes.Date),
        ("TIME", sqltypes.Time),
        ("TIMESTAMP", sqltypes.DateTime),
        ("BLOB", sqltypes.LargeBinary),
        ("JSON", sqltypes.JSON),
    ],
)
def test_known_types_map_to_sqlalchemy_types(duckdb_type, expected):
    assert isinstance(sqlalchemy_type(duckdb_type), expected)


def test_uuid_maps_to_the_best_type_the_installed_sqlalchemy_has():
    """`sqltypes.Uuid` only exists on SQLAlchemy 2.0+.

    Superset 6.1.0 pins sqlalchemy<2, so on 1.4 a UUID column must still
    reflect as something usable (String) rather than crashing on import.
    """
    import sqlalchemy

    mapped = sqlalchemy_type("UUID")
    if sqlalchemy.__version__.startswith("1.4"):
        assert isinstance(mapped, sqltypes.String)
    else:
        assert isinstance(mapped, sqltypes.Uuid)


def test_type_names_are_matched_case_insensitively():
    assert isinstance(sqlalchemy_type("bigint"), sqltypes.BigInteger)


@pytest.mark.parametrize(
    ("pg_type", "expected"),
    [
        # Every one of these was observed coming back from the live
        # /catalog/schema for PostgreSQL-backed catalogs. They are the source
        # system's names, not DuckDB's.
        ("character varying", sqltypes.String),
        ("character varying(255)", sqltypes.String),
        ("character", sqltypes.String),
        ("text", sqltypes.String),
        ("double precision", sqltypes.Float),
        ("jsonb", sqltypes.JSON),
        ("timestamp without time zone", sqltypes.DateTime),
        ("timestamp with time zone", sqltypes.DateTime),
        ("time without time zone", sqltypes.Time),
        ("numeric", sqltypes.Numeric),
        ("oid", sqltypes.Integer),
        ("inet", sqltypes.String),
        ("money", sqltypes.Numeric),
        ("serial", sqltypes.Integer),
        ("bigserial", sqltypes.BigInteger),
    ],
)
def test_postgres_type_names_map_too(pg_type, expected):
    """A miss here is not cosmetic: a NullType column loses its filters and
    groupings in a BI tool, which is the whole point of this driver."""
    assert isinstance(sqlalchemy_type(pg_type), expected)


@pytest.mark.parametrize(
    ("mysql_type", "expected"),
    [
        # MySQL-backed catalogs report `information_schema.columns.data_type`,
        # i.e. the bare type name.
        ("datetime", sqltypes.DateTime),
        ("timestamp", sqltypes.DateTime),
        ("tinyint", sqltypes.SmallInteger),
        ("mediumint", sqltypes.Integer),
        ("longtext", sqltypes.String),
        ("mediumtext", sqltypes.String),
        ("tinytext", sqltypes.String),
        ("varbinary", sqltypes.LargeBinary),
        ("longblob", sqltypes.LargeBinary),
        ("enum", sqltypes.String),
        ("set", sqltypes.String),
        ("year", sqltypes.Integer),
        ("json", sqltypes.JSON),
        ("decimal", sqltypes.Numeric),
    ],
)
def test_mysql_type_names_map_too(mysql_type, expected):
    """Same stakes as the PostgreSQL case above: a NullType column loses its
    filters and groupings in a BI tool."""
    assert isinstance(sqlalchemy_type(mysql_type), expected)


def test_mysql_datetime_is_naive_but_a_bare_timestamp_is_not_mistaken_for_time():
    """`DATETIME` must not fall through the `TIME`-prefix branch — it starts
    with `DATE` — and must not come back timezone-aware."""
    mapped = sqlalchemy_type("datetime")
    assert isinstance(mapped, sqltypes.DateTime)
    assert mapped.timezone is False


def test_a_bare_array_stays_an_array():
    """PostgreSQL reports `ARRAY` with no element type."""
    mapped = sqlalchemy_type("ARRAY")
    assert isinstance(mapped, sqltypes.ARRAY)


def test_timezone_awareness_is_read_from_postgres_spellings():
    assert sqlalchemy_type("timestamp with time zone").timezone is True
    assert sqlalchemy_type("timestamp without time zone").timezone is False


def test_timestamp_with_time_zone_is_timezone_aware():
    mapped = sqlalchemy_type("TIMESTAMP WITH TIME ZONE")
    assert isinstance(mapped, sqltypes.DateTime)
    assert mapped.timezone is True


def test_decimal_precision_and_scale_are_preserved():
    mapped = sqlalchemy_type("DECIMAL(18,3)")
    assert isinstance(mapped, sqltypes.Numeric)
    assert (mapped.precision, mapped.scale) == (18, 3)


def test_parametrized_simple_types_strip_parameters_and_retry():
    """VARCHAR(255) and similar: the parameter-stripping fallback must work."""
    mapped = sqlalchemy_type("VARCHAR(100)")
    assert isinstance(mapped, sqltypes.String)


@pytest.mark.parametrize("duckdb_type", ["STRUCT(a INTEGER)", "MAP(VARCHAR, INTEGER)", "WAT"])
def test_unmapped_types_degrade_to_nulltype(duckdb_type):
    """Display concern, not a correctness one — degrade rather than raise."""
    assert isinstance(sqlalchemy_type(duckdb_type), sqltypes.NullType)


def test_list_types_map_to_array():
    assert isinstance(sqlalchemy_type("VARCHAR[]"), sqltypes.ARRAY)


# ---- literal rendering ----


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "NULL"),
        (True, "TRUE"),
        (False, "FALSE"),
        (42, "42"),
        (-7, "-7"),
        (2.5, "2.5"),
        (Decimal("1.230"), "1.230"),
        ("hello", "'hello'"),
        ("", "''"),
        (dt.date(2026, 8, 19), "'2026-08-19'::DATE"),
        (dt.time(13, 45, 1), "'13:45:01'::TIME"),
        ([1, 2], "[1, 2]"),
        ((1, "a"), "[1, 'a']"),
    ],
)
def test_render_literal(value, expected):
    assert render_literal(value) == expected


def test_datetime_renders_as_a_timestamp_literal():
    rendered = render_literal(dt.datetime(2026, 8, 19, 13, 45, 1))
    assert rendered == "'2026-08-19 13:45:01'::TIMESTAMP"


def test_single_quotes_are_doubled():
    assert render_literal("O'Brien") == "'O''Brien'"


def test_backslashes_are_preserved_literally():
    """DuckDB does not process backslash escapes in standard strings."""
    assert render_literal("a\\b") == "'a\\b'"


def test_bytes_render_as_a_blob():
    assert render_literal(b"\xaa\xbb") == "unhex('aabb')::BLOB"


def test_nul_bytes_are_rejected():
    with pytest.raises(exc.DataError):
        render_literal("a\x00b")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_rejected(value):
    with pytest.raises(exc.DataError):
        render_literal(value)


def test_unknown_types_raise_rather_than_being_stringified():
    class Custom:
        def __str__(self):
            return "1 OR 1=1"

    with pytest.raises(exc.ProgrammingError):
        render_literal(Custom())


# ---- statement rendering ----


def test_render_sql_without_params_is_unchanged():
    assert render_sql("select 1", None) == "select 1"


def test_render_sql_leaves_percent_signs_alone_when_there_are_no_params():
    assert render_sql("select 'a%b'", None) == "select 'a%b'"


def test_render_sql_substitutes_named_params():
    got = render_sql("select * from t where a = %(a)s and b = %(b)s", {"a": 1, "b": "x"})
    assert got == "select * from t where a = 1 and b = 'x'"


def test_render_sql_substitutes_positional_params():
    got = render_sql("select * from t where a = %s and b = %s", (1, "x"))
    assert got == "select * from t where a = 1 and b = 'x'"


def test_render_sql_reports_a_missing_named_param():
    with pytest.raises(exc.ProgrammingError, match="missing"):
        render_sql("select %(a)s", {})


def test_render_sql_reports_a_positional_count_mismatch():
    with pytest.raises(exc.ProgrammingError, match="placeholders") as caught:
        render_sql("select %s, %s", (1,))
    # The message must say how many were supplied, since Python's own error
    # doesn't and that is the first thing a caller needs to know.
    assert "1 parameter(s) were supplied" in str(caught.value)


def test_a_positional_percent_problem_is_not_misreported_as_a_count_mismatch():
    """Python raises the same TypeError for both causes, so the message must
    name both rather than asserting a count mismatch that may be wrong."""
    with pytest.raises(exc.ProgrammingError, match="%%"):
        render_sql("select %s, '%x'", ("1",))


def test_render_sql_does_not_let_a_string_param_inject_sql():
    got = render_sql("select %(a)s", {"a": "'; drop table t; --"})
    assert got == "select '''; drop table t; --'"


def test_an_unescaped_percent_alongside_params_gives_a_clear_error():
    """pyformat's rule, same as psycopg2: a literal % must be written %%.

    Without this branch the caller gets a bare
    `ValueError: unsupported format character` from the % operator, which says
    nothing about what to do.
    """
    with pytest.raises(exc.ProgrammingError, match="%%"):
        render_sql("select %(a)s where s like '%x'", {"a": 1})


def test_an_escaped_percent_survives():
    assert render_sql("select %(a)s, '%%x'", {"a": 1}) == "select 1, '%x'"
