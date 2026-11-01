"""Streamable HTTP clients across SDKs with and without the optional SSE event limit."""

import asyncio
from contextlib import asynccontextmanager

import pytest


@pytest.mark.parametrize("supports_event_limit,signature_error", [
    (False, None), (True, None), (False, TypeError), (False, ValueError),
])
def test_streamable_http_optional_event_limit_and_client_cleanup(monkeypatch, supports_event_limit, signature_error):
    from tools import mcp_tool as core
    from tools.mcp_tool_errors import _MCP_HTTP_MAX_BODY_BYTES

    assert core._ensure_mcp_sdk()
    monkeypatch.setattr(core, "_MCP_NEW_HTTP", True)
    clients, limits = [], []
    streams = (object(), object())

    @asynccontextmanager
    async def older_client(url, *, http_client, terminate_on_close=True):
        clients.append(http_client)
        assert not http_client.is_closed
        yield streams

    @asynccontextmanager
    async def newer_client(url, *, http_client, terminate_on_close=True, max_sse_event_size=None):
        limits.append(max_sse_event_size)
        async with older_client(url, http_client=http_client) as result:
            yield result

    class OpaqueClient:
        @property
        def __signature__(self):
            raise signature_error("SDK callable has no inspectable signature")

        def __call__(self, url, *, http_client):
            return older_client(url, http_client=http_client)

    client = newer_client if supports_event_limit else older_client
    monkeypatch.setattr(core, "streamable_http_client", OpaqueClient() if signature_error else client)
    server = core.MCPServerTask("compat")

    async def connect():
        async with server._streamable_http_transport(
            "http://127.0.0.1:8000/mcp", {}, 5.0, True, None, None, False, set()
        ) as result:
            assert result is streams

    asyncio.run(connect())
    assert len(clients) == 1 and clients[0].is_closed
    assert limits == ([_MCP_HTTP_MAX_BODY_BYTES] if supports_event_limit else [])


def test_streamable_http_internal_type_error_is_not_retried_and_client_is_closed(monkeypatch):
    from tools import mcp_tool as core

    assert core._ensure_mcp_sdk()
    monkeypatch.setattr(core, "_MCP_NEW_HTTP", True)
    clients = []
    failure = TypeError("SDK implementation failure, not an unsupported keyword")

    @asynccontextmanager
    async def broken_client(url, *, http_client, max_sse_event_size=None):
        clients.append(http_client)
        raise failure
        yield  # pragma: no cover -- async context manager must be an async generator

    monkeypatch.setattr(core, "streamable_http_client", broken_client)
    server = core.MCPServerTask("compat")

    async def connect():
        async with server._streamable_http_transport(
            "http://127.0.0.1:8000/mcp", {}, 5.0, True, None, None, False, set()
        ):
            pytest.fail("the SDK's TypeError must propagate")

    with pytest.raises(TypeError) as exc:
        asyncio.run(connect())
    assert exc.value is failure
    assert len(clients) == 1 and clients[0].is_closed
