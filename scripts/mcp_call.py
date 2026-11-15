"""Call an oracle MCP tool from the command line (same code path as the MCP server, via FastMCP's client).

Useful when a Claude Code session started before .mcp.json existed, for scripting, and for debugging:

    uv run python scripts/mcp_call.py [--env FILE] <tool> '<json-args>' | @args.json
    uv run python scripts/mcp_call.py --env data/phase8/bridge.env get_work_packets '{"kind": "coupling"}'

``--env FILE`` loads KEY=VALUE lines (e.g. AFD_ORACLE_DATA_DIR, AFD_ORACLE_NEO4J_PASSWORD) before the server is imported.
Prints the tool's structured result as JSON; on a tool error prints {"error": ...} and exits 1.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import warnings


def main(argv: list[str]) -> int:
    warnings.filterwarnings("ignore")
    if len(argv) >= 2 and argv[0] == "--env":
        for line in open(argv[1], encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()
        argv = argv[2:]
    if not argv:
        print(__doc__)
        return 2
    tool = argv[0]
    raw = argv[1] if len(argv) > 1 else "{}"
    if raw.startswith("@"):  # @path/to/args.json avoids shell-quoting long JSON
        raw = open(raw[1:], encoding="utf-8").read()
    args = json.loads(raw)

    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from afd_oracle_mcp.server import mcp

    async def run() -> int:
        async with Client(mcp) as c:
            try:
                res = await c.call_tool(tool, args)
            except ToolError as e:
                print(json.dumps({"error": str(e)}))
                return 1
            out = res.structured_content if res.structured_content is not None else res.data
            print(json.dumps(out, indent=1, default=str))
            return 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
