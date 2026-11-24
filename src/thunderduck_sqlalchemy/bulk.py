"""Bulk result access: a presigned URL to a query's full, already-stored
Parquet result, for notebook/data-collab workflows pulling large result
sets -- bypasses the row-paginated GET /executions/{id}/result endpoint
the DBAPI cursor uses, which is fine for a bounded preview but wasteful
for bulk transfer (every row pays JSON overhead, instead of the caller
reading the stored object directly).

Module-level functions, not Cursor methods: the DBAPI Cursor's contract
is fixed (pandas' read_sql, Superset, and Airflow all depend on its exact
shape), so this is a separate, additive surface instead.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import sqlalchemy


@contextmanager
def _pooled_client(engine: sqlalchemy.Engine):
    """Check out a pooled DBAPI connection and yield its ThunderduckClient,
    returning the connection to the pool on exit (`raw.close()` on a
    pooled connection returns it to the pool -- it does not close the
    underlying socket; verified this doesn't exhaust a pool_size=1 engine
    across repeated checkouts). Without this, every fetch_result_url/
    fetch_dataframe call would leak one pooled connection permanently.

    `driver_connection` is the SQLAlchemy 2.0+ accessor; `.connection` is
    the legacy one this package's `sqlalchemy>=1.4` floor still needs to
    support (using `.connection` unconditionally raises a real
    SADeprecationWarning on 2.0 -- verified against the installed 2.0.52).
    """
    raw = engine.raw_connection()
    try:
        connection = getattr(raw, "driver_connection", None) or raw.connection
        yield connection.client
    finally:
        raw.close()


def fetch_result_url(engine: sqlalchemy.Engine, sql: str) -> dict[str, Any]:
    """Submit `sql` fresh and return {"url": str, "expires_in": int} for
    its full, untruncated stored result. Always available -- no new
    dependency. Raises the same exceptions a normal cursor execute()
    would on a failed/cancelled/timed-out query."""
    with _pooled_client(engine) as client:
        exec_id = client.submit(sql)
        client.await_execution(exec_id)
        return client.download_url(exec_id)


_SUPPORTED_DATAFRAME_BACKENDS = ("pandas", "polars")


def fetch_dataframe(engine: sqlalchemy.Engine, sql: str, backend: str) -> Any:
    """Convenience wrapper: submits `sql`, fetches the presigned URL's
    bytes into memory via the SAME pooled connection (client.download_bytes
    -- not a bare httpx.get(), and not fetch_result_url() separately, which
    would check out and return a SECOND pooled connection for no reason),
    and parses them with the requested backend. pandas/pyarrow and polars
    are optional extras (thunderduck-sqlalchemy[pandas],
    thunderduck-sqlalchemy[polars]) -- imported lazily here, not required
    by the base package."""
    if backend not in _SUPPORTED_DATAFRAME_BACKENDS:
        raise ValueError(
            f"unsupported backend {backend!r}; expected one of {_SUPPORTED_DATAFRAME_BACKENDS} "
            f"(pip install thunderduck-sqlalchemy[pandas] or thunderduck-sqlalchemy[polars])"
        )

    import io

    with _pooled_client(engine) as client:
        exec_id = client.submit(sql)
        client.await_execution(exec_id)
        payload = client.download_url(exec_id)
        data = io.BytesIO(client.download_bytes(payload["url"]))

    if backend == "pandas":
        try:
            import pandas as pd
        except ImportError as exc:
            raise ImportError(
                "fetch_dataframe(backend='pandas') needs pandas + pyarrow: "
                "pip install thunderduck-sqlalchemy[pandas]"
            ) from exc
        return pd.read_parquet(data)

    try:
        import polars as pl
    except ImportError as exc:
        raise ImportError(
            "fetch_dataframe(backend='polars') needs polars: "
            "pip install thunderduck-sqlalchemy[polars]"
        ) from exc
    return pl.read_parquet(data)
