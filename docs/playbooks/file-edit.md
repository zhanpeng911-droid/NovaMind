# File Edit Playbook

Use this playbook when the task involves reading or modifying files.

Preferred workflow:

1. Inspect the target path before changing it.
2. Read the current content before editing.
3. Make the smallest safe change that satisfies the task.
4. Preserve sandbox boundaries and avoid broad shell usage.
5. After changes, verify the result with a focused read or test.

Guardrails:

- Do not invent file contents that were not inspected.
- Do not expand scope from one file to many files without evidence.
- Do not bypass dedicated file tools with interpreter escapes.
