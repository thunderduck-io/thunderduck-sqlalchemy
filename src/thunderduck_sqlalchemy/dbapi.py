"""PEP 249 interface over the thunderduck REST API.

## What a "connection" is here

Nothing on the server. The API is stateless per request, so a
`Connection` is just a configured HTTP client. That has three consequences
worth knowing:

* There is no transaction. `commit()` and `rollback()` are no-ops and the
  dialect reports read-only.
* Opening a connection costs nothing, so `NullPool` is the recommended
  SQLAlchemy pool and pre-ping is pointless (a `SELECT 1` would start a
  Kubernetes Job -- see `dialect.do_ping`).
* A `Cursor` owns one execution. `cancel()` reaches the run through its
  execution id, which is what the Superset engine spec uses to make the stop
  button work.

## Row paging

`execute()` waits for the run to finish, then fetches the first page. The
server clamps `limit` to its own maximum, so the cursor must keep requesting
pages by `offset` until it has `total` rows -- never assume the requested
`limit` was honoured.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import exceptions as exc
from .client import NoStoredResult, ThunderduckClient
from .exceptions import (
    DatabaseError,
    DataError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
)
from .types import render_sql, sqlalchemy_type

_log = logging.getLogger(__name__)

apilevel = "2.0"
#: 1 == "threads may share the module, but not connections".
threadsafety = 1
paramstyle = "pyformat"

__all__ = [
    "apilevel",
    "threadsafety",
    "paramstyle",
    "connect",
    "Connection",
    "Cursor",
    "Error",
    "InterfaceError",
    "DatabaseError",
    "DataError",
    "OperationalError",
    "IntegrityError",
    "InternalError",
    "ProgrammingError",
    "NotSupportedError",
]

DEFAULT_ARRAYSIZE = 1000


def connect(
    *,
    host: str,
    token: str,
    port: int | None = None,
    use_ssl: bool = True,
    poll_interval: float = 1.0,
    timeout: float = 300.0,
    arraysize: int = DEFAULT_ARRAYSIZE,
    transport: httpx.BaseTransport | None = None,
) -> Connection:
    if not host:
        raise exc.InterfaceError("a thunderduck host is required")
    if not token:
        raise exc.InterfaceError(
            "an API token is required -- pass it as the URL password or ?token=…"
        )
    scheme = "https" if use_ssl else "http"
    netloc = f"{host}:{port}" if port else host
    client = ThunderduckClient(
        base_url=f"{scheme}://{netloc}",
        token=token,
        poll_interval=poll_interval,
        timeout=timeout,
        transport=transport,
    )
    return Connection(client, arraysize=arraysize)


class Connection:
    def __init__(self, client: ThunderduckClient, *, arraysize: int = DEFAULT_ARRAYSIZE) -> None:
        self.client = client
        self._arraysize = arraysize
        self._closed = False

    def cursor(self) -> Cursor:
        if self._closed:
            raise exc.ProgrammingError("connection is closed")
        return Cursor(self.client, arraysize=self._arraysize)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.client.close()

    def commit(self) -> None:
        """No-op: thunderduck has no transactions (read-only driver)."""

    def rollback(self) -> None:
        """No-op: see `commit`."""

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class Cursor:
    def __init__(self, client: ThunderduckClient, *, arraysize: int = DEFAULT_ARRAYSIZE) -> None:
        self._client = client
        self.arraysize = arraysize
        self._closed = False
        self._exec_id: str | None = None
        self._description: list[tuple] | None = None
        self._rowcount = -1
        self._buffer: list[tuple] = []
        self._buffer_pos = 0
        self._next_offset = 0
        self._total = 0
        self._exhausted = True

    # ---- introspection ----

    @property
    def description(self) -> list[tuple] | None:
        return self._description

    @property
    def rowcount(self) -> int:
        return self._rowcount

    @property
    def execution_id(self) -> str | None:
        """The thunderduck execution uuid of the last `execute`, if any."""
        return self._exec_id

    # ---- execution ----

    def execute(self, operation: str, parameters: Any = None) -> Cursor:
        self._check_open()
        self._reset()
        sql = render_sql(operation, parameters)
        self._exec_id = self._client.submit(sql)
        self._client.await_execution(self._exec_id)
        self._load_first_page()
        return self

    def executemany(self, operation: str, seq_of_parameters: Any) -> None:
        raise exc.NotSupportedError(
            "executemany is not supported: thunderduck is read-only and every "
            "statement is a separate Kubernetes Job"
        )

    def cancel(self) -> None:
        """Best-effort cancel of the in-flight (or last) execution."""
        if self._exec_id:
            self._client.cancel(self._exec_id)

    # ---- fetching ----

    def fetchone(self) -> tuple | None:
        self._check_executed()
        if not self._ensure_buffered():
            return None
        row = self._buffer[self._buffer_pos]
        self._buffer_pos += 1
        return row

    def fetchmany(self, size: int | None = None) -> list[tuple]:
        self._check_executed()
        want = self.arraysize if size is None else size
        out: list[tuple] = []
        while len(out) < want:
            row = self.fetchone()
            if row is None:
                break
            out.append(row)
        return out

    def fetchall(self) -> list[tuple]:
        self._check_executed()
        out: list[tuple] = []
        while True:
            row = self.fetchone()
            if row is None:
                return out
            out.append(row)

    def close(self) -> None:
        self._closed = True

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    # ---- internals ----

    def _reset(self) -> None:
        self._description = None
        self._rowcount = -1
        self._buffer = []
        self._buffer_pos = 0
        self._next_offset = 0
        self._total = 0
        self._exhausted = True

    def _load_first_page(self) -> None:
        assert self._exec_id is not None
        try:
            page = self._client.fetch_page(self._exec_id, limit=self.arraysize, offset=0)
        except NoStoredResult:
            # A non-SELECT statement: no result set, and that is fine.
            self._description = None
            self._rowcount = -1
            self._buffer = []
            self._exhausted = True
            return
        self._description = [
            (
                name,
                sqlalchemy_type(type_name),
                None,  # display_size
                None,  # internal_size
                None,  # precision
                None,  # scale
                True,  # null_ok -- /catalog/schema carries no nullability
            )
            for name, type_name in zip(
                page.columns,
                list(page.column_types) + [""] * (len(page.columns) - len(page.column_types)),
                strict=False,
            )
        ]
        self._total = page.total
        self._rowcount = page.total
        self._buffer = [tuple(r) for r in page.rows]
        self._buffer_pos = 0
        self._next_offset = len(page.rows)
        self._exhausted = self._next_offset >= self._total or not page.rows

    def _ensure_buffered(self) -> bool:
        """True if a row is available at `_buffer_pos`, paging if needed."""
        if self._buffer_pos < len(self._buffer):
            return True
        if self._exhausted:
            return False
        assert self._exec_id is not None
        page = self._client.fetch_page(
            self._exec_id, limit=self.arraysize, offset=self._next_offset
        )
        if not page.rows:
            # The result set claims more rows than it can serve. Never silently
            # return a short result -- a truncated dashboard looks correct.
            _log.warning(
                "thunderduck result set is short: served %d of %d claimed rows "
                "for execution %s; the stored result may have been truncated",
                self._next_offset,
                self._total,
                self._exec_id,
            )
            self._exhausted = True
            return False
        self._buffer = [tuple(r) for r in page.rows]
        self._buffer_pos = 0
        self._next_offset += len(page.rows)
        self._total = page.total or self._total
        self._exhausted = self._next_offset >= self._total
        return True

    def _check_open(self) -> None:
        if self._closed:
            raise exc.ProgrammingError("cursor is closed")

    def _check_executed(self) -> None:
        self._check_open()
        if self._exec_id is None:
            raise exc.ProgrammingError("no query has been executed on this cursor")
