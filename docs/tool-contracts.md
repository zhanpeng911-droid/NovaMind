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
