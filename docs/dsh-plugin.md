# Using veripipe as a dsh-plugin

`veripipe` is built to plug into a Ducc / dsh host without any host-specific
code in the core. There are two integration surfaces — **skills** and the
**MCP server** — plus a discovery convention.

## Discoverability

Add the [`dsh-plugin`](https://github.com/topics/dsh-plugin) topic to the
repository so other people can find it. That topic is the community convention
for plugins in this ecosystem; it does not couple the plugin to any specific
host build.

## 1. Skills (thin-shells)

A dsh host reads skills from its home directory at `.dsh/skills/<name>/SKILL.md`.
Ship a `skills/dsh/` directory in your product adapter and install it into the
product working directory:

```bash
VERIPIPE_HOME=/path/to/veripipe \
  python3 -m scripts.installer.installer --product <product> --product-cwd /path/to/product
# -> writes .dsh/skills/<name>/SKILL.md (alongside .claude/ and .codex/)
```

See [`adapters.md`](adapters.md) for the full symmetric host mapping. The dsh
thin-shell mirrors the Codex layout exactly, so an adapter that already targets
Claude Code and Codex gains dsh support just by adding `skills/dsh/`.

## 2. MCP server

The zero-dependency MCP server exposes the pipeline as tools over stdio, so a
dsh host can call it directly. Register it as an MCP server for the session
using the standard stdio spec (command + args + env):

```json
{
  "name": "veripipe-pipeline-v2",
  "command": "python3",
  "args": ["-m", "pipeline_v2_mcp"],
  "env": {
    "PYTHONPATH": "/path/to/veripipe/shared-skills/pipeline-v2/scripts"
  }
}
```

Once connected, the host sees the tools `preflight`, `run_batch`, `run_loop`,
`knowledge_audit`, `knowledge_export`, `code_map_validate`, `change_impact`,
and `query_ledger`. `run_batch` / `run_loop` default to `dry_run=true`, and
`query_ledger` never creates a database as a side effect. (If `veripipe` is
`pip install`ed, the `PYTHONPATH` env entry is unnecessary.)

## Verify the connection

You can exercise the same handshake the host performs, without a host:

```bash
export PYTHONPATH="/path/to/veripipe/shared-skills/pipeline-v2/scripts"
printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 -m pipeline_v2_mcp
```

The server replies with the negotiated `protocolVersion` and the tool list on
stdout; diagnostics go to stderr.

## Confidentiality

The plugin ships only the neutral engine, the MCP surface, and the installer.
Product source, internal prompts, intranet addresses, project identifiers, run
logs, queue payloads, credentials, and unreviewed product knowledge stay in a
separate private repository and never enter a published plugin.
