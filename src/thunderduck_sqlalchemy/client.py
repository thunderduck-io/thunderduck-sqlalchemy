"""HTTP transport for the thunderduck API.

This is the only module that speaks HTTP. It deliberately knows nothing about
cursors or SQLAlchemy: it submits SQL, reports execution state, pages results
and cancels runs. The Java JDBC driver's `ApiClient.java` is the reference
implementation of the same protocol.

Two design notes:

* **Polling, not streaming.** Submitting returns immediately with an execution
  id; the run happens in a Kubernetes Job. `await_execution` polls
  `GET /executions/{id}` until a terminal status.
* **The token never appears in a URL or an error message.** It travels only in
  the `Authorization` header, and error construction never interpolates it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from . import exceptions as exc


@dataclass(frozen=True)
class Execution:
    """State of one query run."""

    uuid: str
    status: str
    error_message: str | None = None
    result_location: str | None = None
    result_row_count: int | None = None


@dataclass(frozen=True)
class ResultPage:
    """One page of a stored result set."""

    columns: list[str]
    column_types: list[str]
    rows: list[list[Any]]
    total: int
    offset: int


class NoStoredResult(Exception):
    """The execution stored no result set (a non-SELECT statement).

    Deliberately *not* a PEP 249 error: `dbapi.Cursor` catches it and presents
    an empty result set, which is what a DBAPI caller expects after running
    something that returns no rows.
    """


#: Statuses that mean the run is over, one way or another.
TERMINAL_STATUSES = frozenset({"success", "error", "cancelled"})


class ThunderduckClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        poll_interval: float = 1.0,
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._transport = transport
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(30.0),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )

    # ---- public API ----

    def submit(self, sql: str) -> str:
        payload = self._request("POST", "/queries", json={"sql": sql})
        exec_id = payload.get("id")
        if not exec_id:
            raise exc.InternalError("thunderduck did not return an execution id")
        return str(exec_id)

    def get_execution(self, exec_id: str) -> Execution:
        payload = self._request("GET", f"/executions/{exec_id}")
        return Execution(
            uuid=str(payload.get("uuid", exec_id)),
            status=str(payload.get("status", "")),
            error_message=payload.get("error_message"),
            result_location=payload.get("result_location"),
            result_row_count=payload.get("result_row_count"),
        )

    def await_execution(self, exec_id: str) -> Execution:
        """Poll until the run reaches a terminal status, then return it.

        Raises on `error` and `cancelled` so callers only ever see a
        successful `Execution` come back.
        """
        deadline = time.monotonic() + self._timeout
        while True:
            execution = self.get_execution(exec_id)
            if execution.status in TERMINAL_STATUSES:
                if execution.status == "error":
                    raise exc.ProgrammingError(
                        execution.error_message or "query failed without a message"
                    )
                if execution.status == "cancelled":
                    raise exc.OperationalError("query was cancelled")
                return execution
            if time.monotonic() >= deadline:
                raise exc.OperationalError(
                    f"query timed out after {self._timeout}s (still {execution.status})"
                )
            if self._poll_interval:
                time.sleep(self._poll_interval)

    def fetch_page(self, exec_id: str, limit: int, offset: int) -> ResultPage:
        payload = self._request(
            "GET",
            f"/executions/{exec_id}/result",
            params={"limit": limit, "offset": offset},
            no_result_is_signal=True,
        )
        return ResultPage(
            columns=list(payload.get("columns") or []),
            column_types=list(payload.get("column_types") or []),
            rows=[list(r) for r in (payload.get("rows") or [])],
            total=int(payload.get("total") or 0),
            offset=int(payload.get("offset") or offset),
        )

    def cancel(self, exec_id: str) -> None:
        """Best-effort cancel; a finished or unknown run is not an error.

        "Best effort" covers the run's *state* (already finished, never
        existed) — not the credentials. A 401/403 means the token itself was
        rejected, which must not be swallowed: doing so would make a stop
        button appear to work while quietly hiding a rejected token.
        """
        try:
            self._request("POST", f"/executions/{exec_id}/cancel")
        except exc.InterfaceError:
            raise
        except exc.Error:
            pass

    def catalog_schema(self) -> dict[str, Any]:
        return self._request("GET", "/catalog/schema")

    def download_url(self, exec_id: str) -> dict[str, Any]:
        return self._request("GET", f"/executions/{exec_id}/download-url")

    def download_bytes(self, url: str) -> bytes:
        """GET raw bytes from `url` (typically a presigned S3 URL from
        download_url()). Deliberately a FRESH, header-free client sharing
        only this client's transport, not self._http directly -- a
        presigned URL is self-authenticating via its query string, and
        must never carry our own Bearer token."""
        with httpx.Client(transport=self._transport, timeout=httpx.Timeout(30.0)) as http:
            response = http.get(url)
            response.raise_for_status()
            return response.content

    def close(self) -> None:
        self._http.close()

    # ---- internals ----

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
        no_result_is_signal: bool = False,
    ) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, json=json, params=params)
        except httpx.HTTPError as err:
            # Never chain the original: httpx puts the full URL in its message,
            # and a caller may have embedded a token in a query string.
            raise exc.OperationalError(
                f"could not reach thunderduck: {type(err).__name__}"
            ) from None

        if response.is_success:
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                raise exc.InternalError(
                    "the thunderduck API returned a non-JSON success body"
                ) from None

        detail = self._detail(response)
        status = response.status_code
        if status == 401 or status == 403:
            raise exc.InterfaceError(f"authentication failed (check the API token): {detail}")
        if status == 404 and no_result_is_signal:
            raise NoStoredResult(detail)
        if status == 409:
            raise exc.OperationalError(detail)
        if status == 400 or status == 422:
            raise exc.ProgrammingError(detail)
        raise exc.OperationalError(f"the thunderduck API returned {status}: {detail}")

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        """FastAPI puts the human-readable message in a JSON `detail` field."""
        try:
            body = response.json()
        except ValueError:
            return response.text.strip()[:200] or f"HTTP {response.status_code}"
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
        return str(body)[:200]
