"""
MCP Integration (Feature 8) — connect telechat to external MCP servers.

Inspired by the Blender MCP connector and Playwright MCP integration. Allows
telechat to extend its capabilities by connecting to any MCP-compatible server
(filesystem, web tools, databases, custom APIs).

The MCP client discovers available tools from connected servers and makes them
available to Claude during conversations.

Usage:
    from telechat_pkg.mcp_client import MCPManager
    mgr = MCPManager()
    mgr.add_server("filesystem", {"command": "npx", "args": ["-y", "@anthropic-ai/mcp-filesystem"]})
    tools = await mgr.list_tools()
    result = await mgr.call_tool("filesystem", "read_file", {"path": "/tmp/test.txt"})
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

MCP_ENABLED = os.getenv("MCP_ENABLED", "false").lower() in ("1", "true", "yes")
MCP_CONFIG_FILE = os.getenv("MCP_CONFIG_FILE", "")


@dataclass
class MCPTool:
    name: str
    description: str
    server: str
    input_schema: dict = field(default_factory=dict)


@dataclass
class MCPServer:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    status: str = "disconnected"  # disconnected | connecting | connected | error
    tools: list[MCPTool] = field(default_factory=list)
    process: Optional[Any] = None


class MCPManager:
    """Manages connections to MCP servers and routes tool calls."""

    def __init__(self):
        self._servers: dict[str, MCPServer] = {}
        self._tools_cache: dict[str, MCPTool] = {}
        self._load_config()

    def _load_config(self):
        """Load MCP server config from file or environment."""
        if MCP_CONFIG_FILE and os.path.exists(MCP_CONFIG_FILE):
            try:
                with open(MCP_CONFIG_FILE) as f:
                    config = json.load(f)
                for name, cfg in config.get("mcpServers", {}).items():
                    self.add_server(name, cfg)
                log.info("Loaded %d MCP servers from config", len(self._servers))
            except Exception as e:
                log.error("Failed to load MCP config: %s", e)

    def add_server(self, name: str, config: dict):
        """Register an MCP server configuration."""
        server = MCPServer(
            name=name,
            command=config.get("command", ""),
            args=config.get("args", []),
            env=config.get("env", {}),
        )
        self._servers[name] = server
        log.info("Added MCP server: %s (%s)", name, server.command)

    def remove_server(self, name: str):
        if name in self._servers:
            del self._servers[name]
            # Clear cached tools from this server
            self._tools_cache = {
                k: v for k, v in self._tools_cache.items() if v.server != name
            }

    async def connect(self, server_name: str) -> bool:
        """Connect to an MCP server and discover its tools."""
        server = self._servers.get(server_name)
        if not server:
            return False

        server.status = "connecting"
        try:
            # Use stdio transport to connect to MCP server
            env = {**os.environ, **server.env}
            proc = await asyncio.create_subprocess_exec(
                server.command, *server.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            server.process = proc
            server.status = "connected"

            # Send initialize request
            init_msg = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "telechat", "version": "1.6.0"},
                },
            }) + "\n"
            proc.stdin.write(init_msg.encode())
            await proc.stdin.drain()

            # Read response
            response = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
            init_result = json.loads(response.decode())

            # List tools
            list_msg = json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }) + "\n"
            proc.stdin.write(list_msg.encode())
            await proc.stdin.drain()

            response = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
            tools_result = json.loads(response.decode())

            for tool_data in tools_result.get("result", {}).get("tools", []):
                tool = MCPTool(
                    name=tool_data["name"],
                    description=tool_data.get("description", ""),
                    server=server_name,
                    input_schema=tool_data.get("inputSchema", {}),
                )
                server.tools.append(tool)
                self._tools_cache[f"{server_name}.{tool.name}"] = tool

            log.info("Connected to MCP server %s, found %d tools", server_name, len(server.tools))
            return True

        except Exception as e:
            server.status = "error"
            log.error("Failed to connect to MCP server %s: %s", server_name, e)
            return False

    async def connect_all(self):
        """Connect to all configured servers."""
        for name in self._servers:
            await self.connect(name)

    async def disconnect(self, server_name: str):
        server = self._servers.get(server_name)
        if server and server.process:
            server.process.terminate()
            await server.process.wait()
            server.status = "disconnected"
            server.tools = []

    async def disconnect_all(self):
        for name in list(self._servers):
            await self.disconnect(name)

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> dict:
        """Call a tool on a specific MCP server."""
        server = self._servers.get(server_name)
        if not server or server.status != "connected" or not server.process:
            return {"error": f"Server {server_name} not connected"}

        try:
            msg = json.dumps({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments,
                },
            }) + "\n"
            server.process.stdin.write(msg.encode())
            await server.process.stdin.drain()

            response = await asyncio.wait_for(server.process.stdout.readline(), timeout=30)
            result = json.loads(response.decode())
            return result.get("result", {})

        except Exception as e:
            log.error("MCP tool call failed: %s.%s: %s", server_name, tool_name, e)
            return {"error": str(e)}

    def list_tools(self) -> list[MCPTool]:
        """List all available tools from connected servers."""
        return list(self._tools_cache.values())

    def list_servers(self) -> list[dict]:
        """List all servers with their status."""
        return [
            {
                "name": s.name,
                "command": s.command,
                "status": s.status,
                "tools_count": len(s.tools),
                "tools": [t.name for t in s.tools],
            }
            for s in self._servers.values()
        ]

    def get_tools_for_prompt(self) -> str:
        """Format available MCP tools for inclusion in Claude's system prompt."""
        tools = self.list_tools()
        if not tools:
            return ""
        lines = ["\n\nAvailable MCP tools:"]
        for t in tools:
            lines.append(f"- {t.server}.{t.name}: {t.description}")
        return "\n".join(lines)


# Singleton
_mcp_manager: MCPManager | None = None


def get_mcp_manager() -> MCPManager:
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPManager()
    return _mcp_manager
