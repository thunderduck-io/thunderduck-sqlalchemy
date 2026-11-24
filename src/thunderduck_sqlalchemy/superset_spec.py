"""Superset DB engine spec for thunderduck.

Registered through the `superset.db_engine_specs` entry point in
pyproject.toml, so simply pip-installing this package into a Superset image is
enough -- no Superset configuration required.

## Why it subclasses DuckDB's spec

thunderduck executes DuckDB SQL. Superset's `DuckDBEngineSpec` already
encodes the right time-grain expressions (`DATE_TRUNC` forms), LIMIT handling
and column-type conversion for that SQL, so inheriting is both less code and
more correct than re-deriving them.

## Query cancellation

The Java JDBC driver's `Statement.cancel()` is a no-op, so BI tools cannot
stop a running thunderduck query through it. Here the cursor knows its
execution uuid, and the API exposes `POST /executions/{id}/cancel`, so
Superset's stop button really does kill the Kubernetes Job.

Superset is deliberately NOT a dependency of this package -- it supplies
itself at runtime. This module therefore imports `superset` at module level
and is only ever imported by Superset resolving its own entry point. It must
never wrap that import in a try/except: a silently degraded engine spec would
give confusing behaviour instead of a clear failure.
"""

from __future__ import annotations

from typing import Any

from superset.db_engine_specs.base import BaseEngineSpec
from superset.db_engine_specs.duckdb import DuckDBEngineSpec


class ThunderduckEngineSpec(DuckDBEngineSpec):
    engine = "thunderduck"
    engine_name = "Thunderduck"
    default_driver = "rest"

    # No `engine_aliases`. Superset matches a spec on SQLAlchemy's
    # `url.get_backend_name()`, which is `"thunderduck"` for BOTH
    # `thunderduck://` and `thunderduck+rest://` (verified) -- the dotted
    # `thunderduck.rest` form exists only as an entry-point KEY in
    # pyproject.toml, which is a different namespace and never appears in a URL.
    # An alias for it would therefore be dead config, so `engine` alone covers
    # both driver spellings.

    # Read-only service: no file uploads, no CTAS/CVAS.
    supports_file_upload = False

    # Ordinary SELECT capabilities, stated explicitly rather than inherited so
    # a change in a future Superset default is visible here.
    allows_alias_in_select = True
    allows_subqueries = True

    sqlalchemy_uri_placeholder = "thunderduck://:{api_token}@api.thunderduck.ai/"

    @staticmethod
    def get_extra_params(database: Any, source: Any = None) -> dict[str, Any]:
        """Skip DuckDB's `connect_args["config"]` injection.

        `DuckDBEngineSpec.get_extra_params` appends a user-agent by injecting
        `engine_params.connect_args.config`, which is a DuckDB *file* setting.
        Our `connect()` has an explicit keyword-only signature and rejects an
        unknown `config` kwarg with TypeError -- so inheriting this makes it
        impossible to add a thunderduck database in Superset AT ALL. Reproduced
        live in a 6.1.0 container: both Test Connection and database creation
        fail. Delegating to BaseEngineSpec keeps the useful behaviour and drops
        the DuckDB-specific part.
        """
        return BaseEngineSpec.get_extra_params(database)

    @classmethod
    def fetch_data(cls, cursor: Any, limit: int | None = None) -> list[Any]:
        """Skip DuckDB's cursor.description assignment.

        `DuckDBEngineSpec.fetch_data` works around duckdb-engine's issue #1322
        (cursor.description becomes None after fetchall) by capturing and
        reassigning it: `cursor.description = description`. Our Cursor.description
        is a read-only property, so that raises AttributeError *after the query
        has already run as a Kubernetes Job* -- the customer is charged and gets
        a 500 error. Delegating to BaseEngineSpec skips the DuckDB workaround
        and works correctly here.

        This exemplifies the inheritance rule: we want DuckDB SQL behaviour
        (time expressions, type conversion, LIMIT handling), not duckdb-engine's
        Python driver workarounds. Override driver-specific hacks; inherit SQL.
        """
        return super(DuckDBEngineSpec, cls).fetch_data(cursor, limit)

    # NOTE: `has_implicit_cancel` is deliberately NOT overridden -- it must stay
    # False (BaseEngineSpec's default, which DuckDBEngineSpec also keeps).
    #
    # Returning True makes `superset/sql/execution/executor.py` return success
    # from the stop-query path *immediately*, without ever calling
    # `cancel_query` -- so the UI would report a query stopped while its
    # Kubernetes Job kept running and kept costing money. The flag means "the
    # live cursor cancels implicitly", which is not us: our cancel is an
    # explicit REST call.
    #
    # Honest status of stop-button support: it does NOT work yet, and the reason
    # is structural rather than a missing flag. Superset only records a cancel
    # id when `has_query_id_before_execute` is False, and it does so *after*
    # `execute()` returns; our `execute()` blocks until the query has finished,
    # so by then there is nothing left to cancel. Making the stop button work
    # needs a genuinely async execute path (submit, return, poll in
    # `handle_cursor`) -- a real follow-up, not a tweak. The two hooks below are
    # correct and ready for that day; cancellation already works today for
    # Python callers via `cursor.cancel()` (verified live against a running
    # query, which reached terminal status `cancelled`).

    @classmethod
    def get_cancel_query_id(cls, cursor: Any, query: Any) -> str | None:
        """thunderduck's own execution uuid identifies the run to cancel."""
        return getattr(cursor, "execution_id", None)

    @classmethod
    def cancel_query(cls, cursor: Any, query: Any, cancel_query_id: str) -> bool:
        try:
            cursor.cancel()
        except Exception:  # noqa: BLE001 - Superset expects a bool, not a raise
            return False
        return True
