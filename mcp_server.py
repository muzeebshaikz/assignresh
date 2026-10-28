"""MCP server exposing the engine to Claude Desktop / Claude Code. Same store + pipeline code, no duplicated logic.

Register (Claude Code):  claude mcp add onboarding-sync -- python mcp_server.py
Claude Desktop, in claude_desktop_config.json under mcpServers:
  {"onboarding-sync": {"command": "python", "args": ["C:/path/to/mcp_server.py"], "env": {"SYNC_DATA_DIR": "data"}}}
Requires `pip install "mcp<2"` (FastMCP lives in mcp v1; v2 renamed it).
"""
import os

from mcp.server.fastmcp import FastMCP

from sync_engine.pipeline import Pipeline

mcp = FastMCP("onboarding-sync")
_pipe = Pipeline(os.environ.get("SYNC_DATA_DIR", "data"))


@mcp.tool()
def get_hire_sync_status(hire_id: str) -> dict:
    """Current canonical hire, per-system op status and open exceptions."""
    h = _pipe.store.get_hire(hire_id)
    if h is None:
        return {"error": "unknown hire"}
    return {"hire": h.model_dump(mode="json"),
            "ops": [{"system": r["system"], "action": r["action"], "status": r["status"]} for r in _pipe.store.ledger_list(hire_id=hire_id)],
            "open_exceptions": [c.case_id for c in _pipe.store.list_exceptions("awaiting_human") if c.hire_id == hire_id]}


@mcp.tool()
def list_open_exceptions() -> list[dict]:
    """Exceptions waiting for a human decision."""
    return [{"case_id": c.case_id, "hire_id": c.hire_id, "kind": c.kind} for c in _pipe.store.list_exceptions("awaiting_human")]


@mcp.tool()
def resolve_exception(case_id: str, decision: str) -> dict:
    """decision: 'approve' executes the held ops; 'reject' cancels them."""
    if decision not in ("approve", "reject"):
        return {"error": "decision must be approve or reject"}
    return {"case_id": case_id, "status": _pipe.resolve(case_id, decision).status}


if __name__ == "__main__":
    mcp.run()
