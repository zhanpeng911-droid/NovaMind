# Tool Contracts

Builtin tools are capability contracts, not suggestions.

Current core groups:

- Time and system info.
- Safe arithmetic.
- User profile persistence.
- Scheduled task management.
- Sandbox file inspection and controlled shell execution.

Tool usage rules:

- Use the narrowest tool that satisfies the task.
- Do not simulate tool results when a tool is available.
- Do not use shell when a dedicated file tool is safer.
- Prefer read-before-write for file modifications.
