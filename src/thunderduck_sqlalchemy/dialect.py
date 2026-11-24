"""The SQLAlchemy dialect.

## Connection URL

    thunderduck://:<tdk_token>@api.thunderduck.ai/

The token is the URL *password* (or `?token=`). It is never interpolated into
a URL that could be logged. `?ssl=false` turns off TLS, for a server you run
yourself that does not terminate it. `?poll_ms=` and `?timeout_ms=` mirror the
JDBC driver's parameter names.

## Reflection without SQL

`get_schema_names` and friends read thunderduck's `/catalog/schema` tree rather
than issuing `information_schema` queries -- issuing SQL here would start a
Kubernetes Job per reflection call. The tree is fetched once per DBAPI
connection and memoised on it.

`/catalog/tables` would give richer metadata (row counts, nullability) but it
is a cache that stays empty until a metadata refresh has run, so reflection
would silently return nothing on a new catalog. `/catalog/schema` is live, and
it is what the JDBC driver uses.

## Why `do_ping` is a lie

`DefaultDialect.do_ping` issues `SELECT 1`. Here that submits a query, starts
a Kubernetes Job and waits seconds for it -- on every pooled connection
checkout. A thunderduck connection holds no server-side state, so it cannot
go stale: reporting healthy without a round trip is both cheaper and more
accurate. Users should also pass `poolclass=NullPool` (see README).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import default
from sqlalchemy.sql import compiler

from . import dbapi as _dbapi
from . import exceptions as exc
from .catalog import flatten, split_schema
from .types import sqlalchemy_type

#: Attribute name used to memoise the flattened catalog on a DBAPI connection.
_CATALOG_CACHE_ATTR = "_thunderduck_catalog_cache"


class ThunderduckIdentifierPreparer(compiler.IdentifierPreparer):
    """Quotes a composite schema segment-by-segment.

    thunderduck schemas are dotted paths (`lego.public`), because SQLAlchemy
    only models two name levels while thunderduck has up to three. Quoting the
    whole thing would emit `"lego.public"` -- a single identifier containing a
    dot, which resolves to nothing. Splitting first emits
    `"lego"."public"`, which is the form DuckDB resolves.
    """

    def quote_schema(self, schema: str, force: Any = None) -> str:
        segments = split_schema(schema)
        return ".".join(self.quote_identifier(segment) for segment in segments)

    def _requires_quotes(self, value: str) -> bool:
        """Quote every identifier, not just the ones SQLAlchemy thinks need it.

        `quote_schema` above always quotes its segments, but the stock
        `format_table` quotes the table name only *conditionally* -- so a plain
        lowercase table came out as `"lego"."public".lego_sets`, mixing the two
        styles. Forcing quotes everywhere makes the output uniform.

        Scope: this is a preparer-wide hook, so it force-quotes every identifier
        SQLAlchemy routes through `quote()` -- columns, labels, aliases, indexes
        and constraints, not only schema and table names. Bind parameters are
        unaffected; they never pass through `quote()`.

        That breadth is safe because **DuckDB identifiers are case-insensitive
        even when quoted** -- verified against duckdb: for a table created as
        `MyTable`, all of `mytable`, `"mytable"` and `"MYTABLE"` resolve to it.
        Unlike Postgres, quoting in DuckDB therefore changes only the rendered
        SQL, never resolution. It also means reserved words and mixed-case names
        need no special handling.

        Server-side resolution is likewise unaffected: the API resolves a table
        from the parsed statement's catalog/db/name, which are unquoted text
        either way.
        """
        return True


class ThunderduckDialect(default.DefaultDialect):
    name = "thunderduck"
    driver = "rest"

    preparer = ThunderduckIdentifierPreparer

    # Every statement is an HTTP round trip plus a Kubernetes Job, so there is
    # nothing to gain from caching compiled forms, and `NullType` columns make
    # cache keys unreliable.
    supports_statement_cache = False

    supports_native_boolean = True
    supports_native_decimal = True
    supports_sane_rowcount = False
    supports_sane_multi_rowcount = False
    postfetch_lastrowid = False
    supports_statement_hints = False
    default_paramstyle = "pyformat"
    # DuckDB folds unquoted identifiers to lower case and is case-insensitive.
    requires_name_normalize = False
    max_identifier_length = 255

    @classmethod
    def import_dbapi(cls) -> Any:
        return _dbapi

    # SQLAlchemy 1.4 compatibility: it looks for `dbapi`, 2.0 for `import_dbapi`.
    @classmethod
    def dbapi(cls) -> Any:
        return _dbapi

    def create_connect_args(self, url: Any) -> tuple[list, dict]:
        query = dict(url.query or {})
        token = url.password or query.pop("token", None)
        if not token:
            raise exc.InterfaceError(
                "no API token in the connection URL -- use "
                "thunderduck://:<tdk_token>@host/ or ?token=<tdk_token>"
            )
        kwargs: dict[str, Any] = {
            "host": url.host,
            "token": token,
            "use_ssl": str(query.pop("ssl", "true")).lower() not in ("false", "0", "no"),
        }
        if url.port:
            kwargs["port"] = int(url.port)
        poll_ms = query.pop("poll_ms", None)
        if poll_ms is not None:
            kwargs["poll_interval"] = int(poll_ms) / 1000.0
        timeout_ms = query.pop("timeout_ms", None)
        if timeout_ms is not None:
            kwargs["timeout"] = int(timeout_ms) / 1000.0
        arraysize = query.pop("arraysize", None)
        if arraysize is not None:
            kwargs["arraysize"] = int(arraysize)
        return [], kwargs

    def do_rollback(self, dbapi_connection: Any) -> None:
        """No transactions to roll back (read-only)."""

    def do_commit(self, dbapi_connection: Any) -> None:
        """No transactions to commit (read-only)."""

    def do_ping(self, dbapi_connection: Any) -> bool:
        """Always healthy. See the module docstring for why this issues no SQL."""
        return True

    # ---- reflection ----

    def _tables(self, connection: Any) -> list:
        """The flattened catalog for this connection, fetched at most once."""
        raw = getattr(connection, "connection", connection)
        raw = getattr(raw, "dbapi_connection", raw)
        cached = getattr(raw, _CATALOG_CACHE_ATTR, None)
        if cached is None:
            cached = flatten(raw.client.catalog_schema())
            setattr(raw, _CATALOG_CACHE_ATTR, cached)
        return cached

    def get_schema_names(self, connection: Any, **kw: Any) -> list[str]:
        return sorted({ref.schema for ref in self._tables(connection)})

    def get_table_names(self, connection: Any, schema: str | None = None, **kw: Any) -> list[str]:
        return [
            ref.table
            for ref in self._tables(connection)
            if ref.schema == schema and ref.kind != "view"
        ]

    def get_view_names(self, connection: Any, schema: str | None = None, **kw: Any) -> list[str]:
        return [
            ref.table
            for ref in self._tables(connection)
            if ref.schema == schema and ref.kind == "view"
        ]

    def get_columns(
        self, connection: Any, table_name: str, schema: str | None = None, **kw: Any
    ) -> list[dict]:
        ref = self._find(connection, table_name, schema)
        if ref is None:
            return []
        return [
            {
                "name": column["name"],
                "type": sqlalchemy_type(column["type"]),
                # /catalog/schema carries no nullability, so report the
                # permissive default rather than inventing a constraint.
                "nullable": True,
                "default": None,
                "autoincrement": False,
            }
            for column in ref.columns
        ]

    def has_table(
        self, connection: Any, table_name: str, schema: str | None = None, **kw: Any
    ) -> bool:
        return self._find(connection, table_name, schema) is not None

    def get_pk_constraint(
        self, connection: Any, table_name: str, schema: str | None = None, **kw: Any
    ) -> dict:
        return {"constrained_columns": [], "name": None}

    def get_foreign_keys(
        self, connection: Any, table_name: str, schema: str | None = None, **kw: Any
    ) -> list:
        return []

    def get_indexes(
        self, connection: Any, table_name: str, schema: str | None = None, **kw: Any
    ) -> list:
        return []

    def get_table_comment(
        self, connection: Any, table_name: str, schema: str | None = None, **kw: Any
    ) -> dict:
        return {"text": None}

    def _find(self, connection: Any, table_name: str, schema: str | None):
        for ref in self._tables(connection):
            if ref.table == table_name and (schema is None or ref.schema == schema):
                return ref
        return None


#: SQLAlchemy resolves `thunderduck://` and `thunderduck+rest://` here via the
#: entry points declared in pyproject.toml.
dialect = ThunderduckDialect
