"""
Launch the server the way an MCP client does and check a clean stdio handshake.

    python tests/smoke_install.py uvx --from dist/faostat_mcp-X-py3-none-any.whl faostat-mcp

Runs with no credentials: startup must still succeed, and a tool call must
return the readable "not authenticated" error rather than crash. Any stray
stdout output from the server would corrupt the protocol and fail here.
"""

import asyncio
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = 23


async def main(command: list[str]) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("FAOSTAT_")}
    env["FAOSTAT_DISK_CACHE"] = "false"
    # Empty HOME: no saved credentials, and an empty uv cache, like a new machine.
    env["HOME"] = tempfile.mkdtemp(prefix="faostat-smoke-")
    params = StdioServerParameters(command=command[0], args=command[1:], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await asyncio.wait_for(session.initialize(), timeout=120)
            print(f"initialize: {init.serverInfo.name} {init.serverInfo.version}")

            tools = (await session.list_tools()).tools
            print(f"tools/list: {len(tools)} tools")
            assert len(tools) == EXPECTED_TOOLS, f"expected {EXPECTED_TOOLS} tools, got {len(tools)}"

            result = await session.call_tool("faostat_ping", {})
            payload = json.loads(result.content[0].text)
            print(f"faostat_ping without credentials: {payload}")
            assert payload.get("error") == "FAOSTATAuthError", payload
            assert "faostat_setup" in payload["message"], payload

    print("OK: fresh install launches cleanly")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1:]))
