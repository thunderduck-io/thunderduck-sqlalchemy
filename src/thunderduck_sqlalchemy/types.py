"""DuckDB type names -> SQLAlchemy types, and Python values -> SQL literals.

## Two directions, two failure policies

Reading (`sqlalchemy_type`) is a *display* concern: an unrecognised type name
degrades to `NullType` so an exotic column never breaks a whole query.

Writing (`render_literal`) is a *security* boundary. The API's
`POST /queries` accepts raw SQL only -- there is no server-side parameter
bind -- so this module renders literals into the statement. An unrecognised
Python type therefore raises: a silent `str()` fallback is precisely how a
wrong-type bug or an injected string reaches generated SQL.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from decimal import Decimal
from typing import Any

from sqlalchemy import types as sqltypes

from . import exceptions as exc

# `Uuid` arrived in SQLAlchemy 2.0. This package supports 1.4 as well (Superset
# 6.1.0 pins sqlalchemy<2), so resolve it dynamically rather than importing it:
# on 1.4 a UUID column reflects as String, which is usable, instead of the
# module failing to import at all.
_UUID_TYPE: Any = getattr(sqltypes, "Uuid", sqltypes.String)

# NOTE: `/catalog/schema` reports the SOURCE system's type names, not DuckDB's.
# A PostgreSQL-backed catalog yields `character varying`, `double precision`,
# `jsonb` and friends -- verified against the live deployment, where a table's
# columns came back as `integer`, `character varying`, `character`. A
# MySQL-backed one yields `datetime`, `longtext`, `tinyint` and friends. So this
# map has to cover all three vocabularies; a miss is not cosmetic, because a
# column that degrades to NullType loses its filters and groupings in a BI tool.
_SIMPLE_TYPES: dict[str, Any] = {
    "BOOLEAN": sqltypes.Boolean,
    "BOOL": sqltypes.Boolean,
    # --- PostgreSQL spellings ---
    "CHARACTER VARYING": sqltypes.String,
    "CHARACTER": sqltypes.String,
    "BPCHAR": sqltypes.String,
    "DOUBLE PRECISION": sqltypes.Float,
    "JSONB": sqltypes.JSON,
    "SMALLSERIAL": sqltypes.SmallInteger,
    "SERIAL": sqltypes.Integer,
    "BIGSERIAL": sqltypes.BigInteger,
    "OID": sqltypes.Integer,
    "MONEY": sqltypes.Numeric,
    "INET": sqltypes.String,
    "CIDR": sqltypes.String,
    "MACADDR": sqltypes.String,
    "XML": sqltypes.String,
    "BIT": sqltypes.String,
    "BIT VARYING": sqltypes.String,
    # --- MySQL spellings ---
    # A MySQL-backed catalog reports `information_schema.columns.data_type`,
    # i.e. the bare type name: `datetime`, `longtext`, `mediumint`, ... Sizes
    # and params are already handled by the strip-and-retry below, so only the
    # names PostgreSQL and DuckDB never emit need listing here.
    "DATETIME": sqltypes.DateTime,
    "MEDIUMINT": sqltypes.Integer,
    "TINYTEXT": sqltypes.String,
    "MEDIUMTEXT": sqltypes.String,
    "LONGTEXT": sqltypes.String,
    "TINYBLOB": sqltypes.LargeBinary,
    "MEDIUMBLOB": sqltypes.LargeBinary,
    "LONGBLOB": sqltypes.LargeBinary,
    "BINARY": sqltypes.LargeBinary,
    "VARBINARY": sqltypes.LargeBinary,
    # ENUM/SET are string-valued to any client that isn't MySQL itself.
    "ENUM": sqltypes.String,
    "SET": sqltypes.String,
    "YEAR": sqltypes.Integer,
    "TINYINT": sqltypes.SmallInteger,
    "SMALLINT": sqltypes.SmallInteger,
    "INTEGER": sqltypes.Integer,
    "INT": sqltypes.Integer,
    "BIGINT": sqltypes.BigInteger,
    "HUGEINT": sqltypes.BigInteger,
    "UBIGINT": sqltypes.BigInteger,
    "UINTEGER": sqltypes.Integer,
    "USMALLINT": sqltypes.SmallInteger,
    "UTINYINT": sqltypes.SmallInteger,
    "FLOAT": sqltypes.Float,
    "REAL": sqltypes.Float,
    "DOUBLE": sqltypes.Float,
    "VARCHAR": sqltypes.String,
    "TEXT": sqltypes.String,
    "STRING": sqltypes.String,
    "DATE": sqltypes.Date,
    "TIME": sqltypes.Time,
    "BLOB": sqltypes.LargeBinary,
    "BYTEA": sqltypes.LargeBinary,
    "UUID": _UUID_TYPE,
    "JSON": sqltypes.JSON,
    "INTERVAL": sqltypes.Interval,
}

_DECIMAL_RE = re.compile(r"^(?:DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)$")


def sqlalchemy_type(duckdb_type: str) -> Any:
    """Map one `column_types` entry onto a SQLAlchemy type instance."""
    name = (duckdb_type or "").strip().upper()

    if name.endswith("[]"):
        return sqltypes.ARRAY(sqlalchemy_type(name[:-2]))

    if name.startswith("TIMESTAMP"):
        # TIMESTAMP, TIMESTAMP WITH TIME ZONE, TIMESTAMPTZ, TIMESTAMP_NS ...
        timezone = "WITH TIME ZONE" in name or name.endswith("TZ")
        return sqltypes.DateTime(timezone=timezone)

    # Must come AFTER the TIMESTAMP branch -- "TIMESTAMP..." also starts with
    # "TIME". Covers PostgreSQL's `time without time zone` / `time with time
    # zone` as well as a bare `TIME`.
    if name.startswith("TIME"):
        return sqltypes.Time(timezone="WITH TIME ZONE" in name)

    decimal_match = _DECIMAL_RE.match(name)
    if decimal_match:
        precision, scale = decimal_match.groups()
        return sqltypes.Numeric(precision=int(precision), scale=int(scale))
    if name in ("DECIMAL", "NUMERIC"):
        return sqltypes.Numeric()

    simple = _SIMPLE_TYPES.get(name)
    if simple is not None:
        return simple()

    # A bare `ARRAY` with no element type -- PostgreSQL reports this for array
    # columns. Keep it an ARRAY so a consumer still knows the shape, with an
    # unknown item type rather than losing the column entirely.
    if name == "ARRAY":
        return sqltypes.ARRAY(sqltypes.NullType())

    # VARCHAR(255) and friends: strip the parameters and retry once.
    base = name.split("(", 1)[0].strip()
    simple = _SIMPLE_TYPES.get(base)
    if simple is not None:
        return simple()

    return sqltypes.NullType()


def render_literal(value: Any) -> str:
    """Render one Python value as a DuckDB SQL literal.

    Strict by design: anything not explicitly handled raises.
    """
    if value is None:
        return "NULL"
    # bool before int -- bool is a subclass of int in Python.
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise exc.DataError(f"cannot render non-finite float {value!r} as SQL")
        return repr(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise exc.DataError(f"cannot render non-finite decimal {value!r} as SQL")
        return str(value)
    if isinstance(value, str):
        return _quote_string(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"unhex('{bytes(value).hex()}')::BLOB"
    if isinstance(value, dt.datetime):
        return f"'{value.isoformat(sep=' ')}'::TIMESTAMP"
    if isinstance(value, dt.date):
        return f"'{value.isoformat()}'::DATE"
    if isinstance(value, dt.time):
        return f"'{value.isoformat()}'::TIME"
    if isinstance(value, (list, tuple, set, frozenset)):
        items = ", ".join(render_literal(v) for v in value)
        return f"[{items}]"
    raise exc.ProgrammingError(
        f"cannot bind a value of type {type(value).__name__} -- "
        "thunderduck renders parameters client-side and only supports "
        "None, bool, int, float, Decimal, str, bytes, date/time/datetime and sequences"
    )


def _quote_string(value: str) -> str:
    if "\x00" in value:
        raise exc.DataError("cannot bind a string containing a NUL byte")
    # DuckDB does not process backslash escapes in standard single-quoted
    # strings, so doubling the quote character is the whole escape.
    return "'" + value.replace("'", "''") + "'"


def render_sql(sql: str, params: Any) -> str:
    """Substitute `pyformat` parameters into `sql`.

    With no params the statement is returned untouched -- important, because
    literal `%` signs are common in SQL (`like '%x%'`) and must not be treated
    as placeholders.
    """
    if params is None or (hasattr(params, "__len__") and len(params) == 0 and not sql_needs(sql)):
        return sql
    if isinstance(params, dict):
        try:
            return sql % {k: render_literal(v) for k, v in params.items()}
        except KeyError as err:
            raise exc.ProgrammingError(f"missing bind parameter {err.args[0]!r}") from None
        except (TypeError, ValueError) as err:
            raise exc.ProgrammingError(_placeholder_hint(err)) from None
    if isinstance(params, (list, tuple)):
        rendered = tuple(render_literal(v) for v in params)
        try:
            return sql % rendered
        except (TypeError, ValueError) as err:
            raise exc.ProgrammingError(_placeholder_hint(err, len(rendered))) from None
    raise exc.ProgrammingError(
        f"parameters must be a mapping or a sequence, got {type(params).__name__}"
    )


def sql_needs(sql: str) -> bool:
    """True if the statement contains a pyformat placeholder."""
    return "%(" in sql or "%s" in sql


def _placeholder_hint(err: Exception, supplied: int | None = None) -> str:
    """Turn Python's opaque format error into actionable advice.

    Python does NOT distinguish the two ways this fails: both "too few
    parameters supplied" and "the statement contains an unescaped literal
    percent sign" surface as
    `TypeError: not enough arguments for format string` (verified on 3.10-3.13
    -- an earlier version of this code wrongly assumed ValueError and reported
    every failure as a count mismatch). Since we cannot tell them apart, the
    message names both causes rather than guessing.

    `paramstyle` is `pyformat`, so -- exactly as with psycopg2 -- a literal
    percent sign must be written `%%` when parameters are supplied. SQLAlchemy
    does this automatically when it compiles; hand-written raw SQL does not.
    """
    counted = f" {supplied} parameter(s) were supplied." if supplied is not None else ""
    return (
        f"could not bind parameters ({err}).{counted} Either the number of "
        "placeholders does not match the parameters, or the statement contains "
        "a literal percent sign that must be escaped as '%%' "
        "(e.g. LIKE '%%abc%%') -- thunderduck uses the 'pyformat' paramstyle"
    )
