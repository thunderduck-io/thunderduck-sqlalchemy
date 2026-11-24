"""REST transport.

The payload shapes here are the API's; if a test looks over-specific about a
field name, that is deliberate.
"""

import httpx
import pytest

from thunderduck_sqlalchemy import exceptions as exc
from thunderduck_sqlalchemy.client import NoStoredResult

from .conftest import TOKEN, json_response

EXEC_ID = "11111111-2222-3333-4444-555555555555"


def test_submit_returns_execution_id(make_client):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/queries"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return json_response({"id": EXEC_ID, "namespace": "duckexecutions"}, status=201)

    client, _ = make_client(handler)
    assert client.submit("select 1") == EXEC_ID


def test_submit_sends_sql_in_body(make_client):
    import json

    def handler(request):
        assert json.loads(request.content) == {"sql": "select 42"}
        return json_response({"id": EXEC_ID}, status=201)

    client, _ = make_client(handler)
    client.submit("select 42")


def test_submit_401_raises_interface_error_without_leaking_the_token(make_client):
    def handler(request):
        return json_response({"detail": "invalid token"}, status=401)

    client, _ = make_client(handler)
    with pytest.raises(exc.InterfaceError) as caught:
        client.submit("select 1")
    assert TOKEN not in str(caught.value)


def test_submit_500_raises_operational_error_with_the_detail(make_client):
    def handler(request):
        return json_response({"detail": "kubernetes exploded"}, status=500)

    client, _ = make_client(handler)
    with pytest.raises(exc.OperationalError, match="kubernetes exploded"):
        client.submit("select 1")


def test_await_execution_polls_until_success(make_client):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        status = "running" if calls["n"] < 3 else "success"
        return json_response({"uuid": EXEC_ID, "status": status, "result_row_count": 7})

    client, _ = make_client(handler)
    execution = client.await_execution(EXEC_ID)
    assert execution.status == "success"
    assert execution.result_row_count == 7
    assert calls["n"] == 3


def test_await_execution_raises_programming_error_on_query_failure(make_client):
    def handler(request):
        return json_response(
            {"uuid": EXEC_ID, "status": "error", "error_message": "no such table: nope"}
        )

    client, _ = make_client(handler)
    with pytest.raises(exc.ProgrammingError, match="no such table: nope"):
        client.await_execution(EXEC_ID)


def test_await_execution_raises_operational_error_when_cancelled(make_client):
    def handler(request):
        return json_response({"uuid": EXEC_ID, "status": "cancelled"})

    client, _ = make_client(handler)
    with pytest.raises(exc.OperationalError, match="cancelled"):
        client.await_execution(EXEC_ID)


def test_await_execution_times_out(make_client):
    def handler(request):
        return json_response({"uuid": EXEC_ID, "status": "running"})

    client, _ = make_client(handler, timeout=0)
    with pytest.raises(exc.OperationalError, match="timed out"):
        client.await_execution(EXEC_ID)


def test_fetch_page_parses_the_result_page(make_client):
    def handler(request):
        assert request.url.params["limit"] == "2"
        assert request.url.params["offset"] == "4"
        return json_response(
            {
                "execution_uuid": EXEC_ID,
                "result_location": "s3://results/x.parquet",
                "columns": ["a", "b"],
                "column_types": ["BIGINT", "VARCHAR"],
                "rows": [[1, "x"], [2, "y"]],
                "row_count": 2,
                "total": 9,
                "limit": 2,
                "offset": 4,
            }
        )

    client, _ = make_client(handler)
    page = client.fetch_page(EXEC_ID, limit=2, offset=4)
    assert page.columns == ["a", "b"]
    assert page.column_types == ["BIGINT", "VARCHAR"]
    assert page.rows == [[1, "x"], [2, "y"]]
    assert page.total == 9
    assert page.offset == 4


def test_fetch_page_404_signals_no_stored_result(make_client):
    """A non-SELECT query stores no Parquet. That is not an error."""

    def handler(request):
        return json_response({"detail": "no stored result for this execution"}, status=404)

    client, _ = make_client(handler)
    with pytest.raises(NoStoredResult):
        client.fetch_page(EXEC_ID, limit=100, offset=0)


def test_fetch_page_409_tells_the_user_to_re_run(make_client):
    def handler(request):
        return json_response({"detail": "result expired; re-run the query"}, status=409)

    client, _ = make_client(handler)
    with pytest.raises(exc.OperationalError, match="re-run"):
        client.fetch_page(EXEC_ID, limit=100, offset=0)


def test_cancel_posts_to_the_cancel_endpoint(make_client):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == f"/executions/{EXEC_ID}/cancel"
        return json_response({"status": "cancelling"})

    client, _ = make_client(handler)
    client.cancel(EXEC_ID)


def test_cancel_swallows_errors(make_client):
    """Cancel is best-effort: the run may already have finished."""

    def handler(request):
        return json_response({"detail": "execution not found"}, status=404)

    client, _ = make_client(handler)
    client.cancel(EXEC_ID)  # must not raise


def test_cancel_does_not_swallow_auth_failures(make_client):
    """A rejected token must not look like a successful cancel."""

    def handler(request):
        return json_response({"detail": "invalid token"}, status=401)

    client, _ = make_client(handler)
    with pytest.raises(exc.InterfaceError):
        client.cancel(EXEC_ID)


def test_catalog_schema_returns_the_tree(make_client):
    tree = {"catalogs": [{"name": "demo", "tables": [], "namespaces": None}]}

    def handler(request):
        assert request.url.path == "/catalog/schema"
        return json_response(tree)

    client, _ = make_client(handler)
    assert client.catalog_schema() == tree


def test_download_url_returns_the_presigned_payload(make_client):
    payload = {"url": "https://s3.example.test/results/abc.parquet?sig=xyz", "expires_in": 900}

    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/executions/exec-1/download-url"
        return json_response(payload)

    client, _ = make_client(handler)
    assert client.download_url("exec-1") == payload


def test_download_bytes_fetches_the_url_without_the_auth_header(make_client):
    seen_auth = []

    def handler(request):
        if request.url.path == "/queries":
            return json_response({"id": "exec-1"})
        # An absolute cross-host URL, like a real presigned S3 URL.
        assert str(request.url) == "https://s3.example.test/result.parquet"
        seen_auth.append(request.headers.get("authorization"))
        return httpx.Response(200, content=b"parquet-bytes")

    client, seen = make_client(handler)
    data = client.download_bytes("https://s3.example.test/result.parquet")

    assert data == b"parquet-bytes"
    assert seen_auth == [None]  # no Authorization header leaked to the presigned URL


def test_non_json_error_body_still_produces_a_readable_error(make_client):
    def handler(request):
        return httpx.Response(502, text="<html>bad gateway</html>")

    client, _ = make_client(handler)
    with pytest.raises(exc.OperationalError, match="502"):
        client.submit("select 1")


def test_a_transport_failure_does_not_leak_the_token(make_client):
    """httpx's own exception message can carry the full request URL/headers;
    the client must never chain it into a traceback the caller might log."""

    def handler(request):
        raise httpx.ConnectError("connection refused")

    client, _ = make_client(handler)
    with pytest.raises(exc.OperationalError) as caught:
        client.submit("select 1")
    assert TOKEN not in str(caught.value)
    assert "ConnectError" in str(caught.value)
    assert caught.value.__cause__ is None
