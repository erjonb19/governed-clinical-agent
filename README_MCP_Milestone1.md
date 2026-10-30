# Milestone 1 — Guarded MCP Server

This wraps your Security-Constrained Agent Runtime in an MCP server. Every MCP
tool call is routed through your real policy engine (`AgentRuntime.execute_tool`)
before anything runs. Nothing in `src/` changes — this is a new front door.

## Files
- `mcp_server.py` — the server. Drop it at the **repo root** (next to `src/`).
- `mcp_demo_policy.yaml` — a tiny default-deny policy. Put it at the repo root too.

## Setup
From the repo root:

```bash
# your runtime's own deps (once)
pip install -r requirements.txt

# the MCP SDK
pip install "mcp[cli]"
```

## Run
```bash
python mcp_server.py                                    # stdio (the local dev transport)
MCP_API_KEY=<secret> python mcp_server.py --transport http   # Streamable HTTP, 127.0.0.1:8765/mcp
```
The API serves the same tools at `/mcp` too. Over HTTP every request needs the
key, as `X-API-Key: <key>` or `Authorization: Bearer <key>` (`MCP_API_KEY`, else
`API_KEY`). That is **API-key auth, not OAuth 2.1**; OAuth stays on the roadmap.

## Test it
Easiest is the MCP Inspector, which gives you a UI to call the tools:

```bash
npx @modelcontextprotocol/inspector python mcp_server.py
```

Then call each tool and confirm the behavior:

| Tool call            | Capability        | Expected result                                  |
|----------------------|-------------------|--------------------------------------------------|
| `echo("hello")`      | `demo.echo`       | **ALLOWED** — returns `echo: hello`              |
| `fetch_url("http://x")` | `http.fetch`   | **DENIED** — high-risk, needs approval, no approver |
| `git_push()`         | `git.push`        | **DENIED** — `allowed: false` in the policy      |

If you see one allow and two denies, Milestone 1 is done: your policy engine
is now mediating MCP tool calls. Check `logs/audit/` — the decisions are in
your existing audit trail.

You can also add it to Claude Desktop (Settings → Developer → Edit Config) as
an MCP server pointing at `python /path/to/mcp_server.py`, then call the tools
from a chat.

## What you can say once this runs
"My runtime now mediates MCP tool calls — any client connecting through it
inherits default-deny enforcement." (Before this ran, it was the *planned*
next phase. Now it's built.)

## Where this goes next
- **Milestone 2 — the mapping layer.** Right now each MCP tool hardcodes its
  capability. Build a general mapping: MCP tool name → capability, MCP args →
  validated parameters, taint on untrusted args. Return structured MCP tool
  errors on denial instead of a string.
- **Milestone 3 — the guarded server proper.** Streamable HTTP is DONE (PLAN v2
  PR 7, stateless, API-key auth, standalone and at `/mcp`). Still to do: tools
  across risk tiers and the approval flow (high-risk → human approves → executes).
- **Milestone 4 — auth + ops + the real application.** OAuth 2.1 / bearer auth,
  Docker, and replace `EchoTool` with **your Medicare agent's tools**
  (query the Gold table, detect anomalies, generate a brief) so the guarded
  server is guarding something real. That join is the strong story:
  "an agent over real CMS data whose every tool call is default-deny enforced
  by my own MCP server."

## How to plug in a real tool (preview of Milestone 4)
Any `BaseTool` subclass works. Register it, add its capability to the policy,
and expose an `@mcp.tool()` that calls `guarded("your.capability", {...})`:

```python
class QueryGoldTool(BaseTool):
    @property
    def name(self): return "medicare.query_gold"
    def execute(self, params):
        # run the read-only SQL against your Gold table, return rows
        ...
        return ToolResult(success=True, output=rows)

runtime.register_tool(QueryGoldTool())

@mcp.tool()
def query_gold(sql: str) -> str:
    """Read-only query against the Medicare Gold layer."""
    return guarded("medicare.query_gold", {"sql": sql})
```
Add `medicare.query_gold` (allowed, read-only) to the policy, and a write or
escalation capability as `require_approval: true`, and you have the risk-tiered
agent the spec describes.
