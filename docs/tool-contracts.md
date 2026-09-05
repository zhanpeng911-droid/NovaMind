# Tool Contracts

Builtin tools are capability contracts, not suggestions.

Current core groups:

- Time and system info (current time, system model info).
- Safe arithmetic.
- User profile persistence (bridged into long-term procedural memory).
- Scheduled task management (create, list, delete, modify).
- Sandbox file inspection and controlled shell execution, confined to the office workspace.

Additional tool surfaces:

- Dynamic skill tools loaded from the skill registry at startup.
- MCP tools loaded from configured MCP servers.

Tool usage rules:

- Use the narrowest tool that satisfies the task.
- Do not simulate tool results when a tool is available.
- Do not use shell when a dedicated file tool is safer.
- Prefer read-before-write for file modifications.

Capability status (do not claim beyond what is proven):

- **[已接线]** The default agent assembly binds the provider-backed office
  toolset (list/read/write/shell, same names and schemas as the legacy four)
  that resolves the current Sandbox from the run's execution context and fails
  closed without one; the legacy direct-filesystem tools remain for standalone
  scripts and the plugin loader's no-sandbox fallback.
- **[已接线]** Plugin `run` mode routes through the same Sandbox when executed
  inside an agent run (same provider, no silent fallback to legacy Local);
  standalone invocations keep legacy behavior.
- **[已实测]** Names, argument schemas and golden behaviors of the four office
  tools are frozen by contract tests (`tests/unit/test_sandbox_tools.py`).
