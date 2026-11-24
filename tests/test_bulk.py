"""fetch_result_url / fetch_dataframe: presigned-URL bulk result access.

Drives a real sqlalchemy.Engine through httpx.MockTransport, exactly like
test_dbapi.py does at the DBAPI layer -- these tests go one layer up,
through create_engine(), since fetch_result_url takes a full Engine.
"""

import httpx
import pytest
from sqlalchemy import create_engine

from thunderduck_sqlalchemy.bulk import fetch_result_url

TOKEN = "tdk_test_token"
EXEC_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def make_engine(handler):
    return create_engine(
        f"thunderduck://:{TOKEN}@api.example.test/",
        connect_args={"transport": httpx.MockTransport(handler), "poll_interval": 0},
    )


def test_fetch_result_url_submits_polls_and_returns_the_presigned_payload():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/queries":
            return httpx.Response(201, json={"id": EXEC_ID})
        if request.url.path == f"/executions/{EXEC_ID}":
            return httpx.Response(200, json={"uuid": EXEC_ID, "status": "success"})
        if request.url.path == f"/executions/{EXEC_ID}/download-url":
            return httpx.Response(
                200,
                json={
                    "url": "https://s3.example.test/results/abc.parquet?sig=xyz",
                    "expires_in": 900,
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    engine = make_engine(handler)
    result = fetch_result_url(engine, "select * from demo.public.customers")
    engine.dispose()

    assert result == {
        "url": "https://s3.example.test/results/abc.parquet?sig=xyz",
        "expires_in": 900,
    }
    assert ("POST", "/queries") in calls
    assert ("GET", f"/executions/{EXEC_ID}/download-url") in calls


def test_fetch_result_url_raises_on_a_failed_query():
    from thunderduck_sqlalchemy import exceptions as exc

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/queries":
            return httpx.Response(201, json={"id": EXEC_ID})
        if request.url.path == f"/executions/{EXEC_ID}":
            return httpx.Response(
                200, json={"uuid": EXEC_ID, "status": "error", "error_message": "boom"}
            )
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    engine = make_engine(handler)
    with pytest.raises(exc.ProgrammingError, match="boom"):
        fetch_result_url(engine, "select 1")
    engine.dispose()


def test_fetch_dataframe_pandas_reads_the_presigned_parquet(tmp_path):
    pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    import pandas as pd

    from thunderduck_sqlalchemy.bulk import fetch_dataframe

    frame = pd.DataFrame({"a": [1, 2, 3]})
    parquet_path = tmp_path / "result.parquet"
    frame.to_parquet(parquet_path)
    parquet_bytes = parquet_path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/queries":
            return httpx.Response(201, json={"id": EXEC_ID})
        if request.url.path == f"/executions/{EXEC_ID}":
            return httpx.Response(200, json={"uuid": EXEC_ID, "status": "success"})
        if request.url.path == f"/executions/{EXEC_ID}/download-url":
            return httpx.Response(
                200, json={"url": "https://s3.example.test/result.parquet", "expires_in": 900}
            )
        # An absolute cross-host URL (like a real presigned S3 URL) --
        # str(request.url), not request.url.path, since .path alone would
        # only ever be "/result.parquet" regardless of host.
        if str(request.url) == "https://s3.example.test/result.parquet":
            return httpx.Response(200, content=parquet_bytes)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    engine = make_engine(handler)
    result = fetch_dataframe(engine, "select * from demo.public.t", backend="pandas")
    engine.dispose()

    pd.testing.assert_frame_equal(result, frame)


def test_fetch_dataframe_unknown_backend_raises_a_clear_error():
    from thunderduck_sqlalchemy.bulk import fetch_dataframe

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made before backend validation")

    engine = make_engine(handler)
    with pytest.raises(ValueError, match="pandas.*polars"):
        fetch_dataframe(engine, "select 1", backend="not-a-real-backend")
    engine.dispose()
