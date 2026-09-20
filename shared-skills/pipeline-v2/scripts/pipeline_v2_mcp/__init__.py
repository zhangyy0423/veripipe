"""MCP server exposing the pipeline_v2 verification core as callable tools.

The server speaks the Model Context Protocol stdio transport (newline-delimited
JSON-RPC 2.0) with no third-party dependencies, so any MCP host can launch it
with ``python3 -m pipeline_v2_mcp``.
"""

from __future__ import annotations


def _resolve_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("veripipe")
        except PackageNotFoundError:
            return "0.1.0+source"
    except Exception:  # pragma: no cover - importlib always present on 3.9+
        return "0.1.0+source"


__version__ = _resolve_version()

__all__ = ["__version__"]
