"""The PEP 249 surface.

These tests drive a real `Cursor` over a mocked API, so they cover
the submit -> poll -> page choreography end to end without a cluster.
"""

import httpx
import pytest

from thunderduck_sqlalchemy import dbapi
from thunderduck_sqlalchemy import exceptions as exc

EXEC_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def make_connection(handler, **kwargs):
    return dbapi.connect(
        host="api.example.test",
        token="tdk_test_token",
        transport=httpx.MockTransport(handler),
        poll_interval=0,
        **kwargs,
    )


def route(*, rows, columns=("a",), column_types=("BIGINT",), total=None, page_size=None):
    """A handler serving one successful query whose result pages on demand."""
    total = len(rows) if total is None else total

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/queries":
            return httpx.Response(201, json={"id": EXEC_ID})
        if path == f"/executions/{EXEC_ID}":
            return httpx.Response(200, json={"uuid": EXEC_ID, "status": "success"})
        if path == f"/executions/{EXEC_ID}/result":
            limit = int(request.url.params["limit"])
            offset = int(request.url.params["offset"])
            if page_size is not None:
                limit = min(limit, page_size)
            window = rows[offset : offset + limit]
            return httpx.Response(
                200,
                json={
                    "execution_uuid": EXEC_ID,
                    "result_location": "s3://x",
                    "columns": list(columns),
                    "column_types": list(column_types),
                    "rows": window,
                    "row_count": len(window),
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                },
            )
        return httpx.Response(404, json={"detail": f"unexpected {path}"})

    return handler


def test_module_level_dbapi_attributes():
    assert dbapi.apilevel == "2.0"
    assert dbapi.threadsafety == 1
    assert dbapi.paramstyle == "pyformat"
    # SQLAlchemy resolves the exception classes off the DBAPI module.
    assert dbapi.Error is exc.Error
    assert dbapi.ProgrammingError is exc.ProgrammingError


def test_fetchall_returns_every_row():
    conn = make_connection(route(rows=[[1], [2], [3]]))
    cur = conn.cursor()
    cur.execute("select * from t")
    assert cur.fetchall() == [(1,), (2,), (3,)]


def test_fetchone_walks_then_returns_none():
    conn = make_connection(route(rows=[[1], [2]]))
    cur = conn.cursor()
    cur.execute("select * from t")
    assert cur.fetchone() == (1,)
    assert cur.fetchone() == (2,)
    assert cur.fetchone() is None


def test_fetchmany_respects_size_then_drains():
    conn = make_connection(route(rows=[[1], [2], [3]]))
    cur = conn.cursor()
    cur.execute("select * from t")
    assert cur.fetchmany(2) == [(1,), (2,)]
    assert cur.fetchmany(2) == [(3,)]
    assert cur.fetchmany(2) == []


def test_results_page_across_server_page_boundaries():
    """The server caps a page; the cursor must keep asking for more."""
    rows = [[i] for i in range(250)]
    conn = make_connection(route(rows=rows, page_size=100), arraysize=100)
    cur = conn.cursor()
    cur.execute("select * from t")
    assert cur.fetchall() == [(i,) for i in range(250)]


def test_result_size_that_is_an_exact_multiple_of_the_page_size():
    rows = [[i] for i in range(200)]
    conn = make_connection(route(rows=rows, page_size=100), arraysize=100)
    cur = conn.cursor()
    cur.execute("select * from t")
    assert len(cur.fetchall()) == 200
    assert cur.fetchone() is None


def test_empty_result_set():
    conn = make_connection(route(rows=[]))
    cur = conn.cursor()
    cur.execute("select * from t where false")
    assert cur.fetchall() == []
    assert cur.fetchone() is None


def test_a_short_result_set_warns_instead_of_looping_forever(caplog):
    """The server claims 500 rows but can only serve 100.

    Without a guard the paging loop would keep asking from offset 100 and
    keep getting nothing. Stop, and say so loudly -- silently returning 100
    rows for a 500-row result would corrupt someone's dashboard.
    """
    rows = [[i] for i in range(100)]
    conn = make_connection(route(rows=rows, total=500, page_size=100), arraysize=100)
    cur = conn.cursor()
    cur.execute("select * from t")
    with caplog.at_level("WARNING"):
        fetched = cur.fetchall()
    assert len(fetched) == 100
    assert "100" in caplog.text and "500" in caplog.text


def test_description_carries_names_and_types():
    conn = make_connection(
        route(rows=[[1, "x"]], columns=("n", "s"), column_types=("BIGINT", "VARCHAR"))
    )
    cur = conn.cursor()
    cur.execute("select * from t")
    assert [d[0] for d in cur.description] == ["n", "s"]
    assert len(cur.description[0]) == 7  # PEP 249 seven-tuple


def test_rowcount_is_the_total_not_the_page():
    rows = [[i] for i in range(250)]
    conn = make_connection(route(rows=rows, page_size=100), arraysize=100)
    cur = conn.cursor()
    cur.execute("select * from t")
    assert cur.rowcount == 250


def test_rowcount_is_minus_one_before_execute():
    conn = make_connection(route(rows=[]))
    assert conn.cursor().rowcount == -1


def test_non_select_statement_yields_an_empty_result_not_an_error():
    """The API 404s the result endpoint when nothing was stored."""

    def handler(request):
        path = request.url.path
        if request.method == "POST" and path == "/queries":
            return httpx.Response(201, json={"id": EXEC_ID})
        if path == f"/executions/{EXEC_ID}":
            return httpx.Response(200, json={"uuid": EXEC_ID, "status": "success"})
        return httpx.Response(404, json={"detail": "no stored result for this execution"})

    conn = make_connection(handler)
    cur = conn.cursor()
    cur.execute("create table t as select 1")
    assert cur.fetchall() == []
    assert cur.description is None
    assert cur.rowcount == -1


def test_failed_query_raises_with_the_server_message():
    def handler(request):
        if request.method == "POST" and request.url.path == "/queries":
            return httpx.Response(201, json={"id": EXEC_ID})
        return httpx.Response(
            200,
            json={"uuid": EXEC_ID, "status": "error", "error_message": "no such table: nope"},
        )

    conn = make_connection(handler)
    with pytest.raises(exc.ProgrammingError, match="no such table: nope"):
        conn.cursor().execute("select * from nope")


def test_parameters_are_rendered_into_the_statement():
    seen = {}

    def handler(request):
        import json as jsonlib

        if request.method == "POST" and request.url.path == "/queries":
            seen["sql"] = jsonlib.loads(request.content)["sql"]
            return httpx.Response(201, json={"id": EXEC_ID})
        if request.url.path == f"/executions/{EXEC_ID}":
            return httpx.Response(200, json={"uuid": EXEC_ID, "status": "success"})
        return httpx.Response(
            200,
            json={
                "execution_uuid": EXEC_ID,
                "result_location": "s3://x",
                "columns": ["a"],
                "column_types": ["BIGINT"],
                "rows": [],
                "row_count": 0,
                "total": 0,
                "limit": 1000,
                "offset": 0,
            },
        )

    conn = make_connection(handler)
    conn.cursor().execute("select * from t where s = %(s)s", {"s": "O'Brien"})
    assert seen["sql"] == "select * from t where s = 'O''Brien'"


def test_execution_id_is_exposed_for_cancellation():
    conn = make_connection(route(rows=[[1]]))
    cur = conn.cursor()
    cur.execute("select 1")
    assert cur.execution_id == EXEC_ID


def test_cancel_posts_to_the_cancel_endpoint():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/queries":
            return httpx.Response(201, json={"id": EXEC_ID})
        if request.url.path == f"/executions/{EXEC_ID}/cancel":
            return httpx.Response(200, json={"status": "cancelling"})
        if request.url.path == f"/executions/{EXEC_ID}":
            return httpx.Response(200, json={"uuid": EXEC_ID, "status": "success"})
        return httpx.Response(
            200,
            json={
                "execution_uuid": EXEC_ID,
                "result_location": "s3://x",
                "columns": ["a"],
                "column_types": ["BIGINT"],
                "rows": [[1]],
                "row_count": 1,
                "total": 1,
                "limit": 1000,
                "offset": 0,
            },
        )

    conn = make_connection(handler)
    cur = conn.cursor()
    cur.execute("select 1")
    cur.cancel()
    assert ("POST", f"/executions/{EXEC_ID}/cancel") in calls


def test_cancel_before_execute_is_a_no_op():
    conn = make_connection(route(rows=[]))
    conn.cursor().cancel()  # must not raise


def test_fetch_before_execute_raises():
    conn = make_connection(route(rows=[]))
    with pytest.raises(exc.ProgrammingError):
        conn.cursor().fetchone()


def test_using_a_closed_cursor_raises():
    conn = make_connection(route(rows=[[1]]))
    cur = conn.cursor()
    cur.close()
    with pytest.raises(exc.ProgrammingError):
        cur.execute("select 1")


def test_commit_and_rollback_are_no_ops():
    conn = make_connection(route(rows=[]))
    conn.commit()
    conn.rollback()


def test_executemany_is_not_supported():
    conn = make_connection(route(rows=[]))
    with pytest.raises(exc.NotSupportedError):
        conn.cursor().executemany("select %s", [(1,), (2,)])


def test_connect_requires_a_token():
    with pytest.raises(exc.InterfaceError, match="token"):
        dbapi.connect(host="api.example.test", token="")


def test_connect_builds_an_https_base_url_by_default():
    conn = make_connection(route(rows=[]))
    assert str(conn.client._http.base_url).startswith("https://")


def test_use_ssl_false_builds_an_http_base_url():
    conn = make_connection(route(rows=[]), use_ssl=False)
    assert str(conn.client._http.base_url).startswith("http://")


def test_port_is_included_in_the_base_url():
    conn = make_connection(route(rows=[]), port=8000)
    assert ":8000" in str(conn.client._http.base_url)
