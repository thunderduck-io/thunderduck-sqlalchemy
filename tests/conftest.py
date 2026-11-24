"""Test helpers.

Every test drives the client through `httpx.MockTransport`, so nothing here
ever touches the network. `make_client` returns a client wired to a handler
you supply, plus the list of requests it saw so tests can assert on
call counts (which is how "do_ping issues no query" is verified).
"""

import httpx
import pytest

from thunderduck_sqlalchemy.client import ThunderduckClient

TOKEN = "tdk_test_token"


@pytest.fixture
def make_client():
    def _make(handler, **kwargs):
        seen: list[httpx.Request] = []

        def recording_handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        client = ThunderduckClient(
            base_url="https://api.example.test",
            token=TOKEN,
            transport=httpx.MockTransport(recording_handler),
            poll_interval=0,
            **kwargs,
        )
        return client, seen

    return _make


def json_response(payload, status=200):
    return httpx.Response(status, json=payload)
