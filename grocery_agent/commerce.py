"""Connections to the shopping apps, each one an MCP server listed in servers.json.

Each server's tools are offered to Claude under a "<server>__<tool>" name. Tools
that place orders are routed through the safety rails in guard.py, and on a
compare-only server every cart or order tool is blocked outright.
"""

from __future__ import annotations

import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger(__name__)

DEFAULT_ORDER_PATTERN = r"(checkout|place_?order|create_?order|confirm_?order|submit_?order|(^|_)pay($|_))"
DEFAULT_CART_PATTERN = r"(cart|basket|checkout|order|pay)"
_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")


@dataclass
class Server:
    name: str            # short id, e.g. "zepto"
    label: str           # shown to the model, e.g. "Zepto"
    can_order: bool      # False = price comparison only
    order_pattern: re.Pattern
    cart_pattern: re.Pattern
    # Checkout must name the payment method. Turn off only for an app whose checkout tool
    # has no payment argument AND whose account has cash on delivery disabled.
    payment_arg_required: bool = True
    session: ClientSession | None = None
    tools: dict[str, dict] = field(default_factory=dict)  # model-facing name -> definition


class Commerce:
    """Opens every configured MCP server and keeps the sessions alive."""

    def __init__(self, configs: list[dict]):
        self.configs = configs
        self.servers: dict[str, Server] = {}
        self._routes: dict[str, tuple[Server, str]] = {}
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "Commerce":
        for cfg in self.configs:
            try:
                await self._connect(cfg)
            except Exception:  # one broken app must not take the others down
                log.exception("Could not connect to %s; continuing without it", cfg.get("name"))
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    async def _connect(self, cfg: dict) -> None:
        name = _NAME_OK.sub("_", cfg["name"])[:20]
        if cfg.get("transport", "stdio") == "stdio":
            params = StdioServerParameters(command=cfg["command"], args=cfg.get("args", []),
                                           env=cfg.get("env"))
            read, write = await self._stack.enter_async_context(stdio_client(params))
        else:
            import httpx2
            from mcp.client.streamable_http import streamable_http_client

            http = await self._stack.enter_async_context(
                httpx2.AsyncClient(headers=cfg.get("headers", {}), timeout=60))
            read, write = await self._stack.enter_async_context(
                streamable_http_client(cfg["url"], http_client=http))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        server = Server(
            name=name,
            label=cfg.get("label", cfg["name"]),
            can_order=bool(cfg.get("can_order", False)),
            order_pattern=re.compile(cfg.get("order_tools_pattern", DEFAULT_ORDER_PATTERN), re.I),
            cart_pattern=re.compile(cfg.get("cart_tools_pattern", DEFAULT_CART_PATTERN), re.I),
            payment_arg_required=bool(cfg.get("payment_arg_required", True)),
            session=session,
        )
        listed = await session.list_tools()
        for tool in listed.tools:
            exposed = f"{name}__{_NAME_OK.sub('_', tool.name)}"[:64]
            mode = "compare only" if not server.can_order else "can order"
            server.tools[exposed] = {
                "name": exposed,
                "description": f"[{server.label} - {mode}] {tool.description or tool.name}",
                "input_schema": tool.input_schema or {"type": "object", "properties": {}},
            }
            self._routes[exposed] = (server, tool.name)
        self.servers[name] = server
        log.info("Connected to %s with %d tools", server.label, len(server.tools))

    # --- used by the agent ---------------------------------------------------
    def tool_definitions(self) -> list[dict]:
        # Sorted so the tool list is byte-identical between requests (prompt caching).
        return [t for s in sorted(self.servers.values(), key=lambda s: s.name)
                for _, t in sorted(s.tools.items())]

    def platforms(self) -> list[dict]:
        return [{"id": s.name, "name": s.label, "can_order": s.can_order}
                for s in sorted(self.servers.values(), key=lambda s: s.name)]

    def route(self, exposed_name: str) -> tuple[Server, str] | None:
        return self._routes.get(exposed_name)

    @staticmethod
    def is_order_tool(server: Server, tool: str) -> bool:
        return bool(server.order_pattern.search(tool))

    @staticmethod
    def is_cart_or_order_tool(server: Server, tool: str) -> bool:
        return bool(server.cart_pattern.search(tool))

    async def call(self, server: Server, tool: str, args: dict) -> tuple[str, bool]:
        """Run a tool; returns (text, is_error)."""
        result = await server.session.call_tool(tool, args)
        content = getattr(result, "content", None) or []
        parts = [getattr(c, "text", None) or f"[{getattr(c, 'type', 'content')}]" for c in content]
        structured = getattr(result, "structured_content", None)
        if not parts and structured is not None:
            import json
            parts = [json.dumps(structured, ensure_ascii=False)]
        return "\n".join(parts) or "(no output)", bool(getattr(result, "is_error", False))
