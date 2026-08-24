# GAP 7 Design — MCP Server Client + Plugin Registry

Status: design accepted · Phase 7 (after Phase 2)

## Problem

Tool set is fixed at five builtins; there is no way to extend tools
without modifying source.

## Part A — MCP client

### Architecture

`src/zero/manage/core/mcp_client.py` wraps the official `mcp` package
(optional `[mcp]` extra). One `MCPServerProcess` per configured server:

```
MCPManager.load(config) -> None
  for server in settings.mcp_servers where enabled:
      MCPServerProcess(name, command).connect()      # stdio JSON-RPC
          ├─ initialize handshake (protocolVersion, capabilities)
          ├─ tools/list → Tool metadata + inputSchema
          └─ register into ToolService as mcp_<server>_<tool>
                handler = lambda input: session.call_tool(tool, input)
```

- Transport: stdio only in v1 (`command` list spawned via
  `subprocess.Popen`, text frames over stdin/stdout, stderr drained to
  the app log at debug level — never stdout).
- Naming: `mcp_<sanitized_server>_<sanitized_tool>` (repo convention
  with single underscores; sanitize maps `[^A-Za-z0-9_]`→`_`),
  matching the design brief.
- Registration uses `ToolService.register_tool(..., inline=False)` so
  invocations go through the isolated runner and every existing gate
  (schema validation, capability grant, budget fence, redaction,
  audit) applies unchanged.
- Lifecycle: connect at startup is lazy-tolerant — a failing server is
  logged and skipped (never crashes the app); reconnect on next
  invocation attempt; `shutdown()` terminates child processes.

Config schema addition (env-var encoded as JSON because Settings is
env-driven):

```
ZERO_MCP_SERVERS=[{"name":"filesystem","command":["npx","-y","@modelcontextprotocol/server-filesystem","/tmp"],"enabled":true}]
```

## Part B — Plugin registry

`src/zero/manage/plugins/registry.py`:

```
PluginManager.discover()
    paths = [~/.zero/plugins/*.py, /opt/zero/plugins/*.py]
    load order: system dir first (alphabetical), then user dir
      (alphabetical); user overrides system by re-registration guard
    for file: importlib.util.spec_from_file_location
        module.register(manage_context)   # contract
        failures logged, skipped, never raised
```

- `ManageContext` dataclass exposes **read-only** facades:
  `config: Settings`, `secret_store` (resolve-only),
  `tool_registry` (register_tool only), plus plugin name for audit.
- A sample plugin ships under `examples/plugins/echo_upper.py`
  demonstrating a custom tool registration.

## Data model changes

None. MCP/plugin tools are ordinary rows created through
`register_tool`.

## API surface

- No new HTTP routes. Tools appear in existing `/tools` listing and
  are granted like builtins (`grant_tool` requires `"tool.manage"`).

## Security considerations

- MCP servers execute arbitrary local processes by operator config —
  documented explicitly; disabled by default (no env var ⇒ no servers).
- Plugin code executes with application privileges; discovery paths
  are user/system controlled; load failures isolated; plugins cannot
  access secret plaintext unless granted resolve calls (audited).
- Tool output redaction applies to MCP results identically to builtin
  tools (same invoke pipeline).

## Test strategy

- MCP: fake MCP server script (tiny Python speaking initialize +
  tools/list + tools/call over stdio) launched in tests; assert tool
  appears with correct name/schema, invocation round-trips, server
  crash at startup is tolerated, duplicate names suffixed/rejected per
  policy.
- Plugins: tmp_path-based discovery dirs via parameterized home;
  alphabetical order assertion; user-overrides-system; broken plugin
  (syntax error / missing register / raising register) logged+skipped;
  sample plugin registers callable tool that agents can invoke through
  the standard grant path.

## Migration path

Additive extras `[mcp]`; managers constructed in composition root when
config present.

## Rollback strategy

Remove env var entries / delete plugin files; no schema impact.

## Acceptance criteria

- An MCP filesystem server's tools appear alongside builtin tools and
  respect grants.
- A sample plugin adds a custom tool callable through normal
  authorization.
