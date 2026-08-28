"""MCP（Model Context Protocol）客户端管理：stdio 模式第三方工具接入。

- 从 McpServer 表读取启用的 server 配置（command + args）。
- 使用 fastmcp.Client（基于 mcp 1.x，兼容 uvicorn/uvloop 事件循环）。
- list_mcp_tools / call_mcp_tool 对外为 async（供 scheduler await）。
"""
from __future__ import annotations

from typing import Any

from .db import SessionLocal
from .models import McpServer


def get_enabled_servers() -> list[dict]:
    """返回启用的 MCP server 配置。"""
    db = SessionLocal()
    try:
        rows = db.query(McpServer).filter(McpServer.enabled == True).all()  # noqa: E712
        return [
            {"name": r.name, "command": r.command, "args": r.args or []}
            for r in rows
        ]
    finally:
        db.close()


def _transport(cfg: dict) -> dict:
    """生成 fastmcp.Client 的 dict transport（MCPConfig 结构）。"""
    return {
        "mcpServers": {
            cfg["name"]: {
                "command": cfg["command"],
                "args": cfg["args"],
            }
        }
    }


async def _list_tools_for(cfg: dict) -> list[dict]:
    from fastmcp import Client
    out: list[dict] = []
    transport = _transport(cfg)
    async with Client(transport) as client:
        tools = await client.list_tools()
        for t in tools:
            out.append(
                {
                    "server": cfg["name"],
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": t.inputSchema or {},
                }
            )
    return out


async def list_mcp_tools() -> list[dict]:
    """连接所有启用的 MCP server，返回 [{server, name, description, input_schema}]。"""
    out: list[dict] = []
    for cfg in get_enabled_servers():
        try:
            out.extend(await _list_tools_for(cfg))
        except Exception as e:  # noqa: BLE001
            out.append(
                {
                    "server": cfg["name"],
                    "name": "__error__",
                    "description": f"MCP server {cfg['name']} 连接失败：{e}",
                    "input_schema": {},
                }
            )
    return out


async def call_mcp_tool(server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
    """调用某个 MCP server 的工具，返回文本结果。"""
    from fastmcp import Client

    cfg = next((s for s in get_enabled_servers() if s["name"] == server_name), None)
    if not cfg:
        return f"MCP server 不存在：{server_name}"

    try:
        transport = _transport(cfg)
        async with Client(transport) as client:
            result = await client.call_tool(tool_name, arguments=arguments)
            # result.content 是文本/资源块列表
            parts = []
            for c in result.content or []:
                if hasattr(c, "text"):
                    parts.append(c.text)
                else:
                    parts.append(str(c))
            return "\n".join(parts) or "（无输出）"
    except Exception as e:  # noqa: BLE001
        return f"MCP 工具调用出错：{e}"
