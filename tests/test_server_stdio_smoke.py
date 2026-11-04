"""End-to-end: start the real stdio server and talk MCP to it.

Every other test calls dispatch.call() directly, so none of them import
server.py against the installed mcp SDK. That is how Dependabot #36 (allow
mcp 2.x) went green while server.py failed at import with
"'Server' object has no attribute 'list_tools'" - every new install would
have been broken. This test is the one that catches it.
"""

from __future__ import annotations

import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from cloudprice_mcp import __version__

SERVER = StdioServerParameters(command=sys.executable, args=["-m", "cloudprice_mcp.server"])


@pytest.mark.asyncio
async def test_server_starts_lists_tools_and_answers():
    async with stdio_client(SERVER) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.serverInfo.version == __version__
        assert "pricing" in (init.instructions or "")

        names = {t.name for t in (await session.list_tools()).tools}
        assert {"compare_clouds", "get_price_history", "optimize_commitment"} <= names
        assert len(names) >= 25

        result = await session.call_tool("get_aws_price", {"instance_type": "t3.medium"})
        assert not result.isError
        assert "t3.medium" in result.content[0].text
