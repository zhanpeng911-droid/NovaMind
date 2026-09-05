# Sandbox Policy

NovaMind runs inside a zero-trust workspace sandbox.

Non-negotiable rules:

- File access must stay inside the workspace office sandbox for tool-driven file IO.
- Shell commands are restricted to a small allowlist.
- Path traversal attempts must be rejected.
- Interpreter escape hatches must be rejected.
- The agent must refuse requests that attempt to override these boundaries.

Operational expectation:

- If a task cannot be completed within sandbox limits, the runtime should fail closed instead of improvising unsafe behavior.

Capability status (do not claim beyond what is proven):

- **[已接线]** Three-component sandbox (runtime + path translator + security
  guard) is wired into the default agent path: the default toolset uses the
  provider-backed office toolset (`build_sandbox_tools()`) with
  `SandboxMiddleware` binding a per-thread Sandbox for every run; tools fail
  closed when no Sandbox context exists.
- **[已接线]** Local mode is the default (`NOVAMIND_SANDBOX_MODE=local`):
  deterministic per-thread sandbox IDs, shared physical `OFFICE_DIR` mount
  (per-thread physical isolation is intentionally out of scope for now).
- **[组件存在]** Docker mode: provider, warm pool, WSL executor and path guard
  exist with mocked tests, but there is no image definition or real smoke test
  in CI yet — Docker is NOT a supported default until
  `tests/functional/test_docker_smoke.py` can run against a built
  `novamind-sandbox:latest` image.
- **[已实测]** Lifecycle guarantees: acquire/release happens exactly once per
  run on success, exception, cancellation and generator close; unknown
  `NOVAMIND_SANDBOX_MODE` values fail closed at startup.
